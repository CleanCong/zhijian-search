"""检索流程编排：改写 → 多路召回 → 去重 → 时效 → 质检 → 归并。

每一阶段都往 trace 里写一条，前端据此展示「这些结果是怎么挑出来的」。
trace 是给人看的，不是给日志看的，所以字段名要能直接读。
"""

from __future__ import annotations

import re
import time
from typing import Any

import cache
import filters
import llm
import merge
import zhihu

# 默认时效窗口：超过 2 年的内容直接剔除。
# 理由：实测同一个 query 会召回 2016 年的文章，推荐的机型早已停产。
DEFAULT_MAX_AGE_DAYS = 730

# 场景配置。
# sub_queries 里用 {q} 占位原始问题。
# 这些模板来自实测：把「哪个好」这类购买决策词换成「缺点/耗材/尺寸」
# 之后，软文比例从 40% 降到 2.5%。
SCENES: dict[str, dict[str, Any]] = {
    "vacuum": {
        "label": "对比选购",
        "keywords": ["扫地", "机器人", "吸尘", "拖地", "清洁"],
        "sub_queries": [
            "石头扫地机器人 用了半年 真实感受",
            "追觅扫地机器人 缺点 问题 后悔",
            "扫地机器人 耗材 一年 花多少钱",
            "扫地机器人 基站 尺寸 上下水 预留",
        ],
    },
    "renovation": {
        "label": "经验避坑",
        "keywords": ["装修", "定制", "柜", "板材", "全屋"],
        "sub_queries": [
            "全屋定制 增项 报价 有哪些坑",
            "全屋定制 板材 环保 怎么选",
            "全屋定制 安装 尺寸 翻车",
        ],
    },
    "kaoyan": {
        "label": "争议话题",
        "keywords": ["考研", "读研", "研究生", "上岸"],
        "sub_queries": [
            "考研 后悔 吗 真实经历",
            "读研 就业 值不值",
            "考研 成本 时间 三年",
        ],
    },
    "general": {
        "label": "综合探索",
        "keywords": [],
        # general 的子查询不写死，按问题类型动态选（见 pick_general_subs）
        "sub_queries": [],
    },
}

# 通用问题的子查询模板：先判断「这是个什么类型的问题」，再决定拆什么。
# 为什么需要：固定用「XX 缺点 问题」对所有问题都套，搜「什么是 RAG」
# 会被强行加上「缺点」，召回的东西就跑偏了。
GENERAL_PATTERNS: list[tuple[str, list[str]]] = [
    (
        r"哪个好|还是|对比|选哪个|vs|该选|区别",
        ["{q} 缺点 问题", "{q} 真实使用感受", "{q} 价格 差异"],
    ),
    (
        r"值不值|值得|有必要|该不该|后悔",
        ["{q} 后悔", "{q} 真实体验", "{q} 成本 代价"],
    ),
    (
        r"怎么|如何|有什么坑|注意|避免|方法",
        ["{q} 踩坑 经验", "{q} 注意事项", "{q} 失败 教训"],
    ),
    (
        r"是什么|什么是|为什么|原理|区别在",
        ["{q} 解释", "{q} 举例", "{q} 争议 不同看法"],
    ),
]

GENERAL_FALLBACK = ["{q} 真实体验", "{q} 缺点 问题"]


def pick_general_subs(query: str) -> list[str]:
    """给任意问题挑一组子查询。判断不了就用兜底。"""
    q = query or ""
    for pattern, subs in GENERAL_PATTERNS:
        if re.search(pattern, q):
            return subs
    return GENERAL_FALLBACK


def detect_scene(query: str) -> tuple[str, dict[str, Any]]:
    q = query or ""
    for name, conf in SCENES.items():
        if name == "general":
            continue
        if any(k in q for k in conf["keywords"]):
            return name, conf
    return "general", SCENES["general"]


CN_NUM = {"一": "1", "两": "2", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}

