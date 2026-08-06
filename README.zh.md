# JobRadar

> **中文** · [English](README.md) · [Español](README.es.md)

根据你的 CV 自动搜索全球职位，LLM 匹配评分，多来源聚合去重。

## 快速开始

```bash
# 安装 uv（已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows（PowerShell）：irm https://astral.sh/uv/install.ps1 | iex

git clone https://github.com/sangowu/JobRadar.git
cd JobRadar
uv sync
uv run jobradar serve       # 启动 Web UI（http://127.0.0.1:8765）
# 浏览器打开后，在「API 配置」页面填入 API Key 即可开始使用
# 也可手动配置 .env：
cp .env.example .env         # 填入 API Key
uv run jobradar find cv.docx  # CLI 模式
```

## 常用命令

| 命令 | 说明 |
|---|---|
| `uv run jobradar serve` | 启动 Web UI |
| `uv run jobradar serve --mock` | 测试模式（独立 DB，不污染正式缓存） |
| `uv run jobradar find cv.docx` | CLI：解析 CV → 发现 title → 抓取 → 评估 |
| `uv run jobradar find cv.docx --refresh` | 忽略缓存，强制重新搜索 |
| `uv run jobradar results` | 浏览缓存中最近的搜索结果 |
| `uv run jobradar assess` | 对缓存 JD 单独补跑 LLM 评估 |
| `uv run jobradar model` | 交互式选择 LLM provider 和模型 |
| `uv run jobradar cache clear` | 清空所有缓存 |
| `uv run jobradar --version` | 显示当前版本号 |

## Pipeline 概览

```
CV 文件
  │
  ▼ ① CV 解析（LLM → CVProfile）← SHA-256 永久缓存
         结构化 seniority 区间 + 显式语言能力提取
  ▼ ② 用户确认 title 列表
  ▼ ③ 抓取（Indeed + LinkedIn，JobSpy，无浏览器）
         两个来源并发；每完成一个 role batch 就立即做 URL 去重
  ▼    低成本 Python 预筛 + 落盘检查点
         年资 / 关闭状态 / 经验差距过滤 → filtered list → search_candidates（SQLite）
         同一批对象随后进入内存评估队列
  ▼ ④ 批量评估协调器（与后续抓取 batch 重叠执行）
         前置 LLM title relevance gate
         仅基于 title 做保守语义粗筛；默认 keep=true，只拒绝明显属于另一条职业路径的标题
  ▼    分批 LLM coarse filter
         使用 title + location + snippet 做卡片级 keep/reject
  ▼ ⑤ 有界职位评估池（云端默认 5 workers，本地模型默认 1）
         不同职位并发执行；每个职位内部保持 JD Profile → CV Match 的依赖顺序
         评估结果由协调线程串行提交 SQLite，提交成功后才发送 SSE
  ▼ ⑥ JD Profile 提取
         结构化 required/preferred skills、must-have、年限、seniority 冲突、work mode、语言要求
  ▼ ⑦ 可解释 CV↔JD 匹配
         rubric 分维打分 → 程序化加权总分 → recommendation
         跨城市搬迁 / 到岗办公要求计入风险，不压低 location_score
  ▼ ⑧ 衍生材料生成
          面试准备 / 求职信 / CV 优化
  ▼ ⑨ 搜索统计与缓存
         历史指标、报告、filter events、Web UI / 终端展示
```

典型漏斗（真实数据）：
```
Indeed 741 + LinkedIn 255 = 996 抓取
  → LLM 标题过滤  996 → 689（淘汰 30.8%）
  → 预筛漏斗     689 → 76（年资/去重/技能等各步过滤）
  → LLM 评估     76 → 54 保存（通过率 71.1%）
  → 最终过滤率   94.6%（996 条中仅 54 条需人工审阅）
```

## 环境变量

```env
# LLM Provider（至少配置一个）
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# 本地模型
LLAMACPP_BASE_URL=http://localhost:8080/v1
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# 默认模型（由 jobradar model 命令自动写入）
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-3.5-flash-lite
```

## Web UI 功能

