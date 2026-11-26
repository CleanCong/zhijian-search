# 数据库设计

> 项目：Vibe 科普大赏
> 版本：v1.0
> 日期：2026-11-26

---

## 一、为什么首期用 SQLite

现状：项目无数据库，单台 2 核 4G ECS，无账号/会话体系。

| 选择 | 理由 |
|---|---|
| **SQLite + WAL** | 单文件、零运维、支持并发读、够用到单机上限 |
| Repository 层隔离 | 换库只改仓储层，业务代码不动 |
| JSON 字段直接存 TEXT | 避免首期引入复杂 schema 迁移 |
| 不引入 Redis | 缓存用文件 + SQLite 足够，少一个组件少一处故障 |
| 不引入图数据库 | 7 张表能表达 Claim-Evidence 关系，不需要图 |

**触发迁 PostgreSQL 的条件**：需要多实例部署、或并发写超过 SQLite 承受范围时。

> WAL 模式必须开启，否则读写互相阻塞：
> `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`

---

## 二、实体关系

```
generation_tasks 1 ──── n knowledge_packages 1 ──── n claims
                              │                      │
                              │                      │ n
                              │                      ▼
                              │              claim_evidence（多对多）
                              │                      ▲
                              │                      │ n
                              ▼                      │
                          scripts              evidence
                              │
                        （scenes 内含分段音频信息）

trace_logs  ── 按 task_id 归属，独立记录，不做外键约束
cache_entries ── 分层缓存，独立表
```

---

## 三、表结构

### 3.1 generation_tasks（生成任务）

```sql
CREATE TABLE generation_tasks (
    id          TEXT PRIMARY KEY,          -- task_xxx
    query       TEXT NOT NULL,
    depth       TEXT NOT NULL,             -- beginner | advanced | professional
    status      TEXT NOT NULL,             -- queued/retrieving/.../completed/failed/partial_failed
    progress    INTEGER DEFAULT 0,         -- 0-100，阶段映射值
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_tasks_status ON generation_tasks(status, created_at DESC);
CREATE INDEX idx_tasks_created ON generation_tasks(created_at DESC);
```

**`status` 枚举**：

```
queued → retrieving → extracting → building_knowledge
       → generating_script → generating_audio → completed

任意阶段失败 → failed
部分来源失败但主体完成 → partial_failed
```

`partial_failed` 是关键：全网搜索挂了但知乎成功，用户应该能继续用，而不是整个任务报错。

---

### 3.2 evidence（证据）

```sql
CREATE TABLE evidence (
    id                  TEXT PRIMARY KEY,
    source_type         TEXT NOT NULL,     -- zhihu | web | zhihu_direct_answer
    source_name         TEXT,
    title               TEXT,
    url                 TEXT,
    author              TEXT,
    content             TEXT NOT NULL,
    content_type        TEXT NOT NULL,     -- search_snippet | answer_summary | web_snippet | direct_answer
    published_at        TEXT,
    credibility         TEXT,              -- high | medium | low（沿用 rules.json 判定）
    independence_key    TEXT NOT NULL,     -- 判来源独立性的分组键
    raw_metadata        TEXT,              -- 原始 JSON，便于复查
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_evidence_source ON evidence(source_type);
CREATE INDEX idx_evidence_indep ON evidence(independence_key);
CREATE UNIQUE INDEX idx_evidence_url ON evidence(url) WHERE url IS NOT NULL;
```

**`independence_key` 是本设计的关键字段**，用于判断「多条来源」是否真独立：

| 情况 | 取值 |
|---|---|
| 不同作者的独立回答 | 各自唯一（如 `ev_001`） |
| 同一内容的转载 | **与前一条相同** |
| 知乎直答 + 它引用的回答 | **全部相同**（同源） |

`evidence_validator` 先按此键去重再数，杜绝「直答 + 其引用 = 两个独立证据」的虚假交叉验证。

> `evidence` 表**不做删除**。一条来源可能被多个 Claim 引用，删除会破坏溯源。

---

### 3.3 knowledge_packages（知识包）

```sql
CREATE TABLE knowledge_packages (
    id                   TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    query                TEXT NOT NULL,
    consensus            TEXT,      -- JSON: claim_id 数组
    controversies        TEXT,      -- JSON: [{claim_ids, sides, summary}]
    unresolved_questions TEXT,      -- JSON: string 数组
    analogy              TEXT,      -- JSON
    glossary             TEXT,      -- JSON: [{term, definition, claim_id}]
    related_questions    TEXT,      -- JSON: [{question, reason, source}]
    created_at           TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES generation_tasks(id)
);
CREATE INDEX idx_packages_task ON knowledge_packages(task_id);
```

