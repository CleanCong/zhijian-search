"""关系判断：让模型判定每条证据是支持、反驳、补充还是仅提及。

## 必须批量

10 个 Claim × 8 条候选 = 80 个判断。逐条调用就是 80 次模型请求，
成本和时间都不可接受。这里按批打包，一次问 10–15 对。

## 必须要求 evidence_quote

模型说某条材料反驳 Claim，却指不出原文中的具体依据时，
很可能它误读了上下文。留着这段引文，人工复核才有抓手。

## 模型失败不伪装

调用失败返回 None，由上层记为 pending。**绝不能把失败当成 mentions**——
那会把"没判断"混进"判断为仅提及"，污染统计。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from models import Claim, Evidence

BATCH_SIZE = 12
MAX_EVIDENCE_CHARS = 700       # 单条证据送进去的正文上限，够判断又不爆 token

RELATION_PROMPT = """你要判断每对「主张-材料」之间的关系。

四种关系：
- supports     材料明确给出了支持该主张的理由或证据
- refutes      材料明确给出了反对该主张的理由、反例或相反结论
- supplements  材料提供了背景、机制、适用条件，但没有直接证明该主张
- mentions     材料只提到相关概念，没有论证

两个最常见的误判，务必避免：

1. 看到反向词就判 refutes。
   例：「传统伯努利解释有局限，但压力差仍是升力的重要机制」
   这不是反驳「压力差产生升力」，而是 supplements（补充了适用范围）。

2. 看到相似内容就判 supports。
   例：「飞机机翼采用特殊翼型设计」
   提到了机翼，但没有直接支持某个具体的升力机制主张，应判 mentions。

信息不足以判断时，判 mentions 并把 confidence 给低（≤0.4）。

严格输出 JSON 数组，每项：
{
  "index": 输入编号,
  "relation": "supports|refutes|supplements|mentions",
  "confidence": 0.0-1.0,
  "rationale": "一句话说明为什么",
  "evidence_quote": "材料中支撑该判断的原文片段（不超过 60 字）；判 mentions 时可为空"
}
不要输出任何其他文字。"""


def _load_env() -> None:
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _build_prompt(pairs: list[tuple[Claim, Evidence]]) -> str:
    lines = []
    for i, (claim, ev) in enumerate(pairs, 1):
        body = re.sub(r"\s+", " ", ev.content or "").strip()
        if len(body) > MAX_EVIDENCE_CHARS:
            body = body[:MAX_EVIDENCE_CHARS] + "…"
        lines.append(f"[{i}] 主张：{claim.claim_text}")
        lines.append(f"    材料（{ev.source_name or ev.source_type}，{ev.content_type}）：{body}")
        lines.append("")
    return "\n".join(lines)


def _parse_array(text: str) -> list[dict] | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _call_llm(pairs: list[tuple[Claim, Evidence]], timeout: int = 45) -> list[dict] | None:
    import requests

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": RELATION_PROMPT},
                    {"role": "user", "content": _build_prompt(pairs)},
                ],
                "temperature": 0.1,
                "max_tokens": 1800,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_array(content)
    except Exception:
        return None


def classify_batch(pairs: list[tuple[Claim, Evidence]]) -> list[dict[str, Any]] | None:
    """判断一批关系。返回原始判定列表（未校验），失败返回 None。"""
    if not pairs:
        return []
    _load_env()
    raw = _call_llm(pairs)
    if raw is None:
        return None

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(pairs):
            continue
        claim, ev = pairs[idx]
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append({
            "claim_id": claim.claim_id,
            "evidence_id": ev.evidence_id,
            "relation": item.get("relation"),
            "confidence": max(0.0, min(1.0, conf)),
            "rationale": str(item.get("rationale") or "")[:160],
            "evidence_quote": (str(item.get("evidence_quote") or "")[:120] or None),
        })
    return out


def classify_relations(pairs: list[tuple[Claim, Evidence]]) -> tuple[list[dict], list[str]]:
    """按批切分并逐批判断。

    Returns:
        (判定列表, 失败说明列表)
    """
    results: list[dict] = []
    errors: list[str] = []

    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start:start + BATCH_SIZE]
        got = classify_batch(batch)
        if got is None:
            errors.append(f"批次 {start//BATCH_SIZE + 1} 判断失败（{len(batch)} 对），保留待处理")
            continue
        results.extend(got)

    return results, errors
