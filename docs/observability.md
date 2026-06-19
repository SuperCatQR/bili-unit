# Run Observability

This document describes the current run-observability read side.

## Current Shape

The core data flow is:

```text
fetching -> raw.db (raw_payload + fetch_progress)
asr      -> raw.db (audio_transcription + page + segment)
```

Runners now also emit semantic run events through `RunReporter`. The SQLite
sink persists those events into the same uid DB:

- `stage_run`: one row per command run.
- `stage_event`: append-only semantic timeline for that run.

`stage_run.status` can be `PENDING`, `RUNNING`, `SUCCESS`, `PARTIAL`,
`FAILED`, `CANCELLED`, or `DRY_RUN`. `DRY_RUN` is used by `asr --dry-run`:
the run records candidate discovery and estimates, but it does not mutate
`stage_task[stage='asr']` or write `audio_transcription` rows.

The current-state tables remain the control state:

- `stage_task`
- `fetch_endpoint_state`
- `audio_transcription`

`stage_run` and `stage_event` are internal producer-state tables. They are not
part of the consumer content contract, but they are the source for CLI final
summaries and future read-side tools.

## Stable Event Prefixes

Stable semantic event names use the user-visible stage prefix:

| Prefix | Meaning |
| --- | --- |
| `fetch.*` | fetching command, endpoint, and item events |
| `asr.*` | ASR command, item, segment, and coverage events |

Do not use implementation prefixes such as `audio.item.*` for stable events.
Use `asr.item.*` and keep `pipeline="audio"` as a structured field.

Diagnostic Python logging may still use implementation-specific logger names or
messages. Those log names are not read-side facts.

## Run Summary

`bili_unit.observability.summary` provides the read-side aggregation. Every
`RunSummary` carries a `schema_version: int` field (currently `2`) that lets
consumers reject incompatible serialisations as the structure evolves.

```python
from bili_unit.observability import load_run_summary

summary = await load_run_summary(uid=123, root="output/bili")
```

Use `build_run_summary(conn, uid=..., run_id=...)` when a caller already has an
open DB connection.

For CLI final output, command results carry the exact `run_id` produced by the
write-side command. The CLI loads `RunSummary` with that `run_id`; selecting the
latest run by `uid` is only a fallback/manual mode.

```python
async with session() as cmd:
    result = await cmd.asr(123)

summary = await load_run_summary(
    uid=123,
    root="output/bili",
    run_id=result.run_id,
)
```

The summary combines:

- latest or specified `stage_run`
- recent `stage_event` rows
- attention events: warnings/errors/retries/rate limits/high-risk/failures
- fetch endpoint current state
- ASR current coverage from `raw_payload(endpoint='video_detail')` + `audio_transcription`

ASR coverage treats every distinct `item_id` under
`raw_payload(endpoint='video_detail')` as expected work. A missing
`audio_transcription` row is reported as `missing`; a row with
`status='failed'` is reported as `failed`.

`candidate_count` is read from the latest `asr.discovery.completed` event for
the selected run. It is intentionally independent from the recent-event window.

## Dashboard Snapshot

`bili_unit.observability.dashboard` provides a TUI-ready read model over the
same SQLite facts:

```python
from bili_unit.observability import load_dashboard_snapshot

snapshot = await load_dashboard_snapshot(root="output/bili")
```

The snapshot lists known uid DBs, resolves each uid's DB / workdir paths,
reads `manifest_summary`, embeds the latest `RunSummary`, and derives
recommended next actions such as retrying failed ASR rows or running missing
bvids with `--only-bvids`. It is read-only and degrades per uid with
`read_error` instead of failing the whole dashboard.

## CLI Final Summaries

Write-side CLI commands run the command first, then read `RunSummary` and render
the final human summary through `CliRenderer`.

If summary loading fails, the CLI falls back to the command result so write-side
commands still produce output.

Current final summary behavior:

- `fetch`: task status, endpoint status counts, failed endpoints, attention events.
- `asr`: ASR status, candidate count, coverage, missing/failed bvids,
  transcription row counts, attention events.
  For dry-runs, the status is `DRY_RUN` and the summary is read from run
  history rather than ASR task progress.

## Testing Contract

The observability tests cover:

- `RunReporter` auto-start and sinks.
- SQLite persistence for `stage_run` and `stage_event`.
- Run Summary over latest and selected runs.
- Current-state summary when no `stage_run` rows exist.
- ASR candidate count independent from recent-event limits.
- Dashboard snapshot reads over manifest/run facts, including concurrent
  read-while-write polling.
- CLI renderer summary output.
- CLI handler fallback when summary loading is unavailable.
- CLI handlers pass command result `run_id` into summary loading.

## Notes For Windows Shells

Project docs are UTF-8. If Chinese text looks corrupted in PowerShell, set the
console output encoding before inspecting files:

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-Content -Encoding UTF8 docs\observability.md
```

Do not batch-transcode files just because PowerShell displays mojibake.

## 事件 data schema

以下列出代码中实际 emit 的主要事件及其 `data` 字段。`endpoint`/`pipeline`/`item_type`/`item_id` 字段属于 `RunEvent` 的固定结构字段，不在下方 `data` 里重复列出。

### fetch.endpoint.completed

Emitted when a fetch endpoint run finishes with SUCCESS status.

```
data: {
  "status":        str,   # EndpointStatus value
  "retry_count":   int | None,
  "last_error_id": int | None,
}
```

### fetch.endpoint.retry_scheduled

Emitted when an endpoint hits a 412 or FetchingError and will retry.

```
data: {
  "retry":        int,
  "delay_s":      float,
  "error_type":   str,
}
```

### fetch.item.failed

Emitted when an individual item fetch is exhausted (retries gone).

```
data: {
  "retry":        int,
  "error_type":   str,
}
```

### fetch.item.saved

Emitted when a fetched item payload is persisted. No `data` keys beyond `endpoint`, `item_type`, `item_id`.

```
data: {}
```

### fetch.endpoint.unavailable

Emitted when an endpoint is marked permanently unavailable (e.g. a 404 from
upstream that classifies as `ResourceUnavailableError`). Identity comes from
the `endpoint` field; `message` carries the upstream reason.

```
data: {}
```

### fetch.item.unavailable

Emitted when a single item id is permanently unavailable mid fan-out;
sibling items continue. Identity comes from `endpoint` + `item_id`.

```
data: {}
```

### asr.discovery.completed

Emitted after ASR candidate discovery completes (before worker dispatch).

```
data: {
  "candidate_count": int,
  "skipped":         int,
  "estimate":        dict,  # AudioEstimate.to_dict()
}
```

### asr.item.completed

Emitted when one bvid completes the full audio pipeline successfully.

```
data: {}   # identity via item_id field
```

### asr.segment.rate_limited

Emitted when an ASR segment request is throttled (429) and being retried.

```
data: {
  "uid":          int,
  "bvid":         str,
  "page_index":   int,
  "segment":      str,
  "start_s":      float,
  "end_s":        float,
  "attempt":      int,
  "max_attempts": int,
  "delay_s":      float,
  "error":        str,
}
```

### asr.coverage.partial

Emitted when uid-level ASR coverage audit finds incomplete transcription.

```
data: {
  "expected":         int,
  "success":          int,
  "missing":          int,
  "failed":           int,
  "pending":          int,
  "running":          int,
  "skipped":          int,
  "complete":         bool,
  "missing_bvids":    list[str],
  "failed_bvids":     list[str],
  "incomplete_bvids": list[str],
}
```
