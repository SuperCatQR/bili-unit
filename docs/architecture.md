# bili_unit architecture

This document describes the current structure of the Bilibili data unit. It is
an implementation map, not a product roadmap.

## Position

`bili_unit` is a standalone Bilibili user-data persistence unit. Given a target
`uid`, it writes one SQLite DB file (`{uid}.raw.db`) and one workdir
(`{uid}/`) under `BILI_DB_DIR`.

```text
Bilibili API / CDN -> bili_unit -> per-uid SQLite file
```

Read-side consumers use `sqlite3` directly against the raw DB. The project does
not expose a stable Python query facade.

## Pipeline

```text
fetching -> raw.db (raw_payload + fetch_progress)
asr      -> raw.db (audio_transcription + page + segment)
```

- `fetching` calls registered Bilibili read endpoints and stores original
  responses in `raw_payload`.
- `asr` reads `raw_payload(endpoint='video_detail')` to discover bvids and
  page metadata, downloads audio, performs ASR, and writes `audio_transcription`
  rows. Segment cache hits are scoped by backend namespace, model, language,
  and timeline range, so changing ASR backend/model/language does not reuse
  stale text from an older run.

A previous `parsing` stage that materialised typed dataclasses
(`UpProfile` / `VideoDetail` / `Article` / `OpusPost` / `DynamicPost` /
`VideoSubtitle`) and `image_asset` rows existed in earlier schema versions.
It was removed in `schema_v3` because typed materialisation conflicted with
the unit's "passive persistence" position — consumers that want columnar views
build them on top of `raw_payload` themselves.

## Modules

```text
bili_unit/
  __main__.py        CLI entry: fetch / asr / delete-uid / login / init-mimo / tui
  __init__.py        top-level helpers, command assembly, sessions
  workbench.py       application-facing boundary for TUI surfaces
  tui.py             portable line-mode TUI over the workbench dashboard
  tui_spec.py        MVP panel/action specification for the TUI
  command/           cross-stage write workflow (BiliCommand)
  fetching/          endpoint catalog, auth, rate limit, runner, raw store;
                     _adapters/ holds video / subtitle / pagination wrappers
  processing/        ASR command, audio runner, ASR cache/backend, raw store,
                     _work_items.py (raw_payload → audio work items)
  observability/     run events, summaries, dashboard snapshots
  _db/               per-uid path resolution, SQLite connection, DDL
  tests/             pytest contract and behavior coverage
```

## Storage

The stable deliverable is one SQLite DB per uid:

```text
output/bili/{uid}.raw.db    canonical DB (raw payloads + ASR output)
output/bili/{uid}/          workdir for audio temp/cache files
```

Tables and views are documented in `docs/schema.md`. Raw payload shapes are
documented in `docs/endpoint-contract.md`.

## Write Boundary

The write-side boundary is `BiliCommand`.

```python
async with bili_unit.session() as cmd:
    await cmd.fetch(uid)
    await cmd.asr(uid)
```

Stage submodules such as fetching runners and audio workers are internal. They
should not be imported by consumer code.

## TUI Boundary

The TUI depends on `BiliWorkbench`, not on stage internals.

```python
async with bili_unit.workbench_session() as workbench:
    snapshot = await workbench.dashboard()
    check = await workbench.can_start_task(uid, stages=("fetching", "asr"))
```

`BiliWorkbench` combines write commands with read-side observability snapshots:

- `dashboard()`
- `inspect_uid()`
- `run_summary()`
- `can_start_task()`
- `fetch()` / `asr()` / `delete_uid()`

The first TUI surface is pinned by `bili_unit.tui_spec`: UID list, status, run
summary, attention events, recent events, adding a new uid, and actions for
fetch / asr plus delete.

The intended full-screen layout is:

```text
┌ UID sidebar ┐ ┌ selected uid detail tabs ┐
│ known uids  │ │ Summary | Fetch | ASR    │
│ active mark │ │ Events                   │
└─────────────┘ └──────────────────────────┘
┌ action bar: Add UID Fetch ASR Delete ┐
└ status bar: refresh/errors/task feedback ┘
```

Keyboard contract:

- `r` refreshes the dashboard snapshot.
- `j/down` and `k/up` move between uids.
- `tab` and `shift+tab` move between detail tabs.
- `n` prompts for a new uid and starts incremental fetch after `can_start_task`.
- `f` / `a` start fetch / asr after `can_start_task` preflight.
- `d` requires explicit confirmation before deleting a uid.
- `q` exits.

The current `bili-unit tui` implementation is a portable line-mode version of
that design. It already uses the same read model, detail tabs, action
vocabulary, preflight checks, and delete confirmation flow so it can be replaced
by a richer full-screen renderer without changing the workbench boundary.

## Observability

Write-side commands emit semantic run events through `RunReporter`. The SQLite
sink writes:

- `stage_run`: one row per command run.
- `stage_event`: append-only event timeline.

The current-state control tables remain:

- `stage_task`
- `fetch_endpoint_state`
- `audio_transcription`

`RunSummary` and dashboard snapshots derive display state from these SQLite
facts. They are the preferred inputs for CLI summaries and TUI panels.

## Boundaries

Keep these constraints intact:

- Do not add a Python query facade; consumers read SQL.
- Do not reintroduce a typed-object materialisation layer between fetching
  and asr. If a consumer wants column-shaped reads, build them as views or
  materialised tables in the consumer's own schema, not here.
- Do not let command code write the DB file directly; writes go through stores.
- Do not let stage stores call each other directly; commands inject a shared
  `UidContext`.
- Do not use `service`, `facade`, or `api` as project boundary language.
  Prefer `command`, `workbench`, `read model`, and `snapshot`.
- Do not put cross-source normalization, cleaning, search, or recommendation
  into this unit.

## External Dependencies

- `bilibili-api-python`: upstream Bilibili read capability.
- Bilibili CDN: video audio download for ASR.
- MiMo-compatible ASR backend: audio transcription.
- SQLite: the only stable output format.
