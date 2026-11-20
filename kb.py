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
    data = call("/wiki/create", {"team_id": a.team, "name": a.name}, auth=True)
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
    data = call("/wiki/page/read", {"wiki_id": a.wiki_id, "path": a.page}, auth=False)
    print(data.get("content") or json.dumps(data, ensure_ascii=False)[:2000])


def cmd_search(a: argparse.Namespace) -> None:
    data = call("/wiki/search", {"wiki_id": a.wiki_id, "query": a.query, "top_k": a.top_k}, auth=False)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="TDAI knowledge wiki CLI")
    ap.add_argument("--team", default=os.environ.get("TEAM_ID", ""), help="team_id（默认取 $TEAM_ID）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("create"); p.add_argument("name"); p.set_defaults(fn=cmd_create)

    p = sub.add_parser("status"); p.add_argument("wiki_id"); p.set_defaults(fn=cmd_status)

    p = sub.add_parser("push"); p.add_argument("wiki_id"); p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("ingest"); p.add_argument("wiki_id")
    p.add_argument("--wait", action="store_true"); p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--on-busy", default="reject", choices=["reject", "replace"])
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("pages"); p.add_argument("wiki_id"); p.set_defaults(fn=cmd_pages)

    p = sub.add_parser("read"); p.add_argument("wiki_id"); p.add_argument("--page", required=True)
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("search"); p.add_argument("wiki_id"); p.add_argument("query")
    p.add_argument("--top-k", type=int, default=8); p.set_defaults(fn=cmd_search)

    a = ap.parse_args()
    if a.cmd in ("list", "create", "push", "ingest") and not a.team:
        raise SystemExit("缺少 team_id：用 --team 或 $TEAM_ID")
    a.fn(a)


if __name__ == "__main__":
    main()
