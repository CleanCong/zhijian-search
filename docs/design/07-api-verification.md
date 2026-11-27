# 知乎接口实测报告（第一天）

> 测试时间：2026-11-27 22:35–22:45
> 测试问题：`为什么飞机那么重，却能飞起来？`
> 原始响应：`data/raw/day1/*.json`
> CLI 版本：0.6.3

---

## 一、六个接口的实测结果

| 接口 | 单次上限 | 分页 | 实测耗时 | 返回字段数 | 内容长度 |
|---|---|---|---|---|---|
| `search zhihu` | **10** | ❌ 不支持 | **0.88s** | 18 | 正文中位 **489 字** |
| `search global` | **10** | ❌ 不支持 | **1.02s** | 15 | 正文节选 + 外链 |
| `question answers` | **50**（默认 20） | ✅ offset | **0.36s** | **4** | Summary 中位 **74 字** |
| `answer`（直答） | 1 段 | — | **5.46s** | OpenAI 格式 | 约 600 字 markdown |
| `hot`（热榜） | 30 | — | **0.40s** | Title/Url | — |
| `question recommend` | 20（默认 5） | — | **0.79s** | Title/Url | — |

### search zhihu 的 18 个字段

```
Title · Headline · ContentType · ContentID
AuthorName · AuthorSignature · AuthorAvatar · AuthorBadge · AuthorBadgeText
ContentText · Url · CommentCount · VoteUpCount
CommentInfoList · CreateTime · EditTime
AuthorityLevel · RankingScore
```

**其中两个字段现有系统丢弃了，建议启用**：
- `AuthorityLevel`（权威等级）—— 样本中 73% 是 4 级
- `RankingScore`（知乎排序分）—— 1.07–2.18

### search global 与知乎搜索的字段差异

少了 `Headline`、`AuthorSignature`、`CommentInfoList`，但**返回真实外部站点**：

```
首条：几百吨重的飞机,在空气中平稳翱翔的升力从哪儿来?
来源：宝鸡市科学技术协会  https://www.bjkx.gov.cn/index-2-42-45631-1.html
```

这证明**全网搜索可以作为独立的外部交叉验证来源**，不是知乎内容的换个包装。

### question answers 的实际价值很低

只有四个字段：

```json
{"ContentToken": "...", "ContentType": "Answer", "Summary": "...", "Url": "..."}
```

**没有标题、没有作者、没有点赞数**，Summary 中位仅 **74 字**。

对比：`search zhihu` 的 ContentText 中位 **489 字**，还有完整元数据。

**结论：`question answers` 不适合作为证据来源，只适合「发现某个问题下还有哪些回答」**——拿到 URL 后再用别的办法处理，但 CLI 无法读全文。

---

## 二、关键异常：额度完全不扣

### 实测过程

```
调用前：zhihu_search 剩余 5000 / global 5000 / hot 100 / discovery 100 / 直答 100
执行：search zhihu × 6 · search global × 1 · question answers × 1
     · hot × 1 · question recommend × 1 · answer × 1
等待：12 秒
调用后：全部仍为 0 已用，剩余数一字未变
```

### 可能的解释

| 解释 | 是否可信 |
|---|---|
| 额度按天/按批次结算，不实时 | 较可信 |
| `quota` 返回的是「配额上限」而非「剩余」 | 可能 |
| 新密钥额度未开始计量 | 可能 |
| 额度从不按调用次数扣（按别的维度） | 不确定 |

### 对开发的直接影响（重要）

**无法用 `quota` 接口做实时预算监控。**

这意味着方案里设计的 `retrieval_budget` 模块**必须自己在代码里计数**，不能依赖接口返回：

```python
class RetrievalBudget:
    def __init__(self, limits: dict[str, int]):
        self.limits = limits          # 从配置读，不从 quota 读
        self.used = {k: 0 for k in limits}

    def reserve(self, api_id: str, n: int = 1):
        if self.used[api_id] + n > self.limits[api_id]:
            raise BudgetExhausted(api_id)
        self.used[api_id] += n
```

**建议的硬上限**（写在配置里）：

