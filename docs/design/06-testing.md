# 测试方案

> 项目：Vibe 科普大赏
> 版本：v1.0
> 日期：2026-11-26

---

## 一、测试策略

测试要验证的**不是"接口能不能返回结果"，而是"知识引擎可不可信"**。

四层结构，越往下越贵，越往下越重要：

```
        ┌─────────────────────┐
   第四层│ 端到端 / 前端验收    │  人工，Demo 前跑
        ├─────────────────────┤
   第三层│ 知识质量测试 ★最重要 │  题库 + 人工抽查
        ├─────────────────────┤
   第二层│ 集成测试（调用链）   │  自动，pytest
        ├─────────────────────┤
   第一层│ 单元测试（模块）     │  自动，pytest，最多
        └─────────────────────┘
```

**投入比例建议**：单元 30%、集成 30%、知识质量 40%。知识质量给最多时间，因为它决定产品是否成立。

---

## 二、单元测试

### 覆盖清单

| 模块 | 测试内容 | 预期 |
|---|---|---|
| `query_planner` | 不同类型问题 | 生成合理的子查询与预算 |
| `result_normalizer` | 知乎 / 全网 / 直答三种来源 | 统一成合法 `Evidence` |
| `result_deduplicator` | 重复 URL、转载内容 | 正确去重 |
| `claim_extractor` | 一篇含多观点的回答 | 拆出 ≥2 条 Claim |
| `evidence_matcher` | 多条材料支持同一 Claim | 正确建立 `supports` 关系 |
| `evidence_validator` | 独立来源 1 / 2 / 冲突 | 得到 `insufficient` / `supported` / `disputed` |
| `conflict_detector` | 相互矛盾的主张 | 标记为争议 |
| `coverage_checker` | 证据不足的 Claim | 标记 `insufficient` |
| `script_generator` | 三档深度 | 生成结构合法、内容有实质差异的脚本 |
| `scene_generator` | 各 `visual.type` | 输出符合 schema |
| `retrieval_budget` | 额度不足 | 停止非必要调用 |
| `cache key 生成` | 改 prompt 版本 | **key 必须变化** |
| `Evidence 独立性` | 同 URL 不同内容 / 带 utm 参数 | 必须算同一个来源 |
| `recalc_timeline` | 连续调用 3 次 | 总时长不累加，结果幂等 |
| `recalc_timeline` | 音频实际时长 ≠ 预估 | 以音频为准重算 |
| `finalize` 重入 | 增/删证据后再次 finalize | Claim 状态同步升/降级 |
| `assert_ready` | 未 finalize 就落库 | 抛错，不许带脏状态出门 |

### 测试数据

直接用现有的 `data/raw/*.json`（70 条真实接口返回）作 fixture，**不造假数据**：

```python
# tests/conftest.py
import json
from pathlib import Path
import pytest

RAW = Path(__file__).parent.parent / "data" / "raw"

@pytest.fixture
def raw_items():
    items = []
    for f in RAW.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        items.extend((data.get("Data") or {}).get("Items") or [])
    return items
```

### 关键用例示例

**① Claim 抽取：一篇多观点**

```python
def test_extract_multiple_claims(raw_items):
    """一条回答同时讲避障、耗材、通过性，应拆出多条 Claim。"""
    ev = normalize(raw_items[0], source_type="zhihu")
    claims = extract([ev])
    assert len(claims) >= 2
    assert all(c.evidence_ids for c in claims)      # 每条都有来源
```

**② evidence_status：独立性判定（对应 7.1）**

```python
def test_direct_answer_not_counted_as_independent():
    """知乎直答 + 它引用的回答，不能算两个独立来源。"""
    direct = make_evidence(source_type="zhihu_direct_answer",
                           independence_key="same")
    quoted = make_evidence(source_type="zhihu",
                           independence_key="same")
    claim = make_claim(evidence_ids=[direct.evidence_id, quoted.evidence_id])
    status = validate(claim, [direct, quoted])
    assert status.independent_source_count == 1
    assert status.evidence_status == "partially_supported"   # 不是 supported
```

**③ finalize 的调用边界**

`finalize()` 保留显式调用（因为证据可能分批加入），但增加 `assert_ready()` 兜底：
落库和返回前端之前必须调用，未 finalize 直接抛 `RuntimeError`，而不是带着
`insufficient` 的脏状态出门。

测试同时覆盖两种情况：**正常 finalize 后可用**，以及**未 finalize 时被拦住**。

