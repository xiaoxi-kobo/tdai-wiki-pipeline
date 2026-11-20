#!/usr/bin/env python3
"""verify_jev.py — 验证 TDAI 用的 JEV（TypeSafe System One）端点是否可用。

只从 TDAI 部署 .env 读 `TYPESAFE_API_KEY`，**绝不打印 key**。输出：
  1) noul 题型（要不要更新画像）
  2) choice 题型（选哪一层）
  3) score 题型（置信档位）
以及每次的 model / 耗时 / usage tokens。

用法：
  ./verify_jev.py                 # 用 .env 里的 endpoint/model/key
  ./verify_jev.py --endpoint URL --model @makers/jev
  JEV_TIMEOUT=20 ./verify_jev.py

退出码：0 全通 / 1 有任一题型失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_CANDIDATES = (
    os.environ.get("TDAI_ENV_FILE", ""),
    "/home/ubuntu/TencentDB-Agent-Memory/deploy/global-images/.env",
)


def dotenv(name: str) -> str:
    for cand in ENV_CANDIDATES:
        if not cand or not Path(cand).is_file():
            continue
        for line in Path(cand).read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


STATE = (
    "用户在 2026-11-20 把 TDAI 的 knowledge wiki 编译模型改成 glm-5.3，"
    "并把 L3 画像更新决策切换到 JEV。他偏好一步一验证，不接受未经验证的改动。"
)

CASES = {
    "noul": {
        "type": "noul",
        "instructions": "这条信息反映的是用户稳定的长期偏好，而不是一次性事件。",
        "criteria": {
            "yes": "稳定偏好、长期习惯或身份事实",
            "no": "一次性操作、临时状态或与本人无关的内容",
        },
    },
    "choice": {
        "type": "choice",
        "instructions": "这条信息最该写进哪一层记忆？",
        "criteria": {
            "L1": "单次事件，保留原文即可",
            "L2": "某个项目/场景的过程性结论",
            "L3": "跨场景的稳定偏好或身份事实",
        },
    },
    "score": {
        "type": "score",
        "instructions": "这条信息对长期画像的价值有多高？",
        "criteria": ["无价值，纯噪音", "一般，可留可去", "高价值，应长期保留"],
    },
}


def call(endpoint: str, key: str, model: str, questions: dict, timeout: int) -> tuple[int, dict, float]:
    body = {"model": model, "state": STATE, "questions": questions}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")[:300]
        return e.code, {"error": raw}, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=dotenv("MEMORY_CORE_JEV_ENDPOINT") or "https://ai-gateway.edgeone.link/v1/systemone")
    ap.add_argument("--model", default=dotenv("MEMORY_CORE_JEV_MODEL") or "@makers/jev")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("JEV_TIMEOUT", "30")))
    a = ap.parse_args()

    key = dotenv("TYPESAFE_API_KEY")
    if not key:
        print("✗ .env 里没有 TYPESAFE_API_KEY", file=sys.stderr)
        return 1
    print(f"endpoint = {a.endpoint}")
    print(f"model    = {a.model}")
    print(f"key      = {key[:4]}…{key[-4:]}（长度 {len(key)}，不打印全文）\n")

    failed = 0
    for name, q in CASES.items():
        status, payload, secs = call(a.endpoint, key, a.model, {name: q}, a.timeout)
        ok = status == 200 and isinstance(payload.get("answers"), dict) and name in payload["answers"]
        usage = payload.get("usage") or {}
        print(f"[{'✓' if ok else '✗'}] {name:<7} HTTP {status}  {secs:.1f}s  model={payload.get('model', '-')}")
        if ok:
            print(f"    answer = {json.dumps(payload['answers'][name], ensure_ascii=False)[:220]}")
            print(f"    usage  = in {usage.get('input_tokens', '?')} / out {usage.get('output_tokens', '?')}")
        else:
            failed += 1
            print(f"    body = {json.dumps(payload, ensure_ascii=False)[:300]}")
        print()

    print("结论：" + ("全部通过，JEV 可用。" if not failed else f"{failed} 个题型失败，检查 key / endpoint / model。"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
