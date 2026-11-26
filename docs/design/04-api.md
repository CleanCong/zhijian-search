# API 设计

> 项目：Vibe 科普大赏
> 版本：v1.0
> 日期：2026-11-26
> 风格：REST + JSON，异步任务模式

---

## 一、设计原则

| 原则 | 说明 |
|---|---|
| 前端不感知内部调用 | 前端不知道后端调了几次知乎，只关心任务状态与结果 |
| 长任务一律异步 | 检索 + LLM + TTS 合计 15–25 秒，同步 HTTP 必然超时 |
| 部分失败可继续 | 全网搜索挂了但知乎成功，应返回 `partial_failed` 而不是整体失败 |
| 契约先行 | 前后端按本文档并行开发，不互相等待 |

---

## 二、接口总览

| 方法 | 路径 | 功能 | 耗时 |
|---|---|---|---|
| POST | `/api/v1/generations` | 创建生成任务 | 立即返回 |
| GET | `/api/v1/generations/{id}` | 查任务状态 | 立即 |
| GET | `/api/v1/generations/{id}/result` | 取生成结果 | 立即 |
| POST | `/api/v1/generations/{id}/script` | 切换讲解深度 | 异步或 3–5s |
| DELETE | `/api/v1/generations/{id}` | **取消任务**【新增】 | 立即 |
| GET | `/api/v1/packages/{id}` | 取知识包 | 立即 |
| POST | `/api/v1/packages/{id}/questions` | 追问 | 3–8s |
| GET | `/api/v1/packages/{id}/related` | 关联问题 | 立即（已预生成） |
| GET | `/api/v1/discovery/hot` | 知乎热点 | 立即 |
| GET | `/api/v1/health` | 健康检查 | 立即 |

---

## 三、创建生成任务

### POST /api/v1/generations

**请求**

```json
{
  "query": "量子纠缠到底是什么？",
  "depth": "beginner",
  "options": {
    "enable_global_search": true,
    "enable_tts": true,
    "enable_related_questions": true
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | ✅ | 用户问题，1–200 字 |
| `depth` | string | ✅ | `beginner` / `advanced` / `professional` |
| `options.enable_global_search` | bool | ❌ | 默认 true |
| `options.enable_tts` | bool | ❌ | 默认 true |
| `options.enable_related_questions` | bool | ❌ | 默认 true |

**响应** `202 Accepted`

```json
{
  "task_id": "task_7f3a2c",
  "status": "queued",
  "progress": 0,
  "message": "任务已创建"
}
```

### 为什么用任务模式

一次生成包含：4–5 次检索（3–4s）+ 6–8 次 LLM 调用（10–18s）+ TTS（3–5s）。

同步请求会遇到三个问题：反向代理超时、用户以为卡死、刷新页面任务白跑且额度白花。

---

## 四、任务状态

### 状态机

```
queued
  ↓
retrieving          （多源检索）
  ↓
extracting          （Claim 提取 + 证据关联）
  ↓
building_knowledge  （组装知识包）
  ↓
generating_script   （生成脚本）
  ↓
generating_audio    （TTS 合成）
  ↓
completed

