#!/usr/bin/env python3
"""演示预热 & 线上冒烟（任务 A7）。

演示前把三个演示场景各跑一遍：预热缓存（cache.py，24h TTL），
顺带当健康冒烟——任一场景返回 degraded 或空结果立即报警。

用法：
    python scripts/warmup.py                               # 本地 127.0.0.1:8000
    python scripts/warmup.py --base https://your-domain    # 线上
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# 三个演示场景（与 pipeline.SCENES 对齐）
SCENES = [
    "两千价位扫地机器人 石头 追觅 怎么选",
    "全屋定制怎么避免被坑",
    "考研值不值得 二战",
]


def _post(base: str, query: str, timeout: int):
    data = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/search",
        data=data, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8")), time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args()

    try:
        with urllib.request.urlopen(args.base.rstrip("/") + "/", timeout=5) as r:
            print(f"[health] {json.loads(r.read())}")
    except Exception as exc:
        print(f"[FATAL] 服务不可达：{type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

    all_ok = True
    for q in SCENES:
        try:
            status, body, cost = _post(args.base, q, args.timeout)
            mode = body.get("mode")
            n = len(body.get("answers", []))
            ok = status == 200 and mode in ("real",) and n > 0
            print(f"[{'OK ' if ok else 'WARN'}] {q[:20]:20s} status={status} mode={mode} answers={n} {cost:.2f}s")
            all_ok = all_ok and ok
        except Exception as exc:
            all_ok = False
            print(f"[FAIL] {q[:20]:20s} {type(exc).__name__}")

    print("=> 预热完成，三场景均真实返回" if all_ok
          else "=> 预热发现异常（degraded/空/报错），演示前务必排查")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
