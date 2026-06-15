# processing_feature — B站用户数据处理层代码现状

> 记录 `bili_unit/processing` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`
> 对应数据契约：`docs/schema.md`

## 概述

processing 层负责对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。当前仅一条 pipeline：audio。视频元数据 / 内容帖 / UP 主画像由 parsing 层落到 SQLite 主库，processing 通过 `ParsingStore` 的 read 方法（`get_video_payload` / `get_video_subtitle_payload` / ...）只读取所需字段，不做"字段透传"层。

audio 流水线通过 asyncio.Queue + worker pool 并发调度，支持 incremental / full 两种处理模式。数据源是 parsing 层产出的 `VideoDetail`（提供 cid 列表）+ fetching 层 raw payload（提供 CDN URL）。详见 [docs/feature/parsing.md](parsing.md)、[docs/schema.md](../schema.md)。

## MiMo ASR 真实样本（2026-06-11，Token Plan key）

| 项 | 值 |
|----|----|
| Endpoint | `POST https://token-plan-cn.xiaomimimo.com/v1/chat/completions` |
| Auth | `api-key: tp-***`（Token Plan key 必须配 token-plan-* 域名；用 `https://api.xiaomimimo.com/v1` 返回 401） |
| Probe 输入 | bilibili `BV1o3YbzVEEo` page-0 audio (m4s 1.0 MiB) → ffmpeg 转 mp3 16kHz mono (391 KiB) → base64 |
| 响应 status | 200 |
| 响应文本 | 309 字符英文歌词（视频 BGM；UP 主无解说） |
| `usage.seconds` | 134 |
| `usage.prompt_tokens_details.audio_tokens` | 837 |
| `usage.prompt_tokens_details.cached_tokens` | 4 |
| `usage.completion_tokens` | 87 |

**关键事实**（实测确认）：
- 响应仅含完整文本（`choices[0].message.content`），**无 segments / 时间戳 / 检测语言**。
- `usage.seconds` 是 `int`，按音频秒数向上取整（实测 1s 正弦波样本返回 `seconds: 2`）。
- 流式响应（`stream=true`）通过 SSE 输出 chat.completion.chunk；最后一条 chunk 携带 `usage`，结尾 `data: [DONE]`。
- 无人声（引擎噪音 / 纯环境音）输入返回空文本 + 极少量输出 token，不报错。

完整响应 fixture：[bili_unit/tests/fixtures/mimo_asr_response.json](../../bili_unit/tests/fixtures/mimo_asr_response.json)。

## 模块结构

```
bili_unit/processing/
├── __init__.py            # 状态枚举 + 异常（含 AudioError 子类）+ ProcessingCommandResult + assemble()
├── __main__.py            # thin backward-compat wrapper（转发到统一 CLI）
├── _store.py              # ProcessingStore（写主 DB 的 audio_transcription + stage_task + stage_error）
├── command.py             # ProcessingCommand.process_uid
├── runner/                # Phase 0/1/2 编排 + audio worker pool
│   ├── __init__.py        # ProcessingRunner 类、编排、公共 helper
│   ├── _audio.py          # _AudioMixin：audio pipeline
│   ├── _audio_work.py     # download / convert / transcribe 三段拆分
│   └── _pipeline_executor.py  # 通用 pipeline executor + WorkItem 定义
└── audio/
    ├── __init__.py        # 公开所有 audio 组件
    ├── _asr_backend.py    # ASRBackend Protocol + ASRResult + MockASRBackend + create_asr_backend 工厂
    ├── _mimo_backend.py   # MimoASRBackend — MiMo 云端 ASR（aiohttp + chat completions）
    ├── _asr_cache.py      # 段级断点续传缓存
    ├── _stitch.py         # 段间文本去重拼接
    ├── _vad.py            # Silero VAD 切分
    ├── _downloader.py     # AudioDownloader — bilibili CDN 音频流下载
    ├── _converter.py      # convert_single / convert_m4s_to_mp3 / convert_and_segment
    ├── _ffmpeg.py         # resolve_ffmpeg(setting) — system / imageio-ffmpeg / 显式路径
    └── _init_wizard.py    # init-mimo CLI 的交互式向导
```

import 边界：

