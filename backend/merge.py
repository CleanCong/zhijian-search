"""归并：把几十条内容分成三桶，并生成一句话结论。

================== 给接手开发的说明 ==================
这是**基线版本**，规则很粗，目的只是让前端有结构可渲染。

待优化的地方（按优先级）：
1. consensus 目前是「兜底桶」，凡是没命中关键词的都进去，
   这不准。应该改成「多条内容语义一致才进共识」。
   可行做法：对正文做关键词/向量相似度聚类，同一簇 ≥2 条才算共识。
2. disputed 现在靠「但是/不过」这类转折词判断，太浅。
   真正该做的是：同一问题下找出结论相反的条目（如 A 说值、B 说不值），
   可以用立场分类（正/负/中性）后配对比。
3. pitfalls 靠「后悔/翻车」判断，会把抱怨内容也算进来。
   应该要求「有具体场景」才算坑（出现面积/户型/宠物/长发等具体条件词）。
4. core_conclusion 现在是模板拼接，只是占位。
   若能接模型，应该真正做抽取式摘要；不能接就保持抽取式（取共识里
   信息量最高的一句），不要用模板句糊弄。
5. 同一问题多条回答（考研场景实测出现过 3 条同题）需要先按问题聚合，
   再在问题内部找分歧。

接口约定不要改：group(items) 返回 items（就地写入 group 字段）和 cards 列表。
=====================================================
"""

from __future__ import annotations

import re
from typing import Any

# 三桶关键词（粗糙版，待替换为语义方法）
PITFALL_WORDS = ["缺点", "后悔", "翻车", "踩坑", "坑", "问题", "故障", "坏", "缠", "卡住", "趴窝"]
DISPUTE_WORDS = ["但是", "不过", "因人而异", "看情况", "取决于", "不一定", "其实", "反过来", "有人说"]
SCENE_WORDS = ["平米", "户型", "猫", "狗", "宠物", "长发", "地毯", "卫生间", "阳台", "老人", "小孩"]

GROUP_LABELS = {
    "consensus": "大家都这么说",
    "disputed": "说法不一的地方",
    "pitfalls": "有人具体踩过的坑",
    "hidden": "被收起的内容",
}


def _hits(text: str, words: list[str]) -> int:
    return sum(1 for w in words if w in text)


def _classify(item: dict[str, Any]) -> str:
    """给单条内容分桶。顺序很重要：先排除软文，再判断具体性。"""
    if item.get("is_ad_suspect"):
        return "hidden"

    text = (item.get("title") or "") + (item.get("content_text") or "")

    pitfall = _hits(text, PITFALL_WORDS)
    dispute = _hits(text, DISPUTE_WORDS)
    scene = _hits(text, SCENE_WORDS)

    # 「坑」的判定要严：必须既有负面信号，又有具体场景。
    # 只要提到「问题」两个字就归进坑，会把一半内容都塞进去（实测踩过）。
    if pitfall >= 2 and scene >= 1:
        return "pitfalls"
    if dispute >= 2:
        return "disputed"
    return "consensus"


# 判断句的标志词：出现这些词，说明这句话在给结论，而不是在铺垫
JUDGEMENT_WORDS = [
    "其实", "关键", "主要", "真正", "最好", "建议", "不要", "注意",
    "区别", "差距", "取决于", "看的是", "不是", "而不是", "优先", "别",
]

# 叙事/开场白的标志词：这类句子信息量为零，必须压下去
NARRATIVE_WORDS = [
    "作为一个", "大家好", "哈喽", "说实话", "说回我", "我是一名", "楼主",
    "码字", "不知不觉", "说实话", "坦白讲", "先自我", "相信很多",
]


# 强结论句式。真正的判断句几乎都长这样：
# 「差距不在吸力，而在拖地方式」「取决于你家的户型」「关键是耗材成本」
STRONG_PATTERNS = [
    r"(不是|不在|并非|不在于)[^。]{0,24}(而是|而在|在于)",
    r"取决于",
    r"关键(是|在于|看)",
    r"真正[^。]{0,8}(是|在于)",
    r"(最重要的|核心)(是|在于)",
]


