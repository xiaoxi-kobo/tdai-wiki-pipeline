#!/usr/bin/env python3
"""kb.py — TDAI Knowledge（wiki 知识库）命令行工具。

只依赖标准库。用于在不打开面板的情况下完成：
  建 wiki / 推源文件 / 触发 ingest / 看页面 / 读页面 / 清理。

鉴权：写操作（create / push / ingest / delete）要求 `Authorization: Bearer <key>`；
查询类（list / get / page / read / search / graph）免鉴权。key 只从环境变量或 .env 文件读取，
任何情况下都不会打印到 stdout。

典型用法：
  ./kb.py list
  ./kb.py create "服务器运维与踩坑手册" --team team-xxxx
  ./kb.py push wiki-xxxx docs/*.md
  ./kb.py ingest wiki-xxxx --wait
  ./kb.py pages wiki-xxxx
  ./kb.py read wiki-xxxx --page overview.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_KS_URL = "http://127.0.0.1:8424/v3"
# 只在这几个位置找 .env：容器化部署的默认落点 + 环境变量覆盖
ENV_CANDIDATES = (
    os.environ.get("TDAI_ENV_FILE", ""),
    "/home/ubuntu/TencentDB-Agent-Memory/deploy/global-images/.env",
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def resolve_key() -> str:
    """优先环境变量，其次 .env 文件。只取值、不输出。"""
    key = os.environ.get("KNOWLEDGE_SERVICE_KEY", "").strip()
    if key:
        return key
    for cand in ENV_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("KNOWLEDGE_SERVICE_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 KNOWLEDGE_SERVICE_KEY（既不在环境变量也不在 .env）")


def call(path: str, body: dict[str, Any], *, auth: bool, timeout: int = 120) -> dict[str, Any]:
    url = os.environ.get("KS_URL", DEFAULT_KS_URL).rstrip("/") + path
    data = json.dumps(body).encode()
    headers = {
        "content-type": "application/json",
        "x-tdai-service-id": os.environ.get("SERVICE_ID", "default"),
    }
    if auth:
        headers["authorization"] = "Bearer " + resolve_key()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"code": e.code, "message": raw[:400]}
        payload["_http"] = e.code
    if payload.get("code") not in (0, None):
        raise SystemExit(f"HTTP {payload.get('_http', '?')} code={payload.get('code')} {payload.get('message')}")
    return payload.get("data") or {}


def cmd_list(a: argparse.Namespace) -> None:
    data = call("/wiki/list", {"team_id": a.team}, auth=False)
    items = data.get("items") or []
    if not items:
        log("(没有 wiki)")
    for it in items:
        print(f"{it.get('wiki_id')}  {it.get('status'):<12} pages={it.get('page_count', 0):<4} {it.get('name')}")


def cmd_create(a: argparse.Namespace) -> None:
    body: dict[str, Any] = {"team_id": a.team, "name": a.name}
    # user_id 决定 owner_user_id；不传会导致后续内核明细同步被 zod 拒掉（400 user_id null）
    user = a.user or os.environ.get("TDAI_USER_ID", "")
    if user:
        body["user_id"] = user
    data = call("/wiki/create", body, auth=True)
    print(data.get("wiki_id", ""))


def cmd_status(a: argparse.Namespace) -> None:
    data = call("/wiki/get", {"wiki_id": a.wiki_id}, auth=False)
    print(json.dumps({
        "wiki_id": data.get("wiki_id"),
        "name": data.get("name"),
        "status": data.get("status"),
        "internal_status": data.get("internal_status"),
        "page_count": data.get("page_count"),
        "summary": data.get("summary"),
        "sync_error": data.get("sync_error"),
    }, ensure_ascii=False, indent=2))


def cmd_push(a: argparse.Namespace) -> None:
    files = []
    for pattern in a.files:
        for p in sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[") else [Path(pattern)]:
            if not p.is_file():
                log(f"跳过（不存在）: {p}")
                continue
            files.append({"filename": p.name, "content": p.read_text(encoding="utf-8"), "encoding": "utf8"})
    if not files:
        raise SystemExit("没有可推送的文件")
    if len(files) > 10:
        raise SystemExit(f"单次最多 10 个文件，本次 {len(files)} 个（分批推）")
    data = call("/wiki/raw/write", {"team_id": a.team, "wiki_id": a.wiki_id, "files": files}, auth=True)
    print(json.dumps({"accepted": [f["filename"] for f in files], "result": data}, ensure_ascii=False))


def cmd_ingest(a: argparse.Namespace) -> None:
    data = call("/wiki/ingest", {"wiki_id": a.wiki_id, "team_id": a.team, "on_busy": a.on_busy}, auth=True)
    log(f"ingest 已入队 status={data.get('status')} queued={data.get('queued')}")
    if not a.wait:
        return
    deadline = time.time() + a.timeout
    last = None
    while time.time() < deadline:
        time.sleep(a.interval)
        st = call("/wiki/get", {"wiki_id": a.wiki_id}, auth=False)
        cur = (st.get("status"), st.get("internal_status"), st.get("page_count"))
        if cur != last:
            log(f"  {cur[0]} / {cur[1]} / pages={cur[2]}")
            last = cur
        if st.get("status") in ("ready", "failed", "error"):
            print(json.dumps({k: st.get(k) for k in ("status", "internal_status", "page_count", "sync_error")},
                             ensure_ascii=False, indent=2))
            return
    raise SystemExit(f"超时 {a.timeout}s 仍未结束（用 status 继续观察）")


def cmd_pages(a: argparse.Namespace) -> None:
    data = call("/wiki/page/ls", {"wiki_id": a.wiki_id}, auth=False)
    for it in data.get("items") or []:
        print(f"{it.get('path')}")


def cmd_read(a: argparse.Namespace) -> None:
    data = call("/wiki/page/read", {"wiki_id": a.wiki_id, "refs": a.page}, auth=False)
    for it in data.get("items") or []:
        print(f"# {it.get('ref') or it.get('path')}\n")
        print(it.get("content") or "")


def cmd_search(a: argparse.Namespace) -> None:
    data = call("/wiki/search", {"wiki_id": a.wiki_id, "query": a.query, "top_k": a.top_k}, auth=False)
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── meta 侧登记：KS 建 wiki ≠ 出现在注入里 ────────────────────────────────
# Panel 建 wiki 时会在 create 阶段顺手打 /v3/meta/asset/create；纯 API 建的 wiki
# 没有这一步，结果就是「页面都在、面板也能看，但 agent 预热时永远注入不出来」。
# 而且 callback 写内核明细 /v3/knowledge/create 时带的是 detail.owner_user_id，
# 为 null 会被 zod 拒掉（400 user_id: expected string, received null）→ 明细缺失
# → 注入器同样跳过。所以 API 路线必须自己补三步。

def resolve_admin_key() -> str:
    key = os.environ.get("TDAI_ADMIN_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("TDAI_ADMIN_KEY_FILE", "/home/ubuntu/TencentDB-Agent-Memory/deploy/global-images/.admin-key")
    p = Path(path)
    if p.is_file():
        try:
            return p.read_text().strip()
        except PermissionError:
            import subprocess
            return subprocess.check_output(["sudo", "-n", "cat", path]).decode().strip()
    raise SystemExit("找不到 admin key（$TDAI_ADMIN_KEY 或 .admin-key 文件）")


def resolve_user_key() -> str:
    key = os.environ.get("TDAI_USER_KEY", "").strip()
    if key:
        return key
    cfg = Path(os.environ.get("HERMES_CONFIG", "/home/ubuntu/.hermes/config.yaml"))
    if cfg.is_file():
        m = re.search(r"api_key:\s*(sk-mem-\S+)", cfg.read_text())
        if m:
            return m.group(1)
    raise SystemExit("找不到 user key（$TDAI_USER_KEY 或 hermes config 的 sk-mem-*）")


def meta_call(path: str, body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    url = os.environ.get("CORE_URL", "http://127.0.0.1:8420").rstrip("/") + path
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + resolve_admin_key(),
        "x-tdai-service-id": os.environ.get("SERVICE_ID", "default"),
        "x-tdai-user-key": resolve_user_key(),
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        payload = {"code": e.code, "message": e.read().decode()[:400]}
    if payload.get("code") not in (0, None):
        raise SystemExit(f"{path} 失败: code={payload.get('code')} {payload.get('message')}")
    return payload.get("data") or {}


def cmd_register(a: argparse.Namespace) -> None:
    detail = call("/wiki/get", {"wiki_id": a.wiki_id}, auth=False)
    name = detail.get("name") or a.wiki_id
    user = a.user or os.environ.get("TDAI_USER_ID", "")
    if not user:
        raise SystemExit("缺少 user_id：用 --user 或 $TDAI_USER_ID")
    team = detail.get("team_id") or a.team
    service_url = detail.get("service_url")
    if not service_url:
        raise SystemExit("wiki 缺 service_url（KS 的 KNOWLEDGE_PUBLIC_BASE_URL 没配？）")

    # asset/create 不是 upsert：重复登记会 500 internal_error。先查后建。
    existing = None
    try:
        existing = meta_call("/v3/meta/asset/get", {"asset_id": a.wiki_id})
    except SystemExit:
        existing = None
    if existing:
        log("meta asset 已存在，跳过 create")
        meta_call("/v3/meta/asset/update", {"asset_id": a.wiki_id, "status": "approved"})
        log("meta asset 置为 approved")
    else:
        meta_call("/v3/meta/asset/create", {
            "asset_id": a.wiki_id, "team_id": team, "asset_type": "llm_wiki",
            "name": name, "owner_user_id": user, "source_type": "manual",
            "description": a.description or name, "status": "approved",
        })
        log("meta asset 已登记")

    summary = a.summary or f"{detail.get('page_count') or 0} 页知识库：{name}"
    meta_call("/v3/knowledge/create", {
        "knowledge_id": a.wiki_id, "type": "wiki", "service_url": service_url,
        "name": name, "summary": summary[:250], "team_id": team, "user_id": user,
    })
    log("内核明细已写入")

    for agent in a.bind or []:
        # agent-fixed-asset/set 是「整表替换」语义，不是 append：必须先读现有绑定再合并，
        # 否则第二次绑新 wiki 会把上一个 wiki 顶掉（2026-11-20 踩过）。
        cur = meta_call("/v3/meta/agent-fixed-asset/list", {
            "agent_id": agent, "page": 1, "page_size": 100,
        })
        bindings = [
            {"asset_id": i.get("asset_id"), "asset_type": i.get("asset_type"), "created_by": user}
            for i in (cur.get("items") or [])
            if i.get("asset_id") and i.get("asset_id") != a.wiki_id
        ]
        bindings.append({"asset_id": a.wiki_id, "asset_type": "llm_wiki", "created_by": user})
        meta_call("/v3/meta/agent-fixed-asset/set", {"agent_id": agent, "bindings": bindings})
        log(f"已绑定到 agent {agent}（合并后共 {len(bindings)} 条）")
        detail_items = meta_call("/v3/meta/agent-fixed-asset/list-with-detail", {
            "agent_id": agent, "page": 1, "page_size": 50, "asset_types": ["llm_wiki"],
        })
        got = [(i.get("asset_id"), i.get("name")) for i in (detail_items.get("items") or [])]
        print(f"  {agent} 当前 llm_wiki 绑定: {got}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TDAI knowledge wiki CLI")
    ap.add_argument("--team", default=os.environ.get("TEAM_ID", ""), help="team_id（默认取 $TEAM_ID）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("create"); p.add_argument("name"); p.add_argument("--user"); p.set_defaults(fn=cmd_create)

    p = sub.add_parser("register"); p.add_argument("wiki_id")
    p.add_argument("--user"); p.add_argument("--summary"); p.add_argument("--description")
    p.add_argument("--bind", nargs="*", metavar="AGENT_ID")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("status"); p.add_argument("wiki_id"); p.set_defaults(fn=cmd_status)

    p = sub.add_parser("push"); p.add_argument("wiki_id"); p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("ingest"); p.add_argument("wiki_id")
    p.add_argument("--wait", action="store_true"); p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--on-busy", default="reject", choices=["reject", "replace"])
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("pages"); p.add_argument("wiki_id"); p.set_defaults(fn=cmd_pages)

    p = sub.add_parser("read"); p.add_argument("wiki_id"); p.add_argument("--page", nargs="+", required=True)
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("search"); p.add_argument("wiki_id"); p.add_argument("query")
    p.add_argument("--top-k", type=int, default=8); p.set_defaults(fn=cmd_search)

    a = ap.parse_args()
    if a.cmd in ("list", "create", "push", "ingest") and not a.team:
        raise SystemExit("缺少 team_id：用 --team 或 $TEAM_ID")
    a.fn(a)


if __name__ == "__main__":
    main()
