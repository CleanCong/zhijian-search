"""可信度过滤引擎。

设计原则：
1. 判据全部放在 rules.json 里，这里只负责「怎么执行」，不写死任何词表。
   C 改规则不需要碰代码，也不需要找开发。
2. 输给人看的是 reasons（人话），不是分数。
   分数只留在 debug 里，用来调阈值。
3. 判为疑似推广的内容**不删除**，只打标记。是否展示由前端决定
   （我们的产品主张是判断权归用户）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).with_name("rules.json")


def load_rules(path: Path | str = RULES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _text_for(item: dict[str, Any], scope: str) -> str:
    """按 scope 取出要匹配的文本。"""
    title = item.get("title") or ""
    author = f"{item.get('author_signature') or ''} {item.get('author_badge') or ''}"
    body = item.get("content_text") or ""
    if scope == "title":
        return title
    if scope == "author":
        return author
    if scope == "body_head":
        return body[:400]
    if scope == "body_tail":
        return body[-200:]
    return body


def _apply_rule(item: dict[str, Any], rule: dict[str, Any]) -> tuple[bool, str]:
    """执行单条规则，返回 (是否命中, 补充说明)。"""
    if not rule.get("enabled", True):
        return False, ""
    text = _text_for(item, rule.get("scope", "body"))
    if not text:
        return False, ""

    kind = rule.get("type")

    if kind == "contains_any":
        for word in rule.get("words", []):
            if word in text:
                return True, f"（命中「{word}」）"

    elif kind == "count_at_least":
        threshold = int(rule.get("threshold", 1))
        hits = [w for w in rule.get("words", []) if w in text]
        if len(hits) >= threshold:
            return True, f"（命中 {len(hits)} 个：{'、'.join(hits[:3])}）"

    elif kind == "absent_all":
        if all(word not in text for word in rule.get("words", [])) and len(text) > 100:
            return True, ""

    elif kind == "regex_at_least":
        pattern = rule.get("pattern")
        if pattern:
            threshold = int(rule.get("threshold", 1))
            found = re.findall(pattern, text)
            if len(found) >= threshold:
                return True, f"（{len(found)} 处参数数字）"

    elif kind == "ends_with_any":
        stripped = text.strip().rstrip(" -—知乎")
        for word in rule.get("words", []):
            if stripped.endswith(word):
                return True, ""

    return False, ""


def judge(item: dict[str, Any], config: dict[str, Any], scene: str | None = None, debug: bool = False) -> dict[str, Any]:
    """对单条内容做判定，就地写入 is_ad_suspect / reasons。

    scene 用于跳过只对某个品类生效的规则（如扫地机的品牌罗列检测）。
    """
    score = 0
    reasons: list[str] = []
    hits: list[dict[str, Any]] = []

    for rule in config.get("rules", []):
        scenes = rule.get("scenes")
        if scenes and scene and scene not in scenes:
            continue
        matched, extra = _apply_rule(item, rule)
        if matched:
            weight = int(rule.get("weight", 1))
            score += weight
            if weight > 0:
                # 负权重只用于降分，不进 reasons —— 给用户看的理由必须是「为什么可疑」
                reason = rule.get("reason") or rule.get("name") or "命中规则"
                reasons.append(reason)
                hits.append({"id": rule.get("id"), "reason": reason, "extra": extra})
            else:
                hits.append({"id": rule.get("id"), "reason": "（减分项）", "extra": extra})

    threshold = int((config.get("decision") or {}).get("ad_threshold", 3))
    item["is_ad_suspect"] = score >= threshold
    item["reasons"] = reasons

    if debug:
        return {"score": score, "threshold": threshold, "hits": hits, "is_ad": item["is_ad_suspect"]}
    return item


def judge_all(items: list[dict[str, Any]], config: dict[str, Any] | None = None, scene: str | None = None) -> list[dict[str, Any]]:
    cfg = config or load_rules()
    for item in items:
        judge(item, cfg, scene=scene)
    return items


def evaluate(items: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, Any]:
    """在标注集上算召回与误杀。

    labels: {item_id: "可信" | "软文" | "过时" | "跑题"}
    """
    tp = fp = fn = tn = 0
    misses: list[dict[str, str]] = []
    false_alarms: list[dict[str, str]] = []

    for item in items:
        truth = labels.get(item.get("id", ""))
        if truth not in {"可信", "软文"}:
            continue  # 过时/跑题不参与这个维度的评估
        pred_ad = bool(item.get("is_ad_suspect"))
        if truth == "软文" and pred_ad:
            tp += 1
        elif truth == "软文" and not pred_ad:
            fn += 1
            misses.append({"id": item.get("id", ""), "title": item.get("title", "")})
        elif truth == "可信" and pred_ad:
            fp += 1
            false_alarms.append({"id": item.get("id", ""), "title": item.get("title", "")})
        else:
            tn += 1

    recall = tp / (tp + fn) if (tp + fn) else 0.0
    false_positive_rate = fp / (fp + tn) if (fp + tn) else 0.0
    return {
        "recall": round(recall, 3),
        "false_positive_rate": round(false_positive_rate, 3),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "missed_ads": misses,
        "false_alarms": false_alarms,
    }
