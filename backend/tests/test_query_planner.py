"""query_planner 的测试。

重点：
1. 规则兜底必须可用（演示时不依赖模型）
2. 必有一条 counter（反方观点）
3. 脏输出被洗掉，不会进模型层
4. 预算不会超项目上限
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.retrieval import DEFAULT_BUDGET, RetrievalPlan, SubQuery  # noqa: E402
from retrieval.query_planner import _sanitize, plan, rule_based_plan  # noqa: E402


def test_rule_plan_is_valid():
    p = rule_based_plan("为什么飞机那么重却能飞起来")
    assert isinstance(p, RetrievalPlan)
    assert len(p.sub_queries) >= 3
    assert p.total_calls <= sum(DEFAULT_BUDGET.values())


def test_rule_plan_always_has_counter():
    """只找顺耳证据会得出片面结论，兜底计划也必须安排反方。"""
    for q in ["什么是向量数据库", "扫地机器人哪个好", "考研值不值", "如何提升核心竞争力"]:
        p = rule_based_plan(q)
        assert p.has_counter_query, f"{q} 的计划缺少反方观点"


def test_budget_never_exceeds_ceiling():
    p = rule_based_plan("测试问题")
    for k, v in p.retrieval_budget.items():
        assert v <= DEFAULT_BUDGET.get(k, 0)


def test_budget_override_rejected():
    with pytest.raises(ValueError, match="超过上限"):
        RetrievalPlan(
            original_query="q",
            sub_queries=[SubQuery(query="q1")],
            retrieval_budget={"zhihu_search": 20},
        )


def test_sub_query_limit():
    subs = [SubQuery(query=f"q{i}") for i in range(6)]
    with pytest.raises(ValueError, match="最多 5 个"):
        RetrievalPlan(original_query="q", sub_queries=subs)


def test_empty_sub_query_rejected():
    with pytest.raises(ValueError):
        SubQuery(query="   ")


def test_priority_range():
    with pytest.raises(ValueError):
        SubQuery(query="q", priority=0)


def test_sanitize_drops_invalid_values():
    raw = {
        "intent": "concept",
        "sub_queries": [
            {"query": "飞机升力原理", "purpose": "core", "target_sources": ["zhihu"], "priority": 1},
            {"query": "飞机升力 误解", "purpose": "bogus", "target_sources": ["google"], "priority": "x"},
            {"query": "", "purpose": "core"},          # 空查询，应被丢弃
            "不是对象",                                  # 非对象，应被丢弃
        ],
        "source_strategy": ["zhihu", "baidu"],
        "time_window_days": "abc",
    }
    out = _sanitize(raw, "飞机为什么能飞")
    assert out is not None
    assert len(out["sub_queries"]) >= 2                      # 至少两条有效（可能再补一条 counter）
    assert out["sub_queries"][1]["purpose"] == "core"        # 非法 purpose 归 core
    assert out["sub_queries"][1]["target_sources"] == ["zhihu"]  # 非法来源归默认
    assert out["source_strategy"] == ["zhihu"]
    assert out["time_window_days"] is None


def test_sanitize_injects_counter_when_missing():
    """模型只给顺耳的角度时，代码补一条反方。"""
    raw = {
        "intent": "concept",
        "sub_queries": [{"query": "量子纠缠定义", "purpose": "core", "priority": 1}],
    }
    out = _sanitize(raw, "量子纠缠是什么")
    assert any(s["purpose"] == "counter" for s in out["sub_queries"])


def test_sanitize_rejects_empty_plan():
    assert _sanitize({"sub_queries": []}, "q") is None
    assert _sanitize({}, "q") is None


def test_sanitize_caps_sub_queries():
    raw = {"sub_queries": [{"query": f"q{i}", "purpose": "core"} for i in range(10)]}
    out = _sanitize(raw, "q")
    assert len(out["sub_queries"]) <= 5


def test_plan_falls_back_to_rules_offline():
    """allow_llm=False 时纯规则，不触网。"""
    p, src = plan("扫地机器人值得买吗", allow_llm=False)
    assert src == "rule"
    assert p.has_counter_query


def test_plan_rejects_empty_query():
    with pytest.raises(ValueError):
        plan("   ", allow_llm=False)
