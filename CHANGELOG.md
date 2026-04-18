# Changelog

## [Unreleased]

### Breaking Changes

- **项目从 JobFinder 重命名为 JobRadar**
  包目录 `jobfinder/` → `jobradar/`，PyPI 包名、CLI 入口、所有模块导入路径同步更新。
  `pyproject.toml` 补充 `[build-system]` 块（`setuptools.build_meta`），修复 `uv run jobradar` entry point 无法安装的问题。
  `.gitignore` 模板路径从 `jobfinder/templates/` 更新为 `jobradar/templates/`。

### Improvements

- **UI 品牌更新**（`index.html`）
  页面标题、导航栏 Logo、三语 i18n（`cfg.enter`）全部从 JobFinder 更新为 JobRadar。

### New Features

- **动态版本号**（`__init__.py` / `cli.py` / `server.py` / `index.html`）
  `jobfinder/__init__.py` 通过 `importlib.metadata.version()` 动态读取版本，唯一真相源为 `pyproject.toml`。
  CLI 新增 `--version` / `-V` 选项，打印 `jobfinder vX.Y.Z` 后退出。
  `/api/config` 响应新增 `version` 字段；Web UI 导航栏 "JobFinder" 旁显示当前版本号。

- **LLM 拒绝职位写入缓存**（`schemas.py` / `agent.py`）
  `JobAssessment` 新增 `is_relevant: bool`（默认 `True`）字段持久化 LLM 的 relevant 判断。
  被拒绝的职位（`is_relevant=False`）现在也写入缓存；下次命中该 URL 时直接跳过，不再重复评估。
  `/api/jobs` 和 SSE job 推送过滤掉 `is_relevant=False` 的条目，UI 不显示被拒绝的职位。

- **搜索后去重验证**（`jobfinder/dedup_check.py` / `server.py` / `index.html`）
  新增 `dedup_check.py` 模块，搜索完成后自动对本次结果运行字段级去重验证：
  - L1：`dedup_key` 精确重复（PRIMARY KEY 保证，验证用）
  - L2：同一 URL 出现在不同 `dedup_key` 下（跨来源合并漏洞）
  结果附在 SSE `done` 事件的 `dedup` 字段，完成卡片漏斗统计下方展示。
  `scripts/verify_dedup.py` 也可单独运行对全量缓存做检查。

### Improvements

- **JD 内容截断上限从 4000 → 8000 字符**（`agent.py`）
  实测缓存数据中 61% 的 JD 超过 4000 字符，中位数 4384，均值 4646，最长 8368。
  将 `_batch_assess_jds` 中的 `content[:4000]` 改为 `content[:8000]`，覆盖绝大多数完整 JD。

### Documentation

- **README 拆分为三个语言文件**
  原单文件三语 README 拆分为 `README.md`（英文，GitHub 首页默认）、`README.zh.md`（中文）、`README.es.md`（西班牙文）。
  每个文件顶部均有三语切换链接，当前语言加粗显示。

- **`BUGFIX_LOG.md` 新建**
  记录每次 bug 的错误现象、根本原因、解决方案和修复结果。

### Bug Fixes

- **`cache.py` SyntaxError：SQL 注释语法混入 Python 字符串**（详见 BUGFIX_LOG BUG-001）
- **`_row_to_job` Pydantic ValidationError：`sources` 列混入 dict 对象**（详见 BUGFIX_LOG BUG-002）

---

## [0.2.0] — 2026-04-17

### New Features

- **多来源原始记录（`raw_sources`）**（schemas.py / cache.py / agent.py / tools.py）
  `job_cache` 新增 `raw_sources` 列，存储每个来源的原始记录 `[{source, url, date_posted}]`。
  同一职位在 Indeed 和 LinkedIn 均出现时，两条来源 URL 均被保留，支持后续多来源跳转。
  `agent.py` 的 `_job_all_sources` 从 `list[str]` 改为 `list[dict]`，预计算阶段同时收集 URL 和发布日期。

- **卡片来源徽标可点击**（index.html）
  职位卡片上的 Indeed / LinkedIn 徽标改为 `<a>` 标签，点击直接跳转对应来源 URL（来自 `raw_sources`）。
  点击徽标不触发卡片选中（`stopPropagation`）。

