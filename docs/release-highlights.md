# Release highlights

User-facing summary of what each release improved. Every figure here must trace
back to a `### Validation` entry in [CHANGELOG.md](../CHANGELOG.md); this file
never introduces a number of its own.

From 0.5.0 onward each release is compared against the previous release.

## 0.4.0

Baselines: `09b20c0` serial architecture for the performance figures,
`fb5b211` → `d4cfd11` single-variable runs for the cost figures. This release
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
