"""知乎开放平台 CLI 封装。

把 subprocess 调用、编码处理、字段映射都收在这里，
上层（pipeline）只关心 normalize 之后的统一结构。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

# CLI 路径：优先读环境变量，方便部署到服务器时改
CLI_PATH = os.environ.get(
    "ZHIHU_CLI_PATH",
    str(Path.home() / "AppData/Local/ZhihuCLI/current/zhihu-cli.exe"),
)

DEFAULT_TIMEOUT = 20
DEFAULT_COUNT = 10


class ZhihuError(RuntimeError):
    """知乎调用失败。上层据此走降级分支，不要让它冒成 500。"""


def search(query: str, count: int = DEFAULT_COUNT, timeout: int = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """调一次 search zhihu，返回原始 Items 列表。

    坑都在这里处理掉了：
    - 输出是 UTF-8，必须显式 decode，用系统默认编码会乱码
    - 单次最多 10 条，HasMore 恒为 false，不能翻页
    - Code != 0 视为失败，抛 ZhihuError 而不是返回空列表
    """
    if not Path(CLI_PATH).exists():
        raise ZhihuError(f"找不到知乎 CLI：{CLI_PATH}")

    try:
        proc = subprocess.run(
            [CLI_PATH, "search", "zhihu", "--query", query, "--count", str(count)],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ZhihuError(f"检索超时（{timeout}s）：{query}") from exc
    except OSError as exc:
        raise ZhihuError(f"无法执行 CLI：{exc}") from exc

    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise ZhihuError(f"CLI 无输出，stderr：{stderr or '(空)'}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ZhihuError(f"返回不是合法 JSON：{raw[:160]}") from exc

    if payload.get("Code") != 0:
        raise ZhihuError(f"接口返回异常：{payload.get('Message') or payload.get('Code')}")

    return (payload.get("Data") or {}).get("Items") or []


def _age_days(timestamp: Any) -> int | None:
    """知乎 CreateTime 是秒级时间戳，换算成距今天数。"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return max(0, int((time.time() - ts) / 86400))


def _published_year(timestamp: Any) -> int | None:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return time.localtime(ts).tm_year


def normalize(item: dict[str, Any]) -> dict[str, Any]:
    """把知乎原始条目映射成前端契约字段。

    只做映射与类型清洗，不做任何判断（判断在 filters 里）。
    """
    created = item.get("CreateTime")
    body = (item.get("ContentText") or "").strip()
    return {
        "id": str(item.get("ContentID") or item.get("Url") or ""),
        "title": (item.get("Title") or "").strip(),
        "content_text": body,
        "url": (item.get("Url") or "").split("?")[0],  # 去掉 utm 参数，给用户看得干净些
        "author_signature": (item.get("AuthorName") or "知乎用户").strip(),
        "author_badge": (item.get("AuthorBadgeText") or "").strip(),
        "vote_up_count": int(item.get("VoteUpCount") or 0),
        "comment_count": int(item.get("CommentCount") or 0),
        "content_type": (item.get("ContentType") or "Unknown"),
        "age_days": _age_days(created),
        "published_year": _published_year(created),
        "char_count": len(body),
        # 以下由后续阶段填充
        "is_ad_suspect": False,
        "reasons": [],
        "group": None,
        "sub_query": None,
    }
