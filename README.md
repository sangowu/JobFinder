# JobFinder

> [中文](#中文) · [English](#english) · [Español](#español)

---

## 中文

根据你的 CV 自动搜索全球职位，LLM 匹配评分，分析目标公司信息。

### 快速开始

```bash
uv sync
uv run jobfinder serve       # 启动 Web UI（http://127.0.0.1:8765）
# 浏览器打开后，在「API 配置」页面填入 API Key 即可开始使用
# 也可手动配置 .env：
cp .env.example .env         # 填入 API Key
uv run jobfinder find cv.docx  # CLI 模式
```

### 常用命令

| 命令 | 说明 |
|---|---|
| `uv run jobfinder serve` | 启动 Web UI |
| `uv run jobfinder serve --mock` | 测试模式（独立 DB，不污染正式缓存） |
| `uv run jobfinder find cv.docx` | CLI：解析 CV → 发现 title → 抓取 → 评估 |
| `uv run jobfinder find cv.docx --refresh` | 忽略缓存，强制重新搜索 |
| `uv run jobfinder find cv.docx --enrich` | 额外查询高分职位的公司信息 |
| `uv run jobfinder results` | 浏览缓存中最近的搜索结果 |
| `uv run jobfinder assess` | 对缓存 JD 单独补跑 LLM 评估 |
| `uv run jobfinder model` | 交互式选择 LLM provider 和模型 |
| `uv run jobfinder cache clear` | 清空所有缓存 |

### Pipeline 概览

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
  ▼    [可选] 公司信息查询（--enrich）
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

### 环境变量

```env
# LLM Provider（至少配置一个）
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# 本地模型
OLLAMA_BASE_URL=http://localhost:11434
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# Adzuna（Title 发现，免费注册：developer.adzuna.com）
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# 默认模型（由 jobfinder model 命令自动写入）
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

### Web UI 功能

- **实时进度**：搜索期间 SSE 逐条推送职位卡片
- **管道漏斗统计**：搜索完成后在进度日志和完成卡片展示各阶段明细（抓取量 → LLM 标题过滤 → 预筛漏斗 → LLM 评估 → 最终保存量 / 过滤率）
- **三栏布局**：职位列表 + 详情 + CV 上传/搜索面板
- **搜索历史**：每条记录可展开 📊 管道漏斗详情，按来源（Indeed / LinkedIn）分项显示
- **日志面板**：级别过滤、关键词高亮、自动刷新
- **配置页**：在线管理 LLM API Key 和 Adzuna 职位检索 API、选择默认模型、清除缓存；新用户无需编辑 `.env`，直接在页面完成所有配置
- **多语言**：界面支持中文 / English / Español 切换

### 统计报告

每次搜索完成后自动写入 `reports/` 目录：

| 文件 | 说明 |
|---|---|
| `pipeline_stats.jsonl` | 逐行追加，保存全量历史，每行一次搜索的完整漏斗数据 |
| `pipeline_stats_latest.json` | 覆盖写入，始终为最新一次搜索的 JSON 报告 |

字段包括：各来源抓取量、LLM 标题过滤量、预筛各步计数、LLM 评估通过率、最终过滤率等。

### 隐私说明

- **CV 内容**会发送给你配置的 LLM API（Anthropic / Google / OpenAI 等）用于解析和评估。请确认你信任所选 provider 的数据政策。
- **所有数据本地存储**：CV 解析结果和职位信息存储在本机 SQLite 数据库（`jobfinder_cache.db`），不上传至任何第三方服务器。
- **日志文件**（`jobfinder.log`）记录搜索词和操作时间，不包含 CV 个人信息或 API Key，且已加入 `.gitignore`。

### 法律免责声明

本工具通过 [python-jobspy](https://github.com/cullenwatson/JobSpy) 抓取 Indeed 等招聘平台的公开数据。

> **使用前请注意：** 网络抓取可能违反相关网站的服务条款（ToS）。本工具仅供**个人求职、学习和研究**使用。用户需自行承担合规责任，作者不对任何滥用行为负责。请合理控制抓取频率，勿用于商业或批量采集目的。

---

## English

Automatically search global job listings based on your CV, score matches with LLM, and analyze target companies.

### Quick Start

```bash
uv sync
uv run jobfinder serve       # Launch Web UI (http://127.0.0.1:8765)
# Open your browser and configure API keys in the "API Config" page
# Or configure manually via .env:
cp .env.example .env         # Fill in your API keys
uv run jobfinder find cv.docx  # CLI mode
```

### Commands

| Command | Description |
|---|---|
| `uv run jobfinder serve` | Launch Web UI |
| `uv run jobfinder serve --mock` | Test mode (isolated DB, won't affect real cache) |
| `uv run jobfinder find cv.docx` | CLI: parse CV → discover titles → scrape → assess |
| `uv run jobfinder find cv.docx --refresh` | Force re-search, ignore all caches |
| `uv run jobfinder find cv.docx --enrich` | Also fetch company info for top-scored jobs |
| `uv run jobfinder results` | Browse cached results from the last search |
| `uv run jobfinder assess` | Re-run LLM assessment on cached JDs |
| `uv run jobfinder model` | Interactively choose LLM provider and model |
| `uv run jobfinder cache clear` | Clear all caches |

### Pipeline Overview

```
CV file
  │
  ▼ ① CV parsing (LLM → CVProfile)  ← permanent SHA-256 cache
  ▼ ② Title discovery (Adzuna API + LLM)  ← 7-day cache
  ▼    User reviews & confirms title list
  ▼ ③ Scraping (Indeed + LinkedIn, JobSpy, no browser)
         LLM title pre-filter → rate-limited serial (Indeed 2s / LinkedIn 3s) → URL dedup
  ▼ ④ Filter funnel: seniority → relevance → URL cache hit → closed → exp limit → skills
  ▼ ⑤ Batch LLM assessment (score / strengths / weaknesses / matched_keywords)
  ▼    [Optional] Company lookup (--enrich)
  ▼ ⑥ Pipeline stats written to reports/pipeline_stats.jsonl
  ▼    Web UI / terminal display
```

Real-world funnel (actual data):
```
Indeed 741 + LinkedIn 255 = 996 scraped
  → LLM title filter   996 → 689  (30.8% removed)
  → Pre-filter funnel  689 → 76   (seniority / dedup / skills etc.)
  → LLM assessment      76 → 54 saved  (71.1% pass rate)
  → Overall filter rate: 94.6%  (only 54 of 996 require human review)
```

### Environment Variables

```env
# LLM Provider (configure at least one)
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# Local models
OLLAMA_BASE_URL=http://localhost:11434
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# Adzuna (title discovery, free signup: developer.adzuna.com)
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# Default model (auto-written by `jobfinder model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

### Web UI Features

- **Live progress**: jobs streamed card-by-card via SSE during search
- **Pipeline funnel stats**: after each search, the progress log and completion card display per-stage breakdown (scraped → LLM title filter → pre-filter funnel → LLM assessment → saved / filter rate)
- **Three-column layout**: job list + detail + CV upload/search panel
- **Search history**: each record has a 📊 button to expand the full pipeline funnel, with per-source breakdown (Indeed / LinkedIn)
- **Log panel**: level filtering, keyword highlight, auto-refresh
- **Config page**: manage LLM API keys and Adzuna job search API, select default model, clear cache — new users can complete all setup without editing `.env`
- **Multilingual**: UI supports Chinese / English / Español

### Pipeline Stats Reports

After every search, stats are automatically written to the `reports/` directory:

| File | Description |
|---|---|
| `pipeline_stats.jsonl` | Append-only log — one JSON line per search, full history preserved |
| `pipeline_stats_latest.json` | Always overwritten with the most recent search report |

Fields include: per-source scrape counts, LLM title filter stats, per-step pre-filter counts, LLM assessment pass rate, overall filter rate, and more.

### Privacy

- **CV content** is sent to your configured LLM API (Anthropic / Google / OpenAI, etc.) for parsing and assessment. Please ensure you trust your chosen provider's data policy.
- **All data is stored locally**: parsed CV profiles and job listings are stored in a local SQLite database (`jobfinder_cache.db`) and are never uploaded to any third-party server.
- **Log file** (`jobfinder.log`) records search terms and timestamps only — it does not contain CV personal data or API keys, and is excluded from git via `.gitignore`.

### Legal Disclaimer

This tool scrapes publicly available job data from Indeed and other platforms via [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Please note:** Web scraping may violate the Terms of Service of the targeted websites. This tool is intended for **personal job searching, learning, and research only**. Users are solely responsible for ensuring compliance with applicable terms. The author accepts no liability for any misuse. Please scrape responsibly and avoid high-frequency or commercial use.

---

## Español

Busca automáticamente ofertas de trabajo en todo el mundo basándose en tu CV, puntúa coincidencias con LLM y analiza empresas objetivo.

### Inicio Rápido

```bash
uv sync
uv run jobfinder serve       # Lanza la Web UI (http://127.0.0.1:8765)
# Abre el navegador y configura las API Keys en la página "Config. API"
# O configura manualmente via .env:
cp .env.example .env         # Rellena tus API Keys
uv run jobfinder find cv.docx  # Modo CLI
```

### Comandos

| Comando | Descripción |
|---|---|
| `uv run jobfinder serve` | Lanza la Web UI |
| `uv run jobfinder serve --mock` | Modo test (BD aislada, no afecta la caché real) |
| `uv run jobfinder find cv.docx` | CLI: analiza CV → descubre títulos → extrae → evalúa |
| `uv run jobfinder find cv.docx --refresh` | Fuerza nueva búsqueda ignorando la caché |
| `uv run jobfinder find cv.docx --enrich` | Obtiene además información de las empresas mejor puntuadas |
| `uv run jobfinder results` | Muestra los resultados en caché de la última búsqueda |
| `uv run jobfinder assess` | Reejecuta la evaluación LLM sobre JDs en caché |
| `uv run jobfinder model` | Selecciona interactivamente el proveedor y modelo LLM |
| `uv run jobfinder cache clear` | Limpia toda la caché |

### Visión General del Pipeline

```
Archivo CV
  │
  ▼ ① Análisis de CV (LLM → CVProfile)  ← caché permanente SHA-256
  ▼ ② Descubrimiento de títulos (Adzuna API + LLM)  ← caché 7 días
  ▼    El usuario revisa y confirma la lista de títulos
  ▼ ③ Extracción (Indeed + LinkedIn, JobSpy, sin navegador)
         Pre-filtro LLM de títulos → serie limitada (Indeed 2s / LinkedIn 3s) → dedup
  ▼ ④ Embudo de filtros: antigüedad → relevancia → caché URL → cerrada → exp → habilidades
  ▼ ⑤ Evaluación LLM por lotes (score / strengths / weaknesses / matched_keywords)
  ▼    [Opcional] Consulta de empresa (--enrich)
  ▼ ⑥ Estadísticas escritas en reports/pipeline_stats.jsonl
  ▼    Web UI / terminal
```

Embudo real (datos reales):
```
Indeed 741 + LinkedIn 255 = 996 extraídos
  → Filtro título LLM  996 → 689  (30.8% eliminados)
  → Embudo pre-filtro  689 → 76   (antigüedad / dedup / habilidades, etc.)
  → Evaluación LLM      76 → 54 guardados  (tasa aprobación 71.1%)
  → Tasa de filtrado total: 94.6%  (solo 54 de 996 requieren revisión humana)
```

### Variables de Entorno

```env
# Proveedor LLM (configura al menos uno)
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# Modelos locales
OLLAMA_BASE_URL=http://localhost:11434
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# Adzuna (descubrimiento de títulos, registro gratuito: developer.adzuna.com)
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# Modelo predeterminado (escrito automáticamente por `jobfinder model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

### Funciones de la Web UI

- **Progreso en tiempo real**: ofertas enviadas carta a carta vía SSE durante la búsqueda
- **Estadísticas del embudo**: al finalizar la búsqueda, el log de progreso y la tarjeta de finalización muestran el desglose por etapa (extraídos → filtro título LLM → pre-filtro → evaluación LLM → guardados / tasa de filtrado)
- **Diseño de tres columnas**: lista de trabajos + detalle + panel de subida de CV/búsqueda
- **Historial de búsquedas**: cada registro tiene un botón 📊 para expandir el embudo completo, con desglose por fuente (Indeed / LinkedIn)
- **Panel de logs**: filtrado por nivel, resaltado de palabras clave, actualización automática
- **Página de configuración**: gestiona API Keys de LLM y API de búsqueda Adzuna, selecciona modelo por defecto, limpia caché — los nuevos usuarios pueden completar toda la configuración sin editar `.env`
- **Multilingüe**: la interfaz soporta 中文 / English / Español

### Informes de Estadísticas

Tras cada búsqueda se escriben automáticamente en el directorio `reports/`:

| Archivo | Descripción |
|---|---|
| `pipeline_stats.jsonl` | Log de solo añadir — una línea JSON por búsqueda, historial completo |
| `pipeline_stats_latest.json` | Siempre sobreescrito con el informe de la búsqueda más reciente |

Los campos incluyen: conteos de extracción por fuente, estadísticas del filtro de títulos LLM, conteos por paso del pre-filtro, tasa de aprobación de la evaluación LLM, tasa de filtrado global, entre otros.

### Privacidad

- **El contenido del CV** se envía a la API LLM que hayas configurado (Anthropic / Google / OpenAI, etc.) para su análisis y evaluación. Asegúrate de confiar en la política de datos de tu proveedor elegido.
- **Todos los datos se almacenan localmente**: los perfiles de CV analizados y las ofertas de trabajo se guardan en una base de datos SQLite local (`jobfinder_cache.db`) y nunca se suben a ningún servidor externo.
- **El archivo de log** (`jobfinder.log`) solo registra términos de búsqueda y marcas de tiempo — no contiene datos personales del CV ni API Keys, y está excluido de git mediante `.gitignore`.

### Aviso Legal

Esta herramienta extrae datos públicos de empleo de Indeed y otras plataformas a través de [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Aviso importante:** El web scraping puede vulnerar los Términos de Servicio (ToS) de los sitios web afectados. Esta herramienta está destinada **únicamente para búsqueda de empleo personal, aprendizaje e investigación**. Los usuarios son los únicos responsables de garantizar el cumplimiento de los términos aplicables. El autor no acepta ninguna responsabilidad por un uso indebido. Por favor, raspa de forma responsable y evita un uso de alta frecuencia o comercial.
