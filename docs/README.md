# bili_unit 文档

本目录只保存本项目的当前真相、结构约束和历史背景；上游
`bilibili-api-python` 的完整参考不再镜像到仓库内，统一见
[`upstream.md`](upstream.md)。

## 阅读路线

| 你想做什么 | 先读 | 再读 |
| --- | --- | --- |
| 跑一次用户同步 | [`../README.md`](../README.md) | [`feature/fetching.md`](feature/fetching.md)、[`feature/parsing.md`](feature/parsing.md) |
| 查询落盘数据 | [`schema.md`](schema.md) | [`feature/processing.md`](feature/processing.md) |
| 理解模块边界 | [`structure/bili.md`](structure/bili.md) | [`../CONTEXT.md`](../CONTEXT.md) |
| 调整抓取端点 | [`feature/fetching.md`](feature/fetching.md) | [`structure/fetching-contract.md`](structure/fetching-contract.md)、[`upstream.md`](upstream.md) |
| 排查历史决策 | 当前真相文档 | [`history/`](history/) |

## 当前真相

| 路径 | 内容 | 适用场景 |
| --- | --- | --- |
| [`schema.md`](schema.md) | SQLite 表、视图、索引和常用 SQL | consumer 读侧契约 |
| [`observability.md`](observability.md) | run events、Run Summary、CLI 最终摘要 | CLI / TUI / 长跑任务排查 |
| [`structure/bili.md`](structure/bili.md) | module 职责、import 规则、项目边界 | 架构和重构前阅读 |
| [`structure/fetching-contract.md`](structure/fetching-contract.md) | 64 个 endpoint 的 raw payload 形状 | parser / endpoint 维护 |
| [`feature/fetching.md`](feature/fetching.md) | endpoint catalog、profile、mode、runner、store、CLI | fetching 实现真相 |
| [`feature/parsing.md`](feature/parsing.md) | typed object、materializer、image assets、parsing CLI | parsing 实现真相 |
| [`feature/processing.md`](feature/processing.md) | audio pipeline、ASR、cache、budget、ASR CLI | processing 实现真相 |
| [`upstream.md`](upstream.md) | 上游 `Nemo2011/bilibili-api` 的角色和链接 | 查上游能力 / 行为漂移 |

## 历史背景

| 路径 | 内容 |
| --- | --- |
| [`history/architecture-review-2026-06-14.md`](history/architecture-review-2026-06-14.md) | 历史架构评审 |
| [`history/refactor-plan-sqlite.md`](history/refactor-plan-sqlite.md) | SQLite 重构计划与阶段记录 |
| [`history/refactor-phase3-conventions.md`](history/refactor-phase3-conventions.md) | Phase 3 约定留档 |

当当前真相与历史背景冲突时，以当前真相为准。

## Windows 编码提示

文档按 UTF-8 保存。若 PowerShell 里中文显示成乱码，先设置控制台输出编码：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-Content -Encoding UTF8 docs\README.md
```

不要因为终端显示乱码就批量转码仓库文件。

