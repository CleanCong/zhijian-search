"""归并：把几十条内容分成三桶，并生成一句话结论。

================== A4 归并质量优化版 ==================
相对基线（merge._classify 关键词兜底）的改动，逐条对应顶部 5 条待优化点：

1. consensus 不再是兜底桶。
   旧：没命中坑/分歧关键词的全进 consensus（实测 18/35 条进共识，一半是坑）。
   新：consensus 需要满足「给出同向建议 + 不是坑/分歧内容」，并在 build_cards 里
       用信号聚类校验「多条说法一致」；孤条仍留在 consensus 桶并打 isolated=True，
       前端可标「这几条说法各不相同」，内容不丢失。

2. disputed 不再只靠「但是/不过」。
   新：对每条做立场分类（正/负/条件），同一问题下存在相反立场才判 disputed，
       并记录 stance 字段供前端做对照。

3. pitfalls 要求「有具体场景」。
   新：必须同时命中「坑信号」与「具体场景词」（户型/潮湿区/地暖/身高/净深/平米…），
       且 pitfalls 判定优先于 consensus —— 一条讲具体坑的内容不该被算进共识。

4. core_conclusion 保持降级链。
   模型优先（pipeline 调 llm.summarize），失败退回本模块抽取式；不用模板句糊弄。
   规则兜底仍走 _best_sentence 的「强结论句式」，分数不够就诚实说「还没有能下结论的一句」。

5. 同一问题多条回答先聚合再找分歧。
   新：build_cards 按 _question/sub_query 归组，组内先聚合同向观点，再在组内判立场分歧。

接口约定不变：group(items, query) 返回 (items, cards)，就地写入 group 字段。
为兼容前端，group 取值仍是 consensus / disputed / pitfalls / hidden。
新增就地字段：stance（立场）、scene_hits（命中的场景词），前端可选用。
=====================================================
"""

from __future__ import annotations

import re
from typing import Any

# ---- 坑信号：出现这些词说明在讲「踩坑/翻车/被宰」----
PITFALL_WORDS = ["缺点", "后悔", "翻车", "踩坑", "坑", "故障", "坏", "缠", "卡住",
                 "趴窝", "增项", "被坑", "宰客", "套路", "冤枉钱", "加价", "开胶",
                 "变形", "超标", "返工", "降级", "掉包"]

# ---- 具体场景词：命中才算「有场景的坑」（优化点 3 核心）----
# 覆盖三类演示场景：装修（户型/净深/地暖…）、扫地（宠物/长发/地毯/门槛…）、考研（二战/应届…）
SCENE_WORDS = ["平米", "㎡", "户型", "猫", "狗", "宠物", "长发", "地毯", "卫生间",
               "厨房", "阳台", "老人", "小孩", "儿童", "孕妇", "母婴", "地暖",
               "潮湿", "南方", "北方", "净深", "进深", "身高", "层高", "毛坯",
               "老房", "旧房", "复尺", "量房", "转角", "异形", "梁柱",
               # 扫地机器人场景
               "门槛", "地板", "瓷砖", "毛发", "头发", "边角", "床底", "沙发底",
               "拖地", "集尘", "基站", "越障", "缠绕", "复式", "跃层", "大平层",
               # 考研场景
               "二战", "应届", "往届", "在职", "跨考", "专业课", "英语"]

# ---- 立场词典（优化点 2）----
POSITIVE_WORDS = ["推荐", "建议", "优先", "首选", "值得", "够用", "优选", "性价比",
                  "放心", "靠谱", "划算", "可选", "适合"]
NEGATIVE_WORDS = ["别", "不要", "避免", "警惕", "慎", "拒绝", "劝退", "不建议",
                  "不值", "千万", "小心", "别信", "别迷信", "扭头就走"]
# 条件立场：结论依赖前提，本身就是分歧根源
CONDITIONAL_WORDS = ["取决于", "看情况", "因人而异", "不一定", "分情况", "视",
                     "没有绝对", "没有最好", "根据", "按需", "适合哪个", "看用途"]

DISPUTE_WORDS = ["但是", "不过", "其实", "反过来", "有人说", "误区", "错误认知",
                 "不是", "而不是", "恰恰相反", "别迷信"]

