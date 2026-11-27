# 详细模块与接口定义

> 项目：Vibe 科普大赏
> 版本：v1.0
> 日期：2026-11-26

---

## 一、目录结构

```
zhijian-search/
├── main.py                      # FastAPI 入口，保留
├── pipeline.py                  # 兼容层：原流程入口（旧接口不破坏）
│
├── api/
│   ├── routes/
│   │   ├── generation.py        # 科普生成（异步任务）
│   │   ├── knowledge.py         # 知识包读取
│   │   ├── dialogue.py          # 追问
│   │   ├── discovery.py         # 关联问题、热点
│   │   └── health.py
│   └── schemas/
│       ├── request.py
│       └── response.py
│
├── retrieval/
│   ├── query_planner.py         # 问题理解 → 检索计划
│   ├── source_router.py         # 来源选择与预算分配
│   ├── zhihu_adapter.py         # 知乎搜索（继承 backend/zhihu.py）
│   ├── global_adapter.py        # 全网搜索（新）
│   ├── answer_adapter.py        # 回答摘要 / 直答（新）
│   ├── hotlist_adapter.py       # 热榜
│   ├── discovery_adapter.py     # 问题发现
│   ├── result_normalizer.py     # 统一为 Evidence
│   ├── result_deduplicator.py   # 去重
│   ├── retrieval_budget.py      # 额度预算与记录
│   ├── rate_limiter.py          # 串行控制
│   └── content_cache.py         # L1/L2 缓存
│
├── evidence/
│   ├── claim_extractor.py       # 材料 → 原子 Claim
│   ├── evidence_matcher.py      # Claim ↔ Evidence 关联
│   ├── evidence_validator.py    # 计算 evidence_status
│   ├── conflict_detector.py     # 冲突与立场识别
│   └── coverage_checker.py      # 覆盖检查
│
├── knowledge/
│   ├── knowledge_builder.py     # 组装 Knowledge Package
│   ├── consensus_analyzer.py
│   ├── analogy_generator.py
│   ├── glossary_generator.py
│   └── related_question_generator.py
│
├── generation/
│   ├── script_generator.py      # 按深度生成脚本
│   ├── depth_adapter.py         # 深度策略
│   ├── scene_generator.py       # Scene 结构化
│   ├── tts_adapter.py           # 分段 TTS
│   └── audio_manager.py         # 音频文件管理
│
├── dialogue/
│   ├── context_builder.py
│   └── question_answerer.py
│
├── models/                      # Pydantic 数据契约
│   ├── retrieval.py
│   ├── claim.py
│   ├── evidence.py
│   ├── knowledge.py
│   └── script.py
│
├── storage/
│   ├── database.py              # SQLite 连接与 WAL
│   ├── repositories/
│   └── migrations/
│
├── prompts/                     # Prompt 外置，便于版本管理
│   ├── query_planner.md
│   ├── claim_extractor.md
│   ├── knowledge_builder.md
│   ├── script_generator.md
│   └── dialogue.md
│
├── tests/
└── data/
    ├── cache/
    └── raw/                     # 现有 70 条真实返回，作测试 fixture
```

> 这是目标结构，不是第一天的任务清单。按阶段逐步落地，先建核心模块。

---

## 二、检索层

| 模块 | 职责 | 接口 |
|---|---|---|
| `query_planner` | 理解问题，产出检索计划 | `plan(query, depth) -> RetrievalPlan` |
| `source_router` | 依计划选择来源、分配预算 | `route(plan) -> SourcePlan` |
| `zhihu_adapter` | 调知乎搜索 | `search(query, count) -> list[RawItem]` |
| `global_adapter` | 调全网搜索 | `search(query, count) -> list[RawItem]` |
| `answer_adapter` | 回答摘要 / 直答 | `get_summaries(qid)` / `direct_answer(query)` |
| `result_normalizer` | 统一为 Evidence | `normalize(raw, source_type) -> Evidence` |
| `result_deduplicator` | 去重 | `deduplicate(items) -> list[Evidence]` |
| `retrieval_budget` | 预算管理 | `reserve(api_id, n)` / `record(api_id, used)` |
| `rate_limiter` | 串行控制 | `acquire()` / `release()` |
| `content_cache` | L1/L2 缓存 | `get(key)` / `set(key, value)` |

### 检索计划的数据结构

```python
class RetrievalPlan(BaseModel):
    intent: str                       # concept_explanation / mechanism / comparison / controversy
    sub_queries: list[str]
    sources: SourceFlags              # zhihu / global / direct_answer / hotlist / discovery
    budget: dict[str, int]            # {"zhihu_search": 4, "global_search": 1, ...}
```

示例：