# 从问题里提取「价格/预算」这类硬约束。
# 为什么需要：实测拆子查询会把「两千价位」这个限定丢掉，
# 结果召回一堆 4000 元机型的内容，模型看到价格对不上就会说「材料不足」。
PRICE_PATTERNS = [
    re.compile(r"(\d{3,5})\s*(?:元|块|以内|左右|上下)"),
    re.compile(r"([一两二三四五六七八九]千|[一两二三四五六七八九]万)\s*(?:元|块|价位|以内|左右|预算)"),
    re.compile(r"预算\s*(\d{3,5})"),
]


def extract_constraints(query: str) -> list[str]:
    """抽出问题里的硬性限定，附加到子查询上，避免召回跑题。"""
    found: list[str] = []
    for pat in PRICE_PATTERNS:
        for match in pat.finditer(query or ""):
            raw = match.group(1)
            if raw[0] in CN_NUM and raw.endswith(("千", "万")):
                unit = 1000 if raw.endswith("千") else 10000
                value = CN_NUM[raw[0]]
                # 「两千」→ 2000
                cost = int(value) * unit if value.isdigit() else 0
                if cost:
                    found.append(f"{cost}元")
            else:
                found.append(f"{raw}元")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:1]


def build_sub_queries(query: str, conf: dict[str, Any]) -> list[str]:
    """原始问题放在第一位，后面跟场景模板，并把硬约束带到每个子查询上。"""
    constraints = extract_constraints(query)
    prefix = " ".join(constraints)

    subs = [query]
    templates = conf.get("sub_queries") or []
    if not templates:                      # general 场景：按问题类型动态选
        templates = pick_general_subs(query)
    for tpl in templates:
        sub = tpl.format(q=query)
        if prefix and prefix not in sub:
            sub = f"{prefix} {sub}"
        subs.append(sub)

    # 去重但保持顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for s in subs:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = item.get("id") or item.get("url") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def run(query: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """跑完整流程。任何一步失败都要能给出可用的结果，不要把异常抛给前端。"""
    options = options or {}
    query = (query or "").strip()
    if not query:
        return _empty_payload("", "问题是空的", options)

    # 缓存命中直接返回（演示可复现，也保护额度）
    cached = cache.get(query, options)
    if cached:
        cached["mode"] = "real"
        cached["from_cache"] = True
        return cached

    start = time.time()
    scene_name, conf = detect_scene(query)
    subs = build_sub_queries(query, conf)
    trace: list[dict[str, Any]] = []

    trace.append({
        "stage": "query_rewrite",
        "label": "改写查询",
        "subqueries": subs,
        "detail": f"拆成 {len(subs)} 个问题分别去找",
    })

    # ---------- 多路召回 ----------
    raw_items: list[dict[str, Any]] = []
    per_query: list[dict[str, Any]] = []
    errors: list[str] = []

    for sub in subs:
        try:
            hits = zhihu.search(sub)
        except zhihu.ZhihuError as exc:
            errors.append(f"{sub}：{exc}")
            per_query.append({"query": sub, "count": 0, "ok": False})
            continue
        normalized = [zhihu.normalize(h) for h in hits]
        for item in normalized:
            item["sub_query"] = sub
        raw_items.extend(normalized)
        per_query.append({"query": sub, "count": len(normalized), "ok": True})

    if not raw_items:
        # 全部失败 → 降级，不让前端看到 500
        fallback = cache.latest_any()
        if fallback:
            fallback["mode"] = "degraded"
            fallback["degraded_reason"] = "本次检索没有拿到内容，展示上一次的结果"
            return fallback
        return _empty_payload(query, "检索服务暂时不可用，也没有可用的历史结果", options)

    trace.append({
        "stage": "recall",
        "label": "多路召回",
        "total": len(raw_items),
        "per_query": per_query,
        "detail": f"从 {sum(1 for p in per_query if p['ok'])} 个问题里拿到 {len(raw_items)} 条",
    })

    # ---------- 去重 ----------
    before = len(raw_items)
    items = _dedupe(raw_items)
    if before != len(items):
        trace.append({
            "stage": "dedupe",
            "label": "去掉重复",
            "before": before,
            "after": len(items),
            "detail": f"有 {before - len(items)} 条在不同问题里重复出现",
        })

    # ---------- 时效过滤 ----------
    max_age = int(options.get("time_window_days") or 0) or DEFAULT_MAX_AGE_DAYS
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        age = item.get("age_days")
        if age is not None and age > max_age:
            dropped.append({"title": item.get("title"), "age_days": age})
        else:
            kept.append(item)
    if dropped:
        trace.append({
            "stage": "freshness",
            "label": "排掉旧内容",
            "before": len(items),
            "after": len(kept),
            "dropped": len(dropped),
            "detail": f"{len(dropped)} 条超过 {max_age // 365 or 1} 年，情况可能已经变了",
            "examples": [d["title"] for d in dropped[:3]],
        })

    # ---------- 可信度质检 ----------
    cfg = filters.load_rules()
    filters.judge_all(kept, cfg, scene=scene_name)
    ad_count = sum(1 for i in kept if i.get("is_ad_suspect"))
    trace.append({
        "stage": "credibility",
        "label": "看看有没有在卖东西的",
        "total": len(kept),
        "ad_suspect": ad_count,
        "detail": f"{ad_count} 条像是在推广，标记后收起，不删除",
    })

    # ---------- 归并 ----------
    items_grouped, cards = merge.group(kept, query)
    counts = {
        "consensus": sum(1 for i in items_grouped if i["group"] == "consensus"),
        "disputed": sum(1 for i in items_grouped if i["group"] == "disputed"),
        "pitfalls": sum(1 for i in items_grouped if i["group"] == "pitfalls"),
        "hidden": sum(1 for i in items_grouped if i["group"] == "hidden"),
    }
    trace.append({
        "stage": "grouping",
        "label": "把不同说法归到一起",
        "counts": counts,
        "detail": f"一致的 {counts['consensus']} 条，对立的 {counts['disputed']} 条，"
                  f"具体的坑 {counts['pitfalls']} 条",
    })

    # ---------- 生成结论（模型优先，规则兜底）----------
    # 规则法只能挑句子，挑不出「判断」。有模型就用模型，失败就退回规则结果，
    # 不阻断主流程。
    summary = llm.summarize(query, items_grouped)
    if summary and cards:
        cards[0]["core_conclusion"] = summary["conclusion"]
        cards[0]["conclusion_confidence"] = summary["confidence"]
        if summary.get("key_points"):
            cards[0]["key_points"] = summary["key_points"]
        cards[0]["basis_titles"] = summary.get("basis_titles", [])
        cards[0]["summary_source"] = "model"
        trace.append({
            "stage": "summarize",
            "label": "把结论写出来",
            "detail": f"依据 {len(summary.get('basis_titles', []))} 条材料归纳，置信度 {summary['confidence']}",
        })
    elif cards:
        cards[0]["summary_source"] = "rule"
        trace.append({
            "stage": "summarize",
            "label": "把结论写出来",
            "detail": "模型没给出可用结论，改用关键词抽取",
        })

    payload = {
        "query": query,
        "intent": scene_name,
        "intent_label": conf.get("label", "综合探索"),
        "mode": "real",
        "answers": items_grouped,
        "cards": cards,
        "matrices": [],
        "trace": trace,
        "errors": errors,
        "stats": {
            "recalled": len(raw_items),
            "unique": len(items),
            "after_freshness": len(kept),
            "hidden": counts["hidden"],
            "kept": len(kept) - counts["hidden"],
        },
        "elapsed_ms": int((time.time() - start) * 1000),
    }

    cache.put(query, options, payload)
    return payload


def _empty_payload(query: str, reason: str, options: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": query,
        "intent": "general",
        "intent_label": "综合探索",
        "mode": "degraded",
        "degraded_reason": reason,
        "answers": [],
        "cards": [],
        "matrices": [],
        "trace": [],
        "errors": [reason],
        "stats": {"recalled": 0, "unique": 0, "after_freshness": 0, "hidden": 0, "kept": 0},
    }