**`consensus` / `controversies` 存 claim_id 数组，不存文本。**

理由：文本会与 `claims` 表不同步——改了 Claim 表述、删了某条 Claim，这里还是旧内容。存 id 则展示时 JOIN 实时取。

`analogy` / `glossary` 存 JSON 没问题，它们不是 Claim 的机械派生，是独立产物。

---

### 3.4 claims（原子主张）

```sql
CREATE TABLE claims (
    id                        TEXT PRIMARY KEY,
    package_id                TEXT NOT NULL,
    claim_text                TEXT NOT NULL,
    claim_type                TEXT,      -- fact | mechanism | experience | opinion | recommendation
    explanation               TEXT,

    model_confidence          TEXT,      -- 模型自评：high | medium | low
    evidence_status           TEXT,      -- 代码计算：supported | partially_supported | disputed | insufficient
    independent_source_count  INTEGER DEFAULT 0,

    coverage_status           TEXT,      -- supported | partially_supported | disputed | insufficient
    controversy               INTEGER DEFAULT 0,
    created_at                TEXT NOT NULL,
    FOREIGN KEY(package_id) REFERENCES knowledge_packages(id)
);
CREATE INDEX idx_claims_package ON claims(package_id);
CREATE INDEX idx_claims_status ON claims(evidence_status);
```

**两个置信度字段必须分开存**：

| 字段 | 谁写 | 能否用于「已验证」展示 |
|---|---|---|
| `model_confidence` | LLM | ❌ |
| `evidence_status` | `evidence_validator` 按证据关系算 | ✅ |

这是防止「模型很自信但没依据」的最后一道闸门。

---

### 3.5 claim_evidence（多对多关系）

```sql
CREATE TABLE claim_evidence (
    claim_id         TEXT NOT NULL,
    evidence_id      TEXT NOT NULL,
    relation         TEXT NOT NULL,     -- supports | refutes | supplements
    support_strength TEXT,              -- strong | weak
    PRIMARY KEY (claim_id, evidence_id, relation),
    FOREIGN KEY(claim_id) REFERENCES claims(id),
    FOREIGN KEY(evidence_id) REFERENCES evidence(id)
);
CREATE INDEX idx_ce_claim ON claim_evidence(claim_id);
CREATE INDEX idx_ce_evidence ON claim_evidence(evidence_id);
```

`relation` 允许 `refutes` 是必要的——**证据不只是支持，也可以反驳**。这正是「争议」的数据基础。

必须多对多，不能把 evidence_id 塞进 Claim 的 JSON 数组：一个 Claim 会被多条证据支持，一条证据也会支撑多个 Claim。

---

### 3.6 scripts（讲解脚本）

```sql
CREATE TABLE scripts (
    id                TEXT PRIMARY KEY,
    package_id        TEXT NOT NULL,
    depth             TEXT NOT NULL,      -- beginner | advanced | professional
    title             TEXT,
    scenes            TEXT NOT NULL,      -- JSON 数组，见下
    total_duration_ms INTEGER,
    created_at        TEXT NOT NULL,
    FOREIGN KEY(package_id) REFERENCES knowledge_packages(id)
);
CREATE UNIQUE INDEX idx_scripts_pkg_depth ON scripts(package_id, depth);
```

**`scenes` 是 JSON 数组，每个 Scene 含分段音频信息**（对应 TTS 分段生成）：

```json
[
  {
    "scene_id": "scene_01",
    "start_ms": 0,
    "end_ms": 8000,
    "narration": "想象有两枚神奇的硬币……",
    "subtitle": "两枚相互关联的硬币",
    "visual": {
      "type": "illustration",
      "description": "两枚硬币并排展示",
      "asset_url": null
    },
    "highlight_terms": [],
    "claim_ids": ["claim_01"],
    "evidence_ids": ["ev_001"],
    "audio_url": "/audio/task_xxx/scene_01.mp3",
    "audio_duration_ms": 7800
  }
]
```

**`start_ms` / `end_ms` 由音频实际时长反推**，不是预设值。语速和停顿会让真实时长偏离估算，硬切必然导致字幕与声音错位。

`idx_scripts_pkg_depth` 让「切换深度」能直接命中已有脚本，无需重新生成。

---

### 3.7 trace_logs（流程追踪）