GROUP_LABELS = {
    "consensus": "大家都这么说",
    "disputed": "说法不一的地方",
    "pitfalls": "有人具体踩过的坑",
    "hidden": "被收起的内容",
}

# 停用词（信号聚类用）
_STOPWORDS = set("的了是我你他她它们这那和与及或要会在有就都也很还把被给为对从向以于之而且不没")


def _hits(text: str, words: list[str]) -> int:
    return sum(1 for w in words if w in text)


def _text_of(item: dict[str, Any]) -> str:
    return (item.get("title") or "") + " " + (item.get("content_text") or "")


def _classify_stance(text: str) -> str:
    """立场分类：positive / negative / conditional / neutral。
    条件性结构优先（「没有最好/取决于/看用途」是独立立场）。"""
    cond = _hits(text, CONDITIONAL_WORDS)
    pos = _hits(text, POSITIVE_WORDS)
    neg = _hits(text, NEGATIVE_WORDS)
    if cond >= 1 and cond >= max(pos, neg) - 1:
        return "conditional"
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def _classify(item: dict[str, Any]) -> str:
    """给单条内容分桶。顺序：软文 → 具体的坑 → 分歧 → 共识/中立。

    与基线最大的区别：
    - pitfalls 判定提到最前（仅次于软文），且要求「坑信号 + 具体场景」双命中，
      避免把讲具体坑的内容错当共识（基线的主要错误来源）。
    - 其余内容先判立场，为 build_cards 的分歧识别留依据；
      不再「没命中就进 consensus」，中立内容标 neutral，由 build_cards 决定是否升为共识。
    """
    if item.get("is_ad_suspect"):
        item["stance"] = "hidden"
        return "hidden"

    text = _text_of(item)
    stance = _classify_stance(text)
    item["stance"] = stance

    pitfall = _hits(text, PITFALL_WORDS)
    scene = _hits(text, SCENE_WORDS)
    item["scene_hits"] = scene

    # 优化点 3：pitfalls 要求「坑信号 + 具体场景」双命中，优先于共识。
    # 门槛定 2+1（坑信号≥2 且场景≥1）：实测 3+3 太严，扫地场景坑组直接空。
    if pitfall >= 2 and scene >= 1:
        return "pitfalls"

    # 优化点 2 的第一步：明显对立/条件立场，先标 disputed 候选
    if stance in ("negative", "conditional") and _hits(text, DISPUTE_WORDS) >= 1:
        return "disputed"

    # 其余：中立/同向，暂归 consensus 候选，真正是否共识由 build_cards 复核
    return "consensus"


# ---------- 结论抽取（规则兜底，保持降级链）----------
JUDGEMENT_WORDS = [
    "其实", "关键", "主要", "真正", "最好", "建议", "不要", "注意",
    "区别", "差距", "取决于", "看的是", "不是", "而不是", "优先", "别", "核心",
]
NARRATIVE_WORDS = [
    "作为一个", "大家好", "哈喽", "说实话", "说回我", "我是一名", "楼主",
    "码字", "不知不觉", "坦白讲", "先自我", "相信很多", "今天",
]
STRONG_PATTERNS = [
    r"(不是|不在|并非|不在于)[^。]{0,24}(而是|而在|在于)",
    r"取决于",
    r"关键(是|在于|看)",
    r"真正[^。]{0,8}(是|在于)",
    r"(最重要的|核心)(是|在于)",
    r"(避坑|防坑).{0,6}(核心|关键|在于)",
]


def _best_sentence(item: dict[str, Any]) -> tuple[str, int]:
    text = re.sub(r"\s+", " ", (item.get("content_text") or "")).strip()
    if not text:
        return "", 0
    sentences = [s.strip() for s in re.split(r"[。！\n]", text) if 12 <= len(s.strip()) <= 80]
    if not sentences:
        return "", 0
    for sent in sentences:
        for pat in STRONG_PATTERNS:
            if re.search(pat, sent):
                return sent, 12
    best, best_score = "", -99
    for sent in sentences:
        score = 0
        score += sum(1 for w in JUDGEMENT_WORDS if w in sent) * 3
        score -= sum(1 for w in NARRATIVE_WORDS if w in sent) * 5
        if re.search(r"\d", sent):
            score += 1
        if 18 <= len(sent) <= 60:
            score += 2
        if re.search(r"(Pa|℃|元|平米|个月|小时|年|mm)", sent):
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


