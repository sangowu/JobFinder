# JobFinder

> **中文** · [English](README.md) · [Español](README.es.md)

根据你的 CV 自动搜索全球职位，LLM 匹配评分，多来源聚合去重。

## 快速开始

```bash
uv sync
uv run jobfinder serve       # 启动 Web UI（http://127.0.0.1:8765）
# 浏览器打开后，在「API 配置」页面填入 API Key 即可开始使用
# 也可手动配置 .env：
cp .env.example .env         # 填入 API Key
uv run jobfinder find cv.docx  # CLI 模式
```

## 常用命令

| 命令 | 说明 |
|---|---|
| `uv run jobfinder serve` | 启动 Web UI |
| `uv run jobfinder serve --mock` | 测试模式（独立 DB，不污染正式缓存） |
| `uv run jobfinder find cv.docx` | CLI：解析 CV → 发现 title → 抓取 → 评估 |
| `uv run jobfinder find cv.docx --refresh` | 忽略缓存，强制重新搜索 |
| `uv run jobfinder results` | 浏览缓存中最近的搜索结果 |
| `uv run jobfinder assess` | 对缓存 JD 单独补跑 LLM 评估 |
| `uv run jobfinder model` | 交互式选择 LLM provider 和模型 |
| `uv run jobfinder cache clear` | 清空所有缓存 |
| `uv run jobfinder --version` | 显示当前版本号 |

## Pipeline 概览

```
CV 文件
  │
  ▼ ① CV 解析（LLM → CVProfile）← SHA-256 永久缓存
  ▼ ② Title 发现（Adzuna API + LLM）← 7 天缓存
  ▼    用户确认 title 列表
  ▼ ③ 抓取（Indeed + LinkedIn，JobSpy，无浏览器）
         LLM 标题预筛 → 串行限速（Indeed 2s / LinkedIn 3s）→ URL 去重
  ▼ ④ 预筛漏斗：年资 → 相关性 → URL 缓存命中 → 关闭检测 → 经验年限 → 技能关键词
  ▼ ⑤ LLM 批量评估（score / strengths / weaknesses / matched_keywords）
  ▼ ⑥ 统计报告写入 reports/pipeline_stats.jsonl
  ▼    Web UI / 终端展示
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

# Adzuna（Title 发现，免费注册：developer.adzuna.com）
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# 默认模型（由 jobfinder model 命令自动写入）
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

## Web UI 功能

- **实时进度**：搜索期间 SSE 逐条推送职位卡片
- **管道漏斗统计**：搜索完成后在进度日志和完成卡片展示各阶段明细（抓取量 → LLM 标题过滤 → 预筛漏斗 → LLM 评估 → 最终保存量 / 过滤率）
- **三栏布局**：职位列表 + 详情 + CV 上传/搜索面板
- **多来源聚合**：同一职位在 Indeed 和 LinkedIn 均出现时自动合并，卡片徽标可点击跳转对应来源；详情页 Apply 按钮变为多来源下拉菜单
- **搜索历史**：每条记录可展开 📊 管道漏斗详情，按来源（Indeed / LinkedIn）分项显示
- **日志面板**：级别过滤、关键词高亮、自动刷新
- **配置页**：在线管理 LLM API Key 和 Adzuna 职位检索 API、选择默认模型、清除缓存；新用户无需编辑 `.env`，直接在页面完成所有配置
- **多语言**：界面支持中文 / English / Español 切换

## 统计报告

每次搜索完成后自动写入 `reports/` 目录：

| 文件 | 说明 |
|---|---|
| `pipeline_stats.jsonl` | 逐行追加，保存全量历史，每行一次搜索的完整漏斗数据 |
| `pipeline_stats_latest.json` | 覆盖写入，始终为最新一次搜索的 JSON 报告 |

## 隐私说明

- **CV 内容**会发送给你配置的 LLM API（Anthropic / Google / OpenAI 等）用于解析和评估。请确认你信任所选 provider 的数据政策。
- **所有数据本地存储**：CV 解析结果和职位信息存储在本机 SQLite 数据库（`jobfinder_cache.db`），不上传至任何第三方服务器。
- **日志文件**（`jobfinder.log`）记录搜索词和操作时间，不包含 CV 个人信息或 API Key，且已加入 `.gitignore`。

## 法律免责声明

本工具通过 [python-jobspy](https://github.com/cullenwatson/JobSpy) 抓取 Indeed 等招聘平台的公开数据。

> **使用前请注意：** 网络抓取可能违反相关网站的服务条款（ToS）。本工具仅供**个人求职、学习和研究**使用。用户需自行承担合规责任，作者不对任何滥用行为负责。请合理控制抓取频率，勿用于商业或批量采集目的。