```text
command  → runner, _store, _db (UidContext), fetching._store, parsing._store, DTO
runner   → _store, audio, fetching._store, fetching.auth, parsing._store, _retry
_store   → _db (UidContext)
audio.*  → aiohttp / subprocess / bilibili_api（_mimo_backend / _downloader / _converter 三处）
```

processing 通过 `bili_unit.fetching._store.FetchingStore` 只读 raw DB 的 `raw_payload`（audio pipeline 从 `video_detail` fanout 行解析 CDN URL），通过 `bili_unit.parsing._store.ParsingStore` 只读主库的 `video` / `video_subtitle`（拿 cid 列表与字幕短路判定）。三个 store 共享同一 `UidContext`，由 `ProcessingCommand.process_uid` 统一开关。

## Audio 流水线

audio 流水线以 bvid 为单位，每个 bvid 产出一个 WorkItem（携带其 page 列表）：

| item_type | source_endpoints | item_id | 备注 |
|-----------|------------------|---------|------|
| transcription | video_detail | bvid | 每个 bvid 一个工作项，包含所有分 P |

单个 bvid 的 audio 处理流程：
1. 从 `FetchingStore.get_raw_payload("video_detail", bvid)` 取 raw video_detail，提取 cid 列表
2. 对每个 page：`Video(bvid).get_download_url_data()` → `VideoDownloadURLDataDetecter.detect()` → 筛选 `AudioStreamDownloadURL`（64K）
3. CDN 下载 m4s → temp 目录
4. ffmpeg 转码 m4s → mp3（16kHz mono）；分段策略见下
5. 逐段调用 `MimoASRBackend.transcribe()` → 获取转录文本
6. 合并所有 page 结果，`ProcessingStore.save_audio_transcription(bvid, status='success', ...)` 写入主库 `audio_transcription`
7. 清理 temp 文件

**分段决策**（`convert_single`，[_converter.py](../../bili_unit/processing/audio/_converter.py)）：
1. 始终先做整段转码生成 `full.mp3`。
2. 若 caller 提供了 `duration_seconds + max_input_tokens + tokens_per_second`（runner 默认走这条路）：
   - `compute_segment_seconds()` 估算 `tokens = ceil(duration * tokens_per_second)`；若 ≤ 预算则直接返回单段。
   - 否则按 `max_input_tokens // tokens_per_second` 计算 `-segment_time`，最低不低于 60 秒。
3. 否则走 size fallback：mp3 > `BILI_PROCESSING_ASR_MAX_FILE_SIZE_MB` 时按 `BILI_PROCESSING_AUDIO_MAX_SEGMENT_MINUTES * 60` 切段。

**为什么需要 token 预算分段**：MiMo `mimo-v2.5-asr` 上下文 8192 token，音频按 ≈ 6.5 token/秒 编码。一段 17 分钟 16 kHz mono q:a 9 mp3 仅 ~3 MB（永远命中不到 size 阈值），但折算 ~6500 input tokens + 默认 2048 completion = 8550，**触发 400 BadRequest**。size-only 策略实测对长视频 100% 失败，token-budget 路径才是根治方式。

bilibili-api 17.x workaround：`detect_best_streams` 在某些视频上抛 NoneType 错误；
改用 `detect(audio_max_quality=AudioQuality._64K)` + `type(stream).__name__ == "AudioStreamDownloadURL"` 筛选。

ASR 后端通过 `BILI_PROCESSING_ASR_BACKEND` 切换：
- `mock`（默认）：`MockASRBackend`，返回固定文本，用于测试
- `mimo`：`MimoASRBackend`，通过 aiohttp 调用 MiMo API

Credential 由 `fetching.auth.get_credential()` 提供，runner 在 audio pipeline 启动时获取并注入 `AudioDownloader`。

**多段合并 / duration 字段语义**（[runner.py](../../bili_unit/processing/runner.py) `_do_audio_work`）：
- `pages[i].text`：所有 ASR 段以 `" "` 拼接后的完整文本。
- `pages[i].duration`：page 实际时长，优先来源：
  1. 该 page 在 `video_detail.info.pages[].duration`（runner 读到的整数秒）
  2. 多段 ASR 返回 `usage.seconds` 的累加值
  3. CDN audio 元数据 `duration`（注：单位与秒不一致，仅作最后兜底）
