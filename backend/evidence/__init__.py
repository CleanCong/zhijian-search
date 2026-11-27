"""证据层：候选召回 → 关系判断 → 校验编排。"""

from evidence.candidate_retriever import Candidate, retrieve_candidates, score_pair, term_coverage
from evidence.evidence_matcher import ACCEPT_THRESHOLD, REVIEW_THRESHOLD, MatchReport, match_evidence
from evidence.relation_classifier import classify_relations

__all__ = [
    "retrieve_candidates", "score_pair", "term_coverage", "Candidate",
    "classify_relations",
    "match_evidence", "MatchReport", "ACCEPT_THRESHOLD", "REVIEW_THRESHOLD",
]
