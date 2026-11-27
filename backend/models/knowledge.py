"""Knowledge Package：一次生成的知识总成。

它是前后端之间的主契约——前端不直接消费 LLM 输出，只消费这个对象。

## 字段选择上的两个决定

1. consensus / controversies 存 claim_id，不存文本
   存文本会与 claims 不同步：改了 Claim 表述、删了某条 Claim，
   这里还是旧的。存 id，展示时 JOIN 实时取。

2. analogy 用纯字符串（不用结构化对象）
   首期类比只用于旁白口播，不需要逐句溯源。
   如果后续要让类比重挂 Claim，再加 analogy_claim_ids 字段。

## scripts 为什么是个字典

同一份知识包对应三种深度，各有一份脚本：

    scripts = {"beginner": Script(...), "advanced": Script(...), ...}

切换深度时直接取，不重新检索、不重建知识包。
"""

from __future__ import annotations

from typing import Literal

import hashlib
import re

from pydantic import BaseModel, Field, PrivateAttr

from models.claim import Claim, ClaimEvidence, apply_claim_evidence, evaluate_claim_evidence
from models.evidence import Evidence
from models.script import Script

Depth = Literal["beginner", "advanced", "professional"]


class Analogy(BaseModel):
    """核心类比。一段话 + 它想解释的那个点。"""

    text: str
    explains: str = ""


class GlossaryItem(BaseModel):
    """术语表条目。"""

    term: str
    definition: str
    claim_id: str | None = None


class RelatedQuestion(BaseModel):
    """关联问题。

    source 必须标注，否则前端无法区分「模型想的」和「知乎真有人问的」。
    """

    question: str
    reason: str = ""
    source: Literal["llm", "zhihu_discovery"] = "llm"
    url: str | None = None


class Controversy(BaseModel):
    """一处争议：谁和谁对立、争的是什么。"""

    summary: str
    claim_ids: list[str] = Field(default_factory=list)
    sides: list[str] = Field(default_factory=list)


