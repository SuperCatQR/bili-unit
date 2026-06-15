# One uid per database file

Status: accepted (2026-06-15, cutover in commit `c515407`)

## Context

Once SQLite was chosen as the backend (ADR 0005), the next axis to settle was tenancy: one shared database with `uid` as a partition column on every row, or one database per uid. Both are workable. The shared topology is conventional for general-purpose services; the per-uid topology mirrors the unit's actual semantics — each uid is an independently fetchable, deletable, archivable unit of data. The user picked per-uid in `docs/refactor-plan-sqlite.md` §1 ("一 uid 一库 — 匹配 data unit 语义；delete_uid = rm file；天然隔离").

## Decision

Each uid owns three artefacts on disk: `{db_dir}/{uid}.db` (the consumer-facing main database), `{db_dir}/{uid}.raw.db` (raw API payloads, producer-private), and `{db_dir}/{uid}/` (workdir for binaries — images, audio segments). `bili_unit.db_path(uid)` and `raw_db_path(uid)` resolve the two database paths. `BiliCommand.delete_uid(uid)` is implemented as `os.unlink()` on the two databases plus `rmtree()` on the workdir.

## Why

- **Independent lifecycle matches the domain.** A uid is the unit of fetch / re-fetch / delete. One bad fetch can't corrupt another user's rows because there are no other rows in the file. Re-fetching uid A is `unlink + new run`; uid B is untouched.
- **No multi-tenant query bugs.** Every SELECT in a per-uid file implicitly scopes to one uid. There is no `WHERE uid = ?` clause to forget; consumer queries can't accidentally leak rows across users. This is a real category of bug we removed by topology rather than by discipline.
- **`delete_uid` is atomic and complete.** Two `unlink` calls and a `rmtree` finish the job. No row-level cascades, no orphan rows, no `VACUUM` to reclaim space, no chance of half-deleted state.
- **No write contention between uids.** WAL serialises writers within one DB; with separate files, parallel runs on different uids never block each other. This is invisible at today's scale (1-10 concurrent uids) but cheap insurance against future fan-out.
- **Backup and archive granularity matches business need.** A per-uid `.db` can be moved, copied, or shipped to cold storage on its own. A shared DB would force `.dump` + filter or table-level surgery for the same operation.
- **Raw and main split cleanly.** Producer-private raw payloads live in `{uid}.raw.db`; consumers attach only `{uid}.db` and never see the wire format. Same topology applied at the database level.

## Consequences

- **Cross-uid queries require `ATTACH DATABASE`.** Plan §9 makes this explicit non-goal: "跨 uid 查询封装 — 消费者要跨 uid 自己 ATTACH DATABASE". Documented in `docs/schema.md`.
- **File-handle count grows with active-uid count.** At today's ceiling (1-10 concurrent uids) this is irrelevant. A deployment that ever runs thousands of uids in parallel would need to revisit pooling or a shared-DB topology; YAGNI for now.
- **WAL companion files multiply.** Each open DB produces `{uid}.db-wal` and `{uid}.db-shm`; `delete_uid` cleans them up, and backup tooling needs to pick them up alongside the `.db` to capture in-flight WAL frames.
- **Schema migrations touch every file.** A v1 → v2 schema change has to be applied to N databases, not 1. The unit owns only the producer side, so it migrates files as it opens them; consumers read whatever `meta.schema_version` the file declares and adapt.
- **A single uid still has internal contention.** Concurrent fetch + process on the same uid can collide on shared tables (`stage_task`, `stage_error`). The command layer holds an advisory `{uid}.db.lock` to prevent two `BiliCommand` instances running on one uid (plan §8 risk row).
