"""候选召回：决定哪些 Evidence 值得交给模型判断关系。

这不是证据判定，只是「值不值得花模型调用」的粗筛。

## 为什么不能只看关键词重叠

反例（评审给的）：

    Claim：机翼上下表面的压力差能够产生升力
    Evidence：机翼上方气流速度较快，静压较低，机翼受到向上的压力合力

两者几乎没有相同的连续短语，但明显是支持关系。
只用"关键词重叠"会把它筛掉。

所以这里用多个低成本信号加权：

    1. 词项覆盖  —— Claim 里的词有多少在 Evidence 中出现
    2. 内容完整度 —— answer_excerpt 优先于几十字的 search_snippet
    3. 来源多样性 —— 不同 independence_key 适当加权，避免候选全来自同一篇

## 这里不做的事

不判断支持还是反驳。那是 relation_classifier 的事。
打分的唯一目的是排序与截断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import Claim, Evidence

# 中文按 2-gram 切，英文/数字按词切
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN = re.compile(r"[A-Za-z0-9]{2,}")

# 高频虚词，参与匹配会制造假相似
STOP_GRAMS = {
    "的是", "了", "这个", "那个", "我们", "他们", "可以", "什么", "怎么",
    "如何", "为什", "么能", "能够", "一个", "没有", "就是", "不是", "还是",
    "其实", "因为", "所以", "但是", "如果", "这样", "那样", "一些", "很多",
}

CONTENT_TYPE_WEIGHT = {
    "answer_excerpt": 1.0,     # 千字级正文，信息最全
    "web_snippet": 0.9,
    "summary": 0.7,
    "search_snippet": 0.4,     # 几十字，只够发现线索
    "direct_answer": 0.3,      # AI 生成，且不计入独立来源
}


@dataclass
class Candidate:
    """一条待判断的 Claim-Evidence 配对。"""

    claim_id: str
    evidence_id: str
    score: float
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "evidence_id": self.evidence_id,
            "score": round(self.score, 4),
            "reasons": self.reasons,
        }


def _tokens(text: str) -> set[str]:
    """切出用于匹配的词项：汉字 2-gram + 英文数字词。"""
    text = (text or "").lower()
    grams: set[str] = set()
    for seg in _CJK.findall(text):
        if len(seg) == 1:
            continue
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in STOP_GRAMS:
                grams.add(g)
    for w in _TOKEN.findall(text):
        grams.add(w)
    return grams


def term_coverage(claim_tokens: set[str], evidence_tokens: set[str]) -> float:
    """Claim 的词项有多大比例出现在 Evidence 中。

    用覆盖率而不是 Jaccard：Evidence 通常远长于 Claim，
    Jaccard 会被 Evidence 的大量无关词稀释，导致正确候选被压下去。
    """
    if not claim_tokens:
        return 0.0
    hit = claim_tokens & evidence_tokens
    return len(hit) / len(claim_tokens)


def score_pair(claim: Claim, evidence: Evidence, claim_tokens: set[str] | None = None) -> tuple[float, list[str]]:
    """给一对 Claim-Evidence 打分，返回 (分数, 理由列表)。"""
    ct = claim_tokens if claim_tokens is not None else _tokens(claim.claim_text)
    et = _tokens(evidence.content)
    if evidence.title:
        et |= _tokens(evidence.title)

    cov = term_coverage(ct, et)
    weight = CONTENT_TYPE_WEIGHT.get(evidence.content_type, 0.5)
    length_bonus = min(1.0, len(evidence.content) / 400.0)

    score = 0.62 * cov + 0.23 * weight + 0.15 * length_bonus

    reasons = [f"词覆盖 {cov:.2f}", f"{evidence.content_type} 权重 {weight}"]
    if cov == 0.0:
        # 词面完全不重合也保留极低分候选：语义相关但换过说法的材料
        # 恰恰是最容易被漏掉的那类，交给模型判一次比直接丢更安全。
        score = max(score, 0.16)
        reasons.append("词面零重合，保留为低分候选")
    return score, reasons


def retrieve_candidates(
    claims: list[Claim],
    evidences: list[Evidence],
    top_n: int = 8,
    min_score: float = 0.12,
) -> list[Candidate]:
    """为每个 Claim 选 Top-N 条候选证据。

    Args:
        top_n: 每个 Claim 保留的候选数。工程起始值，不是最优值。
               后续要用真实样本测召回率，重点看反驳证据有没有被筛掉。
    """
    out: list[Candidate] = []
    for claim in claims:
        claim_tokens = _tokens(claim.claim_text)
        scored: list[tuple[float, list[str], Evidence]] = []

        for ev in evidences:
            if ev.content_type == "direct_answer":
                # AI 生成的综合答案不参与独立证据判断，直接排除出候选
                continue
            s, reasons = score_pair(claim, ev, claim_tokens)
            if s >= min_score:
                scored.append((s, reasons, ev))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 多样性：同分时优先不同来源，避免候选全来自同一篇的多段
        picked: list[Candidate] = []
        seen_keys: set[str] = set()
        for s, reasons, ev in scored:
            if len(picked) >= top_n:
                break
            key = ev.independence_key
            adjusted = s
            if key in seen_keys:
                adjusted *= 0.85
                reasons = [*reasons, "同源降权"]
            seen_keys.add(key)
            picked.append(Candidate(claim_id=claim.claim_id, evidence_id=ev.evidence_id,
                                    score=adjusted, reasons=reasons))

        picked.sort(key=lambda c: c.score, reverse=True)
        out.extend(picked)

    return out
