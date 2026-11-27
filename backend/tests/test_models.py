"""核心数据契约的单元测试。

这些测试守护的是「字段语义」，不是业务逻辑：
一旦有人改字段名或改判定规则，这里会先红。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Claim, Evidence, KnowledgePackage, Scene, Script, Visual  # noqa: E402
from models.claim import apply_evidence_status, compute_evidence_status, normalize_claim_text  # noqa: E402
from models.evidence import (  # noqa: E402
    from_global_search,
    from_question_answer,
    from_zhihu_search,
    make_independence_key,
)

RAW = Path(__file__).parent.parent.parent / "data" / "raw" / "day1"


@pytest.fixture(scope="module")
def zhihu_items():
    data = json.loads((RAW / "search_zhihu.json").read_text(encoding="utf-8"))
    return (data.get("Data") or {}).get("Items") or []


@pytest.fixture(scope="module")
def global_items():
    data = json.loads((RAW / "search_global.json").read_text(encoding="utf-8"))
    return (data.get("Data") or {}).get("Items") or []


# ---------------- Evidence ----------------

def test_evidence_from_zhihu_keeps_metadata(zhihu_items):
    ev = from_zhihu_search(zhihu_items[0], 0)
    assert ev.source_type == "zhihu"
    assert ev.content_type == "answer_excerpt"
    assert ev.content
    assert ev.url and "zhihu.com" in ev.url
    assert ev.source_question_id
    # 平台质量信号必须保留
    assert "authority_level" in ev.raw_metadata
    assert "ranking_score" in ev.raw_metadata


def test_evidence_from_global_records_host(global_items):
    ev = from_global_search(global_items[0], 0)
    assert ev.source_type == "web"
    assert ev.content_type == "web_snippet"
    assert ev.source_name and ev.source_name != ""


def test_question_answer_marked_as_snippet():
    """question answers 的 Summary 只有 74 字左右，必须标成 search_snippet，
    不能标成 answer_excerpt，否则下游会误判材料充分度。"""
    ev = from_question_answer({"Url": "https://www.zhihu.com/question/1/answer/2", "Summary": "短摘要"}, 0)
    assert ev.content_type == "search_snippet"
    assert ev.title == ""


def test_empty_content_rejected():
    with pytest.raises(ValueError):
        Evidence(evidence_id="x", source_type="zhihu", content="   ", content_type="answer_excerpt")


def test_independence_key_autofilled():
    ev = Evidence(evidence_id="x", source_type="zhihu", content="一些内容", content_type="answer_excerpt")
    assert ev.independence_key


def test_same_url_same_key():
    a = Evidence(evidence_id="a", source_type="zhihu", content="内容A", content_type="answer_excerpt",
                 url="https://www.zhihu.com/question/1/answer/2")
    b = Evidence(evidence_id="b", source_type="zhihu", content="内容A", content_type="answer_excerpt",
                 url="https://www.zhihu.com/question/1/answer/2")
    assert a.independence_key == b.independence_key


def test_ai_generated_flagged():
    ev = Evidence(evidence_id="x", source_type="zhihu_direct_answer",
                  content="综合答案", content_type="direct_answer")
    assert ev.is_ai_generated is True


# ---------------- Claim / evidence_status ----------------

def _ev(eid, key, stype="zhihu"):
    return Evidence(evidence_id=eid, source_type=stype, content=f"内容{eid}",
                    content_type="answer_excerpt", independence_key=key)


def test_two_independent_sources_supported():
    evs = [_ev("e1", "k1"), _ev("e2", "k2")]
    c = Claim(claim_id="c", claim_text="某说法成立", evidence_ids=["e1", "e2"])
    status, n = compute_evidence_status(c, evs)
    assert (status, n) == ("supported", 2)


def test_single_source_partial():
    evs = [_ev("e1", "k1")]
    c = Claim(claim_id="c", claim_text="某说法成立", evidence_ids=["e1"])
    assert compute_evidence_status(c, evs) == ("partially_supported", 1)


def test_no_source_insufficient():
    c = Claim(claim_id="c", claim_text="无来源说法", evidence_ids=[])
    assert compute_evidence_status(c, []) == ("insufficient", 0)


def test_controversy_disputed():
    evs = [_ev("e1", "k1"), _ev("e2", "k2")]
    c = Claim(claim_id="c", claim_text="存在对立说法", evidence_ids=["e1", "e2"], controversy=True)
    assert compute_evidence_status(c, evs) == ("disputed", 2)


def test_same_key_counted_once():
    """同一来源被引用两次，不能算两个独立证据。"""
    evs = [_ev("e1", "same"), _ev("e2", "same")]
    c = Claim(claim_id="c", claim_text="重复来源", evidence_ids=["e1", "e2"])
    assert compute_evidence_status(c, evs) == ("partially_supported", 1)


def test_direct_answer_not_independent():
    """知乎直答 + 它引用的原文，只能算一条独立证据。"""
    direct = Evidence(evidence_id="ai", source_type="zhihu_direct_answer",
                      content="综合答案", content_type="direct_answer", independence_key="ai-1")
    quoted = _ev("q", "zhihu-1")
    c = Claim(claim_id="c", claim_text="机翼产生升力", evidence_ids=["ai", "q"])
    status, n = compute_evidence_status(c, [direct, quoted])
    assert status == "partially_supported"
    assert n == 1


def test_model_confidence_cannot_upgrade_status():
    """模型说 high，但只有一条来源，展示置信只能是 medium。"""
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"],
              model_confidence="high", evidence_status="partially_supported")
    assert c.display_confidence == "medium"
    assert c.is_verified is False


def test_insufficient_never_high():
    c = Claim(claim_id="c", claim_text="无依据", model_confidence="high",
              evidence_status="insufficient")
    assert c.display_confidence == "low"


def test_claim_text_too_long_rejected():
    with pytest.raises(ValueError):
        Claim(claim_id="c", claim_text="很长的一句" * 30)


def test_normalize_claim_text():
    a = normalize_claim_text("机翼，产生升力。")
    b = normalize_claim_text("机翼产生升力")
    assert a == b


# ---------------- KnowledgePackage ----------------

def test_finalize_writes_status(zhihu_items):
    evs = [from_zhihu_search(it, i) for i, it in enumerate(zhihu_items[:5])]
    c1 = Claim(claim_id="c1", claim_text="说法一", evidence_ids=[evs[0].evidence_id, evs[1].evidence_id])
    c2 = Claim(claim_id="c2", claim_text="说法二", evidence_ids=[evs[2].evidence_id])
    c3 = Claim(claim_id="c3", claim_text="说法三", evidence_ids=[])

    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c1, c2, c3], evidence=evs).finalize()
    stats = pkg.stats()
    assert stats["supported"] == 1
    assert stats["partially_supported"] == 1
    assert stats["insufficient"] == 1


def test_package_derived_views():
    pkg = KnowledgePackage(package_id="kp", query="q")
    assert pkg.verified_claims == []
    assert pkg.stats()["claims"] == 0


# ---------------- Script / Scene ----------------

def test_scene_timeline_from_audio():
    s = Script(script_id="s", depth="beginner")
    s.scenes = [
        Scene(scene_id="a", narration="第一段", audio_duration_ms=4000),
        Scene(scene_id="b", narration="第二段", audio_duration_ms=6000),
    ]
    s.recalc_timeline()
    assert s.total_duration_ms == 10000
    assert s.scenes[0].start_ms == 0 and s.scenes[0].end_ms == 4000
    assert s.scenes[1].start_ms == 4000 and s.scenes[1].end_ms == 10000


def test_scene_timeline_estimates_without_audio():
    s = Script(script_id="s")
    s.scenes = [Scene(scene_id="a", narration="这是一段没有配音的旁白，大约二十个字左右")]
    s.recalc_timeline()
    assert s.total_duration_ms >= 2000


def test_scene_at_position():
    s = Script(script_id="s")
    s.scenes = [Scene(scene_id="a", audio_duration_ms=3000), Scene(scene_id="b", audio_duration_ms=3000)]
    s.recalc_timeline()
    assert s.scene_at(1000).scene_id == "a"
    assert s.scene_at(4000).scene_id == "b"


def test_unknown_visual_type_falls_back_to_text():
    v = Visual(type="three_d_render")
    assert v.type == "text"


def test_visual_accepts_all_six():
    for t in ["illustration", "text", "diagram", "chart", "formula", "comparison"]:
        assert Visual(type=t).type == t


# ---------------- 边界与幂等（用户补充的六条） ----------------

def test_model_high_but_no_evidence_cannot_be_verified():
    """模型自评 high，但只有一条来源 —— 走真实计算流程，不能变成已验证。"""
    evs = [_ev("e1", "k1")]
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"], model_confidence="high")
    apply_evidence_status(c, evs)
    assert c.evidence_status == "partially_supported"
    assert c.is_verified is False
    assert c.display_confidence == "medium"     # 不能因为模型自信就 high


def test_model_high_with_zero_evidence_is_low():
    c = Claim(claim_id="c", claim_text="凭空的说法", model_confidence="high", evidence_ids=[])
    apply_evidence_status(c, [])
    assert c.evidence_status == "insufficient"
    assert c.display_confidence == "low"
    assert c.is_verified is False


def test_direct_answer_plus_quoted_counts_one():
    direct = Evidence(evidence_id="ai", source_type="zhihu_direct_answer",
                      content="综合答案", content_type="direct_answer", independence_key="ai:same")
    quoted = Evidence(evidence_id="q", source_type="zhihu", content="原回答",
                      content_type="answer_excerpt",
                      url="https://www.zhihu.com/question/1/answer/2")
    c = Claim(claim_id="c", claim_text="机翼产生升力", evidence_ids=["ai", "q"])
    status, n = compute_evidence_status(c, [direct, quoted])
    assert n == 1
    assert status == "partially_supported"


def test_same_url_different_content_deduped():
    """同一 URL 抓两次、内容截断位置不同 —— 必须算同一个来源。"""
    a = Evidence(evidence_id="a", source_type="zhihu", content="前半段内容",
                 content_type="answer_excerpt",
                 url="https://www.zhihu.com/question/1/answer/2")
    b = Evidence(evidence_id="b", source_type="zhihu", content="后半段内容完全不一样",
                 content_type="answer_excerpt",
                 url="https://www.zhihu.com/question/1/answer/2?utm_source=x")
    assert a.independence_key == b.independence_key
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["a", "b"])
    assert compute_evidence_status(c, [a, b]) == ("partially_supported", 1)


def test_refinalize_after_evidence_added():
    """补了一条证据后重新 finalize，Claim 状态要同步升级。"""
    e1 = _ev("e1", "k1")
    e2 = _ev("e2", "k2")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1]).finalize()
    assert c.evidence_status == "partially_supported"

    pkg.evidence.append(e2)
    c.evidence_ids.append("e2")
    pkg.finalize()
    assert c.evidence_status == "supported"
    assert c.independent_source_count == 2


def test_refinalize_after_evidence_removed():
    """移除证据后重新 finalize，状态要降级，不能停在旧值。"""
    e1, e2 = _ev("e1", "k1"), _ev("e2", "k2")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1", "e2"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1, e2]).finalize()
    assert c.evidence_status == "supported"

    pkg.evidence = [e1]
    c.evidence_ids = ["e1"]
    pkg.finalize()
    assert c.evidence_status == "partially_supported"
    assert c.independent_source_count == 1


def test_recalc_timeline_is_idempotent():
    """多次调用 recalc_timeline，总时长不能累加。"""
    s = Script(script_id="s")
    s.scenes = [
        Scene(scene_id="a", narration="第一段", audio_duration_ms=4000),
        Scene(scene_id="b", narration="第二段", audio_duration_ms=6000),
    ]
    s.recalc_timeline()
    first = (s.total_duration_ms, s.scenes[0].start_ms, s.scenes[1].end_ms)
    for _ in range(3):
        s.recalc_timeline()
    assert (s.total_duration_ms, s.scenes[0].start_ms, s.scenes[1].end_ms) == first
    assert s.total_duration_ms == 10000


def test_timeline_uses_real_audio_over_estimate():
    """TTS 实际时长与预估不同时，以音频为准重算。"""
    s = Script(script_id="s")
    s.scenes = [
        Scene(scene_id="a", narration="短文本", audio_duration_ms=9000),   # 预估只有 2 秒左右
        Scene(scene_id="b", narration="另一段", audio_duration_ms=3000),
    ]
    s.recalc_timeline()
    assert s.scenes[0].end_ms == 9000
    assert s.scenes[1].start_ms == 9000
    assert s.total_duration_ms == 12000


def test_assert_ready_blocks_unfinalized():
    """未 finalize 的知识包不允许直接出门。"""
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[])
    with pytest.raises(RuntimeError):
        pkg.assert_ready()
    pkg.finalize()
    pkg.assert_ready()      # 不抛错


def test_finalized_flag_tracks_state():
    pkg = KnowledgePackage(package_id="kp", query="q")
    assert pkg.finalized is False
    pkg.finalize()
    assert pkg.finalized is True


# ---------------- 跨源去重（真实数据实测暴露的问题） ----------------

def test_same_answer_across_sources_deduped(zhihu_items, global_items):
    """同一篇知乎回答可能同时出现在 search zhihu 和 search global 里。

    实测数据里 20 条中有 4 条完全重复。如果不按 URL 统一归并，
    一条 Claim 会被这 4 篇算成 8 个独立来源 —— 虚假的「多来源印证」。
    """
    zh = [from_zhihu_search(it, i) for i, it in enumerate(zhihu_items)]
    gl = [from_global_search(it, i) for i, it in enumerate(global_items)]

    keys = {e.independence_key for e in zh + gl}
    assert len(keys) < len(zh) + len(gl), "两个接口存在重复，合并后应少于总数"
    # 实测：20 条 → 16 个独立来源
    assert len(keys) == 16


def test_global_result_on_zhihu_domain_classified_as_zhihu(global_items):
    """全网搜索里有 6/10 来自知乎域名，必须归为 zhihu 而不是 web。"""
    evs = [from_global_search(it, i) for i, it in enumerate(global_items)]
    zhihu_count = sum(1 for e in evs if e.source_type == "zhihu")
    assert zhihu_count == 6, f"预期 6 条知乎域名，实际 {zhihu_count}"


def test_claims_across_sources_not_double_counted(zhihu_items, global_items):
    """同一回答从两个接口各进来一次，作为证据时只算 1 条。"""
    zh = [from_zhihu_search(it, i) for i, it in enumerate(zhihu_items)]
    gl = [from_global_search(it, i) for i, it in enumerate(global_items)]

    # 找到跨源重复的那一条
    zh_by_key = {e.independence_key: e for e in zh}
    dup_pair = next((e for e in gl if e.independence_key in zh_by_key), None)
    assert dup_pair is not None, "这批数据应当存在跨源重复"

    original = zh_by_key[dup_pair.independence_key]
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=[original.evidence_id, dup_pair.evidence_id])
    status, n = compute_evidence_status(c, [original, dup_pair])
    assert n == 1
    assert status == "partially_supported"


def test_url_normalization_edge_cases():
    """域名大小写、www 前缀、http/https、尾斜杠、锚点、utm 都不应影响判同。"""
    pairs = [
        ("https://Example.com/a", "https://example.com/a"),
        ("https://www.example.com/a", "https://example.com/a"),
        ("http://example.com/a", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a#sec", "https://example.com/a"),
        ("https://example.com/a?utm_source=x", "https://example.com/a"),
    ]
    for u1, u2 in pairs:
        assert make_independence_key("web", u1, "c") == make_independence_key("web", u2, "c"), f"{u1} 与 {u2} 应同键"


def test_independence_key_has_no_source_type_prefix():
    """键不能带 source_type 前缀，否则同 URL 跨源会不同键。"""
    k1 = make_independence_key("zhihu", "https://example.com/a", "x")
    k2 = make_independence_key("web", "https://example.com/a", "x")
    assert k1 == k2
    assert k1.startswith("url:")


# ---------------- 关系失效告警 ----------------

def test_dangling_reference_recorded_as_warning():
    """Claim 引用了已不存在的 evidence_id，finalize 时不能沉默。"""
    e1 = _ev("e1", "k1")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1", "已经删掉的ID"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1]).finalize()
    assert pkg.warnings, "悬空引用必须留下告警"
    assert "已经删掉的ID" in pkg.warnings[0]
    assert c.independent_source_count == 1


def test_no_warning_when_references_clean():
    e1 = _ev("e1", "k1")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1]).finalize()
    assert pkg.warnings == []


# ---------------- URL 归一化的四个边界（评审补充） ----------------

def test_port_distinguishes_hosts():
    """非默认端口必须保留：example.com:8443 与 example.com 可能是两套服务。"""
    k1 = make_independence_key("web", "https://a.com:8443/x", "c")
    k2 = make_independence_key("web", "https://a.com/x", "c")
    assert k1 != k2


def test_default_port_stripped():
    """默认端口应被去掉，与不带端口等价。"""
    assert make_independence_key("web", "https://a.com:443/x", "c") == make_independence_key("web", "https://a.com/x", "c")
    assert make_independence_key("web", "http://a.com:80/x", "c") == make_independence_key("web", "http://a.com/x", "c")


def test_path_case_preserved():
    """路径大小写必须保留：部分服务器 /Wiki 与 /wiki 是两个页面。"""
    k1 = make_independence_key("web", "https://a.com/Wiki", "c")
    k2 = make_independence_key("web", "https://a.com/wiki", "c")
    assert k1 != k2


def test_content_identity_query_kept():
    """?id=100 与 ?id=200 是不同文章，不能被合并。"""
    k1 = make_independence_key("web", "https://a.com/x?id=100", "c")
    k2 = make_independence_key("web", "https://a.com/x?id=200", "c")
    assert k1 != k2


def test_tracking_params_stripped():
    k1 = make_independence_key("web", "https://a.com/x?utm_source=a", "c")
    k2 = make_independence_key("web", "https://a.com/x?utm_source=b", "c")
    assert k1 == k2


def test_query_param_order_irrelevant():
    k1 = make_independence_key("web", "https://a.com/x?a=1&b=2", "c")
    k2 = make_independence_key("web", "https://a.com/x?b=2&a=1", "c")
    assert k1 == k2


def test_normalized_url_kept_separately():
    """原始 URL 与归一化 URL 都要留，便于排查误合并。"""
    ev = Evidence(evidence_id="x", source_type="web", content="内容",
                  content_type="web_snippet",
                  url="https://www.Example.com:8443/Path/?utm_source=x&id=3#frag")
    assert ev.url == "https://www.Example.com:8443/Path/?utm_source=x&id=3#frag"
    assert ev.normalized_url == "https://example.com:8443/Path?id=3"


# ---------------- 端到端：20 条 Evidence 只算 16 个独立来源 ----------------

def test_twenty_evidence_counted_as_sixteen_sources(zhihu_items, global_items):
    """这条堵的是「按对象数量计数」而不是「按独立来源计数」的漏洞。

    20 条原始材料（知乎 10 + 全网 10），其中 4 条完全相同。
    全部关联到同一个 Claim，独立来源数必须是 16，不是 20。
    """
    zh = [from_zhihu_search(it, i) for i, it in enumerate(zhihu_items)]
    gl = [from_global_search(it, i) for i, it in enumerate(global_items)]
    all_ev = zh + gl
    assert len(all_ev) == 20

    c = Claim(claim_id="c", claim_text="飞机机翼产生升力",
              evidence_ids=[e.evidence_id for e in all_ev])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=all_ev).finalize()

    assert c.independent_source_count == 16, f"应按独立来源算 16，实际 {c.independent_source_count}"
    assert c.evidence_status == "supported"
    assert len(pkg.warnings) == 0


def test_retrieval_channel_independent_of_source_type(global_items):
    """retrieval_channel 说「怎么找到的」，source_type 说「内容来自哪」。"""
    evs = [from_global_search(it, i) for i, it in enumerate(global_items)]
    # 从全网搜索渠道进来，但内容是知乎的
    from_zhihu_host = next(e for e in evs if e.source_type == "zhihu")
    assert from_zhihu_host.retrieval_channel == "global_search"
    assert from_zhihu_host.source_type == "zhihu"


# ---------------- assert_ready 识别 finalize 后的变化 ----------------

def test_assert_ready_detects_evidence_added_after_finalize():
    e1 = _ev("e1", "k1")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1]).finalize()
    pkg.assert_ready()

    pkg.evidence.append(_ev("e2", "k2"))
    with pytest.raises(RuntimeError, match="发生变化"):
        pkg.assert_ready()


def test_assert_ready_detects_reference_change_after_finalize():
    e1, e2 = _ev("e1", "k1"), _ev("e2", "k2")
    c = Claim(claim_id="c", claim_text="某说法", evidence_ids=["e1"])
    pkg = KnowledgePackage(package_id="kp", query="q", claims=[c], evidence=[e1, e2]).finalize()
    pkg.assert_ready()

    c.evidence_ids.append("e2")
    with pytest.raises(RuntimeError, match="发生变化"):
        pkg.assert_ready()

    pkg.finalize()
    pkg.assert_ready()          # 重新 finalize 后恢复正常
    assert c.evidence_status == "supported"
