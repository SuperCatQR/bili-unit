# `ContentPost` runs alongside the legacy typed dataclasses

Status: accepted (2026-06-14, ContentPost introduced in commit `ada37f2`)

## Context

parsing produces six typed models. Five predate the parsing refactor: `UpProfile`, `VideoDetail`, `Article`, `OpusPost`, `DynamicPost` — the "legacy typed dataclasses". The sixth, `ContentPost`, was added as a unified view over Article / Opus / Dynamic: one shape keyed by `article:{cvid}` / `opus:{opus_id}` / `dynamic:{dynamic_id}`, carrying cross-refs between the three content identities.

Both sets are persisted. A natural question: why not delete the legacy three (Article / OpusPost / DynamicPost) now that ContentPost unifies them?

## Decision

Keep all six. `ContentPost` is the contract processing and ingestion consume for content; the legacy three stay as its candidate source.

## Why

- **ContentPost is derived, not primary.** Its candidates come from the legacy dataclasses' persisted dicts via `selectors/{article,opus,dynamic}_posts_from_parsed` (consolidated in commit `7ddf965`). The legacy `from_raw` is the place raw B 站 payloads first become typed; ContentPost is a projection on top. Deleting the legacy three would force ContentPost to re-derive from raw, duplicating the field-extraction logic the legacy classes already own (image dedup, modules normalisation, major-type branching).
- **The legacy three carry fields ContentPost deliberately drops.** `Article.content_json` (the structured node tree), `OpusPost.detail_images` (raw image info with width/height), `DynamicPost.forwarded` (recursive orig dynamic), `VideoDetail.pages` (cid list for audio). These are real downstream needs (audio reads `VideoDetail.pages` for cid lookup); ContentPost's flat text+images+stats shape is too narrow to replace them.
- **`UpProfile` / `VideoDetail` are not content at all.** They are user-profile and video-work shapes; ContentPost only models article/opus/dynamic. The "legacy five" framing is loose — three are content candidates, two are independent models that happen to predate the refactor.

## Consequences

- Three persistence paths run in parallel for content: `article_post/`, `opus_post/`, `dynamic_event/` (legacy) + `content_post/` (unified). Disk cost is negligible (JSON, deduplicated by key); the cost is conceptual — a reader must learn that ContentPost is the contract and the legacy three are sources.
- Field-extraction bugs must be fixed in the legacy `from_raw` (the source), not in ContentPost derivation. The selectors package is the single place that maps legacy → ContentPost (post-`7ddf965`); a fix there propagates to all ContentPost consumers in one edit.
- If ingestion ever stops needing the legacy fields (content_json / detail_images / forwarded), the legacy three could collapse into ContentPost. Not pursued — premature until ingestion's actual read pattern is known.

## 2026-06-14 update: video kind merged into ContentPost

Originally `ContentPost` only modelled three content identities (article / opus / dynamic) — `VideoDetail` lived strictly in `video_work/` because it was framed as a video-work shape rather than a "content post". This worked while `MAJOR_TYPE_ARCHIVE` dynamics were the only video reference inside the unified view (and they were filtered out of `dynamic_posts_from_parsed`, so videos simply did not appear in `content_post/`).

Adding a `kind="video"` `ContentPost` candidate fixes the asymmetry:

- **Why now.** "List all content authored by an UP" was a join across `content_post/` and `video_work/`. With video kind in ContentPost, `qry.parsing.list_items(uid, "content_post")` is a single read and downstream callers no longer need to know about the legacy split.
- **`video_posts_from_parsed`.** Lives in `selectors/_common.py` and follows the same shape as the article/opus/dynamic siblings: consumes `VideoDetail.to_dict()`, emits `ContentPost(content_key=f"video:{bvid}", kind="video", source_refs=[SourceRef("video_detail", bvid)], cross_refs=CrossRefs(bvid=bvid))`. The legacy `video_work/` slot is unchanged — it remains the candidate source (same relationship Article has to its ContentPost candidate), so VideoDetail-only fields (`pages`, `cid`, `tags`, `owner`, ...) stay accessible to processing's audio runner.
- **`bvid` priority in `content_key_for_refs`.** Bumped above `dynamic_id` (now `cvid > opus_id > bvid > dynamic_id`). A `MAJOR_TYPE_ARCHIVE` dynamic that previously keyed as `dynamic:{id}` now keys as `video:{bvid}`, which is the more stable identity and lets archive dynamics merge into the matching video post without a separate alias step. `dynamic_posts_from_parsed` already excluded these (video target refs are not a readable body), so no existing parsed entry's content_key changes; the rule shift only affects new merge candidates that explicitly carry both `bvid` and `dynamic_id`.
- **Merge sort order.** `0=article, 1=opus, 2=video, 3=dynamic` (was `0=article, 1=opus, 2=dynamic`). The `_merge_into` kind-priority set now includes `"video"` alongside `"article"` and `"opus"` — when a dynamic and a video share a key, the merged post's kind is `video`, not `dynamic_*`.

VideoSubtitle is intentionally not a ContentPost candidate: subtitles are a transcription input, not authored content.
