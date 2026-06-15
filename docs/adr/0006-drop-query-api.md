# Drop the Python query API; consumers query SQL directly

Status: accepted (2026-06-15, deletion in commit `6f009b6`)

## Context

Until Phase 5 of the SQLite refactor, the SDK exposed a Python read surface: `BiliQuery` at the top, with per-stage `FetchingQuery` / `ParsingQuery` / `ProcessingQuery` underneath, plus a constellation of read-side DTOs (`TaskDTO`, `EndpointDTO`, `VideoFullDTO`, `ParsingTaskDTO`, `ProcessingItemDTO`, and a dozen more). With SQLite as the storage backend (ADR 0005), every query expressible through these classes is also expressible as plain SQL — but the Python facade still has to be maintained, version-stabilised, documented, and matched to whatever shape the consumer wants this quarter. The user's reposition in `docs/refactor-plan-sqlite.md` §0 / §1 is unambiguous: "完全的被动暴露，不设计查询逻辑，就暴露一个 database，通过 SQL 查询" — the unit hands out a file, nothing more.

## Decision

Delete `bili_unit.query`, the three per-stage `query.py` modules, and every read-side DTO. `bili_unit.session()` and `assemble()` return a single `BiliCommand` instead of a `(cmd, qry)` tuple. Consumers read the data by opening `sqlite3.connect(bili_unit.db_path(uid))` themselves. The old `manifest` / `BiliQuery.list_*` entry points are replaced by the `manifest_summary` SQL view plus the helper functions `db_path()`, `raw_db_path()`, and `list_uids()` on the package.

## Why

- **The facade had no behaviour.** Every method was SELECT-then-shape; deleting it removed roughly 3,000 lines of read-side code (query packages + DTOs + their tests) and zero load-bearing logic.
- **Canned shapes don't fit analytical use.** `list_videos()` and `get_task()` return whatever the SDK author predicted; the consumer (`index.ingestion`) wants joins and aggregates that no canned shape covers. SQL hands that flexibility back without a per-feature SDK round trip.
- **The consumer would wrap us anyway.** Dialectica's ingestion path needs to combine bili-unit data with its own indices in SQL; running through Python DTOs first would have meant marshalling rows into objects and immediately back into SQL. One layer was always going to be redundant; we picked the right one to drop.
- **Smaller surface = clearer SemVer contract.** The unit now promises a SQLite schema (DDL in `bili_unit/_db/ddl/main_v1.sql`) instead of a Python class hierarchy. Schema changes are visible in one file; Python signatures don't drift independently.
- **Documentation gets honest.** The old `docs/api.md` cataloged Python methods; `docs/schema.md` documents the actual contract — tables, columns, views, and a handful of recipe queries.

## Consequences

- **Major SemVer break in the same release as ADR 0005.** `session()` returns `BiliCommand` not `(BiliCommand, BiliQuery)`; any `from bili_unit import BiliQuery` import breaks loudly. Captured by the v1.x → v2.0 bump.
- **Read-side test scaffolding deleted.** Tests that previously asserted via `query.get_*` or DTO equality now open a `sqlite3` connection and assert with `SELECT` + `assert_row`; store-level helpers (`FetchingStore.get_*`) cover incremental-mode runner reads.
- **Write-side DTOs survive.** `CommandResult`, `ParsingCommandResult`, `ProcessingCommandResult`, the status enums, and the exception types are still returned from `fetch / parse / process`. Those are part of the write contract, not the read facade, and the host application needs them to interpret stage outcomes.
- **Cross-uid queries are not the unit's problem.** The consumer does `ATTACH DATABASE` if they want them (see ADR 0007). The SDK does not provide a multi-uid wrapper.
- **Dialectica must land its read-side rewrite in lockstep.** Plan §8 risk row: the bili-unit v2 PR can only merge alongside Dialectica's PR that swaps `BiliQuery` calls for `sqlite3` reads. This was accepted at lock time.
