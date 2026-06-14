# Stable API

bili_unit 作为 SDK 对外承诺的 API 边界。SemVer 保护范围限于本文件 "Stable" 标记的项；"Internal" 部分可能在小版本中变化，不应直接 import。

> 当前版本：0.1.x（pre-1.0）。pre-1.0 阶段的 SemVer 承诺：minor 版本不引入破坏性改动，patch 版本仅修 bug。0.x → 1.0 的 cut-over 会单独走 ADR。

## Stable —— 入口

| 名字 | 形态 | 说明 |
|---|---|---|
| `bili_unit.session` | async ctx mgr | **推荐入口**。包住 `assemble + cmd.close()`。 |
| `bili_unit.assemble` | async function | 低阶入口；返回 `(BiliCommand, BiliQuery)`，调用方负责 `await cmd.close()`。 |
| `bili_unit.BiliCommand` | class | 写侧统一入口，编排三 stage。方法：`fetch / parse / process / delete_uid / close`。每次 `fetch / parse / process` 跑完后会自动刷新 `data/bili/manifest/{uid}.json`，供 CLI `manifest` 子命令读取。 |
| `bili_unit.BiliQuery` | class | 只读统一入口。属性：`fetching / parsing / processing` 三个 sub-query。 |
| CLI `python -m bili_unit manifest <uid>` | sub-command | 打印持久化的跨阶段摘要（`--json` 给完整 JSON）。**只读**：`compute_manifest / write_manifest / read_manifest / delete_manifest` 是内部函数，不在 SDK 公开面。|

## Stable —— 配置 / 类型

| 名字 | 形态 | 说明 |
|---|---|---|
| `bili_unit.BiliSettings` | pydantic-settings 类 | 53 个字段，按 stage 前缀分组。可程序化构造或从 `.env` 加载。 |
| `bili_unit.get_settings` | function | 返回 `BiliSettings` 单例（首次调用时从 `.env` 加载）。 |
| `bili_unit.reload_settings` | function | 重置单例（测试 / 配置热更用）。 |
| `bili_unit.CredentialProvider` | type alias | `Callable[[], Awaitable[Credential | None]]`，`assemble/session` 的 `credential_provider` 入参类型。 |
| `bili_unit.__version__` | str | 包版本（来自 `importlib.metadata`）。 |

## Stable —— DTO / 异常 / 枚举

所有顶层 re-export 的名字均稳定。三 stage 各自的状态机、DTO 形状、异常层级均覆盖：

- **fetching**：`TaskStatus / EndpointStatus / TaskDTO / EndpointDTO / ErrorDTO / TaskResult / CommandResult`，异常 `FetchingError`
- **parsing**：`ParsingTaskStatus / ParsingModelStatus / ParsingTaskDTO / ParsingModelDTO / ParsingImageDTO / ParsingCommandResult`，异常 `ParsingError`
- **processing**：`ProcessingTaskStatus / ProcessingItemStatus / ProcessingPipelineStatus / ProcessingTaskDTO / ProcessingItemDTO / ProcessingPipelineDTO / VideoFullDTO / VideoSummaryDTO / ProcessingCommandResult`，异常 `ProcessingError / AudioError`

每个 stage 的 task DTO 都带 `failed_item_ids: list[str]`：消费方不必再 join task.json 与 error 日志即可拿到"哪些 item 失败"。编码：

- fetching：`"endpoint"`（uid-level 失败）或 `"endpoint:item_id"`（item-level fan-out 失败）
- parsing：失败的 model 名（无 ErrorStore，粒度到 model）
- processing：`"pipeline:item_type:item_id"`（如 `"audio:transcription:BV1abc"`）

完整列表见 `bili_unit.__all__`。

## Internal —— 不要直接 import

以下都属于实现细节，可能在小版本中改名 / 重组 / 删除：

- 任何 `_` 前缀模块：`bili_unit._env`、`bili_unit._storage`、`bili_unit._retry`、`bili_unit._logging`、`bili_unit._manifest`（`compute_manifest / write_manifest / read_manifest / delete_manifest` 仅通过 CLI 暴露）
- stage 子包内部：
  - `bili_unit.fetching.runner` / `.client` / `._endpoint_catalog` / `._bilibili_adapter` / `.rate_limit`
  - `bili_unit.parsing.materializer` / `.models` / `._images` / `.keys`
  - `bili_unit.processing.runner` / `.audio`（含 `._asr_backend` / `._init_wizard` / `._downloader`）
  - 任何 stage 的 `data` / `error` / `command` / `query` 模块——通过 `BiliCommand` / `BiliQuery` 间接访问
- `bili_unit.fetching.auth`：例外——`qr_login()` 与 `save_credential_to_env()` 是登录场景需要的，视为 stable；其余 internal

## 扩展点

通过 `assemble(...)` / `session(...)` 的关键字参数注入：

| 入参 | 类型 | 用途 |
|---|---|---|
| `settings` | `BiliSettings | None` | 完全程序化的配置（不读 `.env`） |
| `asr_backend_override` | `str | None` | 临时换 ASR 后端（`mock` / `mimo` / `whisper`），优先级高于 `BILI_PROCESSING_ASR_BACKEND` |
| `credential_provider` | `CredentialProvider | None` | 由宿主应用提供 `Credential`（绕过 `.env` 读凭据的默认路径） |

`BiliCommand` 内部继续使用 DI：`fetch_fn` / `downloader_factory` / `convert_fn` 也可以通过子层注入做替换，但当前不在 stable 入口面里——如果你的用例需要替换这些，先开 issue 讨论。

## 不在 SDK 范围

- 跨源归一化、清洗、检索（`index.ingestion` 的事）
- bili-api-info 之外的端点（`docs/structure/bili.md §8` 边界）
- 推送 / 主动通知
- 其他 Bilibili 业务（弹幕互动、付费内容操作等）

## 许可

GPL-3.0-only（继承自 `bilibili-api-python`）。嵌入到非 GPL 兼容项目前，请先确认你的项目许可与之兼容。
