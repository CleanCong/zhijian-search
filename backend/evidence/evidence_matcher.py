"""编排层：候选召回 → 模型判断 → 校验 → ClaimEvidence。

三件事必须在这里做对：

1. **校验模型输出**。ID 不存在、relation 非法、confidence 越界，一律拦掉，
   不让脏值流进知识包。
2. **置信度门槛**。高置信才采纳；中等的保留但标待复核；低的降级为 mentions。
   低置信的 refutes 绝不能直接把 Claim 标成争议。
3. **失败不伪装**。模型调用失败的那些对，留 pending 并记录原因，
   不要写成 mentions——"没判断"和"判断为仅提及"是两回事。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models import Claim, ClaimEvidence, Evidence
from models.claim import RELATION_LABELS, Relation
from evidence.candidate_retriever import retrieve_candidates
from evidence.relation_classifier import classify_relations

# 采纳门槛。这两个数是工程起始值，需要用人工标注样本校准，
# 不能当成模型置信度已经过统计校准。
ACCEPT_THRESHOLD = 0.80
REVIEW_THRESHOLD = 0.60

VALID_RELATIONS = set(RELATION_LABELS.keys())


@dataclass
class MatchReport:
    """匹配过程的体检报告，进 Trace。"""

    claim_count: int = 0
    evidence_count: int = 0
    candidate_count: int = 0
    judged_count: int = 0
    accepted: int = 0
    needs_review: int = 0
    rejected: int = 0
    pending: int = 0
    relation_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claims": self.claim_count,
            "evidences": self.evidence_count,
            "candidates": self.candidate_count,
            "judged": self.judged_count,
            "accepted": self.accepted,
            "needs_review": self.needs_review,
            "rejected": self.rejected,
            "pending": self.pending,
            "relations": self.relation_counts,
            "errors": self.errors,
        }


def _decide_status(relation: str, confidence: float) -> tuple[str, str | None]:
    """按置信度决定采信状态。返回 (review_status, 降级后的 relation 或 None)。"""
    if confidence >= ACCEPT_THRESHOLD:
        return "accepted", None
    if confidence >= REVIEW_THRESHOLD:
        return "needs_review", None
    # 低置信一律降级为 mentions：不足以支撑任何明确的关系判断
    return "needs_review", "mentions"


def match_evidence(
    claims: list[Claim],
    evidences: list[Evidence],
    top_n: int = 8,
    allow_llm: bool = True,
) -> tuple[list[ClaimEvidence], MatchReport]:
    """为一组 Claim 建立与 Evidence 的关系。

    Args:
        allow_llm: False 时只做候选召回，全部关系留 pending，
                   用于离线测试与故障降级。
    """
    report = MatchReport(claim_count=len(claims), evidence_count=len(evidences))

    candidates = retrieve_candidates(claims, evidences, top_n=top_n)
    report.candidate_count = len(candidates)
    if not candidates:
        return [], report

    by_claim = {c.claim_id: c for c in claims}
    by_evidence = {e.evidence_id: e for e in evidences}

    relations: list[ClaimEvidence] = []

    if not allow_llm:
        for cand in candidates:
            relations.append(ClaimEvidence(
                claim_id=cand.claim_id, evidence_id=cand.evidence_id,
                relation="mentions", confidence=None,
                rationale="未启用模型判断", review_status="pending",
            ))
        report.pending = len(relations)
        return relations, report

    pairs = [(by_claim[c.claim_id], by_evidence[c.evidence_id])
             for c in candidates
             if c.claim_id in by_claim and c.evidence_id in by_evidence]

    judged, errors = classify_relations(pairs)
    report.errors.extend(errors)

    seen: set[tuple[str, str]] = set()

    for item in judged:
        cid, eid = item["claim_id"], item["evidence_id"]
        if cid not in by_claim or eid not in by_evidence:
            report.errors.append(f"模型返回了不存在的引用：{cid}/{eid}")
            continue
        relation = item["relation"]
        if relation not in VALID_RELATIONS:
            report.errors.append(f"非法 relation「{relation}」（{cid}/{eid}），已跳过")
            continue
        conf = item["confidence"]
        if not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
            report.errors.append(f"confidence 越界（{cid}/{eid}），已跳过")
            continue

        status, downgraded = _decide_status(relation, conf)
        final_relation: Relation = (downgraded or relation)  # type: ignore[assignment]
        rationale = item.get("rationale") or ""
        if downgraded:
            rationale = f"{rationale}（置信度 {conf:.2f} 不足，降级为仅提及）".strip()

        relations.append(ClaimEvidence(
            claim_id=cid,
            evidence_id=eid,
            relation=final_relation,
            confidence=conf,
            rationale=rationale,
            evidence_quote=item.get("evidence_quote"),
            review_status=status,  # type: ignore[arg-type]
        ))
        seen.add((cid, eid))
        report.judged_count += 1

    # 模型没覆盖到的候选，留 pending（通常是批次失败）
    for cand in candidates:
        key = (cand.claim_id, cand.evidence_id)
        if key in seen:
            continue
        if any(f"{cand.claim_id}/{cand.evidence_id}" in e for e in report.errors):
            continue
        relations.append(ClaimEvidence(
            claim_id=cand.claim_id, evidence_id=cand.evidence_id,
            relation="mentions", confidence=None,
            rationale="模型未返回该对的判断", review_status="pending",
        ))

    # 同源内部冲突：同一 independence_key 下同时出现 supports 与 refutes
    by_evidence_key: dict[str, set[str]] = {}
    for r in relations:
        if r.review_status != "accepted":
            continue
        ev = by_evidence.get(r.evidence_id)
        if ev is None:
            continue
        by_evidence_key.setdefault(f"{r.claim_id}|{ev.independence_key}", set()).add(r.relation)

    conflicted = {k.split("|", 1)[1] for k, rels in by_evidence_key.items()
                  if "supports" in rels and "refutes" in rels}
    if conflicted:
        report.errors.append(f"检测到 {len(conflicted)} 个来源内部同时存在支持与反驳，已标记待复核")
        for r in relations:
            ev = by_evidence.get(r.evidence_id)
            if ev and ev.independence_key in conflicted and r.review_status == "accepted":
                r.review_status = "needs_review"
                r.rationale = f"{r.rationale}（来源内部矛盾）".strip()

    # 统计
    for r in relations:
        report.relation_counts[r.relation] = report.relation_counts.get(r.relation, 0) + 1
        if r.review_status == "accepted":
            report.accepted += 1
        elif r.review_status == "needs_review":
            report.needs_review += 1
        elif r.review_status == "rejected":
            report.rejected += 1
        else:
            report.pending += 1

    return relations, report
