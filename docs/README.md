# bili_unit docs

This directory keeps only current project truth. Historical reviews, migration
notes, and long per-stage feature notes are intentionally not kept here.

## Reading path

| Need | Read |
| --- | --- |
| Run the tool | [`../README.md`](../README.md) |
| Query output data | [`schema.md`](schema.md) |
| Understand module responsibilities | [`architecture.md`](architecture.md) |
| Inspect run state / future TUI inputs | [`observability.md`](observability.md) |
| Maintain Bilibili endpoint payloads | [`endpoint-contract.md`](endpoint-contract.md) |
| Check upstream library role | [`upstream.md`](upstream.md) |

## Files

| File | Purpose |
| --- | --- |
| [`schema.md`](schema.md) | SQLite tables, views, indexes, and SQL recipes |
| [`architecture.md`](architecture.md) | module responsibilities and import rules |
| [`observability.md`](observability.md) | run events, Run Summary, dashboard snapshot |
| [`endpoint-contract.md`](endpoint-contract.md) | raw payload shapes per Bilibili endpoint |
| [`upstream.md`](upstream.md) | `Nemo2011/bilibili-api` role and links |

## Windows encoding

Docs are UTF-8. If Chinese text looks corrupted in PowerShell:

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-Content -Encoding UTF8 docs\README.md
```