- **详情页 Apply 多来源下拉**（index.html）
  单来源时 Apply 保持普通按钮；多来源时变为下拉按钮，展开后列出每个来源（含彩色圆点区分 Indeed/LinkedIn），点击跳转对应平台。
  点击其他区域自动收起下拉菜单。

- **搜索统计记录 `cv_hash`**（cache.py / server.py）
  `search_stats` 表新增 `cv_hash` 列，每次搜索记录所用 CV 的哈希值，支持按 CV 版本分组统计。

### Improvements

- **年资过滤支持罗马数字等级**（filters.py）
  新增对 `I / II / III / IV / V` 罗马数字职级的识别，对应 junior / mid / senior / staff+ 等级。
  每个 seniority 可见当前及下一级职位（向下兼容），不可见更高级职位。
  `junior` 从 `new_grad/intern` 分组独立出来单独处理（junior 可见 II，new_grad/intern 不可见 II）。

- **CV 解析 prompt 优化**（cv_extractor.py）
  移除 PII 字段 `name`，改为从 `summary` 隐式描述（"不包含姓名"）。
  `seniority` 字段移至 `preferred_roles` 前，确保 LLM 生成职位时已知资历级别。
  `preferred_roles` 删除"变体层"，改为精准→宽泛两层梯度，避免父词已覆盖时产生重复搜索。
  英语市场 `search_terms` 三条规则合并为一条，让模型根据 `preferred_locations` 自行选词。

- **关闭检测正则扩充**（schemas.py / tools.py）
  新增 `expired on indeed`、`this exact role may not be open`、`posting is to advertise potential job opportunities` 三个 Indeed 特有的关闭信号。
  修复 `has been` → `has been|has` 漏判（如 "This job has expired"）。

- **截止日期正则扩充**（agent.py）
  新增 8 个触发词：`expiring/expires on`、`last date/day to apply`、`last application date`、`position/vacancy closes`、`accepting applications until`、`applications accepted until`、`submit application by`。

- **职位缓存 TTL 延长至 14 天**（schemas.py）
  `DEFAULT_TTL_DAYS` 从 7 → 14，减少频繁重新抓取。

- **`title_discovery.py` 去除 AI/ML 硬编码偏见**（title_discovery.py）
  删除死代码 `_select_search_keywords`（含硬编码 "优先选 AI/ML 专属技术词" 指令）及其关联的 `_KeywordResult` 类。
  `_cluster_titles` prompt 中的归并示例改为领域中性示例（Backend Developer / Frontend Engineer / UX Designer）。

### Removed

- **`company_lookup.py` 删除**（company_lookup.py）
  公司信息查询功能（DDG 搜索 + Jina Reader + LLM → CompanyProfile）依赖链脆弱且无评分数据，正式移除。
  级联清理：`CompanyProfile` 类（schemas.py）、`company_cache` 表及相关函数（cache.py）、CLI 显示列（display.py）、`--enrich` 命令引用。

## [Unreleased] — 2026-04-15 (continued 2)

### New Features

- **UI 设计规范 Skill**（`.claude/commands/jobfinder-ui-design.md`）
  新增 Claude Code skill，记录 JobFinder 前端设计系统，涵盖：布局规范、字体层级、颜色系统（含 dark mode 对照）、按钮三级层级（primary / secondary / danger）、间距、过渡动画（统一 0.25s）、表单 focus 样式、Toast 规范、加载/空态、i18n 规则、嵌套点击模式、常见错误对照表。

- **Toast 重写**（index.html）
  底部居中显示，成功绿色（`bg-green-600`）、失败红色（`bg-red-600`），自动关闭时间从 2.8s 延长至 5s，新增 ✕ 关闭按钮（点击同时取消自动关闭计时器）。

- **全局 input focus 蓝色发光**（index.html）
  在全局 `<style>` 中统一 `input / select / textarea` 聚焦时样式：`border-color: #3b82f6`，`box-shadow: 0 0 0 3px rgba(59,130,246,0.25)`，不再依赖各处分散的 Tailwind focus 类。

- **导航栏职位计数加单位**（index.html）
  Logo 右侧数字后补充「个职位 / jobs / empleos」文字描述，随语言切换。

