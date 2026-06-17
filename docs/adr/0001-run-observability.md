# ADR 0001: Run Observability Architecture

Date: 2026-06-18

Status: Accepted

## Context

The current core data architecture is sound:

```text
fetching -> raw.db
parsing  -> raw.db -> main.db
asr      -> main.db
```

The per-uid SQLite output, the raw/main DB split, and direct SQL read-side
contract should remain intact.

The weak point is execution-time observability. Today the same facts are spread
across several surfaces:

- Python `logging` events with ad hoc names and `extra` fields.
- The stdlib `Progress` renderer in `bili_unit/_logging.py`.
- Direct progress construction inside fetching and processing runners.
- CLI-level `print(...)` summaries in `bili_unit/__main__.py`.
- Current-state tables such as `stage_task`, `fetch_endpoint_state`,
  `stage_error`, and `audio_transcription`.

This makes long-running tasks hard to reason about. Examples seen in practice:

- A run can finish with incomplete ASR coverage while the command output does
  not clearly explain why.
- `--retry-failed-only` only retries rows currently marked `failed`, but missing
  rows are a different state and were not previously visible enough.
- Progress labels can be misleading when nested fan-out work is running.
- There is no stable notion of "this command run" tying logs, errors, coverage,
  and summaries together.
- A future TUI would have to scrape logs or reverse-engineer multiple tables.

## Decision

Introduce a run observability layer as a first-class architecture boundary.

Runners must emit structured run events through a `RunReporter` instead of
directly owning terminal presentation. Existing Python logging remains for
developer diagnostics and third-party/library messages, but it is no longer the
source of truth for run facts.

Target shape:

```text
Command
  -> RunContext(run_id, uid, command, args)
  -> Runner
      -> Store          content, control state, resumability
      -> RunReporter    execution facts
            -> ConsoleSink
            -> JsonlSink
            -> SqliteSink
            -> future TUI reader
```

`RunReporter` emits typed events such as:

```text
run.started
run.completed
fetch.endpoint.started
fetch.endpoint.completed
fetch.item.retry_scheduled
fetch.item.unavailable
parse.model.started
parse.model.completed
parse.model.failed
asr.item.started
asr.item.retry_scheduled
asr.item.failed
asr.item.completed
asr.segment.high_risk_split
asr.segment.rate_limited
asr.coverage.partial
```

Event names should use the form:

```text
<stage>.<object>.<verb>
```

### Stable Event Naming

The event prefix names the user-visible stage, not necessarily the internal
pipeline key. Pipeline keys remain structured fields such as
`pipeline="audio"`.

Stable semantic events are the facts read by CLI summaries, future TUI views,
and run-summary code. Diagnostic logging may keep lower-level implementation
names in Python logger names or log messages, but those names should not become
read-side contracts.

