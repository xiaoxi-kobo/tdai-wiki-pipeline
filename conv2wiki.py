#!/usr/bin/env python3
"""conv2wiki.py — 把 L1 原子记忆（TDAI 从对话里抽出来的知识点）分拣成两座 wiki。

分拣规则（2026-11-20 昊哥哥拍板）：

1. **个人 wiki** — 只有他本人能推进的东西：个人任务、个人计划、偏好、画像、待办。
2. **团队 wiki** — 与他相关但属于「基础设施事实」的东西：服务器配置、跑着哪些服务、
   端口/域名/容器/证书/cron/备份……。**必须带来源**（哪个 AI、哪个会话、什么时候）。
3. **同一条信息可以两边都有，但内容不同、细节不同**：
   - 个人版 = 原文 + 背景（保留人称、动机、计划细节）
   - 团队版 = 去人称、只留可复用的配置/命令/结论 + 来源块

用法：

  ./conv2wiki.py --dry-run                 # 只分拣 + 打印，不写任何东西
  ./conv2wiki.py --apply                   # 推源文件 + 触发 ingest
  ./conv2wiki.py --apply --no-llm          # 团队版用规则改写，不调模型

依赖 kb.py（同目录），复用它的鉴权与 KS 调用。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8420")
STATE_PATH = Path(os.environ.get("CONV2WIKI_STATE", "~/.config/tdai-wiki-pipeline/state.json")).expanduser()

# 基础设施关键词：命中即视为「与他相关的基础设施事实」→ 团队 wiki
INFRA_KEYWORDS = (
    "服务器", "容器", "端口", "部署", "运维", "备份", "恢复", "证书", "域名", "反代", "代理",
    "磁盘", "内存", "内核", "重启", "日志", "定时", "cron", "systemd", "ssh", "dns",
    "docker", "nginx", "systemctl", "proxy", "tdai", "hermes", "dsh", "openclaw",
    "api key", "api_key", "鉴权", "数据库", "sqlite", "向量库", "插件", "仓库", "git",
    "webhook", "gateway", "端口映射", "frp", "tls", "https",
)

# 只有个人才有的类型（永远不会单独进团队）
PERSONAL_ONLY = {"persona", "work_task"}
# 天然偏基础设施的类型
FACT_TYPES = {"work_fact"}
PERSONAL_TITLE = {"persona": "画像与偏好", "instruction": "规则与约定", "work_task": "任务与计划",
                  "work_fact": "与我的相关事实", "episodic": "事件与结论"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ── 读 L1 ────────────────────────────────────────────────────────────────

def core_call(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        CORE_URL + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-tdai-service-id": os.environ.get("SERVICE_ID", "default"),
                 "authorization": "Bearer " + os.environ.get("CORE_AUTH", "local")},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read() or b"{}")
    if payload.get("code") not in (0, None):
        raise SystemExit(f"{path} code={payload.get('code')} {payload.get('message')}")
    return payload.get("data") or {}


def fetch_l1(agent_id: str, user_id: str, team_id: str, since: str | None, limit: int) -> list[dict]:
    """按 created_at 升序拉 L1；since 为 ISO 时间（不含）。"""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        page = core_call("/v2/atomic/query", {
            "agent_id": agent_id, "user_id": user_id, "team_id": team_id,
            "limit": min(100, limit - len(out)), "offset": offset,
        })
        items = page.get("items") or []
        if not items:
            break
        out.extend(items)
        offset += len(items)
        if len(items) < min(100, limit - len(out) + len(items)):
            break
    if since:
        out = [i for i in out if (i.get("created_at") or "") > since]
    out.sort(key=lambda i: i.get("created_at") or "")
    return out


# ── 分拣 ────────────────────────────────────────────────────────────────

def is_infra(item: dict) -> bool:
    text = f"{item.get('content') or ''} {item.get('background') or ''}".lower()
    return any(k.lower() in text for k in INFRA_KEYWORDS)


def classify(item: dict) -> tuple[bool, bool]:
    """→ (进个人, 进团队)"""
    t = item.get("type") or "episodic"
    infra = is_infra(item)
    if t in PERSONAL_ONLY:
        return True, infra
    if t in FACT_TYPES:
        return infra, True          # 事实默认团队；若含基础设施关键词也留一份个人版
    if t == "instruction":
        return True, infra
    # episodic 及其他
    return True, infra


# ── 生成 md ──────────────────────────────────────────────────────────────

def agent_label(agent_id: str) -> str:
    """agent_id → 人话名字。小昔是当前助手；其他 AI 的名字由用户在 config 里补。"""
    cfg = Path(os.environ.get("CONV2WIKI_AGENTS", "~/.config/tdai-wiki-pipeline/agents.json")).expanduser()
    if cfg.is_file():
        try:
            m = json.loads(cfg.read_text())
            if agent_id in m:
                return m[agent_id]
        except json.JSONDecodeError:
            pass
    return os.environ.get("DEFAULT_AGENT_LABEL", "小昔") if agent_id == os.environ.get("MY_AGENT_ID", "agt-09j5rkob6d") else agent_id


def build_personal_md(items: list[dict], day: str) -> str:
    """个人版：保留人称与背景，按类型分组。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        groups[i.get("type") or "episodic"].append(i)
    lines = [
        "---", f"title: 个人知识与计划 {day}", "source_type: personal", f"date: {day}", "visibility: private", "---",
        "", f"# 个人知识与计划 · {day}", "",
        "> 采集自与小昔的对话（L1 原子记忆）。保留人称、背景与计划细节，仅供本人查阅。", "",
    ]
    for t in ("work_task", "instruction", "persona", "work_fact", "episodic"):
        if t not in groups:
            continue
        lines.append(f"## {PERSONAL_TITLE.get(t, t)}")
        lines.append("")
        for it in groups[t]:
            content = (it.get("content") or "").strip()
            bg = (it.get("background") or "").strip()
            lines.append(f"- {content}")
            if bg:
                lines.append(f"  - 背景：{bg}")
        lines.append("")
    return "\n".join(lines)


