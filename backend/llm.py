"""用 DeepSeek 把多条社区内容压成一句结论 + 三条要点。

为什么需要这个：纯规则抽取做不出「结论」。
实测词频打分会选出「直奔吸尘器而去，看来还有点蒙圈」这种吐槽句，
因为「结论」需要理解上下文，不是词频能算出来的。

安全约定：
· API Key 只从环境变量或 .env 读取，绝不写进代码、日志、返回值
· 调用失败一律返回 None，由上层降级到规则抽取，不影响主流程
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

ENV_PATH = Path(__file__).with_name(".env")
TIMEOUT = 30
MAX_ITEMS = 12
MAX_CHARS_PER_ITEM = 320


def _load_env() -> None:
    """极简 .env 读取，避免引入额外依赖。"""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM_PROMPT = """你是一个把社区讨论压缩成判断的助手。

规则（必须遵守）：
1. 只根据给出的材料说话，不补充材料里没有的信息。
2. 如果材料不足以支撑一个明确结论，就把 confidence 设为 low，并直接说"材料还不够"。
3. 不要写"综上所述""希望对你有帮助"这类废话。
4. 不要用"可能""也许"堆砌模糊表述，要么给判断，要么承认不知道。
5. 结论要具体到能指导决策，不要写"要综合考虑"这种正确但无用的话。

输出必须是 JSON，字段：
{
  "conclusion": "一句话结论，不超过 40 字",
  "confidence": "high | medium | low",
  "key_points": ["最多 3 条，每条不超过 26 字，要具体"],
  "basis": [用到的材料编号，如 1, 3, 5]
}"""


def _build_user_prompt(query: str, items: list[dict[str, Any]]) -> str:
    lines = [f"问题：{query}", "", f"材料（共 {len(items)} 条，已剔除疑似广告）："]
    for idx, item in enumerate(items, 1):
        body = re.sub(r"\s+", " ", (item.get("content_text") or "")).strip()
        if len(body) > MAX_CHARS_PER_ITEM:
            body = body[:MAX_CHARS_PER_ITEM] + "…"
        title = (item.get("title") or "").strip()
        author = item.get("author_signature") or "匿名"
        lines.append(f"[{idx}] 标题：{title}")
        lines.append(f"    作者：{author}　赞同：{item.get('vote_up_count', 0)}")
        lines.append(f"    内容：{body}")
        lines.append("")
    lines.append("请输出 JSON。")
    return "\n".join(lines)


def _parse_json(text: str) -> dict[str, Any] | None:
    """容错解析：模型可能把 JSON 包在 ```json 里。"""
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 退一步：抓第一个完整的大括号块
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def summarize(query: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """调模型生成结论。失败返回 None，调用方降级。"""
    if not API_KEY:
        return None

    # 只喂证据质量最好的那些：非广告、有正文、有一定长度
    usable = [
        i for i in items
        if not i.get("is_ad_suspect") and len(i.get("content_text") or "") >= 60
    ]
    usable.sort(key=lambda i: (i.get("vote_up_count", 0), i.get("char_count", 0)), reverse=True)
    usable = usable[:MAX_ITEMS]
    if len(usable) < 3:
        return None  # 材料太少，不值得调模型

    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(query, usable)},
                ],
                "temperature": 0.2,
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[llm] 请求失败：{type(exc).__name__}")  # 只打类型，不打内容，避免泄露
        return None

    if resp.status_code != 200:
        print(f"[llm] HTTP {resp.status_code}")
        return None

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        print("[llm] 返回结构异常")
        return None

    parsed = _parse_json(content)
    if not parsed or not parsed.get("conclusion"):
        return None

    # 把 basis 编号换成真实标题，方便前端展示"这句话依据哪几条"
    basis_titles: list[str] = []
    for num in parsed.get("basis") or []:
        try:
            idx = int(num) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(usable):
            basis_titles.append(usable[idx].get("title") or "")

    points = [p for p in (parsed.get("key_points") or []) if isinstance(p, str) and p.strip()]

    return {
        "conclusion": parsed["conclusion"].strip()[:80],
        "confidence": parsed.get("confidence") if parsed.get("confidence") in {"high", "medium", "low"} else "medium",
        "key_points": points[:3],
        "basis_titles": basis_titles[:5],
        "model": MODEL,
    }
