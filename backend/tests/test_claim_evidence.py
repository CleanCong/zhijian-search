"""Claim ↔ Evidence 关系与状态计算的测试。

这是知识引擎最核心的一层：判断「多个来源究竟如何对待一个主张」。

六种基本情形（对应评审要求）：
    纯支持 / 纯反驳 / 支持与反驳并存 / 只有补充 / 只有提及 / 同源多条不同关系
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Claim, ClaimEvidence, Evidence, KnowledgePackage  # noqa: E402
from models.claim import evaluate_claim_evidence  # noqa: E402


def ev(eid: str, key: str, source_type: str = "zhihu") -> Evidence:
    return Evidence(evidence_id=eid, source_type=source_type, content=f"内容{eid}",
                    content_type="answer_excerpt", independence_key=key)


def evmap(*evs) -> dict:
    return {e.evidence_id: e for e in evs}


def rel(eid: str, relation: str, claim_id: str = "c") -> ClaimEvidence:
    return ClaimEvidence(claim_id=claim_id, evidence_id=eid, relation=relation)


def evaluate(relations, *evs):
    c = Claim(claim_id="c", claim_text="某说法成立")
    return evaluate_claim_evidence(c, relations, evmap(*evs))


# ---------------- 六种基本情形 ----------------

def test_pure_support():
    r = evaluate([rel("e1", "supports"), rel("e2", "supports")], ev("e1", "k1"), ev("e2", "k2"))
    assert r["status"] == "supported"
    assert r["independent_support_count"] == 2
    assert r["independent_refute_count"] == 0


def test_single_support_is_partial():
    r = evaluate([rel("e1", "supports")], ev("e1", "k1"))
    assert r["status"] == "partially_supported"
    assert r["independent_support_count"] == 1


def test_pure_refute_is_disputed():
    """只有反驳没有支持 —— 不能算 insufficient（那会让人以为没材料）。"""
    r = evaluate([rel("e1", "refutes")], ev("e1", "k1"))
    assert r["status"] == "disputed"
    assert r["independent_refute_count"] == 1
    assert r["independent_support_count"] == 0


def test_support_and_refute_coexist():
    r = evaluate([rel("e1", "supports"), rel("e2", "refutes")], ev("e1", "k1"), ev("e2", "k2"))
    assert r["status"] == "disputed"
    assert (r["independent_support_count"], r["independent_refute_count"]) == (1, 1)


def test_only_supplements_is_insufficient():
    """补充背景不等于支持主张。"""
    r = evaluate([rel("e1", "supplements"), rel("e2", "supplements")], ev("e1", "k1"), ev("e2", "k2"))
    assert r["status"] == "insufficient"
    assert r["independent_support_count"] == 0
    assert r["supplement_count"] == 2


def test_only_mentions_is_insufficient():
    r = evaluate([rel("e1", "mentions")], ev("e1", "k1"))
    assert r["status"] == "insufficient"
    assert r["mention_count"] == 1


def test_same_source_conflicting_relations_excluded():
    """同一来源下一条支持一条反驳 —— 不能虚增成两份独立来源。"""
    r = evaluate([rel("e1", "supports"), rel("e1b", "refutes")],
                 ev("e1", "k1"), ev("e1b", "k1"))
    assert r["internal_conflict_count"] == 1
    assert r["independent_support_count"] == 0
    assert r["independent_refute_count"] == 0
    assert r["status"] == "insufficient"
    assert any("内部同时存在" in w for w in r["warnings"])


# ---------------- 反证优先 ----------------

def test_one_refute_beats_many_supports():
    """一条有依据的反证，足以推翻「已支持」。

    这是刻意的：如果三条支持就能压过一条反对，
    那「存在争议」永远不会被如实呈现。
    """
    r = evaluate(
        [rel("s1", "supports"), rel("s2", "supports"), rel("s3", "supports"), rel("r1", "refutes")],
        ev("s1", "k1"), ev("s2", "k2"), ev("s3", "k3"), ev("r1", "k4"),
    )
    assert r["status"] == "disputed"
    assert r["independent_support_count"] == 3
    assert r["independent_refute_count"] == 1


# ---------------- 独立性与来源类型 ----------------

def test_direct_answer_not_counted():
    """知乎直答不算独立来源，哪怕关系是 supports。"""
    r = evaluate([rel("ai", "supports"), rel("q1", "supports")],
                 ev("ai", "ai-key", source_type="zhihu_direct_answer"), ev("q1", "k1"))
    assert r["independent_support_count"] == 1
    assert r["status"] == "partially_supported"


def test_duplicate_source_supports_counted_once():
    r = evaluate([rel("e1", "supports"), rel("e1b", "supports")],
                 ev("e1", "same"), ev("e1b", "same"))
    assert r["independent_support_count"] == 1


def test_dangling_reference_warned():
    r = evaluate([rel("e1", "supports"), rel("不存在", "supports")], ev("e1", "k1"))
    assert any("不存在" in w for w in r["warnings"])
    assert r["independent_support_count"] == 1


# ---------------- 模型校验 ----------------

def test_relation_enum_enforced():
    with pytest.raises(ValueError):
        ClaimEvidence(claim_id="c", evidence_id="e", relation="agrees")


def test_confidence_range():
    with pytest.raises(ValueError):
        ClaimEvidence(claim_id="c", evidence_id="e", confidence=1.5)
    assert ClaimEvidence(claim_id="c", evidence_id="e", confidence=0.8).confidence == 0.8


# ---------------- 与知识包集成 ----------------

def test_finalize_writes_counts_and_status():
    e1, e2, e3 = ev("e1", "k1"), ev("e2", "k2"), ev("e3", "k3")
    c = Claim(claim_id="c", claim_text="某说法")
    ce = [rel("e1", "supports"), rel("e2", "supports"), rel("e3", "supplements")]
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c],
                           evidence=[e1, e2, e3], claim_evidences=ce).finalize()

    assert c.evidence_status == "supported"
    assert c.independent_source_count == 2
    assert c.refuting_source_count == 0
    assert pkg.internal_conflicts == 0


def test_refute_changes_whole_package_on_refinalize():
    e1, e2 = ev("e1", "k1"), ev("e2", "k2")
    c = Claim(claim_id="c", claim_text="某说法")
    ce = [rel("e1", "supports"), rel("e2", "supports")]
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c],
                           evidence=[e1, e2], claim_evidences=ce).finalize()
    assert c.evidence_status == "supported"

    # 注意：Pydantic 构造时复制列表（元素对象仍共享），
    # 所以往原列表 append 不会影响 pkg，必须往 pkg 自己的列表里加
    pkg.claim_evidences.append(rel("e3", "refutes"))
    pkg.evidence.append(ev("e3", "k3"))
    pkg.finalize()
    assert c.evidence_status == "disputed"
    assert c.refuting_source_count == 1


def test_legacy_evidence_ids_still_work():
    """老代码只给 evidence_ids 时，视为全部 supports，保持向后兼容。"""
    e1, e2 = ev("e1", "k1"), ev("e2", "k2")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1", "e2"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1, e2]).finalize()
    assert c.evidence_status == "supported"
    assert c.independent_source_count == 2