- **实时进度**：搜索期间 SSE 逐条推送职位卡片
- **管道漏斗统计**：搜索完成后在进度日志和完成卡片展示各阶段明细（抓取量 → LLM 标题过滤 → 预筛漏斗 → LLM 评估 → 最终保存量 / 过滤率）
- **三栏布局**：职位列表 + 详情 + CV 上传/搜索面板
- **多来源聚合**：同一职位在 Indeed 和 LinkedIn 均出现时自动合并，卡片徽标可点击跳转对应来源；详情页 Apply 按钮变为多来源下拉菜单
- **搜索历史**：每条记录可展开 📊 管道漏斗详情，按来源（Indeed / LinkedIn）分项显示
- **标准化搜索历史指标**：每次搜索都会记录抓取总数、去重后数量、过滤后数量、新增职位数和 token 消耗
- **模块级监控**：历史记录与搜索完成事件会附带 `module_metrics`，按模块记录 `calls / input_tokens / output_tokens / elapsed`，搜索链路还会附带 `processed / rejected / kept`
- **漏斗 benchmark 摘要**：搜索历史会记录 pipeline/prompt 版本，并展示过滤后占比、新增产出率、每个新增职位 token 成本等效率指标
- **可复现调度对照**：[`docs/pipeline-benchmark.md`](docs/pipeline-benchmark.md) 说明如何固定 batch、使用隔离 SQLite 回放，并配对比较串行/流式调度，全程不写正式缓存或 SSE
- **标题前置语义过滤**：在 JD 评估前，先把职位 title 批量交给 LLM 做保守相关性粗筛；历史漏斗中会显示 `skip_irrelevant`
- **Title gate 行为边界**：只有当职位标题本身已经足以说明它明显属于另一条职业路径时才拒绝；宽泛标题、相邻技术方向标题、信息不足标题都会先保守放行，交给后续 JD 评估处理
- **逐条过滤事件持久化**：每次搜索都会把 `run_id / stage / title / reason / details` 写入 `filter_events`，可以回看每条职位被哪一步过滤掉
- **Web UI 强制重新检索**：搜索面板支持 force refresh，不必改 CV 也能绕过当前 search-session cache 重跑
- **动态标题级别过滤**：基于 CV 的 eligible/stretch/blocked 级别，在 JD matching 前去掉明显职级不符的标题
- **经验差距硬过滤**：预筛阶段会记录 `skip_exp`，`jd_assessment` 也会对“年限差距 > 3 年”的职位直接判 `relevant=false`
- **可解释匹配**：职位详情展示评分拆解、风险、技能匹配和 recommendation 分层
- **搬迁 / 到岗要求只计入风险**：同一目标国家内的跨城市搬迁，以及 hybrid / onsite / 每周到岗若干天等要求，会进入 `risks / risk_penalty`，但不应压低 `location_score`
- **Artifact Hub**：在职位详情中统一生成并复用 Interview Prep、Cover Letter、CV Optimization
- **日志面板**：级别过滤、关键词高亮、自动刷新
- **配置页**：在线管理 LLM API Key、选择默认模型、清除缓存；新用户无需编辑 `.env`，直接在页面完成所有配置
- **多语言**：界面支持中文 / English / Español 切换

## 统计报告

每次搜索完成后自动写入 `reports/` 目录：

| 文件 | 说明 |
|---|---|
| `pipeline_stats.jsonl` | 逐行追加，保存全量历史，每行一次搜索的完整漏斗数据 |
| `pipeline_stats_latest.json` | 覆盖写入，始终为最新一次搜索的 JSON 报告 |

## Benchmark 与版本记录

- **每条历史记录都会保存版本信息**。后端会把 `app_version`、`cv_prompt_version`、`jd_summary_prompt_version`、`match_prompt_version`、`title_gate_version`、`coarse_filter_version` 写入 `search_stats`。
- **每条历史记录也会保存 `run_id`**，便于和逐条 `filter_events` 做关联。
- **UI 的 Funnel Benchmark 会按版本签名分组比较**。版本签名由上述字段拼接而成，所以 benchmark 的“当前版本 / 上一版本”实际上是在比较两组不同签名的历史搜索。
- **当前 UI 历史表格不会逐行展开显示全部版本字段**，但 benchmark 卡片会显示当前组与上一组的版本签名；后端 `/api/stats` 返回的每条历史记录里也包含 `versions`。

## 逐条过滤事件查看

- **API**：
  - `GET /api/stats` 可查看历史记录及对应 `run_id`
  - `GET /api/filter-events?run_id=<run_id>` 可查看该次搜索的逐条过滤事件