- `result.total_duration`：所有 page `duration` 之和。

历史教训：早期实现把 ASR 单段 duration **覆盖**写入 `page_duration`，导致多段视频只保留最后一段时长（1033 秒视频被切两段后落盘 `duration=204`）。已通过累加 + page metadata 优先策略修正，并加 2 个回归测试覆盖。

## 工作项

audio pipeline 是当前唯一的 pipeline，工作项形态见上文「Audio 流水线」表（每个 bvid 一个 `transcription` 工作项）。

## 处理模式

`process_uid(uid, mode)` 支持两档：

| mode | 行为 |
|------|------|
| incremental（默认） | 已 SUCCESS 的 bvid 跳过；已 FAILED 的 bvid 重试一次；新增的抓取结果入队处理 |
| full | 忽略已有 audio 结果，对所有 bvid 重新转录并覆盖写入 |

处理模式不向 fetching / parsing 传播；processing 不会因 mode=full 而触发上游 refresh / full。

## 数据消费规则

audio pipeline 的工作项发现依赖 parsing 层产出的 `VideoDetail`（拿 cid 列表）和 fetching 层 raw payload（拿 CDN URL）。如需新增工作项，应先重跑 fetching → parsing。processing 不写回 fetching 或 parsing 状态。

## 两阶段编排

```
Phase 0  扫描     load_or_init_task → 从 parsing store / fetching 发现 bvid 工作项
Phase 1  分发执行 audio worker pool 并发处理
Phase 2  收尾     reload_task → derive_task_status → save_task → cleanup temp
```

worker 配置：
- `BILI_PROCESSING_AUDIO_WORKERS` 默认 2
- `BILI_PROCESSING_QUEUE_MAXSIZE` 默认 16

## 存储层

processing 写主 DB（`{bili_db_dir}/{uid}.db`）的三张表（DDL：[main_v1.sql](../../bili_unit/_db/ddl/main_v1.sql)，表语义见 [docs/schema.md](../schema.md)）：

- `audio_transcription` —— per-bvid 转写结果（FK CASCADE → `video.bvid`）
- `stage_task[stage='processing']` —— 任务包络（pipeline rollup 在 `payload` JSON）
- `stage_error[stage='processing']` —— 错误 sink（`pipeline` / `item_type` / `item_id` 列定位）

raw DB 不写（processing 是 read-only consumer，从 raw_payload 取 video_detail fanout 行）。

二进制大对象不进 SQLite，而是落在文件系统：

- `{bili_processing_temp_dir}/<workdir>/` —— ffmpeg 中间产物（m4s / mp3）；成功收尾后清理
- `{bili_processing_asr_cache_dir}/<key>/` —— 段级 ASR cache（重跑命中时跳过 API），bvid 全部成功时清理

`audio_transcription.bvid` FK CASCADE 到 `video.bvid`：parsing 删除某个 video 行时其 ASR 结果同步消失；正常运行顺序是 fetching → parsing → processing，processing 写入时 video 行已存在。

### `audio_transcription` 行示例

```sql
SELECT bvid, status, transcription_source, audio_tokens, seconds, cache_hits
FROM audio_transcription WHERE bvid = 'BV1xxxxxxxxxx';
```

| 列 | 示例值 | 说明 |
|---|---|---|
| `bvid` | `'BV1xxxxxxxxxx'` | PK；FK CASCADE → video.bvid |
| `status` | `'success'` | `pending` / `running` / `success` / `failed` / `skipped` |
| `transcription_source` | `'asr'` | `asr` / `subtitle` / `mock` / `mimo` / `whisper` |
| `transcript` | `'完整转录文本...'` | 全文（success/skipped 时非空；失败时通常 NULL） |
| `audio_tokens` | `1875` | MiMo `usage.prompt_tokens_details.audio_tokens` 累加 |
| `seconds` | `300.0` | ASR 计费秒数（`usage.seconds` 累加；REAL） |
| `cache_hits` | `0` | 本次跑通过段级 cache 命中的段数 |
| `payload` | `'{"pages":[{...}],"total_duration":300,"cost":{...}}'` | 完整 ProcessingItem dict（per-page text / segments / cost / source_endpoints） |
| `processed_at_ms` | `1718000002000` | 处理完成时间 |

