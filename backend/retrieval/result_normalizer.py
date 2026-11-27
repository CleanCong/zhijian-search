"""把多来源的原始检索结果统一成 Evidence。

这是「统一数据契约」的落地环节。三条硬要求：

1. **字段不丢** —— 每条原始结果至少转出 source_type / retrieval_channel /
   title / author / url / normalized_url / content / published_at /
   independence_key / raw_metadata
2. **单条失败不拖垮整批** —— 一条数据有问题，跳过它并记进 errors，
   其余照常转换。检索任务不能因为一条脏数据整体失败。
3. **同源记录只算一次** —— 按 independence_key 去重，保留正文更长的那条
   （信息更完整），并记录被合并的数量。

## 关于 content_type 的刻意区分

question answers 的 Summary 只有七十来字，知乎直答是 AI 生成的综合答案，
普通搜索结果是正文节选。三者都转成 Evidence，但 content_type 各不相同。

**下游的 Claim 抽取器必须据此区分材料质量**，不能因为都是 Evidence 就平等对待。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from models.evidence import (
    Evidence,
    from_global_search,
    from_question_answer,
    from_zhihu_search,
)


@dataclass
class NormalizeReport:
    """一次转换的体检报告。用于 Trace 与排查。"""

    total_raw: int = 0
    converted: int = 0
    skipped: int = 0
    merged: int = 0                 # 因同源被合并掉的条数
    errors: list[str] = field(default_factory=list)

    by_channel: dict[str, int] = field(default_factory=dict)
    by_source_type: dict[str, int] = field(default_factory=dict)
    by_content_type: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_raw": self.total_raw,
            "converted": self.converted,
            "skipped": self.skipped,
            "merged": self.merged,
            "unique": len(self.by_channel) and self.converted,
            "errors": self.errors,
            "by_channel": self.by_channel,
            "by_source_type": self.by_source_type,
            "by_content_type": self.by_content_type,
        }

    def summary_line(self) -> str:
        return (
            f"原始 {self.total_raw} 条 → 转换 {self.converted} 条 "
            f"（同源合并 {self.merged}，跳过 {self.skipped}）"
        )


def _safe_convert(fn, raw: dict, idx: int, channel: str, report: NormalizeReport) -> Evidence | None:
    """单条转换。任何异常都吞掉并记录，不影响整批。"""
    try:
        ev = fn(raw, idx)
    except Exception as exc:                       # noqa: BLE001 — 这里就要兜住一切
        report.errors.append(f"{channel}#{idx}: {type(exc).__name__}: {exc}")
        report.skipped += 1
        return None
    if not ev.content.strip():
        report.errors.append(f"{channel}#{idx}: 正文为空，跳过")
        report.skipped += 1
        return None
    return ev


def normalize_batch(
    zhihu_items: list[dict] | None = None,
    global_items: list[dict] | None = None,
    qa_items: list[dict] | None = None,
    max_items: int | None = None,
) -> tuple[list[Evidence], NormalizeReport]:
    """把三个渠道的原始结果统一成 Evidence 列表。

    Args:
        zhihu_items:  search zhihu 返回的 Items
        global_items: search global 返回的 Items
        qa_items:     question answers 返回的 Items
        max_items:    截断上限，防止一次吃进过多（额度与内存保护）

    Returns:
        (去重后的 Evidence 列表, 报告)
    """
    report = NormalizeReport()
    pool: list[Evidence] = []
    channel_map: Counter = Counter()

    sources = [
        ("zhihu_search", zhihu_items, from_zhihu_search),
        ("global_search", global_items, from_global_search),
        ("question_answers", qa_items, from_question_answer),
    ]

    for channel, items, fn in sources:
        for idx, raw in enumerate(items or []):
            report.total_raw += 1
            if not isinstance(raw, dict):
                report.errors.append(f"{channel}#{idx}: 不是对象，跳过")
                report.skipped += 1
                continue
            ev = _safe_convert(fn, raw, idx, channel, report)
            if ev is not None:
                pool.append(ev)
                channel_map[ev.retrieval_channel] += 1

    # ---- 按独立来源去重，保留正文更长的那条 ----
    best: dict[str, Evidence] = {}
    for ev in pool:
        key = ev.independence_key
        if key not in best:
            best[key] = ev
        else:
            report.merged += 1
            # 正文更长的信息更完整；等长时保留先到的
            if len(ev.content) > len(best[key].content):
                best[key] = ev

    unique = list(best.values())
    if max_items is not None and len(unique) > max_items:
        unique = unique[:max_items]

    report.converted = len(unique)
    report.by_channel = dict(channel_map)
    report.by_source_type = dict(Counter(e.source_type for e in unique))
    report.by_content_type = dict(Counter(e.content_type for e in unique))
    return unique, report


def to_json_payload(evidences: list[Evidence], report: NormalizeReport | None = None) -> dict:
    """导出为可审查的 JSON。给前端调试面板或人工核对用。"""
    payload = {
        "count": len(evidences),
        "items": [e.model_dump(mode="json") for e in evidences],
    }
    if report is not None:
        payload["report"] = report.to_dict()
    return payload
