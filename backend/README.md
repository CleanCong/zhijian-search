# 知鉴 Search · 后端

给前端提供数据的服务。只出 JSON，不出 HTML —— 这样以后改界面不用动后端。

## 跑起来

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env      # 填入 ZHIHU_ACCESS_SECRET 与 DEEPSEEK_API_KEY
./run.sh                  # Windows 用 .\run.ps1
```

`run.sh` 会先把 `.env` 里的配置导进环境变量，再启动 uvicorn。
本地和服务器用同一个入口，避免「我这里能跑」的问题。

打开 <http://127.0.0.1:8000/docs> 可以直接在浏览器里试接口。

前端地址：`POST http://127.0.0.1:8000/api/search`

```json
{ "query": "两千价位扫地机器人 石头 追觅 怎么选",
  "options": { "time_window_days": 0 } }
```

## 依赖知乎 CLI

调用的是本机已安装的 `zhihu-cli`，路径默认 `~/AppData/Local/ZhihuCLI/current/zhihu-cli.exe`。
换机器或部署到服务器时，用环境变量指定：

```bash
export ZHIHU_CLI_PATH=/path/to/zhihu-cli
```

**凭据方式已验证（11-23）**：用 `ZHIHU_ACCESS_SECRET` 环境变量调 CLI
能正常返回结果，且环境变量优先于 keychain（`environment_shadows_keychain=true`）。
所以服务器上不需要 keychain。

服务器上还需要做一件事：装 Linux 版 CLI 二进制。

```bash
bash <skill-dir>/scripts/setup.sh   # 自动下载 linux-amd64 版本
```

## 目录

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 入口，三个路由：健康检查、检索、清缓存 |
| `zhihu.py` | CLI 调用封装。subprocess、UTF-8 解码、字段映射都在这 |
| `pipeline.py` | 五阶段编排：改写 → 多路召回 → 去重 → 时效 → 质检 → 归并 |
| `filters.py` | 过滤引擎。只负责执行规则，词表全在 `rules.json` |
| `rules.json` | **判据配置，由 C 维护**。改这里不用改代码 |
| `merge.py` | 归并三桶 + 生成卡片。**基线版，需要优化** |
| `cache.py` | 本地缓存，24 小时。保证演示可复现，也保护额度 |

## 几个刻意的设计

**1. 永远不返回 5xx。** 接口失败、额度耗尽、超时，都返回 200 + `mode: "degraded"`，
前端据此显示「演示数据」。这是官方提交检查第 7 条的明文要求。

**2. 软文不删除，只标记。** 判为 `is_ad_suspect` 的内容照样返回，带 `reasons`。
折叠与否由前端决定。产品主张是「判断权归用户」，后端不能替用户删东西。

**3. reasons 必须是人话。** 内部有分数，但只给调试用；返回给前端的只有
「文末有购买链接」这种句字。用户看不懂 0.62。

**4. 查询改写本身就是过滤。** 实测：原始 query「哪个好」的软文比例 40%，
拆成「缺点 问题 后悔」「耗材 一年 花多少钱」之后降到 2.5%。
所以 `pipeline.SCENES` 里的子查询模板是核心资产，不要随意删。

**5. 子查询必须串行，不能并发。**

实测 4 个子查询并发调用会直接触发 `rate limit exceeded`，串行正常（3.11 秒）。

所以**响应时间不能靠并发优化**，只能靠缓存。几个相关数字：

| 指标 | 实测值 |
|---|---|
| 单次 CLI 调用 | 0.73–0.80 秒（与返回条数无关，10 条和 3 条一样） |
| 5 个子查询串行 | 约 3.9 秒 |
| 加模型摘要后的总时长 | 5–6 秒 |
| 缓存命中 | 0.01 秒 |

**演示前必须预热缓存**——三个演示场景各跑一遍。否则几个评委同时点开，
后面的人会撞上限流。这不是优化问题，是能不能演示的问题。

## 已知问题（交接重点）

**① 一句话结论抽不准 —— 这是最大的短板**

`merge._best_sentence()` 用「强结论句式 + 词频打分」从正文里挑句子。
效果有限：
- 全屋定制场景能挑出「传统量房的问题，不是不能用，而是不够完整」（可用）
- 扫地场景挑出的是「直奔吸尘器而去，看来还有点蒙圈」（不可用）

所以设了阈值：得分不够就不硬凑，改成
「这次找到的 9 条内容各说各的，还没有能直接下结论的一句」，
并给出 `key_points` 列表让用户自己看。

**这个位置要真正做好，需要接 LLM 做抽取式摘要。没有 Key 就只能维持现状。**
不要为了「看起来有结论」而把阈值调低，那会选出更离谱的句子。

**② 分桶规则很粗**

`merge._classify()` 靠关键词判断「共识 / 分歧 / 坑」。
实测扫地场景 9/2/9/20，比例勉强能用，但归类不准：
很多「问答式」内容被归进共识，其实它们没有形成共识。