`payload` JSON 形如：

```json
{
  "uid": 123,
  "pipeline": "audio",
  "item_type": "transcription",
  "item_id": "BV1xxxxxxxxxx",
  "status": "SUCCESS",
  "result": {
    "bvid": "BV1xxxxxxxxxx",
    "pages": [
      {
        "page_index": 0,
        "cid": 12345,
        "duration": 300.0,
        "text": "完整转录文本...",
        "language": "auto",
        "asr_model": "mimo-v2.5-asr",
        "segments": []
      }
    ],
    "total_duration": 300.0,
    "total_chars": 5000,
    "transcription_source": "asr",
    "cost": {
      "audio_tokens": 1875,
      "seconds": 300,
      "model": "mimo-v2.5-asr",
      "cache_hits": 0,
      "fresh_segments": 2
    }
  },
  "source_endpoints": ["video_detail"],
  "processed_at": 1718000002000
}
```

`cost` 字段记录本 bvid 实际花费（按 page 累加）：

| 字段 | 含义 |
|----|----|
| `audio_tokens` | MiMo `usage.prompt_tokens_details.audio_tokens` 之和；缓存命中段也计入（首次 ASR 时已写入 cache，重跑读 cache 后照常累加，所以 bvid-level cost 在 retry 之间稳定） |
| `seconds` | ASR 实际计费的秒数（`usage.seconds` 之和） |
| `model` | 当前后端 `model`（mimo-v2.5-asr / mock-asr-v0 / 字幕短路记 `subtitle`） |
| `cache_hits` | 本次跑通过 cache 命中的段数 |
| `fresh_segments` | 本次跑实际打到 ASR 后端的段数（即新发生的费用） |

字幕短路写出的 result 里 `cost = {audio_tokens: 0, seconds: 0, model: "subtitle", cache_hits: 0, fresh_segments: 0}`，标记本次"零成本完成"。

`transcription_source` 标记本次结果的产生路径：

| 值 | 含义 | 来源 |
|----|------|------|
| `"asr"` | 走完整音频流水线（CDN 下载 → ffmpeg → ASR） | `video_detail`（CDN URL） |
| `"subtitle"` | 字幕短路：从 parsing 的 `video_subtitle` 直接拼出文本，跳过 ASR | `video_subtitle`（parsed） |

字幕短路触发条件：当 audio pipeline 在 discovery 阶段对一个 bvid 调 `ParsingStore.video_subtitle_is_complete(bvid)` 返回 True（每个 page 都至少有一种 lang 命中 body），runner 直接构造 audio result 写入 `audio_transcription` 并把该 item 从 worker 队列移除（`source_endpoints: ["video_subtitle"]`）。其他情况（无字幕 / 部分 page 缺字幕）走 ASR 路径。

> dry-run 跳过字幕短路 — 不写盘也不影响 candidate 列表。

### `stage_task[stage='processing']` payload

```json
{
  "pipelines": {
    "audio": {
      "status": "SUCCESS",
      "items": {
        "transcription": { "total": 77, "completed": 77, "failed": 0, "skipped": 0 }
      }
    }
  }
}
```

任务级 `status` 列由 pipeline 状态聚合（见下文「状态枚举」）。终结状态时失败 bvid 由 `ProcessingStore.list_failed_audio_bvids()` 现算（`SELECT bvid FROM audio_transcription WHERE status='failed'`）；不再持久化为 task 字段。

### `stage_error[stage='processing']` 行

`record_error(*, pipeline, item_type, item_id, error_type, message, retryable, detail=None)` 通过 `INSERT ... RETURNING id` 写入。`pipeline='audio'` / `item_type='transcription'` / `item_id=bvid`，`retryable` 三态：1 / 0 / NULL。`list_errors(pipeline='audio', item_id=bvid)` 反查某 bvid 的历史失败。

## 状态枚举

`ProcessingTaskStatus`：PENDING / RUNNING / SUCCESS / PARTIAL / FAILED_RETRYABLE / FAILED_EXHAUSTED / FAILED_PERMANENT

`ProcessingPipelineStatus`：PENDING / RUNNING / SUCCESS / PARTIAL / FAILED_RETRYABLE / FAILED_PERMANENT

