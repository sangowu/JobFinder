# Replay-driven pipeline benchmark

This benchmark compares serial and streaming assessment scheduling while keeping
the candidate data, batch boundaries, persistence work, and assessment work
identical. It is deliberately isolated from the production cache and SSE layer.

## Architecture

The production streaming pipeline and the benchmark use the same
`BatchScheduler` implementation. The benchmark replaces external side effects
with adapters:

- `ReplayBatchSource`: replays frozen post-Python-filter batches.
- `IsolatedCandidateRepository`: persists candidates to a dedicated temporary SQLite database.
- `AssessmentEngine`: protocol for deterministic delay, recorded response, or real assessment adapters.
- `CollectingResultWriter`: collects results without writing `job_cache` or sending SSE events.

Both modes persist every batch before assessment:

- `serial`: buffer all persisted tasks until the replay producer finishes, then assess them.
- `streaming`: enqueue each persisted task immediately and assess while later batches arrive.

## Dataset contract

Use JSON or JSONL. Contract v2 separates observable source/role query cost from
candidate availability. A dataset contains every query event plus the candidates
that passed the deterministic Python prefilter, grouped by original producer
batch with a relative availability time:

```json
{
  "schema_version": 2,
  "dataset_id": "jobs-2026-08-06",
  "producer_finished_offset_seconds": 12.8,
  "query_events": [
    {
      "batch_id": "indeed-ai-engineer-01",
      "source": "indeed.ie",
      "role": "AI Engineer",
      "request_started_offset_seconds": 0.2,
      "request_finished_offset_seconds": 4.2,
      "scrape_elapsed_seconds": 4.0,
      "raw_count": 35,
      "python_filtered_count": 9
    }
  ],
  "batches": [
    {
      "batch_id": "indeed-ai-engineer-01",
      "source": "indeed.ie",
      "role": "AI Engineer",
      "ready_offset_seconds": 4.25,
      "jobs": [
        {
          "title": "AI Engineer",
          "company": "Example",
          "url": "https://example.test/jobs/1",
          "description_snippet": "Build Python AI services.",
          "capture_meta": {
            "batch_id": "indeed-ai-engineer-01",
            "source": "indeed.ie",
            "role": "AI Engineer",
            "observed_offset_seconds": 4.25
          }
        }
      ]
    }
  ]
}
```

JobSpy returns a role query as a batch, so network cost is recorded on the
query/batch. A job's `observed_offset_seconds` is the first time that candidate
became available after the deterministic Python prefilter; it is not presented
as an independently measured per-job network duration. Empty query results stay
in `query_events`, while `producer_finished_offset_seconds` preserves scraping
tail time after the last non-empty candidate batch.

The benchmark calculates a canonical dataset hash and one hash per assessment
task. A comparison is valid only when serial and streaming runs have identical
dataset hashes, task hashes, result keys, and persisted candidate counts.

## Capture a real frozen dataset

Export the `CVProfile` used for the search to JSON, then run:

```powershell
.\.venv\Scripts\python.exe scripts\capture_pipeline_dataset.py `
  --profile reports\benchmark-cv-profile.json `
  --location ireland `
  --output reports\pipeline_benchmark\jobs-2026-08-06.json
```

The capture command performs real JobSpy scraping and the deterministic Python
prefilter. It records source, role, request start/finish, query duration,
raw/URL-unique/date-filtered/Python-filtered counts, candidate observation time,
per-source finish offsets, and total producer finish. It writes filter/cache side effects to a separate
`*_capture_cache.db`; it does not use the production JobRadar cache and stops
before title/coarse/JD LLM assessment.

## Run the controlled scheduling comparison

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_pipeline.py `
  --dataset reports\pipeline_benchmark\jobs-2026-08-06.json `
  --output-dir reports\pipeline_benchmark\controlled `
  --runs 10 `
  --warmups 1 `
  --assessment-delay 0.5 `
  --timing recorded
```

`--timing recorded` reproduces batch availability offsets. `--timing instant`
is useful for correctness checks, but it cannot measure scrape/assessment
overlap. `--speed-factor` compresses recorded producer delays; assessment delay
must be scaled by the same factor if the producer/consumer ratio should remain
representative.