```json
{
  "intent": "concept_explanation",
  "sub_queries": [
    "量子纠缠的基本概念",
    "量子纠缠的物理原理",
    "量子纠缠有哪些实际应用",
    "量子纠缠存在什么争议"
  ],
  "sources": {"zhihu": true, "global": true, "direct_answer": true,
              "hotlist": false, "discovery": false},
  "budget": {"zhihu_search": 4, "global_search": 1, "direct_answer": 1}
}
```

> 这是结构示例，不是固定模板。实际规划由 LLM 依问题动态决定。

### 统一错误契约（重要）

所有 adapter **失败时抛 `RetrievalError`，不返回空列表**。

```python
class RetrievalError(Exception):
    def __init__(self, api_id: str, code: str, message: str, retryable: bool):
        ...
```

理由：空列表无法区分「真的没搜到」和「调用失败」。上层需要据此决定是降级、重试还是标记 `partial_failed`。

这一约定继承自现有 `zhihu.py` 的 `ZhihuError`，全项目统一。

---

## 三、Evidence 层

| 模块 | 职责 | 接口 |
|---|---|---|
| `claim_extractor` | 一篇材料拆出多条原子 Claim | `extract(evidence_list) -> list[Claim]` |
| `evidence_matcher` | Claim 与 Evidence 建关系 | `match(claims, evidence) -> list[ClaimEvidence]` |
| `evidence_validator` | 计算 `evidence_status` | `validate(claim, links) -> EvidenceStatus` |
| `conflict_detector` | 找出冲突与立场分歧 | `detect(claims) -> list[Conflict]` |
| `coverage_checker` | 检查核心问题是否被覆盖 | `check(query, claims) -> CoverageReport` |

### 核心变化：从「整篇归组」到「原子 Claim」

旧系统：一条回答 → 一个分组（共识/分歧/坑），且**一篇只能进一个桶**。

新系统：一篇回答拆出多条 Claim，**每条 Claim 独立关联证据**。

```
旧：回答 A（讲避障强 + 耗材贵 + 过不了矮家具）→ 只能进「坑」组
新：回答 A → Claim1（避障强）· Claim2（耗材贵）· Claim3（过矮家具受限）
              ↓ 各自匹配证据、各自计算覆盖状态
```

这是整个架构最关键的一步：**粒度从文档降到断言**。

迁移成本低——现有 `merge.py` 的坑信号词、场景词、立场词可直接复用。

---

## 四、Knowledge 层

| 模块 | 职责 | 接口 |
|---|---|---|
| `knowledge_builder` | 组装完整知识包 | `build(query, claims, evidence) -> KnowledgePackage` |
| `consensus_analyzer` | 分析共识与争议 | `analyze(claims) -> ConsensusResult` |
| `analogy_generator` | 生成核心类比 | `generate(claims, depth) -> Analogy` |
| `glossary_generator` | 提取 3–5 个关键术语 | `generate(claims) -> list[GlossaryItem]` |
| `related_question_generator` | 生成关联问题 | `generate(package) -> list[RelatedQuestion]` |

> `related_question_generator` 建议在构建知识包时**同步生成并持久化**，不要让 `GET /related` 触发实时生成。

---

## 五、内容生成层

| 模块 | 职责 | 接口 |
|---|---|---|
| `depth_adapter` | 深度策略（选材与展开方式） | `adapt(package, depth) -> DepthView` |
| `script_generator` | 生成时间轴脚本 | `generate(package, depth) -> Script` |
| `scene_generator` | Scene 结构化 | `build(script) -> list[Scene]` |
| `tts_adapter` | **按 Scene 分段**合成 | `synthesize(text) -> AudioResult` |
| `audio_manager` | 音频存储与 URL | `save(blob, scene_id) -> str` |

### TTS 必须按 Scene 分段

**不要**先生成一整段旁白再猜时间点。每个 Scene 独立合成，保存：

```
scene_id · audio_url · duration_ms · start_ms · end_ms
```

前端按**音频实际时长**排时间轴，而不是预设时间硬切。

理由：语速、标点停顿都会影响真实时长，硬切必然出现字幕和声音错位。

---

## 六、交互层

| 模块 | 职责 | 接口 |
|---|---|---|
| `context_builder` | 组装知识包与对话上下文 | `build_context(package_id, history)` |
| `question_answerer` | 处理追问 | `answer(question, context) -> Answer` |

**追问只返回文字与结构化卡片，不重新生成视频。**

追问时的检索策略：

```
1. 优先用当前 Knowledge Package 里的 Claim / Evidence 回答
2. 材料不足时，再由 query_planner 决定是否二次检索（受预算约束）
3. 回答中区分「有来源支持」与「解释或推断」
4. 返回 need_more_retrieval 标志，让前端知道是否已触发补充检索
```

---

## 七、核心数据模型

所有层之间传递 Pydantic 模型，**前端不消费 LLM 原始输出**。