`ProcessingItemStatus`：PENDING / PROCESSING / SUCCESS / FAILED / SKIPPED

任务级状态由 pipeline 状态聚合（runner._derive_task_status）：
- 全 SUCCESS → SUCCESS
- 任一 RUNNING → RUNNING
- 至少一个 FAILED_PERMANENT 且无 SUCCESS → FAILED_PERMANENT
- 其它含失败 / pending → PARTIAL

## 错误处理

异常层级（`bili_unit/processing/__init__.py`）：
```
ProcessingError
├── AudioError
│   ├── ASRConfigError
│   ├── DownloadError
│   ├── ConvertError
│   ├── ASRConnectionError
│   ├── ASRAPIError
│   └── AudioSizeError
└── QueueError
```

- 单个工作项失败不影响其他工作项；失败的工作项写 `stage_error[stage='processing']` 并把 `audio_transcription.status` 标为 `'failed'`。
- audio worker 包含 safety net：即使 `_process_audio_one` 内部异常未被捕获，worker 也能优雅降级（标记 FAILED）。
- 自动重试调度（per-work-item，单次 `process_uid` 内）：通过共享 `RetryDriver`（`bili_unit/_retry.py`）编排，可配置 `max_retries`（默认 3）+ 延迟间隔（默认 30/60/120 秒，通过 `BILI_PROCESSING_RETRY_DELAYS` 配置）。每次重试记录 error（`retryable=True`）；重试耗尽后写最终 error（`retryable=False`）。`AudioError` 子类（DownloadError / ASRConnectionError / ConvertError / ASRAPIError / AudioSizeError）被视为可重试；其他异常（RuntimeError 等）不重试。`ASRConfigError` 归类为 PERMANENT，立即终止不重试。
- Command 不暴露 `retry_failed()` 接口；FAILED 的工作项在 `mode=incremental` 重新调用时会被重新处理；CLI `--retry-failed-only` 限定只跑 `audio_transcription.status='failed'` 的 bvid。

## CLI

只有写侧子命令；读侧用 `sqlite3` 直连 `db_path(uid)`：

```bash
uv run python -m bili_unit process <uid>                       # incremental（已 SUCCESS 跳过、FAILED 重跑）
uv run python -m bili_unit process <uid> -m full               # 全量重处理（覆盖所有 bvid 转录）
uv run python -m bili_unit process <uid> -b mock               # 临时把 ASR 后端覆盖为 mock
uv run python -m bili_unit process <uid> --limit 5             # 仅处理发现的前 5 个 bvid
uv run python -m bili_unit process <uid> --only-bvids BV1...   # 仅处理给定 bvid（可与 --limit 叠加）
uv run python -m bili_unit process <uid> --retry-failed-only   # 仅重跑 status='failed' 的 bvid（隐含 incremental）
uv run python -m bili_unit process <uid> --dry-run             # 列出 candidate，不真跑 worker
uv run python -m bili_unit init-mimo                           # 交互式 MiMo ASR 配置（写 .env）

# 只读（无 CLI 子命令；直接 SQL）：
sqlite3 data/bili/{uid}.db "SELECT bvid, status, transcription_source, audio_tokens, seconds FROM audio_transcription;"
sqlite3 data/bili/{uid}.db "SELECT * FROM video_full WHERE bvid='BV1...';"   -- video + transcription LEFT JOIN view
sqlite3 data/bili/{uid}.db "SELECT * FROM manifest_summary;"                  -- 跨 stage 摘要
```

> processing 当前只有 audio pipeline，无需 handler 选择标志。视频元数据 / 内容帖 / UP 主画像直接 `SELECT * FROM video / article / opus_post / dynamic_event / user_profile`。

向后兼容：`python -m bili_unit.processing` 仍可用（内部转发到统一 CLI）。

## 装配函数

`bili_unit.processing.assemble(settings, *, asr_backend_override=None, credential_provider=None)` 是 stage 装配入口，**返回单值** `ProcessingCommand`。顶层 `bili_unit.session()` 会把 fetching / parsing / processing 的 command 都装进 `BiliCommand`：

```python
from bili_unit import session

async with session() as cmd:
    await cmd.fetch(uid)
    await cmd.parse(uid)
    await cmd.process(uid)

# 读侧：直接 SQL
import sqlite3, bili_unit
conn = sqlite3.connect(bili_unit.db_path(uid))
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT bvid, transcript FROM audio_transcription WHERE status='success'"):
    ...
```