- **存储**：
  - `search_stats` 保存单次搜索摘要
  - `filter_events` 保存 `stage / title / reason / details`
- **mock 清缓存**：
  - mock 模式下清除缓存时，也会同时清空 mock DB 中的 `search_stats` 与 `filter_events`

## 过滤事件查看脚本

可以使用 `scripts/show_filter_events.py` 直接读取 `data/jobradar_test_cache.db` 中的持久化过滤事件，而不重新跑搜索。

示例：

```bash
python scripts/show_filter_events.py
python scripts/show_filter_events.py --stage jd_assessment --out reports/filter_report.md
python scripts/show_filter_events.py --run-id <run_id> --json --out reports/filter_report.json
```

常用参数：

- `--run-id`：查看指定历史搜索
- `--stage`：只看某个阶段，如 `title_relevance`、`coarse_filter`、`experience_gap`、`jd_assessment`、`final_match`
- `--md`：在终端直接输出 Markdown
- `--out`：保存为 `.md` 或 `.json`

## 如何看前置 Title LLM 模块的成本与效果

当前系统已经能直接看到两类信号：

- **效果**：
  - 历史漏斗里的 `skip_irrelevant`：表示前置 title relevance gate 拦掉了多少标题。
  - Benchmark 中的 `new_job_yield`、`tokens_per_filtered_job`、`tokens_per_new_job`、`assessment_efficiency`：可用于比较加模块前后的整体收益。
- **总成本**：
  - 搜索历史已经记录每次搜索的 `tokens_in` / `tokens_out`，可用于做版本前后总 token 对比。

但有一个当前限制：

- **还没有单独记录“title relevance gate 自己消耗了多少 token”**。
  现在记录的是整次搜索的总 token，而不是按模块拆分。因此：
  - 你可以比较“加模块前 vs 加模块后”的总 token 和整体漏斗指标；
  - 但还不能在 UI 里直接看到“这个 title gate 单独花了多少 token”。

推荐的评估方式：

1. 用相近的 `roles + location + provider/model` 连续跑几次，对比新旧版本签名。
2. 查看历史 benchmark 与漏斗明细：
   - `skip_irrelevant` 是否增加
   - `llm_assessed` 是否下降
   - `tokens_per_filtered_job` / `tokens_per_new_job` 是否下降
   - `new_job_yield` 是否没有明显变差
3. 如果你想看**精确模块成本**，需要进一步给 title gate 单独打 telemetry 标签或单独记录 token 计数。

## 邮件投递追踪

Web UI 的“投递追踪”页面通过 Google OAuth 登录 Gmail，将招聘邮件识别为已投递、测评、面试、Offer、拒绝或待确认，并将状态时间线保存到同一个 SQLite 数据库。

1. 在 Google Cloud Console 创建 OAuth 2.0 Web application client。
2. 启用 Gmail API。
3. 添加回调地址：`http://127.0.0.1:8765/api/email/google/callback`。
4. 在 `.env` 中配置：

```env
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
EMAIL_SYNC_INTERVAL_SECONDS=900
EMAIL_SYNC_MAX_MESSAGES=5000
EMAIL_SYNC_FETCH_WORKERS=8
EMAIL_SYNC_ANALYSIS_WORKERS=3
EMAIL_LLM_CLASSIFICATION_ENABLED=1
```

启动 `jobradar serve` 后，在“投递追踪”页面点击“使用 Google 登录”完成授权。应用只申请 Gmail 只读权限，不需要 Gmail 密码。OAuth token 保存在本地 `data/google_gmail_token.json`，该目录不会提交到 Git。

首次同步会分页读取最近 30 天邮件；成功后保存 Gmail History 游标，后续只处理新增邮件。History 游标失效时会自动回退到完整分页同步。`EMAIL_SYNC_MAX_MESSAGES` 是单次同步的安全上限，达到上限时同步失败且不会推进游标，避免静默漏信。邮件正文默认使用 8 个并发请求读取，可通过 `EMAIL_SYNC_FETCH_WORKERS` 调整（范围 1–16）；规则及 LLM 分类默认使用 3 个并发任务，可通过 `EMAIL_SYNC_ANALYSIS_WORKERS` 调整（范围 1–8）。遇到 Gmail 或 LLM provider 限流时应降低对应值。分类完成后仍按邮件时间顺序串行写入 SQLite。