```sql
CREATE TABLE trace_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    stage        TEXT NOT NULL,
    status       TEXT,              -- started | completed | failed | skipped
    duration_ms  INTEGER,
    input_count  INTEGER,
    output_count INTEGER,
    detail       TEXT,
    metadata     TEXT,              -- JSON: {model, tokens, api_id...}
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_trace_task ON trace_logs(task_id, id);
```

**与现有 Trace 的区别**：现有只有 `{stage, label, detail}` 三字段人话描述，无耗时。新表增加了 `duration_ms`、`input_count`、`output_count`、`metadata`，让技术抽屉能展示真实的处理量而不只是感受。

建议的 stage 全集：

```
query_planning · retrieval · normalization · deduplication
claim_extraction · evidence_matching · conflict_detection
knowledge_building · script_generation · tts_generation
```

---

### 3.8 cache_entries（分层缓存）【新增】

对应 7.4 的分层缓存设计，统一用一张表管理：

```sql
CREATE TABLE cache_entries (
    cache_key   TEXT PRIMARY KEY,
    layer       TEXT NOT NULL,      -- L1 | L2 | L3 | L4 | L5
    payload     TEXT,               -- JSON；音频只存路径不存二进制
    expires_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_cache_layer ON cache_entries(layer, expires_at);
```

### 五层缓存定义

| 层 | 缓存内容 | Key 组成 | TTL |
|---|---|---|---|
| **L1** | 检索原始结果 | `sha1(query + sub_query + source + config_hash)` | 7 天 |
| **L2** | 标准化 Evidence | `sha1(evidence_url + content_hash)` | 30 天 |
| **L3** | Knowledge Package | `sha1(query + plan_version + prompt_version)` | 24 小时 |
| **L4** | 讲解脚本 | `sha1(package_id + depth + prompt_version)` | 24 小时 |
| **L5** | TTS 音频 | `sha1(text + voice_config)`（音频文件存磁盘，表存路径） | 30 天 |

### Cache Key 必须包含 Prompt 版本

这是最容易踩的坑：**只按 query 做 key，改完 prompt 后仍会命中旧结果**，表现为「代码改了但输出没变」，极难排查。

必备字段：

```python
CACHE_KEY_FIELDS = [
    "query",
    "time_window_days",
    "plan_version",      # query_planner 的 prompt 版本
    "prompt_version",    # 各阶段 prompt 版本
    "depth",
    "sources",           # 参与检索的来源集合
    "tts_config",
]
```

每次改 prompt，**手动递增对应版本号**，即可让旧缓存自然失效，不用手动清库。

---

## 四、索引与查询模式

主要查询路径与对应索引：

| 查询 | 索引 |
|---|---|
| 按状态查待处理任务 | `idx_tasks_status` |
| 查某任务的知识包 | `idx_packages_task` |
| 查某知识包的全部 Claim | `idx_claims_package` |
| 查某 Claim 的证据 | `idx_ce_claim` |
| 查某证据支撑哪些 Claim | `idx_ce_evidence` |
| 查某 package + depth 的脚本 | `idx_scripts_pkg_depth`（唯一） |
| 查任务的全链路 Trace | `idx_trace_task` |
| 按层清理过期缓存 | `idx_cache_layer` |

---

## 五、迁移与版本管理

首期不引入 Alembic，用最简单的方式：

```
storage/migrations/
  ├── 001_init.sql
  ├── 002_add_independence_key.sql
  └── ...
```

启动时检查 `PRAGMA user_version`，按序执行未应用的迁移。

```python
CURRENT_VERSION = 2

def migrate(conn):
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(v + 1, CURRENT_VERSION + 1):
        conn.executescript(read(f"{version:03d}_*.sql"))
        conn.execute(f"PRAGMA user_version={version}")
```

理由：9–10 天里没时间学 Alembic，手写迁移脚本足够，且更可控。

---

## 六、与现有系统的数据衔接

| 现有数据 | 处理 |
|---|---|
| `backend/_cache/*.json`（24h payload 缓存） | 保留作为 L3 的过渡实现，新系统上线后迁移到 `cache_entries` |
| `data/raw/*.json`（70 条开发样本） | 转为测试 fixture，不删除 |
| `data/labels.csv`（35 条标注） | 保留，用于 `credibility` 字段的准确率评测 |
| `rules.json`（11 条判据） | 保留，`result_normalizer` 计算 `credibility` 时调用 |

**不做的事**：不迁移旧缓存到新库（格式不同，且 24 小时就过期，没有迁移价值）。