任意阶段失败    → failed
部分来源失败    → partial_failed（可继续使用）
```

### GET /api/v1/generations/{id}

```json
{
  "task_id": "task_7f3a2c",
  "status": "building_knowledge",
  "progress": 65,
  "current_stage": "claim_extraction",
  "message": "正在整理核心知识点",
  "stages": [
    {"stage": "query_planning", "status": "completed", "duration_ms": 620},
    {"stage": "retrieval",      "status": "completed", "duration_ms": 3400},
    {"stage": "claim_extraction","status": "running",  "duration_ms": null}
  ]
}
```

> `progress` 是**阶段映射值**，不是精确百分比。不要用它做「还剩几秒」的推算，只用来驱动进度条。

### 关于 partial_failed

```json
{
  "task_id": "task_7f3a2c",
  "status": "partial_failed",
  "progress": 100,
  "message": "知识包已完成，但全网搜索未响应，本次仅使用知乎内容",
  "warnings": [
    {"source": "global_search", "reason": "timeout", "fallback": "仅使用知乎来源"}
  ]
}
```

**前端处理**：`partial_failed` 应当作**可正常展示的结果**，只在顶部显示一条非阻断提示，不要弹错误框。

---

## 五、获取结果

### GET /api/v1/generations/{id}/result

```json
{
  "task_id": "task_7f3a2c",
  "package_id": "pkg_9b1e",
  "script_id": "script_3c7d",
  "depth": "beginner",
  "status": "completed",
  "package": {
    "consensus": ["claim_01", "claim_04"],
    "controversies": [
      {
        "claim_ids": ["claim_07", "claim_09"],
        "summary": "关于能否用于超光速通信存在对立说法"
      }
    ],
    "unresolved_questions": ["观测后纠缠是否立即解除，目前无定论"],
    "claims": [
      {
        "claim_id": "claim_01",
        "claim_text": "量子纠缠不能用于超光速传递信息",
        "claim_type": "fact",
        "model_confidence": "high",
        "evidence_status": "supported",
        "independent_source_count": 3,
        "coverage_status": "supported",
        "controversy": false,
        "evidence_ids": ["ev_001", "ev_005", "ev_012"]
      }
    ],
    "evidence": [
      {
        "evidence_id": "ev_001",
        "source_type": "zhihu",
        "source_name": "知乎",
        "title": "量子纠缠为什么不能超光速？",
        "url": "https://www.zhihu.com/question/xxx/answer/yyy",
        "author": "某物理博士",
        "content_type": "answer_summary",
        "credibility": "high"
      }
    ],
    "analogy": {"text": "像一对有默契的骰子……", "claim_ids": ["claim_01"]},
    "glossary": [{"term": "量子纠缠", "definition": "……", "claim_id": "claim_01"}],
    "related_questions": [
      {"question": "量子纠缠与量子通信有什么关系？", "reason": "从基础概念延伸到应用", "source": "llm"},
      {"question": "量子纠缠能用于量子计算吗？", "reason": "探索实际应用", "source": "zhihu_discovery"}
    ]
  },
  "script": {
    "title": "量子纠缠是什么？",
    "depth": "beginner",
    "total_duration_ms": 60000,
    "scenes": []
  },
  "trace": []
}
```

**前端主要消费 `package` 与 `script` 两个对象**，`trace` 只用于技术抽屉。

### evidence_status 与 model_confidence 的展示规则（关键）

| 组合 | 展示方式 |
|---|---|
| `evidence_status=supported` | 正常展示，可以说「多来源印证」 |
| `partially_supported` | 展示，标注「单一来源」 |
| `disputed` | 必须展示为「存在不同说法」，不能选一边当结论 |
| `insufficient` | **禁止作为已证实事实展示**，只能标「AI 推断」或「材料不足」 |

`model_confidence=high` 但 `evidence_status=insufficient` 时，**以后者为准**。

---

## 六、切换讲解深度

### POST /api/v1/generations/{id}/script

```json
{ "depth": "professional" }
```

**响应**：若该深度脚本已存在，立即返回；否则触发异步生成，返回：

```json
{
  "script_id": "script_new",
  "package_id": "pkg_9b1e",
  "depth": "professional",
  "status": "generating",
  "message": "正在生成专业版讲解"
}
```

前端轮询 `GET /generations/{id}` 或直接轮询该 script 状态。

### 核心实现原则

```
切换深度 = 只重新生成脚本
         ≠ 重新检索
         ≠ 重新构建知识包