### UI

- **职位详情新增区块标题**（index.html）
  技能匹配区块与职位描述区块各增加独立标题行（`text-sm font-bold uppercase tracking-wide`），标题与内容间距统一 `mb-[18px]`。

- **所有区块标题统一规范**（index.html）
  优势、待加强、技能匹配、职位描述标题统一从 `text-xs font-semibold` 改为 `text-sm font-bold`。

## [Unreleased] — 2026-04-15 (continued)

### New Features

- **搜索历史页面**（index.html / server.py / cache.py）
  导航栏新增「历史」入口，打开全屏历史页面，展示每次搜索的时间、职位/地区、模型、耗时、输入/输出 tokens、职位数。
  页面顶部汇总卡片显示累计搜索次数、职位数、总 tokens 消耗、总耗时。
  新增「清空历史」按钮（带二次确认弹窗）。
  持久化层：`cache.py` 新增 `search_stats` 表及 `save_search_stats` / `get_search_stats` / `get_stats_summary` / `clear_search_stats` 函数。
  后端：新增 `GET /api/stats` 和 `DELETE /api/stats` 端点。

- **实时耗时计时器**（index.html）
  搜索进行中，搜索面板右上角实时显示已用时间（秒/分钟）。

- **搜索完成耗时与 token 汇总**（index.html / server.py）
  搜索完成后，在「搜索完成」提示下方显示总耗时和本次输入/输出 token 消耗。
  导航栏同步累计显示本次会话的 token 总量（悬停可查看输入/输出明细）。

- **每个职位抓取上限输入**（index.html / server.py / agent.py）
  搜索面板新增「每个职位抓取上限」数字输入框（默认 200，范围 10~500），允许用户按需调整单 role 抓取量。

### Bug Fixes

- **`API 配置`导航按钮文字未跟随语言切换**（index.html）
  该按钮文字为硬编码中文，未使用 `data-i18n` 属性，补全修复。

- **职位详情面板中文硬编码**（index.html）
  「返回搜索」、「来源：」、「优势」、「待加强」、「投递 →」、「暂无 JD 内容」均为硬编码中文，全部改为 `t()` 查表，支持三语切换。

- **搜索完成耗时/token 文字未跟随语言**（index.html）
  `done` 事件中的耗时和 token 汇总文字为硬编码中文，改为使用 `done.elapsed` / `done.tokens` i18n key。

- **状态 dot 按钮点击无响应**（index.html）
  `data-dot` / `data-label` span 元素从未绑定点击事件，点击只会触发卡片整体的 `showJobDetail`。修复：在 `buildCard` 的 click handler 中优先拦截 dot 点击，阻止冒泡并切换对应状态。

- **切换 dot 状态时排版抖动**（index.html）
  标签文字长度不一致（如 "Applied" vs "Not Applied"）导致卡片宽度变化。给标签 span 设置固定宽度 `w-20` + `truncate` 解决。

- **切换语言时已渲染内容不刷新**（index.html）
  `setLang()` 中补充调用 `renderList()` 和 `showJobDetail(activeJob)`，确保卡片列表及当前打开的职位详情随语言变更同步重新渲染。

- **tooltip 悬停文字未跟随语言**（index.html）
  日志按钮、session tokens、刷新/清空/关闭等元素的 `title` 属性为硬编码中文。将所有 `title=` 改为 `data-i18n-title=` 属性，`applyLang()` 中新增对 `[data-i18n-title]` 的遍历处理；动态 tooltip（session tokens 明细）改用 `t()` 生成。

### UI

- **左侧卡片栏加宽**（index.html）
  宽度从 `w-80`（320px）调整为 `w-96`（384px），避免状态标签文字被截断。

### i18n

- 补全所有缺失的 i18n key（三语）：`nav.history`、`hist.*`（历史页面）、`done.elapsed`、`done.tokens`、`detail.*`（职位详情）、`tip.*`（tooltip）。

## [Unreleased] — 2026-04-15

### New Features

- **SSE 超时自动续期**（server.py）
  SSE 进度流默认等待上限从硬编码 10 分钟改为可配置（`SSE_TIMEOUT_MINUTES`，默认 30 分钟）。
  到达上限时若搜索仍在进行，自动续期最多 4 次（总上限 150 分钟），解决大量 JD 时 LLM 评估超时断开问题。

