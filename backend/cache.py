"""本地缓存。

存在的唯一理由：现场演示必须可复现。
同一个 query 两次搜出不同结果，讲到一半内容变了，比卡顿更糟。

顺带保护额度：调试时反复调用容易把 5000 次耗光。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CACHE_DIR = Path(__file__).with_name("_cache")
TTL_SECONDS = 24 * 3600


def _key(query: str, options: dict[str, Any]) -> str:
    # 只把会影响结果的选项纳入 key，避免改无关开关就失效
    relevant = {
        "q": (query or "").strip(),
        "time_window_days": int(options.get("time_window_days") or 0),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def get(query: str, options: dict[str, Any]) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{_key(query, options)}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - payload.get("_cached_at", 0) > TTL_SECONDS:
        return None
    return payload.get("data")


def put(query: str, options: dict[str, Any], data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{_key(query, options)}.json"
    payload = {"_cached_at": time.time(), "data": data}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响主流程


def latest_any() -> dict[str, Any] | None:
    """降级时用：取最近一次成功的任意结果，先让页面有东西可看。"""
    if not CACHE_DIR.exists():
        return None
    files = sorted(CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if time.time() - payload.get("_cached_at", 0) <= TTL_SECONDS:
            return payload.get("data")
    return None
