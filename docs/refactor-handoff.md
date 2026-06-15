# Refactor Handoff — SQLite Repositioning

> Drop-in pickup document for the next agent. Don't restate plan content; read the
> referenced artifacts first.

## TL;DR

Mid-flight refactor turning `bili_unit` from a "fetcher SDK with Python query API"
into a **passive SQLite-backed data store** consumed via raw SQL. Phases 1-5 done
and committed. **Phase 6** (test rewrites) is the next big chunk; Phases 7-8
(migration tool, docs) follow.

Branch: `refactor/sqlite-data-store-2026-06-15` (off `fix/stale-failed-item-ids-2026-06-15`).
Not pushed. Working tree clean.

## Required reading (in order)

1. [docs/refactor-plan-sqlite.md](docs/refactor-plan-sqlite.md) — full plan, schema, decisions, phase list
2. [docs/refactor-phase3-conventions.md](docs/refactor-phase3-conventions.md) — runner adaptation conventions
3. `git log --stat HEAD~4..HEAD` — what's already landed (4 commits, +1758/-4877 net)
4. [bili_unit/_db/ddl/main_v1.sql](bili_unit/_db/ddl/main_v1.sql) and [raw_v1.sql](bili_unit/_db/ddl/raw_v1.sql) — the actual schema
5. [bili_unit/tests/conftest.py](bili_unit/tests/conftest.py) `collect_ignore_glob` — every file Phase 6 needs to address

## Current state

| Aspect | Value |
|---|---|
| Tests | **398 passing**, 0 failing, 0 errored, 0 skipped at collection (`.venv/Scripts/python.exe -m pytest bili_unit/tests/ -q`) |
| Ruff | clean (`.venv/Scripts/python.exe -m ruff check bili_unit/`) |
| Import | clean (`import bili_unit`) |
| Branch | `refactor/sqlite-data-store-2026-06-15` not pushed |
| Phases done | 1, 2, 3, 4, 5 |
| Phase in flight | 6 (test rewrites) — **not yet started** |

## What's on disk (production state)

- [bili_unit/_db/](bili_unit/_db/) — SQLite plumbing: `paths.py`, `connection.py`, `context.py` (`UidContext`), `ddl/{main_v1,raw_v1}.sql`. Public exports in [bili_unit/_db/__init__.py](bili_unit/_db/__init__.py).
- Three semantic stores: [bili_unit/fetching/_store.py](bili_unit/fetching/_store.py), [bili_unit/parsing/_store.py](bili_unit/parsing/_store.py), [bili_unit/processing/_store.py](bili_unit/processing/_store.py). Each opens against a `UidContext` and exposes typed `save_*` / `get_*` / `list_*` / `update_*` methods.
- Stage runners and `command.py` rewritten to construct `UidContext` per call, build stores from it, run, close. Stage `assemble()` returns single `Command`.
- Top-level [bili_unit/__init__.py](bili_unit/__init__.py): `session() → BiliCommand` (single value, no qry tuple). Exports `db_path(uid)`, `raw_db_path(uid)`, `list_uids(root)`, `UidContext`.
- [bili_unit/command/__init__.py](bili_unit/command/__init__.py) `BiliCommand.delete_uid` is now file IO (`rm {uid}.db / {uid}.raw.db / rmtree({uid}/)`).
- [bili_unit/__main__.py](bili_unit/__main__.py) trimmed to: `fetch / parse / process / delete-uid / login / init-mimo`.
- [bili_unit/_env.py](bili_unit/_env.py) settings: only `bili_db_dir` for storage; old per-stage `*_data_dir` / `*_error_dir` / `bili_manifest_dir` deleted.

## What's deleted

`bili_unit/_storage/`, `bili_unit/query/`, `bili_unit/_aggregates.py`, `bili_unit/_manifest.py`,
plus per-stage `data.py` / `error.py` / `keys.py` / `task.py` / `protocols.py` / `query.py`,
plus all read-side DTOs (`TaskDTO`, `EndpointDTO`, `ParsingTaskDTO`, `ProcessingItemDTO`, etc.),
plus CLI subcommands `query` / `list-uids` / `video-full` / `manifest`.