def build_team_md_rules(items: list[dict], day: str, agent_id: str, session: str) -> str:
    """团队版（无 LLM 兜底）：去人称 + 来源块。"""
    label = agent_label(agent_id)
    lines = [
        "---", f"title: 基础设施事实与运维结论 {day}", "source_type: conversation", f"date: {day}",
        f"agent: {agent_id}", "---", "",
        f"# 基础设施事实与运维结论 · {day}", "",
        f"> **来源**：{label}（AI 助手，`{agent_id}`）；原始会话 `{session}`；采集时间 {day}。",
        "> 本页为对话沉淀的可复用运维事实，已剔除个人隐私与个人计划。", "",
    ]
    for it in items:
        content = (it.get("content") or "").strip()
        content = re.sub(r"\b(我|我的|我们)\b", "", content)
        lines.append(f"- {content}（来源：{label} / 记忆 id `{it.get('id')}`）")
    lines.append("")
    return "\n".join(lines)


def build_team_md_llm(items: list[dict], day: str, agent_id: str, session: str) -> str:
    """团队版（LLM 改写）：去人称、只留可复用内容、保留来源。失败自动回落规则版。"""
    label = agent_label(agent_id)
    bullet = "\n".join(
        f"[{it.get('id')}] type={it.get('type')}\n内容：{it.get('content')}\n背景：{it.get('background') or '-'}"
        for it in items)
    system = (
        "你是运维知识整理助手。把给定的对话记忆条目整理成一篇团队知识库文档。"
        "要求：1) 一律去掉第一人称与个人隐私（持仓、健康、感情、个人计划）；"
        "2) 只保留可复用的配置、命令、端口/域名、故障原因与结论，写成陈述句；"
        "3) 用 markdown：先给一篇「概览」段落，再按主题分小节；"
        "4) 每条结论后面用括号标注来源，格式（来源：<AI 名字> / 记忆 id）；"
        "5) 不要编造条目里没有的内容；6) 不要输出 frontmatter，正文从标题开始。"
    )
    prompt = (f"AI 名字：{label}（agent {agent_id}）\n会话：{session}\n日期：{day}\n\n以下是记忆条目：\n\n{bullet}")
    try:
        text = llm_chat(system, prompt)
    except Exception as e:  # noqa: BLE001 — 任何失败都回落规则版，不让流水线中断
        log(f"LLM 改写失败，回落规则版：{e}")
        return build_team_md_rules(items, day, agent_id, session)
    header = [
        "---", f"title: 基础设施事实与运维结论 {day}", "source_type: conversation", f"date: {day}",
        f"agent: {agent_id}", "---", "",
        f"> **来源**：{label}（AI 助手，`{agent_id}`）；原始会话 `{session}`；采集时间 {day}。", "",
    ]
    return "\n".join(header) + text.strip() + "\n"