`assemble()` 根据 `BILI_PROCESSING_ASR_BACKEND`（或 `asr_backend_override`）创建对应的 ASR backend（mock / mimo），把 `credential_provider`（默认 `fetching.auth.get_credential`）注入 `ProcessingCommand`。`ProcessingCommand` 不持有 store；每次 `process_uid` 自开自关 `UidContext`，再绑定 `ProcessingStore` + `FetchingStore` + `ParsingStore` 跑一遍 audio pipeline。

### Command 接口

```python
async def process_uid(
    uid: int,
    mode: str = "incremental",            # "incremental" | "full"
    *,
    limit: int | None = None,
    only_bvids: list[str] | None = None,
    retry_failed_only: bool = False,
    dry_run: bool = False,
) -> ProcessingCommandResult
async def delete_uid(uid: int) -> dict[str, int]    # no-op；BiliCommand 删 db 文件
async def close() -> None                            # 关 ASR 后端 HTTP 会话
```

`ProcessingCommandResult` 字段：`uid: int`、`status: ProcessingTaskStatus`、`dry_run_candidates: list[str] | None`（仅 dry_run 时填充）。

### ProcessingStore 关键方法

`bili_unit/processing/_store.py` 的写侧表面：

```python
# audio writes
async def save_audio_transcription(bvid, *, status, transcription_source, transcript,
                                    audio_tokens, seconds, cache_hits, payload,
                                    processed_at_ms=None) -> None

# audio reads
async def get_audio_status(bvid) -> str | None
async def get_audio_payload(bvid) -> dict | None
async def list_audio_bvids(status=None) -> list[str]
async def list_failed_audio_bvids() -> list[str]

# task state
async def init_task(pipelines) -> None
async def update_task_pipeline(pipeline, status, items=None) -> None
async def update_task_status(status) -> None
async def get_task() -> dict | None

# error sink
async def record_error(*, pipeline, item_type, item_id, error_type, message,
                       retryable, detail=None, occurred_at_ms=None) -> int
async def list_errors(*, pipeline=None, item_type=None, item_id=None) -> list[dict]
```