优化方向写在 `merge.py` 顶部的注释里，按优先级排了 5 条。

**③ 过滤在扫地主场景偏严**

50 条里去掉了 20 条（50%）。人工看大约 40% 是导购。
说明有误杀，需要 C 用标注集调 `rules.json` 的权重和阈值。

## 部署前必须验证

1. **CLI 能否在无图形界面的服务器上工作**
   凭据现在在 keychain 里。服务器上没有。
   两条路：用 `ZHIHU_ACCESS_SECRET` 环境变量（需去
   <https://developer.zhihu.com/profile> 重新生成一份），
   或者改成直接调 HTTP 接口（参考 skill 目录的 `references/http-api.md`）。

2. **CORS 是否放开**（当前 `allow_origins=["*"]`，上线可收紧）

3. **密钥不能出现在返回里** —— 提交前全仓库搜一遍 `secret` / `token`

## 给接手的人

先读 `pipeline.py` 的 `run()`，那是主流程，从上到下一目了然。
想改判断逻辑就改 `rules.json`，不要改 `filters.py`。
想改归并质量就改 `merge.py`，顶部注释写了五个待优化点。

---

## A 系列任务进展（引擎）

### A4 归并质量优化（merge.py，已做）
把基线的「关键词兜底分桶」升级为按内容判据分桶，对应顶部 5 条优化点：
- consensus 不再是兜底桶：build_cards 里用信号聚类复核「多条说法一致」，孤条仍留在 consensus 桶、打 `isolated=True`（前端可标「这几条说法各不相同」），内容不丢失，不虚占「已形成共识」。
- disputed 不再只看「但是/不过」：先做立场分类（正/负/条件），同一问题下有相反立场才判分歧。
- pitfalls 要求「有具体场景」：坑信号 ≥3 且场景词（户型/潮湿区/地暖/净深/身高…）≥3 才算具体的坑，优先于共识。
- core_conclusion 保持降级链：模型优先（pipeline 调 llm.summarize），失败退回规则抽取，不用模板句糊弄。
- 同题多答先聚合再找分歧。
- 接口不变：`group(items, query)` 返回 `(items, cards)`；新增就地字段 `stance`、`scene_hits`。
- 实测（全屋定制样本 35 条）：基线 consensus 18 条里一半是坑，优化后按「具体的坑 / 立场分歧 / 中立」如实归类，不再把坑塞进共识。

### A6 评测命令（backend/evaluate.py，已做）
`filters.evaluate()` 已有核心逻辑，补了命令行入口：
```bash
cd backend
python evaluate.py --labels ../data/labels.csv
python evaluate.py --labels ../data/labels.csv --json
python evaluate.py --labels ../data/labels.csv --rules rules.json
```
读 `labels.csv`（列 `id,label`，label ∈ 可信/软文/过时/跑题），用当前 rules.json 重跑判定后评测，
输出召回率/误杀率/混淆矩阵/漏判案例/误杀案例。真跑真算、可复现，换 rules.json 即可看到变化。
当前样本+rules v1.3 实测：召回率 71.4%、误杀率 4.8%（漏判 4、误杀 1，均已列出，供 C 调权重）。

### A2 部署前验证（结论）
后端走本机知乎 CLI + keychain。无头服务器上 CLI 不可用时，`zhihu.search` 抛 `ZhihuError`，
pipeline 捕获后整条链降级为 `mode=degraded`，不崩。上线前须用
`ZHIHU_CLI_PATH` + `ZHIHU_ACCESS_SECRET`（去 developer.zhihu.com 生成）打通，或改 HTTP 直连。

### A3 部署（scripts/ 与 deploy/，已备脚本）
- 启动脚本职责分开：
  - `backend/run.sh` / `run.ps1`：**本地开发**入口（读 .env + uvicorn --reload）。
  - `scripts/run_prod.sh`：**生产**入口（由 systemd 调），密钥走环境变量，
    **默认单 worker** —— 多 worker 会多进程并发调知乎撞限流（实测 4 路并发即 rate limit），
    演示单 worker + `cache.py` 缓存完全撑得住。
- `deploy/zhijian.service`：systemd 守护，崩溃自动重启；`deploy/zhijian.env.example` 为密钥模板（chmod 600，不进 git）。
- `deploy/nginx.conf`：反代 + HTTPS（certbot 签证书）。
- 若用内网穿透：承载穿透的电脑在评委访问期间不能关机，须提前告知全队。
- 部署后立刻用手机流量（关 WiFi）实测。

### A5 稳定性（已验证）
空/超长/特殊字符/emoji/SQL 样输入全部 HTTP 200（无 CLI 时为 degraded），连续 20 次不崩，日志不打印密钥内容。

### A7 预热（scripts/warmup.py，已做）
`python scripts/warmup.py [--base https://线上地址]` 把三个演示场景各跑一遍，预热缓存并当健康冒烟。
