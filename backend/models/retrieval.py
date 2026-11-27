"""检索计划的数据契约。

query_planner 只负责一件事：根据用户问题决定「检索什么、去哪检索、检索几次」。

它不负责 Claim 抽取，也不判断证据是否成立——那些是下游的事。
把职责分开，是因为规划要快（用户等着），而证据判断要准（可以慢）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 四类查询目的。每一类都有它在链路里的位置，不是随便贴的标签。
QueryPurpose = Literal["core", "mechanism", "counter", "latest"]

PURPOSE_LABELS = {
    "core": "核心问题",
    "mechanism": "原理机制",
    "counter": "反方观点",
    "latest": "最新进展",
}

SourceName = Literal["zhihu", "global", "question_answers", "direct_answer", "hot", "discovery"]

# 预算上限（项目自制，不是平台限制）。
# 一次普通检索最多 7 次调用，按 5000 额度算约可支撑 700 次完整检索。
DEFAULT_BUDGET = {
    "zhihu_search": 3,
    "global_search": 2,
    "question_answers": 1,
    "direct_answer": 1,
    "hot": 0,
    "discovery": 0,
}

# 单次规划允许的子查询数量上限，防止 LLM 失控
MAX_SUB_QUERIES = 5


class SubQuery(BaseModel):
    """一个子查询：搜什么、为什么搜、去哪搜、多重要。"""

    query: str
    purpose: QueryPurpose = "core"
    target_sources: list[SourceName] = Field(default_factory=lambda: ["zhihu"])
    priority: int = 1                     # 1 最高，数字越大越次要

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("子查询不能为空")
        return v

    @field_validator("query")
    @classmethod
    def query_not_too_long(cls, v: str) -> str:
        if len(v) > 80:
            raise ValueError(f"子查询过长（{len(v)} 字），应当更短更聚焦")
        return v

    @field_validator("priority")
    @classmethod
    def priority_sane(cls, v: int) -> int:
        if not 1 <= v <= 9:
            raise ValueError("priority 应在 1-9 之间")
        return v

    @property
    def purpose_label(self) -> str:
        return PURPOSE_LABELS.get(self.purpose, self.purpose)


class RetrievalPlan(BaseModel):
    """一次检索的完整计划。"""

    original_query: str
    intent: str = "general"
    sub_queries: list[SubQuery] = Field(default_factory=list)

    # 这次要用哪些来源（与子查询的 target_sources 是粗略 vs 精细的关系）
    source_strategy: list[SourceName] = Field(default_factory=lambda: ["zhihu"])

    # 各接口的调用次数上限
    retrieval_budget: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_BUDGET))

    # 时效过滤窗口；None 表示不限
    time_window_days: int | None = None

    # 为什么这样规划（给人看的，也便于排查）
    reason: str = ""

    plan_version: str = "v1"              # 进缓存 key，改 prompt 时递增

    @field_validator("sub_queries")
    @classmethod
    def sub_queries_bounded(cls, v: list[SubQuery]) -> list[SubQuery]:
        if not v:
            raise ValueError("至少需要一个子查询")
        if len(v) > MAX_SUB_QUERIES:
            raise ValueError(f"子查询最多 {MAX_SUB_QUERIES} 个，实际 {len(v)}")
        return v

    @field_validator("retrieval_budget")
    @classmethod
    def budget_within_ceiling(cls, v: dict[str, int]) -> dict[str, int]:
        """预算不能超过项目自设的上限，防止 LLM 给出 20 次搜索这种计划。"""
        for key, limit in DEFAULT_BUDGET.items():
            if v.get(key, 0) > limit:
                raise ValueError(f"预算 {key}={v[key]} 超过上限 {limit}")
        return v

    # ---------- 派生视图 ----------

    @property
    def total_calls(self) -> int:
        return sum(self.retrieval_budget.values())

    @property
    def has_counter_query(self) -> bool:
        """是否安排了「反方观点」——这是避免只找顺耳证据的关键。"""
        return any(s.purpose == "counter" for s in self.sub_queries)

    @property
    def is_time_sensitive(self) -> bool:
        return any(s.purpose == "latest" for s in self.sub_queries) or self.time_window_days is not None

    def by_purpose(self) -> dict[str, list[SubQuery]]:
        out: dict[str, list[SubQuery]] = {}
        for s in sorted(self.sub_queries, key=lambda x: x.priority):
            out.setdefault(s.purpose, []).append(s)
        return out

    def ordered_queries(self) -> list[SubQuery]:
        """按优先级排序，额度不够时先跑重要的。"""
        return sorted(self.sub_queries, key=lambda x: (x.priority, len(x.query)))