class KnowledgePackage(BaseModel):
    """一次生成的知识总成。创建后原则上只读。"""

    package_id: str
    query: str
    depth: Depth = "beginner"

    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    # Claim ↔ Evidence 的关系表。支持/反驳/补充/仅提及在这里表达，
    # Claim.evidence_ids 只是它的冗余视图（finalize 时同步）。
    claim_evidences: list[ClaimEvidence] = Field(default_factory=list)

    # 存 claim_id，不存文本
    consensus: list[str] = Field(default_factory=list)
    controversies: list[Controversy] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)

    analogy: str = ""
    glossary: list[GlossaryItem] = Field(default_factory=list)
    related_questions: list[RelatedQuestion] = Field(default_factory=list)

    # 深度 → 脚本
    scripts: dict[str, Script] = Field(default_factory=dict)

    # 生成过程的结构化事件
    trace: list[dict] = Field(default_factory=list)

    created_at: str = ""

    # finalize() 是否执行过。落库前必须为 True，否则 Claim 状态是脏的
    finalized: bool = False

    # finalize 时发现的引用问题（悬空 evidence_id 等），供上层决定是否告警
    warnings: list[str] = Field(default_factory=list)

    # 同一来源内部自相矛盾的来源数，不能计入独立计数
    internal_conflicts: int = 0

    # finalize 那一刻的证据/引用关系快照，用于识别「之后又变了」
    _finalized_fp: str | None = PrivateAttr(default=None)

    # ---------- 派生视图 ----------

    def finalize(self) -> "KnowledgePackage":
        """按证据关系回写所有 Claim 的 evidence_status。

        构建完成后必须调用一次，否则 claims 会停留在默认的 insufficient。
        放在这里而不是构造时自动跑，是因为 evidences 可能分批加入。
        """
        by_id = {e.evidence_id: e for e in self.evidence}
        self.warnings = []
        self.internal_conflicts = 0

        for c in self.claims:
            apply_claim_evidence(c, self.claim_evidences, by_id)
            result = evaluate_claim_evidence(c, self.claim_evidences, by_id)
            self.internal_conflicts += result["internal_conflict_count"]
            for w in result["warnings"]:
                tagged = f"{c.claim_id}: {w}"
                if tagged not in self.warnings:
                    self.warnings.append(tagged)

        self._finalized_fp = self._fingerprint()
        self.finalized = True
        return self

    def _fingerprint(self) -> str:
        """状态指纹。凡影响 Claim 判定的都要进来，不能只查 ID。

        覆盖：
        - 证据的身份与数量（evidence_id）
        - 独立性归并结果（independence_key）—— 变了会改变独立来源计数
        - 证据正文（content 指纹）—— 同一 ID 换了内容，支持关系就变了
        - 可信度（credibility）
        - Claim 与证据的引用关系
        - 争议标记（controversy 直接影响 evidence_status）
        """
        ev_parts = []
        for e in sorted(self.evidence, key=lambda x: x.evidence_id):
            # 内容按「去空白后」计算指纹：纯格式化差异不该频繁触发重算，
            # 但原始内容本身保持不变。
            normalized = re.sub(r"\s+", "", e.content or "")
            body = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
            ev_parts.append(f"{e.evidence_id}|{e.independence_key}|{e.credibility}|{body}")

        ref_parts = []
        for c in sorted(self.claims, key=lambda x: x.claim_id):
            refs = ",".join(sorted(c.evidence_ids))
            ref_parts.append(f"{c.claim_id}|{int(c.controversy)}|{refs}")

        # 关系表必须进指纹：supports 改成 refutes，状态就变了
        rel_parts = [
            f"{r.claim_id}|{r.evidence_id}|{r.relation}"
            for r in sorted(self.claim_evidences, key=lambda x: (x.claim_id, x.evidence_id, x.relation))
        ]

        raw = "||".join(ev_parts) + "##" + "||".join(ref_parts) + "##" + "||".join(rel_parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def assert_ready(self) -> None:
        """落库 / 返回给前端之前调用。

        拦住两种情况：
        1. 从未 finalize —— Claim 状态还是默认的 insufficient
        2. finalize 之后 Evidence 或引用关系又变了 —— 状态已过期
        """
        if not self.finalized:
            raise RuntimeError("KnowledgePackage 未执行 finalize()，Claim 的证据状态不可信")
        if self._finalized_fp != self._fingerprint():
            raise RuntimeError(
                "finalize 之后 Evidence 或 Claim 引用关系发生变化，状态已过期，请重新 finalize()"
            )

    def claim_by_id(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)

    def evidence_by_id(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.evidence if e.evidence_id == evidence_id), None)

    @property
    def verified_claims(self) -> list[Claim]:
        """多来源支持的主张。"""
        return [c for c in self.claims if c.evidence_status == "supported"]

    @property
    def disputed_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.evidence_status == "disputed"]

    @property
    def insufficient_claims(self) -> list[Claim]:
        """证据不足的——不能作为确定事实展示。"""
        return [c for c in self.claims if c.evidence_status == "insufficient"]

    def stats(self) -> dict:
        """给概览页与 Trace 用的统计。"""
        total = len(self.claims)
        return {
            "claims": total,
            "supported": len(self.verified_claims),
            "partially_supported": sum(1 for c in self.claims if c.evidence_status == "partially_supported"),
            "disputed": len(self.disputed_claims),
            "insufficient": len(self.insufficient_claims),
            "evidence": len(self.evidence),
            "controversies": len(self.controversies),
            "unresolved": len(self.unresolved_questions),
        }

    def evidence_of(self, claim: Claim) -> list[Evidence]:
        """取某条 Claim 的证据对象列表。"""
        return [e for e in self.evidence if e.evidence_id in claim.evidence_ids]