### Security Fixes

- **SQL 注入修复**（cache.py）
  `clean_expired()` 中 `JOB_TTL_DAYS` / `SESSION_TTL_HOURS` 直接通过 f-string 插入 SQL 语句，全部改为参数化查询（`execute("... > ?", (int(...),))`）。

- **`/api/config` 写入白名单**（server.py）
  `POST /api/config` 端点新增 `_ALLOWED_ENV_KEYS` 白名单校验，不在白名单的 key 返回 HTTP 400，防止任意环境变量被写入。

- **`subprocess.Popen(shell=False)`**（display.py）
  Windows 下打开 VSCode 时 `shell=True` 改为 `shell=False`，消除潜在命令注入风险。

### Documentation

- **README 三语化**（README.md）
  全面重写为中文 / English / Español 三语版本，顶部导航锚点快速跳转。
  新增**隐私说明**（CV 内容发送至 LLM API、数据本地存储说明）和 **JobSpy 法律免责声明**。

- **`.env.example` 补全**（.env.example）
  补充代码中用到但此前未记录的环境变量：`LOCAL_LLM_BASE_URL`、`LOCAL_LLM_API_KEY`、`TAVILY_API_KEY`、`LOG_LEVEL`、`LOG_FILE`、`SSE_TIMEOUT_MINUTES`；
  同步补齐所有 LLM provider 的 API Key 条目（`DASHSCOPE_API_KEY` / `ZHIPUAI_API_KEY` / `MOONSHOT_API_KEY` / `MINIMAX_API_KEY` / `XAI_API_KEY` / `MISTRAL_API_KEY`）。

- **LICENSE**（LICENSE）
  新增 MIT License 文件。

### Removed / Cleanup

- **移除 `langchain-anthropic` 僵尸依赖**（pyproject.toml）
  代码中无任何引用，清除该无用依赖。

- **清理根目录调试文件**
  删除开发过程中遗留的探索脚本、CDP/Playwright 调试产物、截图、文本转储及个人搜索结果文件（共 20+ 个文件）。

- **`.gitignore` 完善**
  新增 `CLAUDE.md`、`.claude/`、`*.log`、`jobspy_*.md` 排除规则，确保本地私有文件、日志、IDE 配置不被提交。

## [Unreleased] — 2026-04-14

### New Features

- **Web UI 语言切换（ZH / EN / ES）**（index.html）
  导航栏右侧新增语言切换按钮，切换后页面所有静态文本及 LLM 评估输出语言同步变更（Mode A：已缓存数据不重新生成）。所有静态文本改为 `data-i18n` 属性驱动，JS 动态文案通过 `t('key')` 查表。

- **搜索全部确认 role**（cli.py / server.py）
  移除原有「压缩为 2-3 个搜索词」的限制，改为直接搜索所有用户确认的 role，避免遗漏。

- **JobSpy 抓取量提升至 200 条/role**（scraper_jobspy.py / scrapers_jobspy.py / agent.py）
  `limit_per_role` 默认值从 20 → 200，`agent.py` 中硬编码的 60 同步修正为 200。

- **JD 描述截断提升至 15000 字符**（scraper_jobspy.py / tools.py）
  存储与展示的 `description_snippet` 从 8000 → 15000 字符，LLM 评估仍使用前 4000 字符以控制 token 消耗。

- **过滤漏斗汇总日志**（agent.py）
  `_write_scraped` 结束时输出一行汇总，统计输入总数及各阶段过滤量：去重 / 年资 / 不相关 / 缓存命中 / fetch 失败 / 已关闭 / 经验超限 / 技能不符 / LLM 拒绝 / 最终保存。

- **搜索进度面板自动填充视口高度**（index.html）
  搜索期间进度日志区随内容增长，触顶后转为内部滚动，底部保留留白。

- **配置页「刷新模型列表」按钮**（index.html / server.py / model_fetcher.py）
  新增 `POST /api/models/refresh` 端点及对应 UI 按钮；点击后对所有已配置 API Key 的 provider 调用官方 models 接口，更新当前会话的可选模型下拉列表。抓取逻辑封装于 `jobfinder/model_fetcher.py`。