### Evidence

```python
class Evidence(BaseModel):
    evidence_id: str
    source_type: str            # zhihu | web | zhihu_direct_answer
    source_name: str            # 知乎 / 官网 / 新闻
    title: str
    url: str | None
    author: str | None
    content: str
    content_type: str           # search_snippet | answer_summary | web_snippet | direct_answer
    published_at: str | None
    source_question_id: str | None
    credibility: str            # high | medium | low（沿用现有规则判定）
    independence_key: str       # 判独立性的分组键（见下）
```

**`independence_key` 的作用**：判断「多条来源」是否真的独立。

| 情况 | independence_key |
|---|---|
| 不同作者对不同问题的回答 | 各自 `evidence_id`（独立） |
| 同一内容的不同转载 | **相同**（不独立） |
| 知乎直答 + 它引用的那几篇回答 | **相同**（同源，不独立） |

`evidence_validator` 按这个键去重后再数，避免「直答 + 其引用」被算作交叉验证。

### Claim 与 ClaimEvidence（两个模型）

**来源独立 ≠ 内容支持。** 这是必须分开建模的两件事：

```python
class ClaimEvidence(BaseModel):
    claim_id: str
    evidence_id: str
    relation: str          # supports | refutes | supplements | mentions
    confidence: float|None # 关系判断的置信度，不是事实可信度
    rationale: str|None
```

| relation | 含义 | 计入支持数 |
|---|---|---|
| `supports` | 明确支持该主张 | ✅ |
| `refutes` | 明确反驳 | ❌ 单独计入反驳数 |
| `supplements` | 补充背景或机制 | ❌ |
| `mentions` | 仅提及，无明确论证 | ❌ |

```python
class Claim(BaseModel):
    claim_id: str
    claim_text: str
    claim_type: str             # fact | mechanism | experience | opinion | recommendation
    explanation: str
    evidence_ids: list[str]     # 冗余视图，真相在 claim_evidences

    # 两个独立的置信度，不可混为一谈
    model_confidence: str       # high | medium | low（模型自评）
    evidence_status: str        # supported | partially_supported | disputed | insufficient
    independent_source_count: int   # 独立【支持】来源数
    refuting_source_count: int      # 独立【反驳】来源数
    controversy: bool               # 由关系计算得出，不手工设置
```

### 两个置信度的区别（关键设计）

| | 谁决定 | 含义 | 能否单独作为「已验证」 |
|---|---|---|---|
| `model_confidence` | LLM 自评 | 模型对自己这句话有多确信 | ❌ 不可以 |
| `evidence_status` | **代码计算** | 实际有多少独立来源支撑 | ✅ 可以 |

> `coverage_status` 已合并进 `evidence_status`：两者都表达「证据够不够」，
> 分开存只会导致一处更新、一处忘记。

计算规则（**反证优先**）：

| 情况 | evidence_status |
|---|---|
| 存在 ≥1 个独立反驳来源 | `disputed` |
| 独立支持 ≥2 且无反驳 | `supported` |
| 独立支持 =1 且无反驳 | `partially_supported` |
| 无任何支持（只有补充/仅提及/无来源） | `insufficient` |

**一条有依据的反证足以推翻「已支持」。** 否则三条支持压过一条反对，
「存在争议」就永远不会被如实呈现。

**同一来源内部矛盾的处理**：同一 `independence_key` 下若同时存在
supports 与 refutes，该来源不计入任何一方，只记 `internal_conflicts`
并产生 warning —— 不能虚增独立来源数。

**展示层可以综合两者，但不能因为模型自信，就把缺证据的 Claim 标成已验证。**

这是防止幻觉的最后一道闸门。

### KnowledgePackage

```python
class KnowledgePackage(BaseModel):
    package_id: str
    query: str
    claims: list[Claim]
    evidence: list[Evidence]
    consensus: list[str]            # 建议存 claim_id，展示时 JOIN
    controversies: list[dict]
    unresolved_questions: list[str]
    analogy: dict
    glossary: list[dict]
    related_questions: list[dict]
    trace: list[dict]
    created_at: str
```

> `consensus` / `controversies` 建议存 **claim_id 数组**而非纯文本，避免与 `claims` 表不同步。

---

## 八、模块依赖规则

```
api  →  dialogue / knowledge / generation  →  evidence  →  retrieval  →  models
                                              ↓
                                          storage（仅 repositories 层）
```

三条硬约束：

1. **`retrieval` 不依赖 `evidence` 及以上**——它是纯取数层，不知道 Claim 是什么
2. **`models` 不依赖任何业务模块**——只含数据结构
3. **`storage` 只被 repositories 调用**——业务层不直接写 SQL

违反这三条会导致循环依赖，在 9–10 天的周期里很难回头改。
