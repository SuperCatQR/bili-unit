# File-directory JSON as the storage backend

Status: superseded (2026-06-14, unified across stages in commit `1499f4c`; superseded 2026-06-15 by [0005](0005-sqlite-as-deliverable.md))

## Context

bili_unit persists per-uid raw payloads, task state, parsed objects, processing results, rate-limit state, and error logs. The first implementation used SQLite (`aiosqlite`); commit `1499f4c` (`refactor: unify retry/storage/error-map`) replaced it across all three stages with file-directory JSON: one `.json` file per logical key, mapped by `SchemaKeyMapper`.

## Decision

Store everything as JSON files in a directory tree. The physical layout is `{base}/{uid}/{section}/{item}.json`, derived from the logical key (`uid:{uid}:{section}:{item}`) by a declarative `KvSchema`. Stages share the engine (`_storage/_kv.py`) and the CRUD surface (`_storage/_store.py`); each declares only its own key grammar.

## Why

- **No DB dependency.** The unit is a data-collection leaf in Dialectica; adding a database server would force every contributor and CI run to provision one. Files need nothing.
- **Cross-process resumability for free.** Each endpoint result / item is its own file, so a crash leaves valid partial state. `write_pair_locked` (fetch payload + progress marker) gives commit semantics; resume re-reads progress and continues. SQLite would need WAL + careful transaction boundaries to match.
- **Trivial inspection and `delete-uid`.** `rm -rf data/bili/fetching/{uid}` cleans a user; `list-uids` is `ls`. No query language, no migration tooling.
- **The schema already varies per stage.** Fetching has `fetch` / `progress` / `rate_limit` sections; parsing collapses the section word from the path; processing has nested `proc/{item_type}/{item_id}`. Making key grammar data (`KvSchema` + `PathShape`) instead of code let each stage declare its layout in ~10 lines while sharing one engine.

## Consequences

- Atomicity is per-file (single `write_text` is atomic on most filesystems) plus the explicit `write_pair_locked` / `update_in_place` helpers that hold the asyncio lock across a read-modify-write. There is no cross-key transaction — if you need one, do it under one lock hold via `update_in_place`.
- `list_prefix` scans the directory tree; fine at this scale (hundreds to low thousands of files per uid). Would need an index if a single uid grew to tens of thousands of items.
- Concurrent writers from multiple processes are not safe — the lock is in-process. The intended deployment is one runner per uid.