| 场景 | zhihu_search | global_search | answer | hot |
|---|---|---|---|---|
| 普通问题 | ≤ 5 | ≤ 2 | ≤ 1 | 0 |
| 深度问题 | ≤ 8 | ≤ 3 | ≤ 1 | 0 |
| 追问 | ≤ 2 | 0 | 0 | 0 |

按「每次检索 5 次搜索」估算，5000 次额度约可支撑 **1000 次完整检索**——演示和初审完全够用。

---

## 三、QPS 与稳定性

### 串行调用：安全

连续 5 次 `search zhihu`，无间隔：

```
1: 0.64s  Code=0  success
2: 0.49s  Code=0  success
3: 0.65s  Code=0  success
4: 0.58s  Code=0  success
5: 0.56s  Code=0  success
```

实际间隔约 0.5–0.6 秒（≈2 QPS），**全部成功，未触发限流**。

### 并发：有风险（沿用旧结论）

现有项目文档记录「4 路并发即触发 rate limit」。本次未重复测并发——**保持串行即可，不要为了提速改成并发**。

### 参数校验：必须自己做

```
空 query   → 输出非 JSON，Code 为 None
count=99   → 输出非 JSON，Code 为 None
```

CLI 对非法参数没有结构化错误，所以在调用前必须校验：

```python
if not query.strip(): raise InvalidQuery()
count = max(1, min(count, 10))
```

---

## 四、直答（answer）的定位

### 实测表现

- 耗时 **5.46 秒**
- 返回 OpenAI 兼容格式（`chat.completion`，model `zhida-fast-1p5`）
- 内容是**约 600 字的 markdown**，带加粗和编号列表

质量抽样（飞机升力）：

> 飞机能飞起来，核心不是"没有重量"，而是**用机翼把向下的重力转化成了向上的空气动力（升力）**……
> **1. 机翼产生升力** 空气流过机翼时，由于机翼的迎角和弯度，上表面流速更快、压力更低……

内容准确，且给了两个解释视角（压力差 + 牛顿第三定律）。

### 但它的定位必须是「辅助」，不是「证据」

| 用途 | 可以吗 |
|---|---|
| 帮系统判断问题类型、生成子查询 | ✅ 可以 |
| 作为最终讲解的内容来源 | ❌ **不可以** |
| 作为「多来源印证」的其中一条 | ❌ **绝对不可以** |

理由：直答是模型生成的综合答案，它的内容来自它所引用的知乎回答。**把它和那些回答一起计数，就是自己给自己作证。**

实现上，`Evidence.source_type = zhihu_direct_answer` 的记录 `independence_key` 必须与其引用来源一致（见数据库设计）。

---

## 五、对第一天开发的具体结论

| # | 结论 | 影响 |
|---|---|---|
| 1 | 单次检索上限 10 条，不能翻页 | 想多拿只能加子查询 |
| 2 | `question answers` 内容仅 74 字，无元数据 | **不做证据来源**，可做「发现更多回答」 |
| 3 | 全网搜索返回真实外站 | ✅ 交叉验证可行，要保留 `source_name` |
| 4 | **额度不可观测** | 预算必须代码内计数，配置硬上限 |
| 5 | 串行 2 QPS 稳定 | 保持串行，不改并发 |
| 6 | 直答 5.5 秒、质量好但不可作证据 | 用于规划辅助，不用于内容展示 |
| 7 | 参数错误无结构化返回 | 调用前必须自行校验 |
| 8 | `AuthorityLevel` / `RankingScore` 未使用 | 建议启用，作为可信度加权 |

---

## 六、待确认项

| 项 | 怎么确认 |
|---|---|
| 额度何时扣减 | 明天再查一次 quota，看是否累积变化 |
| 失败调用是否扣额度 | 触发一次错误（如无效 URL）后对比 |
| 并发真实上限 | 如确需并发，单独压测（有触发限流风险） |
| 接口超时阈值 | 现有 CLI 默认 10s，实测最长 5.5s，够用 |

---

## 附：原始响应文件

```
data/raw/day1/
├── search_zhihu.json      # 10 条，18 字段
├── search_global.json     # 10 条，外站来源
├── question_answers.json  # 20 条，仅 4 字段
├── answer.json            # 直答，OpenAI 格式
├── hot.json               # 30 条热榜
└── question_recommend.json # 5 条推荐问题
```
