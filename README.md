# JobFinder

> [中文](README.zh.md) · **English** · [Español](README.es.md)

Automatically search global job listings based on your CV, score matches with LLM, and deduplicate across multiple sources.

## Quick Start

```bash
uv sync
uv run jobfinder serve       # Launch Web UI (http://127.0.0.1:8765)
# Open your browser and configure API keys in the "API Config" page
# Or configure manually via .env:
cp .env.example .env         # Fill in your API keys
uv run jobfinder find cv.docx  # CLI mode
```

## Commands

| Command | Description |
|---|---|
| `uv run jobfinder serve` | Launch Web UI |
| `uv run jobfinder serve --mock` | Test mode (isolated DB, won't affect real cache) |
| `uv run jobfinder find cv.docx` | CLI: parse CV → discover titles → scrape → assess |
| `uv run jobfinder find cv.docx --refresh` | Force re-search, ignore all caches |
| `uv run jobfinder results` | Browse cached results from the last search |
| `uv run jobfinder assess` | Re-run LLM assessment on cached JDs |
| `uv run jobfinder model` | Interactively choose LLM provider and model |
| `uv run jobfinder cache clear` | Clear all caches |
| `uv run jobfinder --version` | Show current version |

## Pipeline Overview

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

## Environment Variables

```env
# LLM Provider (configure at least one)
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=

# Local models
LLAMACPP_BASE_URL=http://localhost:8080/v1
LOCAL_LLM_BASE_URL=http://localhost:1234/v1

# Adzuna (title discovery, free signup: developer.adzuna.com)
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# Default model (auto-written by `jobfinder model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

## Web UI Features

- **Live progress**: jobs streamed card-by-card via SSE during search
- **Pipeline funnel stats**: per-stage breakdown after each search (scraped → LLM title filter → pre-filter funnel → LLM assessment → saved / filter rate)
- **Three-column layout**: job list + detail + CV upload/search panel
- **Multi-source dedup**: jobs appearing on both Indeed and LinkedIn are merged; source badges are clickable links; Apply button becomes a dropdown when multiple source URLs exist
- **Search history**: each record has a 📊 button to expand the full pipeline funnel, with per-source breakdown (Indeed / LinkedIn)
- **Log panel**: level filtering, keyword highlight, auto-refresh
- **Config page**: manage LLM API keys and Adzuna job search API, select default model, clear cache — new users can complete all setup without editing `.env`
- **Multilingual**: UI supports Chinese / English / Español

## Pipeline Stats Reports

After every search, stats are automatically written to the `reports/` directory:

| File | Description |
|---|---|
| `pipeline_stats.jsonl` | Append-only log — one JSON line per search, full history preserved |
| `pipeline_stats_latest.json` | Always overwritten with the most recent search report |

## Privacy

- **CV content** is sent to your configured LLM API (Anthropic / Google / OpenAI, etc.) for parsing and assessment. Please ensure you trust your chosen provider's data policy.
- **All data is stored locally**: parsed CV profiles and job listings are stored in a local SQLite database (`jobfinder_cache.db`) and are never uploaded to any third-party server.
- **Log file** (`jobfinder.log`) records search terms and timestamps only — it does not contain CV personal data or API keys, and is excluded from git via `.gitignore`.

## Legal Disclaimer

This tool scrapes publicly available job data from Indeed and other platforms via [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Please note:** Web scraping may violate the Terms of Service of the targeted websites. This tool is intended for **personal job searching, learning, and research only**. Users are solely responsible for ensuring compliance with applicable terms. The author accepts no liability for any misuse. Please scrape responsibly and avoid high-frequency or commercial use.