| Event | Stable | Notes |
| --- | --- | --- |
| `fetch.run.started` | yes | One fetch command run began. |
| `fetch.run.completed` | yes | One fetch command run completed with summary data. |
| `fetch.run.failed` | yes | One fetch command run failed before normal completion. |
| `fetch.endpoint.started` | yes | Endpoint-level fetch work began. |
| `fetch.endpoint.completed` | yes | Endpoint-level fetch work completed. |
| `fetch.endpoint.failed` | yes | Endpoint-level fetch work failed after retries or permanent error. |
| `fetch.endpoint.unavailable` | yes | Endpoint route/data is unavailable but the run may continue. |
| `fetch.endpoint.retry_scheduled` | yes | Endpoint retry/backoff decision. |
| `fetch.endpoint.source_failed` | yes | A source endpoint needed by item fan-out failed. |
| `fetch.item.saved` | yes | A fetched item was persisted. |
| `fetch.item.unavailable` | yes | A fetched item is unavailable but the run may continue. |
| `fetch.item.failed` | yes | Item-level fetch failed finally. |
| `fetch.item.retry_scheduled` | yes | Item-level retry/backoff decision. |
| `parse.run.started` | yes | One parse command run began. |
| `parse.run.completed` | yes | One parse command run completed with summary data. |
| `parse.run.failed` | yes | One parse command run failed before normal completion. |
| `parse.model.started` | yes | One parse model materialization began. |
| `parse.model.completed` | yes | One parse model materialization completed. |
| `parse.model.failed` | yes | One parse model materialization failed. |
| `parse.images.started` | yes | Image side-work began. |
| `parse.images.completed` | yes | Image side-work completed. |
| `parse.images.failed` | yes | Image side-work failed. |
| `asr.run.started` | yes | One ASR command run began. |
| `asr.run.completed` | yes | One ASR command run completed with summary data. |
| `asr.run.failed` | yes | One ASR command run failed before normal completion. |
| `asr.discovery.completed` | yes | ASR candidate discovery completed. |
| `asr.discovery.failed` | yes | Candidate discovery failed and the audio pipeline may continue empty. |
| `asr.budget.exceeded` | yes | ASR candidate estimate exceeded command budget. |
| `asr.dry_run.completed` | yes | ASR dry-run candidate reporting completed. |
| `asr.item.completed` | yes | One audio transcription item succeeded; `pipeline="audio"`. |
| `asr.item.failed` | yes | One audio transcription item failed finally; `pipeline="audio"`. |
| `asr.item.retry_scheduled` | yes | ASR item retry/backoff decision; `pipeline="audio"`. |
| `asr.worker.unexpected_error` | yes | Worker safety net caught an unexpected error. |
| `asr.segment.rate_limited` | yes | MiMo segment request hit rate-limit and scheduled retry. |
| `asr.segment.empty_skipped` | yes | A tiny empty segment was treated as skippable. |
| `asr.segment.high_risk_split` | yes | A high-risk segment was split into smaller pieces. |
| `asr.segment.high_risk_skipped` | yes | A minimum-size high-risk segment was skipped as partial content. |
| `asr.coverage.partial` | yes | Final ASR coverage is incomplete but command did not crash. |

Do not emit stable run events with implementation prefixes such as
`audio.item.*`. Use `asr.item.*` and carry `pipeline="audio"` as data.

Every event should carry stable identity fields where applicable:

```text
run_id
uid
stage
endpoint
pipeline
item_type
item_id
level
event
message
data
ts_ms
```

## Storage

Add append-only producer-state tables to the main DB. These tables are internal
debug/observability state, not part of the consumer-facing content contract.

```sql
CREATE TABLE IF NOT EXISTS stage_run (
    run_id        TEXT PRIMARY KEY,
    uid           INTEGER NOT NULL,
    command       TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms   INTEGER,
    args_json     TEXT NOT NULL,
    summary_json  TEXT
);
```

```sql
CREATE TABLE IF NOT EXISTS stage_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    ts_ms     INTEGER NOT NULL,
    level     TEXT NOT NULL,
    stage     TEXT NOT NULL,
    event     TEXT NOT NULL,
    endpoint  TEXT,
    pipeline  TEXT,
    item_type TEXT,
    item_id   TEXT,
    message   TEXT,
    data_json TEXT
);
```

Recommended indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_stage_event_run_id
    ON stage_event(run_id, id);

CREATE INDEX IF NOT EXISTS idx_stage_event_item
    ON stage_event(stage, endpoint, pipeline, item_type, item_id);

CREATE INDEX IF NOT EXISTS idx_stage_run_uid_started
    ON stage_run(uid, started_at_ms DESC);
