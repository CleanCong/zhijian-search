"""证据匹配层测试。

三部分：
- 候选召回：语义相关但词面不重叠的材料不能漏
- 校验：模型给的脏数据要拦住
- 状态：置信度门槛与同源冲突
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Claim, ClaimEvidence, Evidence  # noqa: E402
from models.claim import evaluate_claim_evidence  # noqa: E402
from evidence import candidate_retriever, evidence_matcher  # noqa: E402
from evidence.candidate_retriever import retrieve_candidates, score_pair  # noqa: E402

CJK = "机翼上下表面的压力差能够产生升力"


def ev(eid="e1", content="", ctype="answer_excerpt", key="k1", source="zhihu"):
    return Evidence(evidence_id=eid, source_type=source, content=content or "内容",
                    content_type=ctype, independence_key=key)


# ---------------- 候选召回 ----------------

def test_semantic_but_lexically_different_still_recalled():
    """评审给的例子：几乎没有相同短语，但明显相关。

    Claim：机翼上下表面的压力差能够产生升力
    Evidence：机翼上方气流速度较快，静压较低，机翼受到向上的压力合力
    """
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(content="机翼上方气流速度较快，静压较低，机翼受到向上的压力合力。")
    score, reasons = score_pair(claim, evid)
    assert score > 0.12, f"应进入候选，实际 {score:.3f} {reasons}"

    cands = retrieve_candidates([claim], [evid], top_n=5)
    assert len(cands) == 1
    assert cands[0].evidence_id == evid.evidence_id


def test_zero_overlap_kept_as_low_score_candidate():
    """词面完全不重合也保留低分候选，交给模型判一次。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(content="鸟类迁徙依靠地磁场与太阳方位定向")
    score, reasons = score_pair(claim, evid)
    assert 0.12 <= score < 0.3
    assert any("零重合" in r for r in reasons)


def test_top_n_limits_candidates():
    claim = Claim(claim_id="c1", claim_text=CJK)
    evidences = [ev(eid=f"e{i}", content=f"机翼压力差产生升力，第{i}条材料补充说明。", key=f"k{i}")
                 for i in range(12)]
    cands = retrieve_candidates([claim], evidences, top_n=5)
    assert len(cands) == 5


def test_short_snippet_ranks_lower_than_excerpt():
    """同样相关时，几十字的摘要不该压过千字正文。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    long_ev = ev(eid="long", content="机翼压力差产生升力。" * 60, ctype="answer_excerpt")
    short_ev = ev(eid="short", content="机翼压力差产生升力。", ctype="search_snippet")
    s_long, _ = score_pair(claim, long_ev)
    s_short, _ = score_pair(claim, short_ev)
    assert s_long > s_short


def test_direct_answer_excluded_from_candidates():
    """AI 综合答案不参与独立证据判断，直接排除出候选。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    ai = ev(eid="ai", content="机翼压力差产生升力。", ctype="direct_answer", source="zhihu_direct_answer")
    cands = retrieve_candidates([claim], [ai])
    assert cands == []


# ---------------- 模型输出校验 ----------------

def _patch(monkeypatch, payload, errors=None):
    def fake(pairs):
        return payload, (errors or [])
    monkeypatch.setattr(evidence_matcher, "classify_relations", fake)


def test_unknown_evidence_id_rejected(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [{
        "claim_id": "c1", "evidence_id": "不存在的证据", "relation": "supports",
        "confidence": 0.9, "rationale": "x", "evidence_quote": None,
    }])
    rels, report = evidence_matcher.match_evidence([claim], [evid], allow_llm=True)
    assert all(r.evidence_id != "不存在的证据" for r in rels)
    assert any("不存在" in e for e in report.errors)


def test_invalid_relation_rejected(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [{
        "claim_id": "c1", "evidence_id": "e1", "relation": "agrees",
        "confidence": 0.9, "rationale": "x", "evidence_quote": None,
    }])
    rels, report = evidence_matcher.match_evidence([claim], [evid])
    assert rels == [] or all(r.relation in {"supports", "refutes", "supplements", "mentions"} for r in rels)
    assert any("非法 relation" in e for e in report.errors)