**④ 缓存 key 必须随 prompt 版本变化（对应 7.4）**

```python
def test_cache_key_changes_with_prompt_version():
    k1 = build_key(query="q", prompt_version="v1")
    k2 = build_key(query="q", prompt_version="v2")
    assert k1 != k2, "改了 prompt 必须让旧缓存失效"
```

**④ 预算耗尽时停止调用**

```python
def test_budget_stops_calls():
    budget = RetrievalBudget({"zhihu_search": 2})
    budget.reserve("zhihu_search", 2)
    with pytest.raises(BudgetExhausted):
        budget.reserve("zhihu_search", 1)
```

---

## 三、集成测试

测完整调用链，**不 mock 内部模块**，只 mock 外部 API。

| 场景 | 验证点 |
|---|---|
| 纯知乎检索 | 能返回结果，Evidence 字段完整 |
| 知乎 + 全网 | 两类来源可统一处理，`source_type` 正确 |
| 知乎直答失败 | 不影响其他检索，任务仍可完成 |
| 部分子查询失败 | 保留成功结果，状态为 `partial_failed` |
| 摘要缺失 | 不伪造原文，`content` 为空时跳过该条 |
| LLM 返回非法 JSON | 重试或降级，不整个任务失败 |
| TTS 失败 | 保留文字与字幕，`audio_url` 为空也能播 |
| 缓存命中 | 不重复执行检索（断言 adapter 未被调用） |
| 切换深度 | 不重新检索、不重建知识包（断言 L3 缓存命中） |
| 任务取消 | 取消后不再发起新调用 |

### 示例：部分失败仍可完成

```python
def test_partial_failure_still_completes(monkeypatch):
    monkeypatch.setattr(global_adapter, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RetrievalError("global_search", "timeout", "超时", True)))
    result = run_task(query="量子纠缠是什么", depth="beginner")
    assert result.status == "partial_failed"
    assert result.package.claims                 # 知识包仍然产出
    assert any(w["source"] == "global_search" for w in result.warnings)
```

---

## 四、知识质量测试 ★

**这是最重要的部分。** 前面两层只能证明"代码跑通了"，这一层才证明"结果可信"。

### 4.1 固定测试题库

至少六个，覆盖不同问题类型：

| # | 问题 | 类型 | 考察点 |
|---|---|---|---|
| 1 | 什么是向量数据库？ | 概念解释 | 定义准确性、术语表 |
| 2 | 为什么飞机能够飞起来？ | 科学原理 | 机制链条 |
| 3 | 人工智能是否会取代程序员？ | 争议问题 | 立场保留、不强行下结论 |
| 4 | 扫地机器人应该怎么选？ | 消费经验 | 真实经验、具体数字 |
| 5 | 量子纠缠为什么不能超光速通信？ | 专业问题 | 深度差异、限定条件 |
| 6 | 某个冷门技术的最新研究进展？ | **材料不足** | 是否诚实说"不够" |

**第 6 题是反向测试**：正确答案是「材料不足」，如果系统硬给一个结论，说明它在编。

### 4.2 每个问题的十项检查

| # | 检查项 | 判定 |
|---|---|---|
| 1 | 每条 Claim 能否在来源材料中找到依据 | 逐条核对 |
| 2 | Evidence 的 URL 能否打开 | 抽查 |
| 3 | 同一回答的多观点是否正确拆分 | 看是否被压成一条 |
| 4 | 不同立场是否都保留 | 争议题必查 |
| 5 | 推测 / 经验 / 事实是否区分 | `claim_type` 是否合理 |
| 6 | 是否把知乎直答当独立证据 | 查 `independence_key` |
| 7 | 证据不足是否明确标记 | 查 `evidence_status` |
| 8 | 三档深度是否真有差异 | 对比文本，不能只换语气 |
| 9 | 类比是否帮助理解、不制造错误认知 | 人工判断 |
| 10 | 关联问题是否与主题相关 | 人工判断 |

### 4.3 反幻觉抽查（必做）

**随机抽 20 条最终展示的 Claim，逐条人工核对 Evidence。**

判定标准：

```
能找到直接来源支持  → 通过
只能找到间接关联    → 记为「弱支持」
完全找不到          → 记为「幻觉」，必须修复
```

**验收线：20 条中「完全找不到」≤ 2 条（90% 可溯源）。**

超过这个比例，说明 Claim 抽取在编造，必须回头调 prompt 或降低抽取激进程度。

