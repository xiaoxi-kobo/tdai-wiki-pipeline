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
