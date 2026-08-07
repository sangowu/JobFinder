# Release highlights

> [中文](release-highlights.zh.md) · **English** · [Español](release-highlights.es.md)

User-facing summary of what each release improved. Every figure here must trace
back to a `### Validation` entry in [CHANGELOG.md](../CHANGELOG.md); this file
never introduces a number of its own.

From 0.5.0 onward, a release appears here when it changes performance or
user-facing behavior, and is described against the previous release. Which
kind of change it is determines what gets compared: performance work is
compared on measured figures, behavior work on what the tool now does
differently. A release that changes neither is not listed.

## 0.5.0

A behavior release, not a performance one. Scoring became CV-aware, which changes
what a search returns and what it costs. Figures below come from the
`### Validation` entry for `f14e369` → `b47dfc0`.

**Changing your CV now re-opens every cached job**
Until 0.4.0 a score was stored without recording which CV produced it, so a job
judged under an old CV stayed judged. On a 293-job cache, 95 jobs an earlier CV
had rejected could never be reconsidered — now 0.

**`assess` stopped reporting work it wasn't doing**
It selected on the legacy column before consulting match results, so with every
row carrying a legacy score it announced "all assessed" while 205 jobs had no
score for the current CV. Reachable jobs: 0 → 205.

**One scoring scale instead of two**
A legacy 0–10 score and a 0–100 match score were both returned by the same
property and sorted against each other. Scales feeding `effective_score`: 2 → 1.

**New: `jobradar cache prune-scores`**
Drops match results from an outdated prompt version, and optionally from an
outdated CV, so the next search recomputes them.

**Cost note**
This release adds assessment work rather than removing it. The first search after
changing CV re-evaluates cached jobs instead of reusing another CV's verdict;
later searches are unaffected. No latency or cost improvement is claimed.

## 0.4.0

Baselines: `bench/serial-baseline` for the performance figures,
`bench/pre-merged-eval` → `bench/merged-eval` for the cost figures. This release
spans ten pull requests, so it is not compared against `v0.3.0`.

**Search is 2.6x faster**
118.5 s → 45.6 s (-61.5%)

**First result arrives 3.7x sooner**
47.5 s → 12.8 s (-73.1%)

**Throughput up 145%**
14.7 → 36.0 jobs per minute

**LLM calls halved**
41 → 20 per run (-51.2%)

**Input tokens per job down 47.5%**
5,251 → 2,759
