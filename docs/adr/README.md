# Architecture Decision Records

Decisions that are hard to reverse, surprising without context, and the result of a real trade-off. New ADRs: scan for the highest number, increment by one.

| # | Title | Status |
|---|-------|--------|
| [0001](0001-file-directory-json-storage.md) | File-directory JSON as the storage backend | accepted |
| [0002](0002-processing-transform-removed.md) | processing transform subsystem removed (audio pipeline only) | accepted |
| [0003](0003-pipeline-executor-kept-as-single-adapter-seam.md) | `_pipeline_executor` kept despite a single adapter | accepted |
| [0004](0004-contentpost-runs-alongside-legacy-dataclasses.md) | `ContentPost` runs alongside the legacy typed dataclasses | superseded |

Domain language lives in [`../../CONTEXT.md`](../../CONTEXT.md). Structure constraints in [`../structure/bili.md`](../structure/bili.md); implementation truth in [`../feature/`](../feature/).
