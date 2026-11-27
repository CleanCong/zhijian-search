"""检索规划：决定检索什么、去哪里、检索几次。

只做规划。不做 Claim 抽取，不做证据判断。

两条路径：
- 有模型 → 让模型按问题特点动态拆解
- 模型不可用 → 规则兜底（按问题类型套模板）

规则兜底不是为了"能跑就行"，而是为了**演示时不依赖网络**。
如果现场模型超时，规划器仍然能给出一个合理的计划。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from models.retrieval import (
    DEFAULT_BUDGET,
    RetrievalPlan,
    SourceName,
    SubQuery,
)

PLAN_VERSION = "v1"          # 改 prompt 时必须递增，否则会命中旧缓存

SYSTEM_PROMPT = """你是检索规划器。用户提一个问题，你决定要去搜什么。

规则：
1. 拆 2-4 个子查询，每个聚焦一个角度。不要把一个意思换几种说法。
2. **必须有一条 purpose=counter**：主动去找反例、反对意见、"其实不是这样"的说法。
   只找顺耳的证据，最后会得到片面的结论。
3. purpose 四选一：
   - core       直接回答问题的材料
   - mechanism  解释原理、因果、机制的材料
   - counter    反方观点、踩坑、不同解释
   - latest     最新进展（**只有明确问时效性时用**，否则不要）
4. 子查询要像搜索词，不超过 30 字，不要写"请问""如何理解"这类话。
5. 不要重复用户原问题的整句。

输出 JSON，不要任何额外文字：
{
  "intent": "简短意图标签",
  "sub_queries": [
    {"query": "...", "purpose": "core", "target_sources": ["zhihu"], "priority": 1}
  ],
  "source_strategy": ["zhihu", "global"],
  "time_window_days": null,
  "reason": "一句话说明为什么这样规划"
}"""

_VALID_SOURCES = {"zhihu", "global", "question_answers", "direct_answer", "hot", "discovery"}


def _load_env() -> None:
    """复用 backend/.env，与现有 llm.py 保持同一套配置来源。"""
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


def _call_llm(query: str, timeout: int = 20) -> dict[str, Any] | None:
    """调 DeepSeek 拿计划。失败返回 None，由调用方走兜底。"""
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
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"问题：{query}"},
                ],
                "temperature": 0.3,
                "max_tokens": 600,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_plan_json(content)
    except Exception:
        return None


def _parse_plan_json(text: str) -> dict[str, Any] | None:
    """模型可能带 markdown 代码块或前后废话，这里做容错。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _sanitize(raw: dict[str, Any], query: str) -> dict[str, Any] | None:
    """把模型输出洗成合法计划。宁可丢字段，不要让脏值进模型层。"""
    subs_raw = raw.get("sub_queries")
    if not isinstance(subs_raw, list) or not subs_raw:
        return None

    subs: list[dict[str, Any]] = []
    for i, s in enumerate(subs_raw[:5]):
        if not isinstance(s, dict):
            continue
        q = (s.get("query") or "").strip()
        if not q or len(q) > 80:
            continue
        purpose = s.get("purpose") if s.get("purpose") in {"core", "mechanism", "counter", "latest"} else "core"
        srcs = [x for x in (s.get("target_sources") or ["zhihu"]) if x in _VALID_SOURCES] or ["zhihu"]
        try:
            prio = int(s.get("priority") or (i + 1))
        except (TypeError, ValueError):
            prio = i + 1
        subs.append({
            "query": q,
            "purpose": purpose,
            "target_sources": srcs,
            "priority": max(1, min(9, prio)),
        })

    if not subs:
        return None

    # 强制补一条反方观点：只找顺耳的证据会得出片面结论
    if not any(s["purpose"] == "counter" for s in subs):
        subs.append({
            "query": f"{query} 反面 不同看法",
            "purpose": "counter",
            "target_sources": ["zhihu"],
            "priority": min(9, len(subs) + 1),
        })
        # 补完可能超过上限，挤掉优先级最低的非 counter 项
        if len(subs) > 5:
            droppable = [i for i, x in enumerate(subs) if x["purpose"] != "counter"]
            if droppable:
                drop = max(droppable, key=lambda i: (subs[i]["priority"], i))
                subs.pop(drop)

    strategy = [x for x in (raw.get("source_strategy") or ["zhihu"]) if x in _VALID_SOURCES] or ["zhihu"]
    tw = raw.get("time_window_days")
    try:
        tw = int(tw) if tw is not None else None
    except (TypeError, ValueError):
        tw = None

    return {
        "intent": str(raw.get("intent") or "general")[:24],
        "sub_queries": subs,
        "source_strategy": strategy,
        "time_window_days": tw,
        "reason": str(raw.get("reason") or "")[:120],
    }


