# tdai-wiki-pipeline

把「AI 会话 / 运维笔记」变成可检索的 **TDAI Knowledge wiki** 页面的小工具集。

配套服务：[TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) 的 MemoryKnowledge（下称 KS，默认 `:8424`）。

```
源文件(md/pdf/docx/url)  →  KS raw/  →  ingest(LLM 编译)  →  结构化页面 + 索引 + 关系图
                                                                  ↓
                                               agent 预热时注入 <knowledge_tools>
                                                                  ↓
                                                      agent 按需 tools/call 检索
```

## 为什么需要它

KS 的 wiki 是**文档知识库**，输入只有 raw 源文件；它不会自己从对话里长页面。
而 KS 的写接口要求 `Authorization: Bearer <KNOWLEDGE_SERVICE_KEY>`，面板点点点能做，
但放进 cron / 放进 agent 的工具链就需要一个 CLI。

这个仓库提供：

| 文件 | 作用 |
|---|---|
| `kb.py` | KS wiki 的 CLI：建库 / 推源文件 / 触发 ingest / 等结果 / 读页面 / 检索 |
| `conv2wiki.py` | 计划中：把 L0/L1/L2 记忆压成 md 再推给 wiki（自动化的那一段） |

## 快速开始

```bash
# 1) 找到 key（KS 写接口用；查询类接口免鉴权）
export KNOWLEDGE_SERVICE_KEY=<你的 KNOWLEDGE_SERVICE_KEY>
export KS_URL=http://127.0.0.1:8424/v3     # 默认值就是这个
export TEAM_ID=team-xxxxxxxx                # 你的 team

# 2) 看看有哪些 wiki
./kb.py list

# 3) 建一个，拿 wiki_id
./kb.py --team "$TEAM_ID" create "服务器运维与踩坑手册"
# → wiki-ihrr5vxf

# 4) 推源文件（单次 ≤10 个 / ≤20MB，md 直接 utf8）
./kb.py --team "$TEAM_ID" push wiki-ihrr5vxf docs/*.md

# 5) 编译（incremental：已编译过的源不会重跑）
./kb.py --team "$TEAM_ID" ingest wiki-ihrr5vxf --wait

# 6) 看结果
./kb.py pages wiki-ihrr5vxf
./kb.py read  wiki-ihrr5vxf --page overview.md
./kb.py search wiki-ihrr5vxf "为什么每个会话都要选一次 agent"
```

`kb.py` 只依赖 Python 标准库，没有第三方包。

## ingest 用的模型从哪来

KS 的模型**不在 wiki 绑定里**，而是容器环境变量：

```
LLM_MODE=custom
LLM_BASE_URL=...      # 任何 OpenAI 兼容端点
LLM_API_KEY=...
LLM_MODEL=glm-5.3     # ← 这里
```

`resolveLlmConfig()` 里 model 取的是 fallback（= 环境变量）而不是 binding 字段，
所以想「memory 抽取用便宜模型、wiki 编译用强模型」就必须在启动脚本里把这两个分开：

```bash
# deploy/global-images/start-memory-hub.sh
-e LLM_MODEL="${KNOWLEDGE_LLM_MODEL:-$MEMORY_LLM_MODEL}" \
```

改完 `.env` 里的 `KNOWLEDGE_LLM_MODEL` 后要重跑 `start-memory-hub.sh`（env 在容器创建时固化）。

## ⚠️ 纯 API 建 wiki 必须补 `register` 三步

面板里建 wiki 能注入、你 API 建的却永远注入不出来，原因在这里：

1. **meta 资产**：面板建 wiki 时会在 create 阶段顺手打 `/v3/meta/asset/create`。纯 API 建不会，于是 meta 里根本没有这个资产，`agent-fixed-asset` 绑不上，注入器自然看不到。
2. **内核明细**：KS 编译完成后回调 Panel，Panel 再写 `/v3/knowledge/create`，body 里的 `user_id` 取的是 `detail.owner_user_id`。**如果建 wiki 时没传 `user_id`，owner 就是 null，这一步会被 zod 拒掉**（`400 user_id: Invalid input: expected string, received null`），明细缺失 → 注入器同样跳过。
3. **status**：meta asset 的合法状态是 `draft|candidate|approved|deprecated|archived|failed`，没有 `active`；建完建议置 `approved`。

所以 API 路线收尾必须：

```bash
TEAM_ID=team-xxxx TDAI_USER_ID=usr-xxxx ./kb.py register wiki-xxxx \
  --summary "一句话摘要（≤250 字，注入块里显示的就是它）" \
  --bind agt-xxxxxxxx        # 绑给哪个 agent，可跟多个
```

`register` 是幂等的：asset 存在就跳过 create 并置 approved，然后写内核明细、绑 agent，最后回显该 agent 当前的 `llm_wiki` 绑定列表。

