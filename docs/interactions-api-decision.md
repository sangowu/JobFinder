# Gemini Interactions API decision

Date: 2026-08-06

## Decision

Keep JobRadar's existing `models.generate_content` transport for the historical,
model, and worker-count benchmark. Do not combine an Interactions API migration
with that experiment.

The matrix changes only two controlled variables:

- model: `gemini-3.1-flash-lite` versus `gemini-3.5-flash-lite`;
- architecture: historical commit `09b20c0` serial versus current 3-worker and
  current 5-worker assessment.

Changing the transport at the same time would create a third variable and make
the observed latency difference impossible to attribute cleanly.

## Current official API status

Google recommends the Interactions API for new projects, while the existing
`generateContent` API remains fully supported. The Python Interactions surface
requires `google-genai>=2.3.0`; JobRadar currently pins `google-genai>=1.71.0`
and the resolved lock version is 1.71.0.

Interactions uses `client.interactions.create(...)`, exposes ordered `steps`,
and accepts structured output through the top-level `response_format`. Its
default server-side interaction storage matters for JobRadar because prompts
contain CV and job-description data; a future migration must explicitly use
`store=False` unless the user opts into stateful storage.

Official references:

- <https://ai.google.dev/gemini-api/docs/interactions-overview>
- <https://ai.google.dev/gemini-api/docs/migrate-to-interactions>
- <https://ai.google.dev/gemini-api/docs/function-calling>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite>
- <https://ai.google.dev/gemini-api/docs/troubleshooting>

## Why migration is not needed for this benchmark

JobRadar's assessment calls are independent, stateless, single-turn structured
extractions and tool calls. They do not need conversation continuation,
background agents, or server-side interaction state. `generateContent` already
supports the required structured output and function-calling behavior, and
Google continues to support it.

The Interactions API may simplify a future unified agent workflow, but it does
not by itself make concurrent independent requests faster. Worker count and
model latency remain the relevant performance variables in this experiment.

## Separate follow-up migration gate

Evaluate Interactions later behind an explicit transport flag, not as an
in-place replacement. That follow-up should:

1. Upgrade `google-genai` in an isolated branch and run all provider tests.
2. Map `generate_content` structured output/tool results to `response_format`
   and `steps` without changing prompts or schemas.
3. Force `store=False` and add a regression test for it.
4. Run an A/B transport benchmark with the same model, frozen inputs, worker
   count, prompt, schema, and alternating order.
5. Compare latency, final failures, token usage, parsed-result equivalence, and
   any provider-visible retry/429 data before changing the default.

The current SDK performs automatic transient retry handling, but it does not
expose internal retry attempts in the normal response object. Current reports
therefore distinguish observable final worker/evaluation failures from internal
retry or 429 counts that cannot be measured reliably.

For the current `generateContent` transport, JobRadar explicitly pins
`thinking_level=minimal` for Gemini 3.1 and 3.5 Flash-Lite structured-output and
tool-calling requests. This makes high-throughput model comparisons independent
of a future server-side default change. A later Interactions migration must
preserve the same setting through `generation_config`.
