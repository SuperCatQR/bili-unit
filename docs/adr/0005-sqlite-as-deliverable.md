# SQLite as the unit's deliverable

Status: accepted (2026-06-15, cutover in commit `c515407`)

## Context

ADR 0001 chose a file-directory JSON layout to dodge a database dependency at a time when the schema was still moving. Eighteen months later the per-uid tree has grown to thousands of files, the data shape has stabilised across the six typed objects (videos, articles, opus, dynamics, profiles, transcriptions), and the consumer (Dialectica `index.ingestion`) has started asking for slice/aggregate views — joins, counts, recency filters — that don't exist on the Python query surface. The user explicitly repositioned the unit (`docs/refactor-plan-sqlite.md` §0): no longer "a Bilibili SDK with a Python query API", but "a passive persistent data unit, fully independent maintenance, exposing one or more SQLite files for consumers to query in SQL". This ADR records the storage half of that turn.

## Decision

Replace the file-directory JSON backend with one SQLite database per uid at `{db_dir}/{uid}.db` (plus a sidecar `{uid}.raw.db` for raw API payloads — see ADR 0007). The unit's contract becomes the `.db` file itself. Consumers `sqlite3.connect(bili_unit.db_path(uid))` and write whatever query they need. Schema is hybrid: hot fields are typed columns; the long tail of the response goes into a `payload TEXT` JSON column.

## Why

- **The schema stabilised.** The six object types haven't changed shape in months; declarative typed columns are now cheaper to maintain than a generic KV layer. The `payload` JSON escape hatch absorbs whatever drifts.
- **SQL is the lingua franca.** The consumer needs cross-table joins (`video` × `audio_transcription`), `pubdate_ms` ranges, `view_count` filters, aggregate counts. Re-implementing these on top of a Python query facade was strictly worse than letting them write SQL.
- **Stdlib is enough.** SQLite ships with `sqlite3` in CPython; WAL gives us atomic multi-statement writes; JSON1 gives `json_extract()` filtering on the `payload` column. No new runtime dependency.
- **`delete_uid` stays trivial.** ADR 0001's "delete is `rm -rf`" promise survives the move — `os.unlink({uid}.db)` plus the raw sidecar plus `rmtree({uid}/)` for binaries. No row-level cascades, no orphan cleanup.
- **Manifest becomes a view, not a computed file.** The old `manifest.json` was regenerated on every stage run; with SQLite it becomes `CREATE VIEW manifest_summary` and computes at SELECT time. One less write path, one less staleness mode.
- **Ad-hoc inspection got faster.** A thousand-file tree is awkward to grep across; one `.db` file opens in any SQLite client and answers questions in milliseconds.

## Consequences

- **ADR 0001 is superseded.** Its Status line is updated; its rationale still applies historically but no longer describes the system.
- **Major SemVer break.** `session()` changes shape (see ADR 0006); the v1.x → v2.0 bump captures this and the query-API removal in one release.
- **Cross-process concurrency narrows.** WAL serialises writers within one DB; multiple writers across processes still aren't safe. The intended deployment continues to be one runner per uid (unchanged from ADR 0001's posture).
- **No automatic migration of existing `data/bili/{uid}/` JSON trees.** Plan §1 locks this — re-fetching is acceptable; an opt-in `tools/migrate_jsonkv_to_sqlite.py` is provided but not run by default.
- **Schema versioning becomes real.** `meta.schema_version` is checked at open time; v1 refuses to open a v2 file. ALTER flows are deferred until a v2 is actually needed (YAGNI per plan §8).
- **WAL companion files (`{uid}.db-wal`, `{uid}.db-shm`) appear on disk** during active runs. `delete_uid` cleans them up; backup tools should pick them up alongside the `.db`.