需要 admin key（meta 用）与 user key（`x-tdai-user-key`），分别从 `$TDAI_ADMIN_KEY` / `.admin-key`、`$TDAI_USER_KEY` / hermes config 的 `sk-mem-*` 读取。

## conv2wiki.py — 把对话沉淀自动分拣进两座 wiki

分拣规则（本人拍板，2026-11-20）：

| 类别 | 判据 | 去哪 |
|---|---|---|
| 个人任务 / 计划 / 偏好 / 画像 | 只有本人能推进的事 | **个人 wiki**（原文 + 背景） |
| 基础设施事实 | 服务器配置、跑着哪些服务、端口/域名/容器/证书/cron/备份… | **团队 wiki**（去人称 + 来源块） |
| 同一信息的深浅两版 | 上面两类天然重叠 | 两边都写，**内容与细节不同** |

实现要点：

- **分拣用模型判断，不用关键词**（`--classifier llm`，默认）：每 10 条一批丢给 `glm-5.3-flash`，要它回 `{"items":[{"id","scope","why"}]}`，`scope ∈ personal|team|both|skip`。单批失败自动回落关键词，流水线不中断。
- 团队 wiki 的**收口范围**：只收「资产 / 服务 / 仓库」，每条尽量给全三要素 —— 是什么（服务/容器/镜像/仓库名）、做什么用、跑在哪（主机/端口/域名/路径/仓库地址）。操作过程、排障细节、命令步骤、决策理由一律不进团队，留在个人 wiki。
- 输入是 **L1 原子记忆**（`/v2/atomic/query`，必须同时给 `user_id`，只给 agent_id 会返回 0 条）。
- **团队版必须带来源**：文件头写 `来源：<AI 名字>（<agent_id>）；原始会话 <session>；采集时间 <日期>`，每条结论后跟 `（来源：<AI 名字> / 记忆 id）`。
- 团队版正文用 GLM 改写（`glm-5.3`）。**模型只有 8K 输出上限时会返回空**——glm-5.3 单条任务就要烧 2000+ reasoning token，`max_tokens` 给到 30000 才稳。
- AI 名字映射放 `~/.config/tdai-wiki-pipeline/agents.json`（`{"agt-xxx": "小昔"}`），未登记的回落 agent_id。
- 游标与已推 id 存 `~/.config/tdai-wiki-pipeline/state.json`，重复运行只处理新记忆。

> **关于 JEV**：JEV 是 TDAI 内置的「System One」决策 provider（`tokenhub.tencentmaas.com/v1/systemone`
> + `MEMORY_CORE_JEV_*` 配置），**不在 OpenCode Go 订阅池里** —— 实测 `model=jev` 返回
> `Model jev isn't included in OpenCode Go`，池子里 29 个模型没有任何 jev。要用 JEV 得单独拿
> TokenHub 的 key。所以这里的分拣用订阅池里的 `glm-5.3-flash`，效果等价、成本更低。

```bash
# 先看分拣结果（不写任何东西）
./conv2wiki.py --dry-run --limit 40

# 真跑：推源 + 触发 ingest
PERSONAL_WIKI_ID=wiki-xxx TEAM_WIKI_ID=wiki-yyy ./conv2wiki.py --apply
```

> ⚠️ `agent-fixed-asset/set` 是**整表替换**语义。绑第二个 wiki 时必须先读现有绑定再合并，
> 否则上一个 wiki 会被顶掉（`kb.py register` 已处理这件事）。

## 权限模型（重要）

- 写权限**只有一把全局 service key**，没有「只读某几个 wiki」的粒度 —— 谁拿到 key 谁就能改删全部 wiki。
- `knowledge_wiki.visibility` 字段存在（默认 `team`），但**代码里没有任何地方按它过滤**。
  「个人 wiki / 团队 wiki」的实际隔离手段是**绑定**：
  用 meta 的 `agent-fixed-asset/set` 把 wiki 绑给具体 agent，该 agent 预热时才会看到它。
- agent 侧只读白名单：`/tools/list`、`/tools/call`、`/wiki/{get,list,search,graph,page/ls,page/read,raw/ls,raw/read}`。

## 常见坑

| 症状 | 原因 |
|---|---|
| `401 unauthorized: valid service key required` | 头写成 `x-knowledge-service-key` 了。正确是 `Authorization: Bearer <key>` |
| `400 wiki has no source files, upload before ingest` | 先 `push` 再 `ingest` |
| `409 busy` | 同 wiki 已有在途任务；用 `--on-busy replace` 或等它结束 |
| ingest 卡在 `extracting` 不动 | 看 `docker logs tdai-memory-hub`，多半是 LLM 端点/模型名错（模型名写错时上游返回 400/404，日志里会有一条 `LLM 调用失败`） |
| 面板里 wiki 列表为空 | `list` 只按 `service_id + team_id` 过滤，检查 `x-tdai-service-id` 与 `team_id` 是否对得上 |

## License

MIT