def _best_sentence(item: dict[str, Any]) -> tuple[str, int]:
    """从正文里挑一句最像「结论」的话，返回 (句子, 得分)。

    两轮：
    第一轮只找强结论句式（「不在…而在…」这类），命中就直接用；
    第二轮才退回到词频打分，并且分数不够高时上层会降级为诚实表述。

    为什么这么麻烦：实测取第一句会拿到「作为一个被KPI追着跑的牛马」，
    纯词频打分会拿到吐槽句。规则法抓不住「结论」这个概念，
    所以宁可承认抓不到，也不要硬凑一句。
    """
    text = re.sub(r"\s+", " ", (item.get("content_text") or "")).strip()
    if not text:
        return "", 0

    sentences = [s.strip() for s in re.split(r"[。！\n]", text) if 12 <= len(s.strip()) <= 80]
    if not sentences:
        return "", 0

    # 第一轮：强结论句式
    for sent in sentences:
        for pat in STRONG_PATTERNS:
            if re.search(pat, sent):
                return sent, 12

    # 第二轮：词频打分
    best, best_score = "", -99
    for sent in sentences:
        score = 0
        score += sum(1 for w in JUDGEMENT_WORDS if w in sent) * 3
        score -= sum(1 for w in NARRATIVE_WORDS if w in sent) * 5
        if re.search(r"\d", sent):
            score += 1
        if 18 <= len(sent) <= 60:
            score += 2
        if re.search(r"(Pa|℃|元|平米|个月|小时|年)", sent):
            score += 2
        if sent.startswith("我") or sent.startswith("你"):
            score -= 1
        if score > best_score:
            best, best_score = sent, score

    return best, best_score


def _first_sentence(text: str, limit: int = 46) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    for sep in ["。", "！", "？", "\n"]:
        idx = text.find(sep)
        if 0 < idx <= limit:
            return text[: idx + 1]
    return text[:limit] + ("…" if len(text) > limit else "")


def build_cards(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """生成结构化卡片。

    TODO（待优化）：core_conclusion 现在是「取共识第一条的第一句话」，
    只是占位。应改为抽取式摘要——从多条共识里挑信息密度最高的一句，
    并把「适用前提」从具体场景词中提取（面积/户型/宠物等）。
    """
    consensus = [i for i in items if i.get("group") == "consensus"]
    pitfalls = [i for i in items if i.get("group") == "pitfalls"]
    disputed = [i for i in items if i.get("group") == "disputed"]

    conclusion = ""
    confidence = "none"
    if consensus:
        scored = [(sent, sc, i) for i in consensus for sent, sc in [_best_sentence(i)]]
        scored = [x for x in scored if x[0]]
        if scored:
            scored.sort(key=lambda x: (x[1], x[2].get("vote_up_count", 0)), reverse=True)
            top_sent, top_score, _ = scored[0]
            # 只有命中强结论句式（12 分）才敢把它当成结论。
            # 实测：词频打分会选出「直奔吸尘器而去，看来还有点蒙圈」这种吐槽句，
            # 与其硬凑一句像结论的话，不如老实说「这批内容还没有能下结论的一句」。
            if top_score >= 12:
                conclusion = top_sent
                confidence = "medium"
    if not conclusion:
        if consensus:
            conclusion = f"这次找到的 {len(consensus)} 条内容各说各的，还没有能直接下结论的一句。"
        else:
            conclusion = "这次没找到能形成共识的内容。"
        confidence = "none"

    return [
        {
            "question": query,
            "core_conclusion": conclusion,
            "conclusion_confidence": confidence,
            # 结论抽不出来时，前端改用「这几条在讲什么」的要点列表。
            # 要点也走同一套选句逻辑，不能退回「取第一句」——实测那样会拿到「来来来！」
            "key_points": [
                (_best_sentence(i)[0] or _first_sentence(i.get("title") or ""))
                for i in sorted(consensus, key=lambda x: x.get("vote_up_count", 0), reverse=True)[:4]
                if _best_sentence(i)[0] or i.get("title")
            ][:3],
            "preconditions": [i["title"] for i in consensus[:2] if i.get("title")],
            "pros": [i["title"] for i in consensus[2:4] if i.get("title")],
            "cons": [i["title"] for i in disputed[:2] if i.get("title")],
            "pitfalls": [_first_sentence(i.get("content_text") or "") for i in pitfalls[:3]],
            "counts": {
                "consensus": len(consensus),
                "disputed": len(disputed),
                "pitfalls": len(pitfalls),
                "hidden": sum(1 for i in items if i.get("group") == "hidden"),
            },
        }
    ]


def group(items: list[dict[str, Any]], query: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """就地写入 group 字段，并返回 (items, cards)。"""
    for item in items:
        item["group"] = _classify(item)
    return items, build_cards(items, query)