`save_audio_transcription` 是 `INSERT OR REPLACE`；`update_task_pipeline` 是 read-modify-write，由 store-local `asyncio.Lock` 串行（避免两个 pipeline 收尾相互覆写 task payload）。

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_DB_DIR | data/bili | SQLite DB 根目录（main + raw + workdir 全部派生） |
| BILI_PROCESSING_TEMP_DIR | data/bili/processing/temp | 音频中间产物目录（ffmpeg m4s/mp3，成功后清理） |
| BILI_PROCESSING_ASR_CACHE_DIR | data/bili/processing/asr_cache | 段级 ASR cache 目录（断点续传；bvid 全成功时清理） |
| BILI_PROCESSING_ASR_CACHE_ENABLED | true | 是否启用段级 cache（关掉每次都重新计费） |
| BILI_PROCESSING_AUDIO_WORKERS | 2 | audio worker 数 |
| BILI_PROCESSING_QUEUE_MAXSIZE | 16 | 工作项队列上限 |
| BILI_PROCESSING_AUDIO_QUALITY | 64K | 音频清晰度 |
| BILI_PROCESSING_AUDIO_MAX_SEGMENT_MINUTES | 8 | 音频分段时长（仅 size fallback 时生效） |
| BILI_PROCESSING_ASR_BACKEND | mimo | ASR 后端选择（mimo / mock / whisper）；CLI `process -b mock` 临时覆盖 |
| BILI_PROCESSING_ASR_PROFILE | token_plan_cn | MiMo 后端模式：`token_plan_cn` / `token_plan_sgp` / `token_plan_ams` / `pay_as_you_go` / `custom`。BASE_URL 由 profile 解析，用户不需要记 host |
| BILI_PROCESSING_ASR_AUTH_STYLE | api_key | 鉴权头风格：`api_key`（默认；header `api-key: $KEY`）/ `bearer`（中转站常用；header `Authorization: Bearer $KEY`） |
| BILI_PROCESSING_ASR_API_KEY | "" | MiMo API Key（tp-* / sk-* / 中转站 key）。空值时调用 `transcribe()` 抛 `ASRConfigError` 指向 `init-mimo` |
| BILI_PROCESSING_ASR_BASE_URL | "" | 仅 `ASR_PROFILE=custom` 时生效；中转站 / 自建端点的 base URL（含 `/v1`）|
| BILI_PROCESSING_ASR_MODEL | mimo-v2.5-asr | MiMo 模型名 |
| BILI_PROCESSING_ASR_LANGUAGE | auto | ASR 语言（auto / zh / en） |
| BILI_PROCESSING_ASR_TIMEOUT | 300 | ASR 超时（秒） |
| BILI_PROCESSING_ASR_MAX_FILE_SIZE_MB | 10 | 单次 ASR 文件大小上限（MB），仅在 duration 未知时作 fallback |
| BILI_PROCESSING_ASR_MAX_INPUT_TOKENS | 5400 | 单次 ASR 输入 token 上限。MiMo 上下文 8192 token，扣除 completion 与系统开销后此值即每段音频可用预算 |
| BILI_PROCESSING_ASR_TOKENS_PER_SECOND | 6.5 | 经验值：16 kHz 单声道 mp3 在 MiMo 上 ≈ 6.5 token/秒（来自 fixture：134 s → 837 audio_tokens） |
| BILI_PROCESSING_ASR_MAX_COMPLETION_TOKENS | 1024 | 写入 payload 的 max_tokens；OpenAI 风格默认 2048，下调以释放输入预算 |
| BILI_PROCESSING_ASR_USE_VAD | true | 启用 Silero VAD 切分；关掉走 fixed-period 切分 |
| BILI_PROCESSING_ASR_VAD_THRESHOLD | 0.3 | VAD 灵敏度（默认低于上游 0.5，更适合 B站含 BGM 内容） |
| BILI_PROCESSING_MAX_RETRIES | 3 | 单工作项最大重试次数 |
| BILI_PROCESSING_RETRY_DELAYS | 30,60,120 | 重试间隔（秒），逗号分隔；超出列表长度时复用最后一个值 |
| BILI_PROCESSING_FFMPEG_PATH | auto | `auto`（系统优先 + imageio-ffmpeg fallback） / `system` / `imageio` / 显式路径 |

> 旧的处理结果 / 错误目录 env 已删除；处理结果直接进 `{BILI_DB_DIR}/{uid}.db` 的 `audio_transcription` / `stage_error` 表。temp 与 asr_cache 仍是文件系统目录（音频/缓存属于二进制大对象，不放 SQLite）。

## 测试状态

测试位于 `bili_unit/tests/`，覆盖 ASR 后端工厂 / MockASRBackend / MimoASRBackend、token-budget 分段、profile + auth_style + ASRConfigError、init-mimo 向导、audio 段级缓存 + 断点续传、VAD 切分 + 段间文本去重、`ProcessingStore` SQLite 契约（`test_processing_store_sqlite.py`）、runner / command 集成（含 retry 路径与多段 duration 累加）。MiMo 真实响应 fixture 在 `bili_unit/tests/fixtures/mimo_asr_response.json`。无外部网络，离线可跑：`uv run pytest`。

## 已知限制 / 开放工作项

- `MockASRBackend` 返回固定文本，用于测试 + 接口稳定保证。`MimoASRBackend` 已实装，
  设 `BILI_PROCESSING_ASR_BACKEND=mimo` + 配好 `BILI_PROCESSING_ASR_API_KEY` 即可使用。
- `bilibili-api-python` 17.x 在某些视频上 `VideoDownloadURLDataDetecter.detect_best_streams` 会抛
  `'NoneType' object has no attribute 'value'`；audio 下载器已采用
  `detect(audio_max_quality=...)` + `type(stream).__name__ == "AudioStreamDownloadURL"` workaround。
- `whisper` ASR 后端尚未实装（`create_asr_backend("whisper")` 仍抛 `NotImplementedError`）。
- `subtitle` / OCR pipeline 尚未实装；ingestion 待实装时如需视频元数据 / 内容帖 / UP 主画像，消费方直接 `SELECT` 主库内容表，processing 不再做"字段透传"层。
