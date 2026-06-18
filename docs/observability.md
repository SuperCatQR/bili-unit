# Run Observability

This document describes the current run-observability read side. The
architecture decision is recorded in [ADR 0001](adr/0001-run-observability.md).

## Current Shape

The core data flow is unchanged:

```text
fetching -> raw.db
parsing  -> raw.db -> main.db
asr      -> main.db
```

Runners now also emit semantic run events through `RunReporter`. The SQLite
sink persists those events into the uid main DB:

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
| `parse.*` | parsing command, model, and image events |
| `asr.*` | ASR command, item, segment, and coverage events |

Do not use implementation prefixes such as `audio.item.*` for stable events.
Use `asr.item.*` and keep `pipeline="audio"` as a structured field.

Diagnostic Python logging may still use implementation-specific logger names or
messages. Those log names are not read-side facts.

## Run Summary

`bili_unit.observability.summary` provides the read-side aggregation:

```python
from bili_unit.observability import load_run_summary

summary = await load_run_summary(uid=123, root="output/bili")
```

Use `build_run_summary(main, uid=..., run_id=...)` when a caller already has an
open main DB connection.

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
- parse model current state
- ASR current coverage from `video` + `audio_transcription`

ASR coverage treats every row in `video` as expected work. A missing
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

The snapshot lists known uid DBs, resolves each uid's main/raw/workdir paths,
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
- `parse`: task status, model status counts, failed models, image summary,
  attention events.
- `sync`: combined fetch/parse status plus endpoint/model counts and attention
  events.
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