# ---------------- 规则兜底 ----------------

_PATTERNS = [
    (r"哪个好|怎么选|对比|还是|vs|区别|选哪个", "comparison"),
    (r"值不值|值得|该不该|有必要|要不要", "decision"),
    (r"为什么|原理|机制|怎么做到|动力|来源", "mechanism"),
    (r"是什么|什么是|定义|概念|解释", "concept"),
    (r"怎么|如何|怎样|方法|技巧|经验", "howto"),
]


def _detect_intent(query: str) -> str:
    for pattern, name in _PATTERNS:
        if re.search(pattern, query):
            return name
    return "general"


def rule_based_plan(query: str) -> RetrievalPlan:
    """不依赖模型的兜底计划。

    保证：至少 3 条子查询、必有一条 counter、预算在限额内。
    """
    intent = _detect_intent(query)
    q = query.strip().rstrip("？?。.")

    subs = [SubQuery(query=q, purpose="core", target_sources=["zhihu"], priority=1)]

    if intent in {"mechanism", "concept"}:
        subs.append(SubQuery(query=f"{q} 原理 为什么", purpose="mechanism", target_sources=["zhihu"], priority=2))
        subs.append(SubQuery(query=f"{q} 误解 错误理解", purpose="counter", target_sources=["zhihu"], priority=3))
    elif intent == "comparison":
        subs.append(SubQuery(query=f"{q} 缺点 问题", purpose="counter", target_sources=["zhihu"], priority=2))
        subs.append(SubQuery(query=f"{q} 真实使用感受", purpose="core", target_sources=["zhihu"], priority=3))
    elif intent == "decision":
        subs.append(SubQuery(query=f"{q} 后悔 踩坑", purpose="counter", target_sources=["zhihu"], priority=2))
        subs.append(SubQuery(query=f"{q} 成本 代价", purpose="mechanism", target_sources=["zhihu"], priority=3))
    else:
        subs.append(SubQuery(query=f"{q} 经验 注意事项", purpose="core", target_sources=["zhihu"], priority=2))
        subs.append(SubQuery(query=f"{q} 反例 不同的说法", purpose="counter", target_sources=["zhihu"], priority=3))

    return RetrievalPlan(
        original_query=query,
        intent=intent,
        sub_queries=subs[:4],
        source_strategy=["zhihu", "global"],
        retrieval_budget=dict(DEFAULT_BUDGET),
        time_window_days=None,
        reason="规则兜底：按问题类型套用固定角度",
        plan_version=PLAN_VERSION,
    )


def plan(query: str, allow_llm: bool = True) -> tuple[RetrievalPlan, str]:
    """生成检索计划。

    Returns:
        (计划, 来源标记)  —— 来源标记为 "llm" 或 "rule"，写进 Trace 便于排查
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("问题不能为空")

    if allow_llm:
        _load_env()
        raw = _call_llm(query)
        if raw:
            cleaned = _sanitize(raw, query)
            if cleaned:
                try:
                    return RetrievalPlan(
                        original_query=query,
                        intent=cleaned["intent"],
                        sub_queries=[SubQuery(**s) for s in cleaned["sub_queries"]],
                        source_strategy=cleaned["source_strategy"],
                        retrieval_budget=dict(DEFAULT_BUDGET),
                        time_window_days=cleaned["time_window_days"],
                        reason=cleaned["reason"],
                        plan_version=PLAN_VERSION,
                    ), "llm"
                except Exception:
                    pass        # 模型给了非法结构，退回规则

    return rule_based_plan(query), "rule"
