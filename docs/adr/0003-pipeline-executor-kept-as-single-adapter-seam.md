# `_pipeline_executor` kept despite a single adapter

Status: accepted (2026-06-14)

## Context

`processing/runner/_pipeline_executor.py` extracts two mechanics every processing pipeline needs: a bounded worker pool with rollup (`run_item_workers` + `WorkerOutcome`), and per-item retry + error-recording + status-persistence (`run_item_with_retry` + `ItemRetryContext`). It was originally built to serve two pipelines — transform and audio. transform was removed (ADR-0002), leaving audio as the only adapter.

A single-adapter seam is normally a smell ("one adapter means a hypothetical seam" — the architecture-review glossary). This ADR records why the seam was kept rather than inlined into `_audio.py`.

## Decision

Keep `_pipeline_executor.py` as a shared module. Do not inline.

## Why

- **The responsibility is orthogonal to audio.** The executor owns retry policy + worker-pool fan-out + locked rollup updates; audio owns CDN download + ffmpeg convert + ASR transcribe. Inlining would smear two unrelated concerns into one ~640-LOC module and force audio's tests to also exercise retry mechanics.
- **The second adapter is planned, not hypothetical.** `docs/feature/processing-shrink-plan.md` "后续清理" states: "subtitle / OCR pipeline 单独提议，不绑在本轮". The deferral is deliberate ("先把现状清理干净再说"), not abandonment. When subtitle/OCR land, they plug into `run_item_with_retry` with their own `ItemRetryContext` (different `pipeline` / `item_type` / `source_endpoints` / log events) and the skeleton is reused unchanged.
- **`WorkItem` is already a shared test fixture.** `test_processing_runner.py` imports `WorkItem` to construct audio work items; collapsing the executor would force the dataclass into `_audio.py` and make it look audio-specific when it is generic `(item_type, item_id, item_data)`.

## Consequences

- The seam has one consumer today. The docstring (updated in commit `ac4fb25`) names this honestly and carries a TODO pointing at the planned second adapter, so a future reviewer does not "fix" a seam that is deliberately waiting.
- If subtitle/OCR are *cancelled* (not just deferred), this decision should be revisited: inline `run_item_with_retry` / `run_item_workers` into `_audio.py` and delete the file. The cost of reversal is low; that is why this ADR exists rather than the code being frozen.
