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
from models.claim import compute_evidence_status, normalize_claim_text  # noqa: E402
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