def test_model_failure_marks_pending_not_mentions(monkeypatch):
    """批量失败不能伪装成 mentions，必须留 pending。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [], ["批次 1 判断失败（1 对），保留待处理"])
    rels, report = evidence_matcher.match_evidence([claim], [evid])
    assert len(rels) == 1
    assert rels[0].review_status == "pending"
    assert rels[0].confidence is None
    assert report.pending == 1
    assert report.accepted == 0


def test_batch_failure_does_not_crash(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evidences = [ev(eid=f"e{i}", content="机翼压力差产生升力。", key=f"k{i}") for i in range(3)]
    _patch(monkeypatch, [], ["批次全部失败"])
    rels, report = evidence_matcher.match_evidence([claim], evidences)
    assert report.pending == len(rels) > 0
    assert report.errors


# ---------------- 置信度门槛 ----------------

def test_high_confidence_accepted(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [{
        "claim_id": "c1", "evidence_id": "e1", "relation": "supports",
        "confidence": 0.92, "rationale": "明确支持", "evidence_quote": "压力合力向上",
    }])
    rels, report = evidence_matcher.match_evidence([claim], [evid])
    assert rels[0].review_status == "accepted"
    assert rels[0].evidence_quote == "压力合力向上"
    assert report.accepted == 1


def test_medium_confidence_needs_review(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [{
        "claim_id": "c1", "evidence_id": "e1", "relation": "supports",
        "confidence": 0.68, "rationale": "大致支持", "evidence_quote": None,
    }])
    rels, _ = evidence_matcher.match_evidence([claim], [evid])
    assert rels[0].review_status == "needs_review"


def test_low_confidence_downgraded_to_mentions(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    _patch(monkeypatch, [{
        "claim_id": "c1", "evidence_id": "e1", "relation": "refutes",
        "confidence": 0.35, "rationale": "可能是反驳", "evidence_quote": None,
    }])
    rels, _ = evidence_matcher.match_evidence([claim], [evid])
    assert rels[0].relation == "mentions"          # 降级
    assert rels[0].review_status == "needs_review"


def test_low_confidence_refute_does_not_trigger_disputed(monkeypatch):
    """一次低置信误判，不能把整个知识包标成争议。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    e1 = ev(eid="e1", content="机翼压力差产生升力。", key="k1")
    e2 = ev(eid="e2", content="机翼压力差产生升力，确实如此。", key="k2")

    _patch(monkeypatch, [
        {"claim_id": "c1", "evidence_id": "e1", "relation": "supports",
         "confidence": 0.95, "rationale": "明确支持", "evidence_quote": "压力差"},
        {"claim_id": "c1", "evidence_id": "e2", "relation": "refutes",
         "confidence": 0.42, "rationale": "不确定", "evidence_quote": None},
    ])
    rels, _ = evidence_matcher.match_evidence([claim], [e1, e2])
    result = evaluate_claim_evidence(claim, rels, {"e1": e1, "e2": e2})
    assert result["status"] == "partially_supported"    # 不是 disputed
    assert result["independent_refute_count"] == 0


def test_supplements_and_mentions_not_counted_as_support(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    e1 = ev(eid="e1", content="机翼压力差产生升力。", key="k1")
    e2 = ev(eid="e2", content="飞机机翼采用特殊翼型设计。", key="k2")

    _patch(monkeypatch, [
        {"claim_id": "c1", "evidence_id": "e1", "relation": "supplements",
         "confidence": 0.9, "rationale": "补充背景", "evidence_quote": "翼型"},
        {"claim_id": "c1", "evidence_id": "e2", "relation": "mentions",
         "confidence": 0.85, "rationale": "仅提及", "evidence_quote": None},
    ])
    rels, _ = evidence_matcher.match_evidence([claim], [e1, e2])
    result = evaluate_claim_evidence(claim, rels, {"e1": e1, "e2": e2})
    assert result["status"] == "insufficient"
    assert result["independent_support_count"] == 0


def test_same_source_conflict_marked_needs_review(monkeypatch):
    claim = Claim(claim_id="c1", claim_text=CJK)
    e1 = ev(eid="e1", content="机翼压力差产生升力。", key="same")
    e2 = ev(eid="e2", content="压力差不足以解释升力。", key="same")

    _patch(monkeypatch, [
        {"claim_id": "c1", "evidence_id": "e1", "relation": "supports",
         "confidence": 0.9, "rationale": "支持", "evidence_quote": "压力差"},
        {"claim_id": "c1", "evidence_id": "e2", "relation": "refutes",
         "confidence": 0.9, "rationale": "反驳", "evidence_quote": "不足以"},
    ])
    rels, report = evidence_matcher.match_evidence([claim], [e1, e2])
    assert all(r.review_status == "needs_review" for r in rels)
    assert any("内部" in e for e in report.errors)


def test_offline_mode_leaves_pending():
    """allow_llm=False 时只召回不判断，全部 pending。"""
    claim = Claim(claim_id="c1", claim_text=CJK)
    evid = ev(eid="e1", content="机翼压力差产生升力。")
    rels, report = evidence_matcher.match_evidence([claim], [evid], allow_llm=False)
    assert rels and all(r.review_status == "pending" for r in rels)
    assert report.accepted == 0