The runner alternates execution order (`serial → streaming`, then
`streaming → serial`) and writes:

- `pipeline_benchmark_runs.jsonl`: raw result for every measured run.
- `pipeline_benchmark_summary.json`: mean/P50/P95 timings, paired improvements with deterministic 95% bootstrap intervals, hashes, and equivalence status.

## One-command version comparison

If the profile and frozen dataset already exist, run the complete non-billable
controlled comparison with:

```powershell
.\.venv\Scripts\python.exe scripts\compare_pipeline_versions.py `
  --profile reports\pipeline_benchmark\cv-profile.json `
  --dataset reports\pipeline_benchmark\jobs-2026-08-06.json `
  --output-dir reports\pipeline_version_comparison
```

Omit `--profile` to export the most recently cached `CVProfile`. If the dataset
does not exist, add `--capture`; this performs one real JobSpy capture using an
isolated capture database:

```powershell
.\.venv\Scripts\python.exe scripts\compare_pipeline_versions.py `
  --capture `
  --location ireland `
  --output-dir reports\pipeline_version_comparison
```

The command always runs the controlled scheduler comparison. Real provider calls
are disabled unless explicitly enabled. To compare historical commit `09b20c0`
with the current working tree using the same frozen data and isolated databases:

```powershell
.\.venv\Scripts\python.exe scripts\compare_pipeline_versions.py `
  --dataset reports\pipeline_version_comparison\inputs\frozen-jobs.json `
  --profile reports\pipeline_version_comparison\inputs\cv-profile.json `
  --baseline 09b20c0 `
  --real-llm `
  --provider gemini `
  --model gemini-3.5-flash-lite `
  --assessment-workers 5 `
  --real-runs 5
```

`--real-llm` is deliberately explicit because these paired runs consume provider
tokens and may incur cost. The controller loads the current `.env` without
printing secrets, creates a detached temporary worktree for the baseline, runs
each version in a separate process and SQLite database, alternates A/B order,
then removes the temporary worktree. With a schema-v2 capture, the worker output
also preserves the complete source/role query-event list, empty-query count,
batch-ready offsets, source completion offsets, total producer duration, and
producer tail. Recorded replay reproduces that timing envelope for both arms;
it does not divide batch network latency into fabricated per-job durations.

Final outputs:

- `version_comparison.json`: controlled summary, raw historical runs, token/call metrics, and result-set comparison.
- `version_comparison.md`: reviewable serial/streaming and baseline/candidate tables.

## Isolate a code change from the execution architecture

`pipeline_version_worker.py` takes two independent arguments that are easy to
confuse:

| Argument | Controls | Where |
| --- | --- | --- |
| `--checkout <dir>` | **which code runs** | `sys.path.insert(0, checkout)`; all JobRadar imports are deferred until after it |
| `--version-mode baseline\|candidate` | **how the run executes** | `baseline` aggregates every batch, cancels streaming arrival and leaves the worker count at one |

`--version-mode` does not select a version. `baseline` means "replay under the
pre-streaming serial contract", which is why `compare_pipeline_versions.py`
pairs it with the baseline worktree: there the execution architecture *is* the
change under test.

That pairing is wrong when the change under test lives purely in the code and
both refs already run the streaming worker pool — it would credit this change
with the earlier concurrency work. `compare_merged_evaluation.py` runs both arms
with `--version-mode candidate` so `--checkout` is the only variable:

```powershell
.\.venv\Scripts\python.exe scripts\compare_merged_evaluation.py `
  --baseline fb5b211 `
  --dataset reports\pipeline_version_comparison\inputs\frozen-jobs.json `
  --real-llm `
  --provider gemini `
  --model gemini-3.5-flash-lite `
  --assessment-workers 5 `
  --runs 3