- **`scripts/fetch_openrouter_models.py` 重写**（scripts/）
  改为从各 provider 官方 API 端点获取模型，而非依赖 OpenRouter 命名。支持 `--patch` 直接写入 `llm_backend.py`，`--debug <prefix>` 查看原始模型列表。过滤规则：排除图像/音频/robotics/TTS/embedding 等非对话模型，Gemini 额外排除 `-latest` 别名及 Gemma 系列。

- **配置页「清除缓存」按钮**（index.html / server.py）
  新增 `POST /api/cache/clear` 端点及 UI 按钮，无需 CLI 即可清空缓存。

- **配置页关闭按钮**（index.html）
  配置页右上角新增 × 按钮，修复进入后无法返回主界面的问题。

### Bug Fixes

- **首次启动左侧卡片栏一直显示「加载中」**（index.html）
  新增 `isLoading` 标志，`loadJobs()` 完成后置 `false`；数据为空时改为显示「暂无职位数据，请上传 CV 开始搜索」，区别于加载中状态。

- **mock 模式下 `load_dotenv(override=True)` 覆盖运行时环境变量**（server.py）
  新增 `_reload_dotenv()`，在重新加载 `.env` 前保存并还原 `JOBFINDER_MOCK` / `CACHE_DB_PATH` 等运行时注入的 key，防止 mock 数据库路径被覆盖。

- **LLM 评估输出为英文**（agent.py）
  在 system prompt 和 user prompt 头部双重声明输出语言，并为每个文字字段单独标注语言要求，强制模型遵守语言设置。

- **LLM 为 new_grad 虚构年限优势**（agent.py）
  prompt 中明确传入 `profile.years_of_experience`，要求 JD 要求年限超过候选人时必须列入劣势；`strengths` 范围改为 0~5 条，无实质优势时返回空列表。

- **`NameError: name 'search_queries' is not defined`**（server.py）
  移除 `_condense_search_queries` 后遗留的变量引用未清理，修正为 `req.roles`。

- **`RecursionError` in `_reload_dotenv`**（server.py）
  `replace_all` 替换时误将函数体内的 `load_dotenv` 调用也替换为自身，导致无限递归，已恢复函数体内为直接调用。

### Removed

- **移除 `meta` / `huggingface` / `doubao` / `siliconflow` / `openrouter` provider**（llm_backend.py）
  精简 provider 列表，移除使用率低或 OpenRouter 命名与原生 API 不一致的 provider。

### Refactoring

- **provider 可选模型列表更新**（llm_backend.py）
  `AVAILABLE_MODELS` 及 `DEFAULT_MODELS` 与实际 API 对齐，默认模型统一选各 provider 轻量级版本（Flash / Mini / Small）。

## [Unreleased] — 2026-04-11

### Bug Fixes

- **`subprocess.mswindows` 已移除**（display.py）
  Python 3.13 删除了 `subprocess.mswindows` 私有属性，改用 `sys.platform == "win32"` 检测 Windows 环境，修复 VSCode/Obsidian 打开 JD 时崩溃的问题。

- **JD 内容被截断为 500 字**（agent.py）
  写入缓存前 `content[:500]` 导致 VSCode 查看 JD 时只能看到片段，改为存储完整 JD 内容。

- **年资过滤漏掉 `Sr.` 等带点缩写**（filters.py）
  title 按 `[\s/\-,|@()]+` 分割时 `"Sr."` 变为 `"sr."`（带点），无法命中过滤词集合 `"sr"`。将分隔符正则改为 `[\s/\-,|@().]+`，含 `.`，修复所有带点缩写（Sr./Jr. 等）的漏过问题。

- **URL 缓存命中返回旧记录缺失 assessment**（agent.py）
  重新搜索时命中 URL 缓存的旧记录（assessment 列为 NULL），直接复用导致 VSCode 评分段落为空。现在区分两种情况：有 assessment 则直接复用；无 assessment 但有 CV 数据则复用缓存 JD 内容补跑 LLM 评估，不重新 fetch 页面。

- **`assessment` 变量作用域问题**（agent.py）
  当 `cv_summary` 或 `cv_skills` 为空时，`assessment` 变量未定义但被引用，改为显式赋值 `job_assessment = assessment if (cv_summary and cv_skills) else None`。