def _dotenv(name: str) -> str:
    """从 TDAI 部署 .env 里取一个值（避免在命令行里出现密钥）。"""
    for cand in (os.environ.get("TDAI_ENV_FILE", ""),
                 "/home/ubuntu/TencentDB-Agent-Memory/deploy/global-images/.env"):
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def llm_chat(system: str, prompt: str) -> str:
    # 注意：不要读通用名 LLM_API_KEY / LLM_BASE_URL —— 宿主环境里可能已有别的值（实测踩过 401）。
    # 覆盖一律用 CONV2WIKI_LLM_* 前缀，默认从 TDAI 部署 .env 取。
    base = (os.environ.get("CONV2WIKI_LLM_BASE_URL") or _dotenv("MEMORY_LLM_BASE_URL")).rstrip("/")
    key = os.environ.get("CONV2WIKI_LLM_API_KEY") or _dotenv("MEMORY_LLM_API_KEY")
    model = os.environ.get("CONV2WIKI_LLM_MODEL") or _dotenv("KNOWLEDGE_LLM_MODEL") or "glm-5.3"
    if not base or not key:
        raise SystemExit("缺 LLM_BASE_URL / LLM_API_KEY（从 .env 注入）")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": 30000, "temperature": 0.3,
    }
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "authorization": "Bearer " + key,
                                          "user-agent": "tdai-conv2wiki/0.1"}, method="POST")
    with urllib.request.urlopen(req, timeout=1200) as r:
        payload = json.loads(r.read())
    if "error" in payload:
        raise SystemExit(str(payload["error"])[:200])
    return ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ── 状态游标 ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"cursor": None, "pushed_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── 主流程 ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="L1 记忆 → 个人/团队 wiki")
    ap.add_argument("--apply", action="store_true", help="真的写 wiki 并触发 ingest")
    ap.add_argument("--dry-run", action="store_true", help="只打印分拣结果与生成的 md")
    ap.add_argument("--no-llm", action="store_true", help="团队版用规则改写，不调模型")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--since", default=None, help="ISO 时间，只处理此后的记忆（默认用本地游标）")
    ap.add_argument("--all", action="store_true", help="忽略游标，全量重跑")
    ap.add_argument("--agent", default=os.environ.get("MY_AGENT_ID", "agt-09j5rkob6d"))
    ap.add_argument("--user", default=os.environ.get("TDAI_USER_ID", "usr-0xvk43zamn"))
    ap.add_argument("--team", default=os.environ.get("TEAM_ID", "team-0w9rq08amf"))
    ap.add_argument("--session", default=os.environ.get("SESSION_LABEL", "hermes:hermes-main"))
    ap.add_argument("--personal-wiki", default=os.environ.get("PERSONAL_WIKI_ID", ""))
    ap.add_argument("--team-wiki", default=os.environ.get("TEAM_WIKI_ID", ""))
    a = ap.parse_args()

    state = load_state()
    since = a.since or (None if a.all else state.get("cursor"))
    items = fetch_l1(a.agent, a.user, a.team, since, a.limit)
    # 幂等：跳过已推过的记忆 id
    known = set(state.get("pushed_ids") or [])
    fresh = [i for i in items if i.get("id") not in known]
    log(f"L1 拉取 {len(items)} 条，其中新条目 {len(fresh)} 条（游标 {since or '全量'}）")
    if not fresh:
        print("没有新记忆，什么都不用做。")
        return

    personal, team, dual = [], [], []
    for it in fresh:
        p, t = classify(it)
        if p and t:
            dual.append(it)
        elif p:
            personal.append(it)
        elif t:
            team.append(it)
    log(f"分拣：个人 {len(personal)} 条 / 团队 {len(team)} 条 / 两边都要 {len(dual)} 条")

    day = (fresh[-1].get("created_at") or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p_items = personal + dual
    t_items = team + dual

    p_md = build_personal_md(p_items, day) if p_items else ""
    t_md = (build_team_md_rules if a.no_llm else build_team_md_llm)(t_items, day, a.agent, a.session) if t_items else ""

    tag = f"{day}-{datetime.now(timezone.utc).strftime('%H%M')}"
    p_name, t_name = f"personal-{tag}.md", f"infra-{tag}.md"

    if a.dry_run or not a.apply:
        print(f"=== 个人 wiki 源（{p_name}，{len(p_items)} 条）===")
        print(p_md[:1800] or "（空）")
        print(f"\n=== 团队 wiki 源（{t_name}，{len(t_items)} 条）===")
        print(t_md[:1800] or "（空）")
        print("\n（dry-run：未写入任何东西。加 --apply 才会推源并触发 ingest）")
        return

    if not a.personal_wiki or not a.team_wiki:
        raise SystemExit("缺 --personal-wiki / --team-wiki（或 PERSONAL_WIKI_ID / TEAM_WIKI_ID）")

    if p_md:
        kb.call("/wiki/raw/write", {"team_id": a.team, "wiki_id": a.personal_wiki,
                                    "files": [{"filename": p_name, "content": p_md, "encoding": "utf8"}]}, auth=True)
        log(f"已推个人源 {p_name}")
    if t_md:
        kb.call("/wiki/raw/write", {"team_id": a.team, "wiki_id": a.team_wiki,
                                    "files": [{"filename": t_name, "content": t_md, "encoding": "utf8"}]}, auth=True)
        log(f"已推团队源 {t_name}")

    for wid, name in ((a.personal_wiki, p_name), (a.team_wiki, t_name)):
        if not name:
            continue
        try:
            kb.call("/wiki/ingest", {"wiki_id": wid, "team_id": a.team, "on_busy": "replace"}, auth=True)
            log(f"已触发 ingest: {wid}")
        except SystemExit as e:
            log(f"ingest {wid} 跳过：{e}")

    state["cursor"] = fresh[-1].get("created_at")
    state["pushed_ids"] = (state.get("pushed_ids") or []) + [i["id"] for i in fresh][-2000:]
    save_state(state)
    log(f"游标推进到 {state['cursor']}，记下 {len(fresh)} 条 id")


if __name__ == "__main__":
    main()
