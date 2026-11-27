"""Claim：一条原子主张。

这是新旧系统最大的区别：旧系统把「一整篇回答」放进某个观点桶，
新系统把回答拆成一条条断言，每条独立挂证据。

## 为什么必须拆开

一篇回答常常同时包含好几个观点，例如：

    某型号避障能力较强（正面）
    但耗材维护成本较高（负面）
    且对低矮家具的通过能力有限（限制条件）

旧系统只能把整篇归进一个桶（多半是「坑」），另外两个观点就消失了。
拆成三条 Claim 后，它们各自归位、各自找证据。

## 两个置信度，不能混为一谈

model_confidence   模型自评：它觉得自己这句话有多大把握
                   —— 只说明模型的自信程度，不说明事实可靠性

evidence_status    代码计算：实际有多少独立来源支撑
                   —— 这才是能用于「已验证」判断的依据

展示层可以综合两者，但**绝不能因为模型说 high 就把缺证据的 Claim 标成已验证**。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from models.evidence import Evidence

ClaimType = Literal["fact", "mechanism", "experience", "opinion", "recommendation"]
ModelConfidence = Literal["high", "medium", "low"]
EvidenceStatus = Literal["supported", "partially_supported", "disputed", "insufficient"]

CLAIM_TYPE_LABELS = {
    "fact": "事实",
    "mechanism": "原理",
    "experience": "经验",
    "opinion": "观点",
    "recommendation": "建议",
}

STATUS_LABELS = {
    "supported": "多来源印证",
    "partially_supported": "单一来源",
    "disputed": "存在不同说法",
    "insufficient": "证据不足",
}

# 这些来源不算独立证据
_NON_INDEPENDENT_SOURCES = {"zhihu_direct_answer"}


class Claim(BaseModel):
    """一条可被证据支持或反驳的原子主张。"""

    claim_id: str
    claim_text: str
    claim_type: ClaimType = "fact"
    explanation: str = ""

    evidence_ids: list[str] = Field(default_factory=list)

    # 模型自评，仅作参考
    model_confidence: ModelConfidence = "medium"

    # 代码计算，用于展示与判断
    evidence_status: EvidenceStatus = "insufficient"
    independent_source_count: int = 0     # 独立支持来源数
    refuting_source_count: int = 0        # 独立反驳来源数
    controversy: bool = False

    # 用于聚类的标准化文本（去标点、统一空白）
    normalized_text: str = ""

    @field_validator("claim_text")
    @classmethod
    def claim_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("claim_text 不能为空")
        return v

    @field_validator("claim_text")
    @classmethod
    def claim_length_reasonable(cls, v: str) -> str:
        # 过长的「主张」多半是段落而不是断言，抽取阶段就应该切分
        if len(v) > 120:
            raise ValueError(f"claim_text 过长（{len(v)} 字），应从长文中切分出单条断言")
        return v

    def model_post_init(self, __context) -> None:
        if not self.normalized_text:
            object.__setattr__(self, "normalized_text", normalize_claim_text(self.claim_text))

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.evidence_status, "")

    @property
    def is_verified(self) -> bool:
        """只有多来源支持才算「已验证」。模型自信不算。"""
        return self.evidence_status == "supported"

    @property
    def display_confidence(self) -> str:
        """展示用置信度：以证据状态为准，模型自信只在证据充分时起点缀作用。"""
        if self.evidence_status == "insufficient":
            return "low"
        if self.evidence_status == "disputed":
            return "medium"
        if self.evidence_status == "supported":
            return "high"
        # partially_supported：最多 medium，不会被模型自评抬高
        return "medium"


def normalize_claim_text(text: str) -> str:
    """标准化，用于相似度比较与去重。

    只做保守处理：去空白、去常见标点、小写化。
    不做同义词替换——那需要词典，首期不引入。
    """
    t = re.sub(r"\s+", "", text or "")
    t = re.sub(r"[，。、；：！？,.;:!?\"'“”‘’（）()【】\[\]…—\-]", "", t)
    return t.lower()


def compute_evidence_status(
    claim: Claim,
    evidence_list: list[Evidence],
) -> tuple[EvidenceStatus, int]:
    """按实际证据关系计算状态。返回 (状态, 独立来源数)。

    规则（对应设计文档 7.1）：

    | 情况                          | 状态                 |
    |-------------------------------|----------------------|
    | 独立来源 ≥ 2 且无冲突          | supported            |
    | 只有 1 个独立来源              | partially_supported  |
    | 存在相互矛盾（claim.controversy）| disputed           |
    | 无来源 / 只有 AI 生成          | insufficient         |

    独立性按 evidence.independence_key 去重，
    且 AI 生成内容（知乎直答）不计入独立来源。
    """
    by_id = {e.evidence_id: e for e in evidence_list}
    related = [by_id[i] for i in claim.evidence_ids if i in by_id]

    # AI 生成的不算独立证据
    real = [e for e in related if e.source_type not in _NON_INDEPENDENT_SOURCES]

    # 按独立性分组去重
    seen_keys = {e.independence_key for e in real if e.independence_key}
    independent_count = len(seen_keys)

    if claim.controversy and independent_count >= 1:
        return "disputed", independent_count
    if independent_count >= 2:
        return "supported", independent_count
    if independent_count == 1:
        return "partially_supported", independent_count
    return "insufficient", 0


def apply_evidence_status(claim: Claim, evidence_list: list[Evidence]) -> Claim:
    """就地更新 Claim 的证据状态，返回同对象便于链式调用。"""
    status, count = compute_evidence_status(claim, evidence_list)
    claim.evidence_status = status
    claim.independent_source_count = count
    return claim


# ============================================================
# Claim ↔ Evidence 关系（方案 B）
#
# 为什么需要这一层：
#
#   旧的 compute_evidence_status 只看「独立来源数 ≥ 2」，
#   于是下面四种情况会被判成同一个结论 —— 这是错的：
#
#     两条来源，一条支持一条反驳   → 实际是争议，却判 supported
#     两条来源，都只是「提到」      → 实际没有支持，却判 supported
#     一条支持，一条与本 Claim 无关 → 独立数虚高
#     两条支持，但同属一份原始材料  → 独立性虚高（已由 independence_key 拦住）
#
#   来源「独立」和来源「支持」是两件事，必须分开建模。
# ============================================================

Relation = Literal["supports", "refutes", "supplements", "mentions"]

RELATION_LABELS = {
    "supports": "支持",
    "refutes": "反驳",
    "supplements": "补充",
    "mentions": "仅提及",
}

# 只有 supports 计入支持数；其余三种都不算
_COUNTED_AS_SUPPORT = {"supports"}


ReviewStatus = Literal["accepted", "needs_review", "rejected", "pending"]

REVIEW_LABELS = {
    "accepted": "已采纳",
    "needs_review": "待复核",
    "rejected": "已排除",
    "pending": "待判断",
}


class ClaimEvidence(BaseModel):
    """一条 Claim 与一条 Evidence 之间的关系。

    confidence 是「这条关系判断有多可信」，不是「这个 Claim 有多可信」。
    两者不要混用——关系判断错了，事实判断自然会错，但它们是两个层级的量。

    review_status 区分「模型说了什么」和「最终采信什么」：
    只有 accepted 才参与 Claim 状态计算。低置信度的 refutes 不能直接把
    知识包标成争议——一次误判就会让整个结论变味。
    """

    claim_id: str
    evidence_id: str
    relation: Relation = "supports"
    confidence: float | None = None
    rationale: str | None = None

    # 支撑该判断的原文片段。模型说某条材料反驳 Claim，却指不出原文依据，
    # 那多半是误读了上下文——这一列就是用来查这个的。
    evidence_quote: str | None = None

    # 模型原始判断 vs 最终采信。默认 accepted 以兼容手工构造的场景，
    # 模型产出的关系一律先待复核，过门槛后才采纳。
    review_status: ReviewStatus = "accepted"

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float | None) -> float | None:
        if v is not None and not 0.0 <= v <= 1.0:
            raise ValueError("confidence 应在 0-1 之间")
        return v

    @property
    def relation_label(self) -> str:
        return RELATION_LABELS.get(self.relation, self.relation)

    @property
    def review_label(self) -> str:
        return REVIEW_LABELS.get(self.review_status, self.review_status)

    @property
    def counts_toward_status(self) -> bool:
        """只有采纳的关系才参与状态计算。"""
        return self.review_status == "accepted"


def _legacy_relations(claim: Claim) -> list[ClaimEvidence]:
    """把旧的 evidence_ids 视为全部 supports，保证老代码还能跑。"""
    return [
        ClaimEvidence(claim_id=claim.claim_id, evidence_id=eid, relation="supports")
        for eid in claim.evidence_ids
    ]


def evaluate_claim_evidence(
    claim: Claim,
    claim_evidences: list[ClaimEvidence],
    evidence_by_id: dict[str, Evidence],
) -> dict:
    """按「独立来源 × 关系」计算 Claim 状态。

    计算步骤（顺序不能颠倒）：
    1. 取本 Claim 的所有关系；没有显式关系时，退回 evidence_ids（全部算 supports）
    2. **先按 independence_key 聚合**，再分组统计
       —— 同一来源下的两条 Evidence，一条支持一条反驳，
          不能算成「一份支持 + 一份反驳」，那会虚增独立来源数
    3. AI 生成内容（知乎直答）不计入独立来源
    4. 判定状态

    判定规则（反证优先）：

    | 情况                          | 状态                 |
    |-------------------------------|----------------------|
    | 存在 ≥1 个独立反驳来源         | disputed             |
    | 独立支持 ≥2 且无反驳           | supported            |
    | 独立支持 =1 且无反驳           | partially_supported  |
    | 无支持                        | insufficient         |

    一条有依据的反证足以推翻「已支持」——这正是反证优先的原因。
    """
    all_rels = [r for r in claim_evidences if r.claim_id == claim.claim_id]
    if not all_rels:
        all_rels = _legacy_relations(claim)

    # 只采信 accepted：待复核 / 已排除 / 待判断的关系不参与状态判定。
    # 这条是防止「一次低置信误判就让整个 Claim 变成争议」的闸门。
    rels = [r for r in all_rels if r.review_status == "accepted"]
    ignored = len(all_rels) - len(rels)

    warnings: list[str] = []
    dangling: list[str] = []

    # independence_key -> relation -> evidence_ids
    grouped: dict[str, dict[str, set[str]]] = {}

    for r in rels:
        ev = evidence_by_id.get(r.evidence_id)
        if ev is None:
            dangling.append(r.evidence_id)
            continue
        if ev.source_type in _NON_INDEPENDENT_SOURCES:
            continue                       # 直答不算独立来源
        key = ev.independence_key or ev.evidence_id
        grouped.setdefault(key, {}).setdefault(r.relation, set()).add(r.evidence_id)

    if dangling:
        warnings.append(f"引用了不存在的证据：{', '.join(sorted(set(dangling)))}")
    if ignored:
        warnings.append(f"{ignored} 条关系未采纳（待复核或已排除），未计入状态")

    support_keys = []
    refute_keys = []
    supplement_count = 0
    mention_count = 0
    internal_conflict_keys = []

    for key, by_relation in grouped.items():
        has_support = bool(by_relation.get("supports"))
        has_refute = bool(by_relation.get("refutes"))

        if has_support and has_refute:
            # 同一来源内部自相矛盾：不计入任何一方，只记冲突
            internal_conflict_keys.append(key)
            warnings.append(f"来源 {key} 内部同时存在支持与反驳，已排除出独立计数")
        elif has_support:
            support_keys.append(key)
        elif has_refute:
            refute_keys.append(key)

        supplement_count += len(by_relation.get("supplements", ()))
        mention_count += len(by_relation.get("mentions", ()))

    support = len(support_keys)
    refute = len(refute_keys)

    if refute >= 1:
        status: EvidenceStatus = "disputed"
    elif support >= 2:
        status = "supported"
    elif support == 1:
        status = "partially_supported"
    else:
        status = "insufficient"

    return {
        "status": status,
        "independent_support_count": support,
        "independent_refute_count": refute,
        "supplement_count": supplement_count,
        "mention_count": mention_count,
        "internal_conflict_count": len(internal_conflict_keys),
        "ignored_relation_count": ignored,
        "warnings": warnings,
    }


def apply_claim_evidence(
    claim: Claim,
    claim_evidences: list[ClaimEvidence],
    evidence_by_id: dict[str, Evidence],
) -> Claim:
    """把关系计算结果回写到 Claim。返回同对象便于链式调用。"""
    result = evaluate_claim_evidence(claim, claim_evidences, evidence_by_id)
    claim.evidence_status = result["status"]
    claim.independent_source_count = result["independent_support_count"]
    claim.refuting_source_count = result["independent_refute_count"]
    claim.controversy = result["status"] == "disputed"
    return claim