```

Choose the script by the question:

| Question | Script | Baseline |
| --- | --- | --- |
| How much did *this* code change contribute? | `compare_merged_evaluation.py` | the commit right before it |
| How far have we come since the serial pipeline? | `compare_pipeline_versions.py` | `09b20c0` |

Both arms label their output `"version_mode": "candidate"`, so distinguish them
by the `checkout` field, not by `version_mode`. The aggregation modules do not
read `version_mode`, so the paired output feeds `build_version_comparison` and
`build_quality_report` unchanged.

Real provider calls are mandatory here: a merged-call comparison measured
against stubbed assessment latency would measure nothing, so there is no
controlled mode. The report adds a call-cost table on top of the standard
performance and quality axes. Read `tokens_out` first — merging removes a
duplicated JD from the prompt, so `tokens_in` should fall while `tokens_out`
stays flat. A large `tokens_out` drop means the model is emitting fewer fields,
which is a quality signal rather than a saving.

Two cautions learned from running this comparison:

- Both arms must carry the same telemetry code. A baseline that predates the
  tool-call usage fix reports zero tokens for every step that succeeds on the
  tool path, which silently inverts the token comparison. The same applies to
  served-model recording: a baseline without it cannot confirm which model it
  actually ran.
- Compare between-arm result overlap against each arm's own run-to-run overlap
  before calling a difference real. At three paired runs the within-arm Jaccard
  was 0.65–0.67, so a between-arm Jaccard of 0.66 is noise, not a change.

## Compare one assessment worker with the production worker count

The production pipeline keeps title/coarse/JD gates batched and runs independent
gate chunks with at most two workers for cloud providers (one for local providers).
It then evaluates different jobs in a bounded pool. An uncached job returns its
`JD Profile` and `CV Match` evidence in one provider call; a cached JD Profile is
reused and only the missing match is generated. Pending candidates are persisted
before submission; worker threads use the in-memory job payload, and the assessment
coordinator atomically commits each profile/match pair before publishing the job callback.

Run a paired real-provider comparison with the same frozen jobs and CV:

```powershell
.\.venv\Scripts\python.exe scripts\compare_assessment_workers.py `
  --dataset reports\pipeline_full_validation_20260806\inputs\frozen-jobs.json `
  --profile reports\pipeline_full_validation_20260806\inputs\cv-profile.json `
  --real-llm `
  --provider gemini `
  --model gemini-3.5-flash-lite `
  --candidate-workers 5 `
  --runs 5 `
  --output-dir reports\assessment_worker_comparison
```

The controller alternates `1 worker -> N workers` and `N workers -> 1 worker`,
uses a separate process and temporary SQLite database for every run, and writes:

- `assessment_worker_comparison.json`: aggregate and raw per-run metrics.
- `assessment_worker_comparison.md`: total/first-job latency, LLM work, result overlap,
  peak evaluation concurrency, and failure counts.

`--real-llm` is mandatory because the comparison consumes provider tokens. For
local providers, JobRadar defaults to one worker; cloud providers default to
five. `ASSESSMENT_WORKERS` and `run_search(assessment_workers=...)` accept 1-8.

## Run the historical/model/worker matrix

Use the matrix controller when the goal is to separate the model effect from
the worker-count effect while retaining the historical serial reference:

```powershell
.\.venv\Scripts\python.exe scripts\compare_pipeline_matrix.py `
  --dataset reports\pipeline_full_validation_20260806\inputs\frozen-jobs.json `
  --profile reports\pipeline_full_validation_20260806\inputs\cv-profile.json `
  --baseline 09b20c0 `
  --provider gemini `
  --reference-model gemini-3.1-flash-lite `
  --replacement-model gemini-3.5-flash-lite `
  --runs 5 `
  --real-llm `
  --output-dir reports\pipeline_model_worker_matrix_20260806
```

This runs 25 measured executions: reference-model serial/3-worker/5-worker and
replacement-model serial/3-worker, five times each. Model order alternates by
block and arm order rotates within each model. Every execution receives its own
process and SQLite database. `pipeline_matrix_runs.jsonl` is appended after each
execution so a long run retains its evidence if a later subprocess fails.

Final outputs:

- `pipeline_matrix.json`: aggregate contrasts and all raw successful runs;
- `pipeline_matrix.md`: latency, first-job, throughput, token/call, concurrency,
  failure, result-overlap, cross-model, and model-by-concurrency tables;
- `pipeline_matrix_runs.jsonl`: incremental success/failure checkpoint.