### 4.4 置信度一致性测试（对应 7.1）

这是防止"模型自信但没依据"的专项测试：

```python
def test_model_confidence_cannot_override_evidence():
    """模型说 high，但只有一条来源，展示层不能用「多来源印证」措辞。"""
    claim = make_claim(model_confidence="high",
                       evidence_status="partially_supported",
                       independent_source_count=1)
    display = build_display(claim)
    assert display.badge != "多来源印证"
    assert display.must_show_single_source_hint is True
```

---

## 五、前端与端到端测试

| 功能 | 验收标准 |
|---|---|
| 问题输入 | 能创建任务，校验空值与超长 |
| 任务进度 | 六个阶段状态正确显示，`partial_failed` 不当错误处理 |
| 图文播放器 | Scene 顺序、字幕、画面切换正确 |
| 配音 | 音频可播放，字幕与音频对得上（误差 ≤0.5s） |
| 深度切换 | 三档可切，切换后不重新检索 |
| 来源展示 | 每个关键 Claim 可点开看到原文链接 |
| 术语表 | 悬停/点击显示解释 |
| 追问 | 返回结构化文字，不重新生成视频 |
| 关联问题 | 点击能启动新任务 |
| 异常处理 | 断网、额度耗尽、LLM 超时都有明确提示 |
| 缓存 | 相同问题第二次进入 <2 秒 |

### 端到端必测的三个异常

```
1. 断网后打开已缓存的问题 → 应完整播放（这是 Demo 的保险）
2. 知乎额度耗尽 → 应提示并展示缓存结果，不白屏
3. LLM 返回非法 JSON → 应降级为规则抽取，仍有结论
```

---

## 六、MVP 验收指标

| 维度 | 指标 | 验收线 |
|---|---|---|
| **主流程** | 输入 → 知识包 → 图文讲解 → 继续探索 | 完整跑通 |
| **检索** | 至少知乎搜索正常 | 必须 |
| **知识包** | 含 Claims / Evidence / 共识 / 争议 / 未解问题 | 五项齐全 |
| **溯源** | 关键 Claim 能关联到来源 | ≥90% 可溯源 |
| **个性化** | 同一知识包支持三档深度 | 或两档（降级可接受） |
| **播放器** | 图文 + 字幕 + 配音 + 时间轴 | 至少前三项 |
| **交互** | 追问 + 关联问题 | 两者都要 |
| **稳定性** | 单一来源失败不导致任务失败 | `partial_failed` 生效 |
| **缓存** | 检索结果与知识包可复用 | 第二次 <2 秒 |
| **可观测** | 各阶段状态、耗时、错误可见 | 技术抽屉能看到 |

---

## 七、测试资源与工具

| 资源 | 用途 |
|---|---|
| `data/raw/*.json`（70 条） | 单元测试 fixture |
| `data/labels.csv`（35 条标注） | 过滤准确率评测 |
| `backend/evaluate.py` | 现成的评测命令行，可直接复用 |
| `pytest` | 单元 + 集成 |
| `pytest-asyncio` | 异步任务测试 |
| 手工 checklist | 知识质量与 E2E |

### 建议的目录

```
tests/
├── conftest.py            # fixture：raw_items、临时 DB
├── unit/
│   ├── test_claim_extractor.py
│   ├── test_evidence_validator.py
│   ├── test_deduplicator.py
│   ├── test_cache_key.py
│   └── test_budget.py
├── integration/
│   ├── test_retrieval_chain.py
│   ├── test_partial_failure.py
│   └── test_depth_switch.py
└── quality/
    ├── questions.md        # 六道测试题
    └── checklist.md        # 十项检查表 + 反幻觉记录
```

---

## 八、时间安排

测试不是最后一天才做，而是分散在每天：

| 时间 | 做什么 |
|---|---|
| 第 1 天 | 搭 pytest 骨架，跑通一个空测试 |
| 第 3 天 | `claim_extractor` 与 `validator` 的单元测试（这天写完就测） |
| 第 5 天 | 集成测试：完整链路能跑 |
| 第 7 天 | 缓存、取消、部分失败的集成测试 |
| **第 8 天** | **集中跑知识质量测试 + 反幻觉抽查** |
| 第 9 天 | 三个异常场景的 E2E 演练 |

**第 8 天的知识质量测试不能省，也不能提前**——必须等 Claim 抽取调到相对稳定再做，否则测的是半成品。
