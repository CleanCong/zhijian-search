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
    independent_source_count: int = 0
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