The controller cannot observe retries handled internally by `google-genai`.
It records final worker-process failures and pipeline evaluation failures, and
only identifies a 429 when it escapes SDK retry handling. See
`docs/interactions-api-decision.md` for the transport decision and a separate
Interactions API migration gate.

## Run the 100-job model-quality standard

Build a deterministic audit unit from the original replay plus balanced cached
boundary cases. The builder enforces exactly 100 unique dedup keys and URLs:

```powershell
.\.venv\Scripts\python.exe scripts\build_quality_audit_dataset.py `
  --seed-dataset reports\pipeline_full_validation_20260806\inputs\frozen-jobs.json `
  --cache-db data\jobradar_cache.db `
  --output reports\model_quality_standard_100\inputs\standard-100.json `
  --size 100
```

Run the same current code and three-worker architecture five times per model:

```powershell
.\.venv\Scripts\python.exe scripts\compare_model_quality.py `
  --dataset reports\model_quality_standard_100\inputs\standard-100.json `
  --profile reports\pipeline_full_validation_20260806\inputs\cv-profile.json `
  --reference-model gemini-3.1-flash-lite `
  --replacement-model gemini-3.5-flash-lite `
  --workers 3 `
  --runs 5 `
  --real-llm `
  --keep-run-databases `
  --output-dir reports\model_quality_standard_100
```

Both Flash-Lite models explicitly use `thinking_level=minimal`. Each isolated
run exports 100 terminal decisions with stage, reason, and available assessment
artifacts. The report compares behavior, latency, tokens, stability, and
five-run pass frequencies; it does not claim ground-truth quality.

`--keep-run-databases` retains each isolated SQLite file under
`OUTPUT_DIR/run_databases` and records its absolute path in the run checkpoint.
The databases preserve candidate JSON, filter events, cached jobs, JD profiles,
and CV matches. Per-run timing remains in `model_quality_runs.jsonl`; the frozen
100-job audit dataset has batch-level replay offsets, not per-job network-fetch
latency, so it must not be presented as a recording of live scraping cost.

Assign the generated `reviewer_a_blind.csv` and `reviewer_b_blind.csv` to two
independent people. After both label all jobs, run:

```powershell
.\.venv\Scripts\python.exe scripts\score_model_quality_reviews.py `
  --report reports\model_quality_standard_100\model_quality_audit.json `
  --manifest reports\model_quality_standard_100\human_review\blind_manifest.json `
  --reviewer-a reports\model_quality_standard_100\human_review\reviewer_a_blind.csv `
  --reviewer-b reports\model_quality_standard_100\human_review\reviewer_b_blind.csv `
  --output reports\model_quality_standard_100\human_quality_score.json
```

If reviewer labels differ, the scorer writes `adjudication_required.csv`. A
coordinator completes it and reruns the command with `--adjudication` before a
model replacement decision is considered final.

Use `scripts/export_benchmark_profile.py` when only profile export is needed:

```powershell
.\.venv\Scripts\python.exe scripts\export_benchmark_profile.py `
  --output reports\pipeline_benchmark\cv-profile.json
```

## Interpretation

The built-in delay engine isolates scheduler behavior. It proves whether overlap
exists and whether persistence/result invariants hold; it does not prove live
JobSpy or real LLM performance. The controller's explicit `--real-llm` mode
provides the separate-process historical validation using the same frozen tasks,
model, CV, temporary databases, and alternating paired-run order.

Do not compare two independent live JobSpy searches as a performance benchmark:
the candidate population, rate limiting, and network latency are uncontrolled.

Visible-job counts differ between architectures even on identical frozen data.
Serial replay hands every batch to the filters at once while streaming replay
filters each batch on arrival, so the batched Title and JD gates see different
batch companions and can judge borderline jobs differently. Check how many of the
differing jobs are stable across runs before reading a result-count gap as a
recall change; most of them are usually borderline jobs that already vary between
runs of the same arm.

Each run records the model the provider reports serving, not only the requested
one. Check `served_models` per step before comparing two runs: an alias or a
silent model change invalidates the comparison, and the requested string alone
cannot detect it.