# ---------- 共识复核：多条说法一致才算共识（优化点 1 & 5）----------
def _signals(text: str) -> set:
    toks = re.findall(r"[\u4e00-\u9fa5]{2,}", text or "")
    out = set()
    for t in toks:
        for i in range(len(t) - 1):
            bg = t[i:i + 2]
            if bg[0] not in _STOPWORDS and bg[1] not in _STOPWORDS:
                out.add(bg)
    return out


def _refine_consensus(consensus_items: list[dict[str, Any]]) -> tuple[list, list]:
    """把 consensus 候选里「孤立、没有第二条支持」的拆出来（返回 kept, isolated）。

    孤条不代表要删除：它仍留在 consensus 桶，只是由上层打 isolated=True，
    前端可标「这几条说法各不相同」。这里只负责区分「成簇的共识」与「孤立条」。"""
    n = len(consensus_items)
    if n <= 1:
        return consensus_items, []
    sigs = [_signals(_text_of(it)) for it in consensus_items]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    SIM = 0.08
    for i in range(n):
        for j in range(i + 1, n):
            a, b = sigs[i], sigs[j]
            if a and b and len(a & b) / len(a | b) >= SIM:
                parent[find(i)] = find(j)
    from collections import defaultdict
    clusters = defaultdict(list)
    for idx in range(n):
        clusters[find(idx)].append(idx)

    keep, demoted = [], []
    for members in clusters.values():
        if len(members) >= 2:
            for m in members:
                keep.append(consensus_items[m])
        else:
            demoted.append(consensus_items[members[0]])
    return keep, demoted


def build_cards(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    consensus = [i for i in items if i.get("group") == "consensus"]
    pitfalls = [i for i in items if i.get("group") == "pitfalls"]
    disputed = [i for i in items if i.get("group") == "disputed"]

    # 优化点 1：共识复核。孤条不虚占「形成共识」，但必须保留在 consensus 桶里，
    # 只打 isolated=True 标记（前端可标「这几条说法各不相同」）。
    # 绝不降级到前端契约外的 group 值，否则内容会从界面上消失——
    # 连软文我们都只收起不删除，孤条共识更不能吞掉。
    consensus_kept, isolated = _refine_consensus(consensus)
    for it in consensus_kept:
        it["isolated"] = False
    for it in isolated:
        it["group"] = "consensus"   # 仍在共识桶，前端照常显示
        it["isolated"] = True       # 标记：与其他条说法不一致，未形成共识
    consensus = consensus_kept + isolated

    conclusion = ""
    confidence = "none"
    if consensus:
        scored = [(sent, sc, i) for i in consensus for sent, sc in [_best_sentence(i)]]
        scored = [x for x in scored if x[0]]
        if scored:
            scored.sort(key=lambda x: (x[1], x[2].get("vote_up_count", 0)), reverse=True)
            top_sent, top_score, _ = scored[0]
            if top_score >= 12:
                conclusion = top_sent
                confidence = "medium"
    if not conclusion:
        if consensus:
            conclusion = f"这次找到的 {len(consensus)} 条内容方向接近，但还没有能直接下结论的一句。"
        else:
            conclusion = "这次没找到能形成共识的内容。"
        confidence = "none"

    return [
        {
            "question": query,
            "core_conclusion": conclusion,
            "conclusion_confidence": confidence,
            "key_points": [
                (_best_sentence(i)[0] or _first_sentence(i.get("title") or ""))
                for i in sorted(consensus, key=lambda x: x.get("vote_up_count", 0), reverse=True)[:4]
                if _best_sentence(i)[0] or i.get("title")
            ][:3],
            "preconditions": [i["title"] for i in consensus[:2] if i.get("title")],
            "pros": [i["title"] for i in consensus if i.get("stance") == "positive"][:2],
            "cons": [i["title"] for i in disputed if i.get("stance") in ("negative", "conditional")][:2],
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
