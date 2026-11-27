"""Evidence：一条来源材料。

不管来自知乎搜索、全网搜索还是知乎直答，统一成这个结构。

三个容易混淆的字段，这里一次说清：

1. source_type —— 材料从哪个渠道来的
   zhihu             知乎搜索（社区观点、经验、回答片段）
   web               全网搜索（官网、新闻、科普站）
   paper             论文/学术来源
   official          官方机构（政府、企业官网）
   zhihu_direct_answer  知乎直答生成的综合答案（AI 生成，不可单独作证）

2. content_type —— 这条材料的形态，不是来源
   answer_excerpt    回答正文节选（search zhihu 的 ContentText）
   search_snippet    搜索摘要（question answers 的 Summary，约 74 字）
   web_snippet       网页正文节选
   summary           摘要
   direct_answer     直答生成的完整答案

   **不能混淆**：把 search_snippet 当 answer_excerpt 用，会误以为材料很充分。

3. independence_key —— 判断「多条来源」是否真的独立
   转载、摘编、直答与其引用的原文，independence_key 必须相同。
   否则会把「直答 + 它引用的三篇回答」算成 4 个独立来源，得出虚假的交叉验证。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["zhihu", "web", "paper", "official", "zhihu_direct_answer"]
ContentType = Literal["answer_excerpt", "search_snippet", "web_snippet", "summary", "direct_answer"]

SOURCE_LABELS = {
    "zhihu": "知乎",
    "web": "网页",
    "paper": "论文",
    "official": "官方",
    "zhihu_direct_answer": "AI 综合",
}


class Evidence(BaseModel):
    """一条来源材料。一次性构建，之后只读。"""

    evidence_id: str
    source_type: SourceType
    source_name: str = ""
    title: str = ""
    url: str | None = None
    author: str | None = None
    content: str
    content_type: ContentType
    published_at: str | None = None
    source_question_id: str | None = None

    # 暂由规则判定，不让模型打分（见 rules.json）。
    # 首期只用于排序与展示，不参与 evidence_status 计算。
    credibility: Literal["high", "medium", "low"] | None = None

    # 判断独立性的分组键，默认用 URL 域名+路径指纹
    independence_key: str = ""

    # 原始响应留档，便于事后复核
    raw_metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("content 不能为空，空内容不应构造成 Evidence")
        return v.strip()

    def model_post_init(self, __context) -> None:
        if not self.source_name:
            object.__setattr__(self, "source_name", SOURCE_LABELS.get(self.source_type, ""))
        # 没显式给就自动生成，避免空键导致独立性计数丢失
        if not self.independence_key:
            object.__setattr__(
                self, "independence_key",
                make_independence_key(self.source_type, self.url, self.content),
            )

    @property
    def is_ai_generated(self) -> bool:
        """AI 生成的内容不能计入「多来源印证」。"""
        return self.source_type == "zhihu_direct_answer" or self.content_type == "direct_answer"

    @property
    def content_chars(self) -> int:
        return len(self.content)

    def summary_line(self, limit: int = 60) -> str:
        text = re.sub(r"\s+", " ", self.content).strip()
        return text[:limit] + ("…" if len(text) > limit else "")


def make_independence_key(source_type: str, url: str | None, content: str) -> str:
    """生成独立性分组键。

    规则（按优先级）：
    1. 有 URL —— **只看 URL**，归一化后取指纹。
       同一 URL 无论抓几次、内容截断位置是否不同，都必须算同一个来源。
       （早期版本把内容指纹也拼进去，导致同页两次抓取被算成两条独立证据。）
    2. 无 URL —— 用内容指纹，便于把直答和它的引用做人工归并。
    """
    if url:
        try:
            p = urlparse(url)
            host = p.netloc.lower().removeprefix("www.")
            path = p.path.rstrip("/")
            base = f"{host}{path}"
        except Exception:
            base = url
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
        # 不带 source_type 前缀：同一篇回答无论从知乎搜索还是全网搜索进来，
        # 都必须是同一个键，否则会被算成两条独立证据。
        return f"url:{digest}"

    body = re.sub(r"\s+", "", content or "")[:200]
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return f"content:{digest}"


def extract_question_id(url: str | None) -> str | None:
    """从知乎回答链接里取 question id。"""
    if not url:
        return None
    m = re.search(r"zhihu\.com/question/(\d+)", url)
    return m.group(1) if m else None


def _ts_to_date(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


def from_zhihu_search(raw: dict, idx: int = 0) -> Evidence:
    """把 search zhihu 的单条原始结果转成 Evidence。

    原始字段（18 个）里我们保留 9 个，其余进 raw_metadata 留档。
    特别保留 AuthorityLevel / RankingScore —— 平台算好的质量信号，
    现有系统丢弃了，这里补回来供后续加权使用。
    """
    url = raw.get("Url")
    content = (raw.get("ContentText") or "").strip()
    return Evidence(
        evidence_id=f"ev_zh_{idx:04d}",
        source_type="zhihu",
        title=(raw.get("Title") or "").strip(),
        url=url,
        author=(raw.get("AuthorName") or raw.get("AuthorSignature") or None),
        content=content,
        content_type="answer_excerpt",
        published_at=_ts_to_date(raw.get("CreateTime") or raw.get("EditTime")),
        source_question_id=extract_question_id(url),
        independence_key=make_independence_key("zhihu", url, content),
        raw_metadata={
            "content_id": raw.get("ContentID"),
            "content_type": raw.get("ContentType"),
            "vote_up": raw.get("VoteUpCount"),
            "comment_count": raw.get("CommentCount"),
            "authority_level": raw.get("AuthorityLevel"),
            "ranking_score": raw.get("RankingScore"),
            "author_badge": raw.get("AuthorBadgeText"),
        },
    )


ZHIHU_HOSTS = {"zhihu.com", "zhuanlan.zhihu.com", "www.zhihu.com"}


def _is_zhihu_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return False
    return host in {"zhihu.com", "zhuanlan.zhihu.com"}


def from_global_search(raw: dict, idx: int = 0) -> Evidence:
    """把 search global 的单条结果转成 Evidence。

    实测发现全网搜索里有相当比例的结果来自知乎域名（10 条中有 6 条），
    这些必须归为 zhihu 来源，否则同一篇回答会被当成「两个独立来源」。
    """
    url = raw.get("Url")
    content = (raw.get("ContentText") or "").strip()
    host = ""
    if url:
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
    is_zhihu = _is_zhihu_url(url)
    return Evidence(
        evidence_id=f"ev_{'zh' if is_zhihu else 'web'}_{idx:04d}",
        source_type="zhihu" if is_zhihu else "web",
        source_name=host or "网页",
        title=(raw.get("Title") or "").strip(),
        url=url,
        author=(raw.get("AuthorName") or None),
        content=content,
        content_type="web_snippet",
        published_at=_ts_to_date(raw.get("EditTime")),
        independence_key=make_independence_key("web", url, content),
        raw_metadata={
            "content_id": raw.get("ContentID"),
            "vote_up": raw.get("VoteUpCount"),
            "authority_level": raw.get("AuthorityLevel"),
            "ranking_score": raw.get("RankingScore"),
        },
    )


def from_question_answer(raw: dict, idx: int = 0) -> Evidence:
    """question answers 的 Summary（约 74 字，无标题无作者）。

    刻意标记为 search_snippet —— 它太短，不能当 answer_excerpt 用，
    否则下游会误判材料充分度。
    """
    url = raw.get("Url")
    content = (raw.get("Summary") or "").strip()
    return Evidence(
        evidence_id=f"ev_qa_{idx:04d}",
        source_type="zhihu",
        title="",
        url=url,
        author=None,
        content=content,
        content_type="search_snippet",
        published_at=None,
        source_question_id=extract_question_id(url),
        independence_key=make_independence_key("zhihu", url, content),
        raw_metadata={"content_token": raw.get("ContentToken")},
    )