```

同一 `package_id` 下三种深度的脚本共享同一份 Claims / Evidence / 争议。这保证了**入门版和专业版不会给出互相矛盾的结论**。

---

## 七、取消任务【新增】

### DELETE /api/v1/generations/{id}

```json
{ "task_id": "task_7f3a2c", "status": "cancelled" }
```

用于：用户输错问题、中途换问题、发现额度不够时止损。

取消后不再发起新的 LLM 与检索调用，已完成的阶段结果保留（可被复用）。

---

## 八、知识包追问

### POST /api/v1/packages/{id}/questions

```json
{
  "question": "为什么量子纠缠不能用来超光速传递信息？",
  "depth": "beginner"
}
```

**响应**

```json
{
  "answer": {
    "text": "因为纠缠本身不传递信息……",
    "key_points": ["测量结果是随机的", "无法指定测量结果"],
    "related_claims": ["claim_01", "claim_07"],
    "evidence_ids": ["ev_001", "ev_012"]
  },
  "sources": [
    {"evidence_id": "ev_001", "title": "……", "url": "https://..."}
  ],
  "need_more_retrieval": false,
  "used_retrieval": false
}
```

### 追问的处理顺序

```
1. 先用当前知识包的 Claim / Evidence 回答
2. 材料不足时，由 query_planner 决定是否二次检索（受剩余预算约束）
3. 明确区分「有来源支持」与「解释推断」
4. need_more_retrieval=true 表示已触发补充检索
```

**追问不重新生成视频/脚本**，只返回文字与结构化卡片。

---

## 九、关联问题

### GET /api/v1/packages/{id}/related

```json
{
  "items": [
    {"question": "量子纠缠与量子通信有什么关系？", "reason": "从基础概念延伸到应用", "source": "llm"},
    {"question": "量子纠缠能用于量子计算吗？", "reason": "探索实际应用", "source": "zhihu_discovery"}
  ]
}
```

**必须标记 `source`**：

| source | 含义 | UI 处理 |
|---|---|---|
| `llm` | 模型生成的关联问题 | 标「AI 推荐」 |
| `zhihu_discovery` | 知乎真实存在的问题 | 标「知乎已有问题」，可直接跳转 |

**不能把模型编造的问题伪装成知乎已有问题。**

> 该接口只读——关联问题在 `knowledge_building` 阶段同步生成并落库，不在这里触发生成（否则 GET 有副作用且耗时）。

---

## 十、图文脚本数据契约（最重要的前后端接口）

### Scene 结构

```json
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
  "highlight_terms": ["量子纠缠"],
  "claim_ids": ["claim_01"],
  "evidence_ids": ["ev_001"],
  "audio_url": "/audio/task_xxx/scene_01.mp3",
  "audio_duration_ms": 7800
}
```

### visual.type 的受约束枚举

首期只支持六种，**前端按类型渲染预置组件**：

| type | 用途 |
|---|---|
| `illustration` | 插图 / 示意图 |
| `text` | 纯文字卡片 |
| `diagram` | 结构示意图 |
| `chart` | 数据图表 |
| `formula` | 公式 |
| `comparison` | 对比展示（左右/前后） |

**绝对不要让 LLM 自由生成前端组件代码。** 它只返回受约束的 JSON，未知 `type` 一律降级为 `text`。

### 时间轴由音频实测时长决定

```
start_ms[0] = 0
end_ms[i]   = start_ms[i] + audio_duration_ms[i]
start_ms[i+1] = end_ms[i]
```

不要用预设的「每段 8 秒」硬切——语速和停顿会让真实时长偏离，字幕必然错位。

### 播放器交互要求

| 功能 | 要求 |
|---|---|
| 播放/暂停 | 音频与画面同步 |
| 字幕 | 跟随 Scene 切换 |
| 术语高亮 | `highlight_terms` 命中时高亮，点击显示 glossary 释义 |
| 来源跳转 | 当前 Scene 的 `evidence_ids` 可展开查看原文 |
| 深度切换 | 顶部三档，切换后从头播放 |

---

## 十一、Trace 接口

### 结构化事件（替代原来的三字段人话描述）

```json
{
  "stage": "claim_extraction",
  "status": "completed",
  "duration_ms": 1200,
  "input_count": 31,
  "output_count": 12,
  "detail": "从材料中提取出 12 条原子主张",
  "metadata": {"model": "deepseek-chat", "tokens": 8420}
}
```

### 建议的 stage 全集

```
query_planning → retrieval → normalization → deduplication
→ claim_extraction → evidence_matching → conflict_detection
→ knowledge_building → script_generation → tts_generation
```

**技术抽屉的价值来自这里的数字**：31 条材料 → 12 条 Claim → 8 条被支持 / 3 条争议 / 1 条证据不足。

这比六句自然语言描述有说服力得多。

---

## 十二、统一错误响应

```json
{
  "error": {
    "code": "RETRIEVAL_FAILED",
    "message": "知乎搜索未响应",
    "retryable": true,
    "detail": {"api_id": "zhihu_search", "attempts": 2}
  }
}
```

### 错误码表

| HTTP | code | 含义 | 前端处理 |
|---|---|---|---|
| 400 | `INVALID_QUERY` | 问题为空或过长 | 提示修改问题 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 | 引导重新创建 |
| 409 | `TASK_NOT_READY` | 结果尚未生成 | 继续轮询 |
| 429 | `QUOTA_EXCEEDED` | 接口额度耗尽 | 提示「今日额度用完」，展示缓存结果 |
| 500 | `LLM_FAILED` | 模型调用失败 | 提示重试 |
| 502 | `RETRIEVAL_FAILED` | 检索源不可用 | 若为 partial 则继续展示 |
| 504 | `TASK_TIMEOUT` | 任务超时 | 提示重试或简化问题 |

**原则**：只要还有可用材料，就不返回 5xx，而是返回 200 + `partial_failed`。