投递追踪页面支持手动同步、暂停/恢复自动同步、清空数据并暂停，以及清空后重新分析最近 30 天。重新分析在后台执行，页面实时显示处理阶段、进度条和分类计数。同步历史以可展开卡片显示，并区分识别邮件数与受影响投递数；同一投递的多封邮件会合并为一条投递时间线。清空和同步共享数据库租约，多进程运行时不会并发写入同一批邮件。

识别时先使用本地规则保留明确的投递、测评、面试、Offer、拒绝和撤回通知，并过滤 Job Alert、职位推荐和带群发邮件头的 ATS 订阅。只有状态不明确、关键信息缺失或规则信号冲突的邮件才会发送给当前默认 LLM 做结构化裁决；设置 `EMAIL_LLM_CLASSIFICATION_ENABLED=0` 可关闭此功能。`unknown`、`N/A`、`not specified` 等公司或职位占位值会统一视为缺失信息，后续同一投递的邮件可补全真实字段。系统支持纯文本和 HTML-only 邮件，不保存邮件正文，只保存正文哈希、结构化分析、模型/延迟/token 指标和人工确认标签。投递追踪页会显示 LLM 调用量、失败、待确认、规则分歧及基于人工确认的本地准确率。

## 对比脚本

可以使用 `scripts/compare_title_gate.py` 做受控 A/B 对比，比较两种流程：

- `baseline_gate_off`
- `title_gate_on`

脚本固定使用以下实验条件：

- 固定 title：`AI Engineer`、`Machine Learning Engineer`、`LLM Engineer`、`Software Engineer`、`Backend Engineer`
- 每个 title 抓取 `30` 条
- 仅抓取 `7` 天内（`168` 小时）
- 仅使用 `Indeed`
- 地点固定为 `Ireland`

示例：

```bash
python scripts/compare_title_gate.py --cv-path "path/to/test_cv.md" --keep-db --out reports/compare_report.json
```

常用参数：

- `--cv-path`：测试 CV 文件路径（支持 `.md` / `.txt` / `.docx`）
- `--out`：保存完整 JSON 对比报告
- `--keep-db`：把 baseline / improved 两轮 sqlite 数据库保存在 `reports/compare_runs/<时间戳>/`
- `--db-dir`：手动指定 sqlite 实验产物输出目录

生成的 `reports/compare_report.json` 里，最值得看的是：

- `summary`：总量、token 成本、每个新增职位成本
- `diff.baseline_only_jobs`：只在 baseline 中保留下来的职位
- `diff.improved_only_jobs`：只在 title gate 版本中保留下来的职位
- `diff.title_gate_rejections`：被 title gate 明确拒绝的标题及原因

## Matching 语义说明

- `location_score` 现在只表示更宽层的地理兼容性。
- 同一目标国家内的跨城市搬迁，以及 `hybrid`、`onsite`、`每周几天办公室办公` 之类要求，会被视为现实执行风险。
- 这些因素会进入 `risks / risk_penalty`，但不应再作为 `location_score` 的扣分项。

## 隐私说明

- **CV 内容**会发送给你配置的 LLM API（Anthropic / Google / OpenAI 等）用于解析和评估。请确认你信任所选 provider 的数据政策。
- **所有数据本地存储**：CV 解析结果和职位信息存储在本机 SQLite 数据库（`data/jobradar_cache.db`），不上传至任何第三方服务器。
- **日志文件**（`logs/jobradar.log`）记录搜索词和操作时间，不包含 CV 个人信息或 API Key，且已加入 `.gitignore`。

## 已知限制

本项目由个人利用业余时间维护。部分功能（尤其是**基于地点的过滤**）可能因职位来源不同而产生不一致的结果。

**LLM Provider 支持**：目前已集成 17 个 Provider，但并非所有 Provider 均经过完整的端到端测试。如果你在使用某个 Provider 或模型时遇到问题，欢迎[提交 Issue](https://github.com/sangowu/JobRadar/issues)，并附上 Provider 名称、模型名称及错误信息。

## 法律免责声明

本工具通过 [python-jobspy](https://github.com/cullenwatson/JobSpy) 抓取 Indeed 等招聘平台的公开数据。

> **使用前请注意：** 网络抓取可能违反相关网站的服务条款（ToS）。本工具仅供**个人求职、学习和研究**使用。用户需自行承担合规责任，作者不对任何滥用行为负责。请合理控制抓取频率，勿用于商业或批量采集目的。
