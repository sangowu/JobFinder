# JobRadar

> [中文](README.zh.md) · **English** · [Español](README.es.md)

Automatically search global job listings based on your CV, score matches with LLM, and deduplicate across multiple sources.

## Quick Start

```bash
# Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex

git clone https://github.com/sangowu/JobRadar.git
cd JobRadar
uv sync
uv run jobradar serve       # Launch Web UI (http://127.0.0.1:8765)
# Open your browser and configure API keys in the "API Config" page
# Or configure manually via .env:
cp .env.example .env         # Fill in your API keys
uv run jobradar find cv.docx  # CLI mode
```

## Commands

| Command | Description |
|---|---|
| `uv run jobradar serve` | Launch Web UI |
| `uv run jobradar serve --mock` | Test mode (isolated DB, won't affect real cache) |
| `uv run jobradar find cv.docx` | CLI: parse CV → discover titles → scrape → assess |
| `uv run jobradar find cv.docx --refresh` | Force re-search, ignore all caches |
| `uv run jobradar results` | Browse cached results from the last search |
| `uv run jobradar assess` | Re-run LLM assessment on cached JDs |
| `uv run jobradar model` | Interactively choose LLM provider and model |
| `uv run jobradar cache clear` | Clear all caches |
| `uv run jobradar --version` | Show current version |

## Pipeline Overview

```
CV file
  │
  ▼ ① CV parsing (LLM → CVProfile)  ← permanent SHA-256 cache
         structured seniority bands + explicit language extraction
  ▼ ② User reviews & confirms title list
  ▼ ③ Scraping (Indeed + LinkedIn, JobSpy, no browser)
         rate-limited serial (Indeed 2s / LinkedIn 3s) → URL dedup
  ▼    pre-JD LLM title relevance gate
         conservative title-only semantic filter; default keep=true and reject only clearly different career paths
  ▼    batched LLM coarse filter
         card-level keep/reject using title + location + snippet
  ▼    dynamic title seniority gate
         blocks obvious level mismatch (e.g. new grad → lead / manager)
  ▼    experience-gap gate
         directly skips roles whose explicit years-required exceeds candidate experience by more than 3 years
  ▼ ④ JD profile extraction
         structured required/preferred skills, must-haves, years, seniority conflict, work mode, language requirements
  ▼ ⑤ Explainable CV↔JD matching
         rubric-based dimension scores → programmatic weighted score → recommendation
         city-to-city relocation / office attendance count as risk, not as location-score penalty
  ▼ ⑥ Artifact generation
          interview prep / cover letter / CV optimization
  ▼ ⑦ Search stats + cache
         history metrics, reports, filter events, Web UI / terminal display
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

# Default model (auto-written by `jobradar model`)
DEFAULT_PROVIDER=gemini
DEFAULT_MODEL=gemini-2.0-flash
```

## Web UI Features

- **Live progress**: jobs streamed card-by-card via SSE during search
- **Pipeline funnel stats**: per-stage breakdown after each search (scraped → LLM title filter → pre-filter funnel → LLM assessment → saved / filter rate)
- **Three-column layout**: job list + detail + CV upload/search panel
- **Multi-source dedup**: jobs appearing on both Indeed and LinkedIn are merged; source badges are clickable links; Apply button becomes a dropdown when multiple source URLs exist
- **Search history**: each record has a 📊 button to expand the full pipeline funnel, with per-source breakdown (Indeed / LinkedIn)
- **Normalized search history metrics**: each record stores total scraped, deduped, filtered, newly saved jobs, and token consumption
- **Module-level telemetry**: history records and search completion events now include `module_metrics` with per-module `calls / input_tokens / output_tokens / elapsed`, plus `processed / rejected / kept` where applicable in the search pipeline
- **Funnel benchmark summary**: history now tracks pipeline/prompt versions and shows derived efficiency metrics such as post-filter rate, new-job yield, and tokens per new job
- **Pre-JD title semantic filter**: before JD assessment, job titles are batch-scored by an LLM using a conservative relevance gate; history funnel shows `skip_irrelevant`
- **Title gate behavior**: the gate only rejects titles that are clearly outside the candidate's career path from the title alone; broad, adjacent, or ambiguous technical titles are kept for later JD assessment
- **Persisted filter events**: each run now writes `filter_events` with `run_id / stage / title / reason / details`, so you can inspect exactly where a title was filtered out
- **Forced refresh in Web UI**: search panel includes a force-refresh option so you can rerun without reusing the current search-session cache
- **Dynamic title seniority gate**: uses CV eligible/stretch/blocked levels to remove obvious title-level mismatches before JD matching
- **Experience-gap hard reject**: prefilter records `skip_exp`, and `jd_assessment` directly rejects roles whose explicit years-required exceeds the candidate's experience by more than 3 years
- **Explainable matching**: job details include score breakdown, risks, skill matches, and recommendation tiers
- **Risk-only relocation / office attendance handling**: same-country relocation and office-attendance requirements are recorded in `risks / risk_penalty` but are not treated as `location_score` penalties
- **Artifact hub**: generate and reuse Interview Prep, Cover Letter, and CV Optimization from the job detail panel
- **Log panel**: level filtering, keyword highlight, auto-refresh
- **Config page**: manage LLM API keys, select default model, clear cache — new users can complete all setup without editing `.env`
- **Multilingual**: UI supports Chinese / English / Español

## Pipeline Stats Reports

After every search, stats are automatically written to the `reports/` directory:

| File | Description |
|---|---|
| `pipeline_stats.jsonl` | Append-only log — one JSON line per search, full history preserved |
| `pipeline_stats_latest.json` | Always overwritten with the most recent search report |

## Benchmark And Version Tracking

- **Every history row stores version metadata**. The backend writes `app_version`, `cv_prompt_version`, `jd_summary_prompt_version`, `match_prompt_version`, `title_gate_version`, and `coarse_filter_version` into `search_stats`.
- **Every history row also stores `run_id`** so you can join a history record to persisted `filter_events`.
- **The UI Funnel Benchmark compares grouped version signatures**. A signature is built from the version fields above, so “current version” vs “previous version” in the benchmark is really a comparison between two different history groups.
- **The history table does not currently render every version field per row**, but the benchmark card shows the active signature, and `/api/stats` returns `versions` for each record.

## Filter Event Inspection

- **API**:
  - `GET /api/stats` returns history rows including `run_id`
  - `GET /api/filter-events?run_id=<run_id>` returns persisted per-title filter events for that search
- **Storage**:
  - `search_stats` stores run-level summary
  - `filter_events` stores `stage / title / reason / details`
- **Mock cache clearing**:
  - In mock mode, clearing cache now also clears `search_stats` and `filter_events` from the mock database

## Inspection Script

Use `scripts/show_filter_events.py` to inspect persisted filter events from `data/jobradar_test_cache.db` without rerunning a search.

Examples:

```bash
python scripts/show_filter_events.py
python scripts/show_filter_events.py --stage jd_assessment --out reports/filter_report.md
python scripts/show_filter_events.py --run-id <run_id> --json --out reports/filter_report.json
```

Useful options:

- `--run-id`: inspect a specific history run
- `--stage`: only show one stage such as `title_relevance`, `coarse_filter`, `experience_gap`, `jd_assessment`, or `final_match`
- `--md`: print Markdown to the terminal
- `--out`: save `.md` or `.json`

## How To Measure The New Title-Gate Module

What you can already inspect:

- **Effect**:
  - `skip_irrelevant` in the funnel history shows how many titles the pre-JD title relevance gate rejected.
  - `new_job_yield`, `tokens_per_filtered_job`, `tokens_per_new_job`, and `assessment_efficiency` in the benchmark help compare before/after behavior.
- **Total cost**:
  - search history already stores per-run `tokens_in` and `tokens_out`, so you can compare total token usage across version signatures.

Current limitation:

- **There is no isolated token counter for the title relevance gate yet**.
  The system records total search tokens, not per-module token spend. So you can compare overall before/after cost, but not the exact token cost of the title gate alone from the UI today.

Recommended evaluation workflow:

1. Run several searches with similar `roles + location + provider/model` before and after the version change.
2. Compare:
   - whether `skip_irrelevant` increased
   - whether `llm_assessed` went down
   - whether `tokens_per_filtered_job` / `tokens_per_new_job` improved
   - whether `new_job_yield` stayed acceptable
3. If you need exact module-level cost attribution, add dedicated telemetry tags or a separate token counter for the title gate.

## Compare Script

Use `scripts/compare_title_gate.py` to run a controlled A/B comparison between:

- `baseline_gate_off`
- `title_gate_on`

The script uses a fixed experiment setup:

- Titles: `AI Engineer`, `Machine Learning Engineer`, `LLM Engineer`, `Software Engineer`, `Backend Engineer`
- `30` jobs per title
- `168` hours (`7` days)
- `Indeed` only
- Location fixed to `Ireland`

Example:

```bash
python scripts/compare_title_gate.py --cv-path "path/to/test_cv.md" --keep-db --out reports/compare_report.json
```

Useful options:

- `--cv-path`: test CV file (`.md`, `.txt`, `.docx`)
- `--out`: save the full JSON comparison report
- `--keep-db`: persist baseline/improved sqlite files under `reports/compare_runs/<timestamp>/`
- `--db-dir`: explicitly choose where those sqlite artifacts are stored

The generated `reports/compare_report.json` includes:

- `summary`: top-line counts and token cost
- `diff.baseline_only_jobs`: jobs kept only by baseline
- `diff.improved_only_jobs`: jobs kept only by the title-gate version
- `diff.title_gate_rejections`: titles explicitly rejected by the title gate

## Email Application Tracking

The **Application Tracker** page connects to Gmail through Google OAuth and classifies recruiting emails as submitted, assessment, interview, offer, rejected, withdrawn, or pending review. The resulting application timeline is stored in the same local SQLite database.

1. Create an OAuth 2.0 Web application client in Google Cloud Console.
2. Enable the Gmail API.
3. Add `http://127.0.0.1:8765/api/email/google/callback` as an authorised redirect URI.
4. Configure `.env`:

```env
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
EMAIL_SYNC_INTERVAL_SECONDS=900
EMAIL_SYNC_MAX_MESSAGES=5000
EMAIL_SYNC_FETCH_WORKERS=8
EMAIL_SYNC_ANALYSIS_WORKERS=3
EMAIL_LLM_CLASSIFICATION_ENABLED=1
```

Start `jobradar serve`, open **Application Tracker**, and select **Sign in with Google**. JobRadar requests read-only Gmail access. The OAuth token is stored locally at `data/google_gmail_token.json` and is excluded from Git.

The first sync reads the last 30 days of email and stores a Gmail History cursor; later syncs process only new messages. Message bodies are fetched with 8 workers by default (`EMAIL_SYNC_FETCH_WORKERS`, range 1-16). Rule and selective LLM classification use 3 workers by default (`EMAIL_SYNC_ANALYSIS_WORKERS`, range 1-8). Database merges remain chronological and single-threaded.

Clear rejections, job alerts, recommendation digests, and ATS subscriptions are handled by local rules. Only ambiguous status, missing identity, or conflicting bulk-header cases are sent to the configured default LLM. Placeholder identity values such as `unknown`, `N/A`, and `not specified` are normalised as missing so a later email can enrich the company or job title. Email bodies are not persisted; JobRadar stores hashes, structured decisions, model/latency/token metrics, and human feedback.

The UI supports manual sync, pausing scheduled sync, clearing email data, background reanalysis with live progress, expandable sync-history cards, direct Gmail links, and confirm/discard actions for pending records.

## Matching Semantics

- `location_score` now represents broad geographic compatibility only.
- Same-country city relocation and office-attendance requirements such as `hybrid`, `onsite`, or `3 days in office` are treated as practical risks.
- Those factors contribute to `risks / risk_penalty`, but are not supposed to lower `location_score`.

## Privacy & Security

- **CV content** is sent to your configured LLM API (Anthropic / Google / OpenAI, etc.) for parsing and assessment. Please ensure you trust your chosen provider's data policy.
- **Local persistence**: parsed CV profiles, job listings, and email-classification observations are stored in a local SQLite database (`data/jobradar_cache.db`). Ambiguous application emails may be sent to the configured LLM provider for classification; email bodies are not persisted in the observations table.
- **Log file** (`logs/jobradar.log`) records search terms and timestamps only — it does not contain CV personal data or API keys, and is excluded from git via `.gitignore`.
- **PII in CV**: If you are concerned about sending personal information to an external LLM provider, remove it from your CV before uploading (name, email, phone, address). LinkedIn / GitHub links carry no additional risk.
- **Prompt injection protection**: Job description content scraped from external sources is wrapped in `<jd_content>` boundary tags, and the system prompt explicitly instructs the LLM to treat tag contents as data only, ignoring any embedded instructions.

## Known Limitations

This is a solo side project maintained in spare time. Some features — particularly **location-based filtering** — may produce inconsistent results depending on the job source.

**LLM provider support**: 17 providers are integrated, but not all have been fully tested end-to-end. If you encounter a bug with a specific provider or model, please [open an issue](https://github.com/sangowu/JobRadar/issues) and include the provider name, model, and error message.

## Roadmap

- **Smarter LLM Assessment** — Stabilise strengths/weaknesses output: ensure experience-gap mismatches consistently surface as weaknesses, and reduce variance across repeated evaluations of the same JD
- **CV Tailoring** — One-click CV rewrite optimised for a specific JD, highlighting the most relevant experience and keywords
- **Cover Letter Generator** — Auto-generate a tailored cover letter per JD, ready to copy-paste or export

## Legal Disclaimer

This tool scrapes publicly available job data from Indeed and other platforms via [python-jobspy](https://github.com/cullenwatson/JobSpy).

> **Please note:** Web scraping may violate the Terms of Service of the targeted websites. This tool is intended for **personal job searching, learning, and research only**. Users are solely responsible for ensuring compliance with applicable terms. The author accepts no liability for any misuse. Please scrape responsibly and avoid high-frequency or commercial use.
