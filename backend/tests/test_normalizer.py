"""result_normalizer 的测试。

重点验证三件事：
1. 脏数据不会让整批失败
2. 同源记录只留一条（且留正文更长的）
3. 统计数字与实际一致
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval import normalize_batch, to_json_payload  # noqa: E402

RAW = Path(__file__).parent.parent.parent / "data" / "raw" / "day1"


@pytest.fixture(scope="module")
def real_items():
    zh = json.loads((RAW / "search_zhihu.json").read_text(encoding="utf-8"))
    gl = json.loads((RAW / "search_global.json").read_text(encoding="utf-8"))
    return (zh.get("Data") or {}).get("Items") or [], (gl.get("Data") or {}).get("Items") or []


def test_real_batch_converts_and_dedupes(real_items):
    zh, gl = real_items
    evs, report = normalize_batch(zh, gl)

    assert report.total_raw == 20
    assert len(evs) == 16                    # 4 条跨源重复被合并
    assert report.merged == 4
    assert report.skipped == 0
    assert report.errors == []


def test_report_channels_correct(real_items):
    zh, gl = real_items
    evs, report = normalize_batch(zh, gl)
    assert report.by_channel["zhihu_search"] == 10
    assert report.by_channel["global_search"] == 10
    # 全网渠道里有 6 条是知乎域名
    assert report.by_source_type.get("zhihu", 0) >= 10
    assert report.by_source_type.get("web", 0) >= 4


def test_no_field_loss(real_items):
    zh, gl = real_items
    evs, _ = normalize_batch(zh, gl)
    for e in evs:
        assert e.evidence_id and e.content and e.content_type
        assert e.source_type in {"zhihu", "web", "paper", "official", "zhihu_direct_answer"}
        assert e.retrieval_channel
        assert e.independence_key
        if e.url:
            assert e.normalized_url


def test_empty_content_skipped():
    items = [{"Title": "有标题但正文空", "Url": "https://a.com/1", "ContentText": ""}]
    evs, report = normalize_batch(items, [], [])
    assert len(evs) == 0
    assert report.skipped == 1
    # 空正文由 Evidence 的字段校验拦下，记进 errors 即可
    assert any("content" in e or "正文" in e for e in report.errors)


def test_non_dict_skipped():
    items = ["这不是对象", 123, None]
    evs, report = normalize_batch(items, [], [])
    assert len(evs) == 0
    assert report.skipped == 3
    assert report.total_raw == 3


def test_missing_fields_do_not_crash():
    """缺 Title / Author / 时间字段也要能转出来。"""
    items = [{"Url": "https://a.com/1", "ContentText": "只有正文也可以"}]
    evs, report = normalize_batch(items, [], [])
    assert len(evs) == 1
    assert evs[0].title == ""
    assert evs[0].author is None
    assert report.errors == []


def test_one_bad_item_does_not_break_batch():
    good = {"Title": "正常", "Url": "https://a.com/ok", "ContentText": "正常内容"}
    bad = {"Title": "坏数据", "Url": 12345, "ContentText": "内容"}   # url 类型不对
    items = [good, bad, good | {"Url": "https://a.com/ok2"}]
    evs, report = normalize_batch(items, [], [])
    assert len(evs) >= 1                  # 好的那几条仍然被转换
    assert report.total_raw == 3


def test_duplicate_keeps_longer_content():
    short = {"Title": "短", "Url": "https://a.com/same", "ContentText": "短内容"}
    long_ = {"Title": "长", "Url": "https://a.com/same", "ContentText": "长内容" * 50}
    evs, report = normalize_batch([short, long_], [], [])
    assert len(evs) == 1
    assert report.merged == 1
    assert len(evs[0].content) > 50


def test_question_answers_marked_as_snippet():
    qa = [{"Url": "https://www.zhihu.com/question/1/answer/2", "Summary": "很短的摘要"}]
    evs, report = normalize_batch([], [], qa)
    assert len(evs) == 1
    assert evs[0].content_type == "search_snippet"
    assert evs[0].retrieval_channel == "question_answers"


def test_max_items_truncates():
    items = [{"Title": f"t{i}", "Url": f"https://a.com/{i}", "ContentText": f"内容{i}"} for i in range(10)]
    evs, report = normalize_batch(items, [], [], max_items=3)
    assert len(evs) == 3
    assert report.total_raw == 10


def test_json_payload_shape(real_items):
    zh, gl = real_items
    evs, report = normalize_batch(zh, gl)
    payload = to_json_payload(evs, report)
    assert payload["count"] == len(evs)
    assert len(payload["items"]) == len(evs)
    assert payload["report"]["total_raw"] == 20
    json.dumps(payload)


def test_merge_keeps_all_channels(real_items):
    """同一来源被两个接口都找到时，两个渠道都要保留。

    只留一个会导致无法统计「全网搜索带来了多少真正的新来源」。
    """
    zh, gl = real_items
    evs, report = normalize_batch(zh, gl)

    multi = [e for e in evs if len(e.retrieval_channels) > 1]
    assert len(multi) == 4, f"应有 4 条被双渠道命中，实际 {len(multi)}"
    for e in multi:
        assert set(e.retrieval_channels) == {"zhihu_search", "global_search"}
        assert e.retrieval_channel in e.retrieval_channels


def test_channel_contribution_is_countable(real_items):
    """能分出「仅知乎」「仅全网」「两者都有」，用于评估渠道价值。"""
    zh, gl = real_items
    evs, _ = normalize_batch(zh, gl)

    only_zh = sum(1 for e in evs if e.retrieval_channels == ["zhihu_search"])
    only_global = sum(1 for e in evs if e.retrieval_channels == ["global_search"])
    both = sum(1 for e in evs if len(e.retrieval_channels) > 1)

    assert (only_zh, only_global, both) == (6, 6, 4)
    # 全网搜索贡献了 6 条独有来源，不是纯重复劳动
    assert only_global > 0
