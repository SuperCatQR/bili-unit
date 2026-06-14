# processing transform subsystem removed (audio pipeline only)

Status: accepted (2026-06-14, commit `5b317f5`)

## Context

`processing/transform/` held three handlers — `video_metadata`, `content_post`, `user_profile` — that read parsing-layer typed objects and wrote derived "bili-shape structured records" to `proc/{item_type}/`. After the parsing refactor introduced `ContentPost` (commit `ada37f2`) and the legacy dataclasses gained their own `to_dict()`, the transform handlers collapsed into field-passthrough plus a one-line `word_count`. `index.ingestion` — the only consumer these records existed for — was not yet implemented, so there was no contract to honour.

## Decision

Delete the entire transform subsystem. processing shrinks to a single `audio` pipeline. The layer name stays `processing` (not renamed to `audio`).

## Why

- **No real work.** The handlers had become pass-through. Keeping them meant three modules whose only behaviour was `dict → dict` with a field rename — a shallow layer that the deletion test fails (complexity moves to the caller, doesn't concentrate here).
- **No consumer.** ingestion was unbuilt; nothing read `proc/video_metadata/*` etc. Deleting unprotected shapes costs nothing; deleting them after ingestion ships would have been a breaking change.
- **`query` already bridges parsing.** `ProcessingQuery.get_video_full` / `list_all_videos` were re-pointed to read metadata straight from `parsing.query` and transcription from `processing`. That gave callers the joint view they wanted without a persistence layer in between.
- **Not renamed because `processing` is the structural slot.** `docs/structure/bili.md` §3 fixes the pipeline as `抓取 → 解析 → 处理`; the third stage's *name* is part of the contract with Dialectica, even if today only one pipeline runs in it.

## Consequences

- `processing` currently has one pipeline (audio). `subtitle` / OCR pipelines are explicitly deferred — see `docs/feature/processing-shrink-plan.md` "后续清理": "subtitle / OCR pipeline 单独提议，不绑在本轮".
- If ingestion later wants a stable view over parsing, it should consume `parsing.query` directly rather than resurrect a transform layer. The query seam is the documented contract.
- On-disk `proc/{video_metadata,content_post,user_profile}/` directories from prior runs are orphaned; new code does not read them. Left for manual cleanup.