## Phase 6 — what's actually pending

[bili_unit/tests/conftest.py](bili_unit/tests/conftest.py) `collect_ignore_glob` lists 25 ignored test files. Categorise them:

**A. Just delete** (test classes that no longer exist):
- `test_storage_kv_contract.py` (JsonKVStore)
- `test_fetching_data.py` (DataStore)
- `test_fetching_error.py` (ErrorStore)
- `test_fetching_error_classification.py` (ErrorStore-coupled)
- `test_fetching_protocol.py` (`FetchingReadView`)
- `test_fetching_task.py` (TaskValue/EndpointEntry)
- `test_fetching_query.py` (Query class)
- `test_fetching_rate_limit.py` (mostly fine but **3 tests** test deleted `to_state()` — delete those tests, keep the rest)
- `test_parsing_data.py` (ParsingDataStore)
- `test_parsing_protocol.py` (`ParsingReadView`)
- `test_processing_data_error.py` (DataError class)
- `test_manifest.py` (manifest.py deleted)

**B. Rewrite against new stores / SQL** (test runner / command behaviour, still relevant):
- `test_fetching_runner.py` — biggest file, ~38 tests. Rebuild fixtures around `UidContext`+`FetchingStore`; assertions either via store API or direct `ctx.main.fetch_one()`.
- `test_fetching_video_detail.py` — fanout endpoint, similar pattern
- `test_fetching_command.py` — Command's per-call UidContext lifecycle
- `test_fetching_integration.py` — multi-endpoint integration
- `test_fetching_media_list_and_runner_safety.py`
- `test_fetching_extended_endpoints.py`
- `test_parsing_command.py` — ParsingCommand with new flow
- `test_parsing_infra.py`
- `test_parsing_video_subtitle.py` (the model tests at file's tail, ~17 of them, were green at HEAD; the import header is what blew up)
- `test_processing_runner.py` — already partially edited by failed subagent; verify state
- `test_processing_cost.py` — still tests audio cost accounting
- `test_processing_cli_filters.py` — filter logic
- `test_processing_subtitle_priority.py` — short-circuit when subtitle complete

**C. Rewrite against unit-level facade** (cross-stage / public API):
- `test_sdk_session.py` — `session()` now yields single `cmd`; rewrite assertions
- `test_sdk_assemble_settings.py` — `assemble()` shape changed
- `test_sdk_public_surface.py` — `__all__` was pruned; rewrite expected exports list
- `test_task_failed_item_ids.py` — `failed_item_ids` is no longer persisted; test the SQL view path or delete
- `test_delete_uid.py` — `BiliCommand.delete_uid` is file IO; rewrite to assert files removed
- `test_cli_subset.py` — query/manifest subcommands deleted; trim or rewrite

## Phase 7 — migration script

Plan §6 step 7. Optional one-shot tool `tools/migrate_jsonkv_to_sqlite.py` that walks
`data/bili/fetching/data/{uid}/...` (legacy JSON tree) and inserts rows into
`{db_dir}/{uid}.db` + `{uid}.raw.db`. Not on disk yet. Reasonable to skip if user
agrees they'll re-fetch.

## Phase 8 — documentation

- Replace `docs/api.md` with `docs/schema.md` (DDL excerpt + 5 SQL recipes)
- Update [README.md](README.md) "用法" / "Embedding" sections — `session()` returns
  single value, query examples become SQL not `qry.X()`
- Three ADRs: `0005-sqlite-as-deliverable.md`, `0006-drop-query-api.md`,
  `0007-one-uid-per-db.md` under [docs/adr/](docs/adr/)

## Locked decisions (already in plan §11 but worth surfacing)

- **One uid per database**: `data/bili/{uid}.db`. `delete_uid` = rm file.
- **Raw payloads in separate file**: `{uid}.raw.db`. Producer-private.
- **Errors in main DB** `stage_error` table, not separate file
- **rate_limit is in-memory only** — `RateLimitController.to_state()` was deleted;
  any test asserting on persisted rate-limit state needs to drop the assertion.
- **`session()` returns single value** — major-bump SemVer break. CLI tests that
  do `async with session() as (cmd, qry)` need to become `async with session() as cmd`.

## Subagent guidance

The fan-out for Phase 3 ate balance and three subagents died mid-task with
"403 insufficient balance". Lessons:

- **Tighten prompts**. Earlier prompts were 200+ lines; the surviving subagent
  in Phase 4 used ~100 line prompts and finished cleanly.
- **Verify tool output**. Failed subagents still run tool calls before dying,
  so check `git status` / pytest output after each fan-out, even on failure.
- **Don't ask subagents to rewrite tests across stages in one task.** Keep each
  agent to one stage's tests. They tried to touch shared `conftest.py` last time,
  which collided.

## Verification commands

```bash
.venv/Scripts/python.exe -m pytest bili_unit/tests/ -q --tb=no    # currently 398 passed
.venv/Scripts/python.exe -m ruff check bili_unit/                  # currently clean
.venv/Scripts/python.exe -c "import bili_unit; print('ok')"
git status   # should be clean before starting Phase 6
```

Use `.venv/Scripts/python.exe` directly. `uv run pytest` triggers a Windows
trampoline canonicalisation bug in this environment.

## Suggested skills

None for the basic flow — Phase 6 is mechanical test rewrites, not skill territory.

If picking up Phase 8 (docs), invoke whatever ADR / docs skill the user has set
up; check `~/.claude/skills/` for relevant entries before generating doc files
from scratch.

## Open questions to surface to user before continuing

1. **Phase 6 sequencing**: do all 25 files in one big batch, or split into
   "fetching tests / parsing tests / processing tests / sdk tests" and verify
   green between batches? The latter is safer.
2. **Push timing**: 4 commits unpushed. User may want to push after Phase 6
   for review, or wait until full refactor lands. Ask.
3. **Branch base**: refactor sits on `fix/stale-failed-item-ids-2026-06-15`
   (parent has the f29a42e fix). When PRing to main, decide: rebase to drop
   f29a42e (it'll come via its own PR) or merge with it included.
4. **Migration tool (Phase 7)**: user said earlier "data/bili/ 视为可重新抓取"
   in the plan §1 lock-in, so Phase 7 is optional. Confirm user wants to skip
   or write the tool.
5. **Dialectica coordination**: the consumer (Dialectica) currently uses
   `BiliQuery`. After this refactor it must switch to `sqlite3.connect(bili_unit.db_path(uid))`.
   Plan §8 says the bili-unit PR must merge alongside Dialectica's switch PR.
   Has the user started that side?

## Pitfalls

- `audio_transcription.bvid` has `FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE`.
  Tests that save audio without first inserting a video row will fail with FK
  constraint error. Either insert minimal video stub, or restructure test to use
  the real parsing→processing pipeline order.
- `progress` JSON shape changed from `{mode, next_request, last_completed_request, done, updated_at}`
  to `{cursor, total, fetched, updated_at_ms}`. Tests asserting on the old shape
  must be rewritten.
- Tests that previously poked `data.put("rate_limit:global", ...)` to seed
  rate-limit state need to drop those calls — that path doesn't exist any more.
- The 33 in-test `@pytest.mark.skip` markers added by Phase 3 subagents (in
  `test_parsing_*.py` files now in collect_ignore_glob) are dead now since
  the whole files are file-level ignored. When rewriting those test files,
  drop the inline skip markers.

## Files modified outside production code

- [bili_unit/tests/test_fetching_env.py](bili_unit/tests/test_fetching_env.py):
  one assertion changed `bili_fetching_data_dir` → `bili_db_dir` after settings
  collapsed. Rest of file untouched.