- **`find` 与 `results` 命令结果排序不一致**（cli.py / cache.py）
  `find` 保持抓取顺序，`results` 按 `fetched_at DESC`，两者显示顺序不同。统一在 `find` 展示前也按 `fetched_at DESC` 排序。

### New Features

- **`results` 命令**（cli.py）
  新增 `uv run jobfinder results [--limit N]`，直接从缓存读取最近搜索结果并进入交互菜单，无需重新搜索。

- **VSCode/Obsidian 查看完整 JD**（display.py）
  交互菜单新增"在 VSCode/Obsidian 中查看完整 JD"选项，将职位信息写入临时 Markdown 文件并打开。

- **模型评分段落**（display.py / agent.py / schemas.py / cache.py）
  JD Markdown 末尾自动附加模型评分段落，包含：
  - 整体匹配分 0~10（带进度条）
  - CV 对该 JD 的优势（2~4 条）
  - CV 对该 JD 的劣势（2~4 条）
  - 公司评分（占位符，待后续实现）
  评估结果复用搜索管道内已有的 `_assess_jd` 调用，零额外 LLM 开销，持久化到 `job_cache.assessment` 列。

- **CV 解析缓存**（cv_extractor.py / cache.py）
  以 `SHA-256(cv_text)` 为 key，CV 内容不变时跳过 LLM 解析直接返回缓存结果。`--refresh` 强制重新解析。

- **Title 发现缓存**（cli.py / cache.py）
  Adzuna title 发现结果以 `cv_hash::countries` 为 key 缓存 7 天，同一 CV 重复运行时跳过 Adzuna API 查询和三次 LLM 调用。`--refresh` 强制重新发现。

- **URL 级别缓存**（agent.py / cache.py）
  `_write_scraped` 中对每条职位先查 `job_cache.url`，命中且未过期则跳过 Jina fetch 和 LLM 评估，直接复用缓存结果。

- **Indeed 点击前 URL 缓存检查**（scrapers.py）
  在点击卡片读右侧面板之前先查 URL 缓存，命中则跳过点击，直接使用缓存内容，减少浏览器交互次数。

- **年资 title 过滤**（filters.py / agent.py / scrapers.py）
  新增 `filters.py` 模块，集中管理年资过滤词表和 `is_seniority_ok()` 函数：
  - `new_grad / intern / junior`：跳过含 senior/sr/staff/lead/principal/director/head/vp/manager/architect/cto 等的 title
  - `mid`：跳过极高级职位 + 实习/应届专项词（保留 senior/lead）
  - `senior / lead`：跳过含 intern/internship/placement/junior/jr/associate/entry 等的 title
  - Indeed：过滤提前至 LLM 批量评分之前（阶段1.5），减少 LLM token 消耗
  - 其余站点：在 `_write_scraped` 第一步过滤

- **`_assess_jd` 扩展**（agent.py）
  原二元判断（relevant/reason）扩展为同时输出 `score`（0~10）、`strengths`（优势列表）、`weaknesses`（劣势列表），一次 LLM 调用获取所有评估信息。

- **Title 发现关键词优化**（title_discovery.py / cli.py）
  `top_keywords` 从 5 提升到 8（`half` 从 2→4），role_phrases 增至 4 个，覆盖 AI/ML/Data/LLM 多个方向；prompt 明确要求涵盖 ML Engineer、MLOps 等相邻角色，避免只生成 AI Engineer + Software Engineer 两个词。

### Refactoring

- **年资过滤逻辑抽取**（filters.py）
  原 `_is_seniority_ok` 和过滤词集合从 `agent.py` 抽取到独立的 `filters.py`，避免 `scrapers.py` → `agent.py` 的循环依赖。

- **缓存表扩展**（cache.py）
  新增 `cv_cache`、`title_cache` 表；`job_cache` 新增 `assessment` 列（旧库通过 `ALTER TABLE` 自动迁移）；新增 `get_job_by_url()`、`get_cv_profile()`、`save_cv_profile()`、`get_title_cache()`、`save_title_cache()` 函数；`clear_all()` 同步清空所有新表。
