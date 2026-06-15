# `ContentPost` runs alongside the legacy typed dataclasses

Status: **superseded** (2026-06-14, by commit `57b4a17` "refactor(parsing): drop content_post merged view"; PR #17)

## Outcome

`ContentPost` was removed. The parsing layer now ships exactly the 6 typed dataclasses it had before the merged view was introduced: `UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`. The `selectors/` package, the `content_post/` parsing slot, the `_parse_content_posts` materializer handler, and the merged-view tests are all gone (26 files changed, +128 / −2451 lines).

The driving observation was simple: **no consumer was reading `content_post/`**. The unified view was a 53% disk-space duplicate of the article / opus / dynamic posts it derived from, paid for in advance against an `index.ingestion` reader that hadn't materialised. With ingestion's actual read pattern still unknown, the projection was premature.

The shared cross-model identity types (`SourceRef`, `CrossRefs`, `content_key_for_refs`) survived the removal — they moved to `bili_unit/parsing/models/_refs.py` and are still imported by all 6 typed models. Cross-content joins (article ↔ opus ↔ dynamic ↔ video by `cvid` / `opus_id` / `dynamic_id` / `bvid`) remain a query-time concern; downstream callers walk the typed dicts directly.

## Why kept as a record

The original decision (keep ContentPost as a derived contract while preserving the legacy six as its sources) was a real trade-off, taken when ingestion's needs looked imminent. Recording its reversal — including which downstream signal (no consumer) collapsed the trade-off — is the point of the ADR series. If a future PR reintroduces a unified content projection, the lesson here is: do not persist the projection ahead of a known consumer; if a join is needed, expose it through `query`, not through a second persistence path.

## Original text (2026-06-14, accepted)

> parsing produces six typed models. Five predate the parsing refactor: `UpProfile`, `VideoDetail`, `Article`, `OpusPost`, `DynamicPost` — the "legacy typed dataclasses". The sixth, `ContentPost`, was added as a unified view over Article / Opus / Dynamic ... Both sets are persisted. ... Decision: Keep all six. `ContentPost` is the contract processing and ingestion consume for content; the legacy three stay as its candidate source.

Full original text in git history: see this file at commit `f2c9a04^` or earlier. The 2026-06-14 follow-up section ("video kind merged into ContentPost") describing `video_posts_from_parsed`, the `bvid > dynamic_id` priority bump, and the kind-priority merge sort order is also superseded — those mechanisms lived in `selectors/` and went with it.