```

`stage_task`, `fetch_endpoint_state`, and `audio_transcription` continue to be
the current-state/control-flow tables. `stage_run` and `stage_event` provide
history and timeline.

## Event Granularity

Do not append every progress tick to SQLite.

Append events for semantic transitions:

- run started/completed
- endpoint/model/pipeline started/completed/failed
- item retry/final failure/success when useful
- rate limit and backoff decisions
- ASR high-risk split and rate-limit handling
- coverage partial/missing/failed summaries
- unexpected errors

High-frequency counters should remain current-state updates:

- `fetch_endpoint_state.item_progress`
- `stage_task.payload.pipelines[*].items`
- `audio_transcription.status`

Console rendering may throttle redraws independently.

Current run-summary behavior and CLI rendering details are documented in
[docs/observability.md](../observability.md).

## CLI Rendering

CLI output should become a sink, not runner behavior.

Default human mode should show concise progress and final summaries. It should
not require reading log lines to understand incomplete work.

Command results should carry the `run_id` created for the run. After a write
command finishes, the CLI must prefer `load_run_summary(uid, run_id=...)` over
loading the latest summary by `uid`. Loading by `uid` alone is only a
compatibility fallback for older result objects or manual read-side calls.
This keeps final output tied to the command that just ran, even if another
same-uid command starts before the summary is rendered.

Recommended global output flags:

```text
--log-level debug|info|warning|error
--log-file PATH
--output human|json
--no-progress
```

`--quiet` may remain as a compatibility alias for warning/error-only runtime
output plus final machine-readable status where appropriate.

Direct `print(...)` calls inside runners should be removed. Command handlers may
still print final output through a renderer.

## TUI

The TUI should be a read-side application over SQLite and event state. It should
not parse terminal logs.

Initial TUI sources:

- `stage_task`
- `fetch_endpoint_state`
- `stage_error`
- `audio_transcription`
- `stage_run`
- `stage_event`

Suggested first layout:

```text
top:     uid, command, run status, elapsed time
left:    stage list and stage status
center:  current endpoint/model/pipeline progress
right:   retry queue and recent failures
bottom:  event timeline
```

The TUI dependency should remain optional, for example via a `tui` extra. The
core CLI and library should not require TUI packages.

## Migration Plan

1. Add the observability package and data types:
   - `RunContext`
   - `RunEvent`
   - `RunReporter`
   - sinks for logging, JSONL, console, and SQLite

2. Add `stage_run` and `stage_event` DDL as internal producer-state tables.
   Include schema tests and migration notes.

3. Wire ASR first.
   ASR has the highest current observability need: retries, 429 handling,
   high-risk splitting, missing coverage, and per-bvid failures.

4. Move ASR progress and dry-run output out of runners.
   Runners emit events; CLI renderer decides what to display.

5. Wire fetching fan-out.
   Replace direct runner progress with reporter events and state updates.

6. Wire parsing.
   Parsing has simpler model-level events and can be migrated after ASR/fetch.

7. Implement the first TUI as an optional read-side command.

## Consequences

Positive:

- Every command run gets a stable identity.
- CLI, JSONL logs, SQLite history, and TUI can be different views of the same
  facts.
- Missing ASR rows, failed rows, retrying rows, and historical errors become
  distinguishable.
- Runner code can focus on orchestration and state transitions instead of
  terminal presentation.

Negative:

- More moving parts: reporter, sinks, event schema, and extra tests.
- Care is required to avoid excessive event volume.
- Existing tests that assert logging/progress behavior will need migration.

## Non-Decisions

This ADR does not require replacing Python `logging` with loguru, structlog, or
Rich logging.

This ADR does not make `stage_run` / `stage_event` part of the stable consumer
content contract.

This ADR does not require implementing the TUI before improving CLI output.

This ADR does not change the raw/main DB split or the direct SQL read-side
contract.

## Implementation Status

As of 2026-06-18:

- Steps 1-3 are implemented for ASR/processing.
- Stable event naming has been converged on `fetch.*`, `parse.*`, and `asr.*`.
  ASR item events use `asr.item.*`; `pipeline="audio"` remains a structured
  field.
- Step 5 is implemented as a side-channel: fetching still owns the current
  progress renderer, but emits run, endpoint, retry, item-saved, item-failed,
  and item-unavailable events to `stage_run` / `stage_event`.
- Step 6 is implemented as a side-channel: parsing emits run, model, and image
  download events to `stage_run` / `stage_event`.
- Step 4 is partially implemented. ASR and fetching now route progress through
  a shared injectable progress seam in `bili_unit._progress`; fetching no
  longer owns the gather-plus-progress implementation, and ASR dry-run output
  is returned to the CLI instead of printed inside the runner. Final human CLI
  summaries for write-side commands are centralized in `bili_unit._cli_render`.
  Full CLI progress rendering is not yet a reporter sink.
- The first read-side Run Summary is implemented in
  `bili_unit.observability.summary`. It reads `stage_run`, `stage_event`,
  `stage_task`, `fetch_endpoint_state`, and `audio_transcription`.
- CLI final summaries for fetch, parse, sync, and ASR now prefer Run Summary
  and fall back to command results if summary loading fails. Command result
  DTOs carry `run_id`, and CLI summary loading uses that exact run when
  available instead of selecting the latest run by `uid`.
- Step 7 remains open. The TUI should be built as a read-side over SQLite once
  the run/event contract has stabilized through a few real runs.
