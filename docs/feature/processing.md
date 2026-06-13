# processing_feature — B站用户数据处理层代码现状

> 记录 `bili_unit/processing` 的实际代码能力。
> 对应设计文档：`docs/design/processing.md`
> 对应结构约束：`docs/structure/bili.md`

## 概述

processing 层负责对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。当前仅一条 pipeline：audio。

> **历史说明（2026-06-14）**：processing 仅持有 audio pipeline；transform 子系统（原 `video_metadata` / `content_post` / `user_profile` 三个 handler）已删除（理由：parsing 重构后 transform 退化为字段透传，且 ingestion 未实装无契约要保护）。视频元数据 / 内容帖 / UP 主画像直接消费 `parsing.query` 出口面（`get_video_detail` / `list_articles` / `list_opus` / `list_dynamics` / `list_items(uid, "content_post")` / `get_user_profile`），不再经过 processing。

audio 流水线通过 asyncio.Queue + worker pool 并发调度，支持 incremental / full 两种处理模式。数据源是 parsing 层产出的 `VideoDetail`（提供 cid 列表）+ fetching 层（提供 CDN URL）。详见 [docs/feature/parsing.md](parsing.md)。

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
├── __init__.py            # DTO + 异常（含 AudioError 子类）
├── __main__.py            # thin backward-compat wrapper（转发到统一 CLI）
├── command.py             # ProcessingCommand.process_uid
├── query.py               # ProcessingQuery（task / item / list / video_full / errors）
├── data.py                # ProcessingDataStore（文件目录 JSON）
├── error.py               # ProcessingErrorStore（per-uid JSON 文件）
├── env.py                 # ProcessingEnv (pydantic-settings)
├── keys.py                # 存储 key 生成
├── runner/                # Phase 0/1/2 编排 + audio worker pool
│   ├── __init__.py        # ProcessingRunner 类、编排、公共 helper
│   ├── _audio.py          # _AudioMixin：audio pipeline
│   ├── _audio_work.py     # download / convert / transcribe 三段拆分
│   └── _pipeline_executor.py  # 通用 pipeline executor + WorkItem 定义
├── task.py                # ProcessingTaskValue / PipelineEntry
└── audio/
    ├── __init__.py        # 公开所有 audio 组件
    ├── _asr_backend.py    # ASRBackend Protocol + ASRResult + MockASRBackend + create_asr_backend 工厂
    ├── _mimo_backend.py   # MimoASRBackend — MiMo 云端 ASR（aiohttp + chat completions）
    ├── _downloader.py     # AudioDownloader — bilibili CDN 音频流下载
    ├── _converter.py      # convert_single / convert_m4s_to_mp3 / convert_and_segment
    └── _ffmpeg.py         # resolve_ffmpeg(setting) — system / imageio-ffmpeg / 显式路径
```

import 边界：
```text
command → runner, DTO
query → data, error, parsing.query（视频元数据来源）
runner → task, audio, data, error, env, _retry, fetching.query（audio CDN URL）, fetching.auth
audio._mimo_backend → aiohttp（HTTP 调用 MiMo API）
audio._downloader → aiohttp（CDN 下载）, bilibili_api（URL 解析）
audio._converter → subprocess（ffmpeg 调用）
data/error → _storage (JsonKVStore + KeyMapper)
env → 不 import data/error/task
```

processing 通过 `bili_unit.fetching.query.Query` 只读访问 fetching 数据（audio pipeline 从 fetching 获取 CDN URL），通过 `bili_unit.parsing.query.ParsingQuery` 只读访问 parsing 数据（audio 从 `VideoDetail` 拿 cid 列表；query 联合视图从 parsing 拿元数据）。不直接访问 fetching / parsing 的 DataStore/ErrorStore，也不写回。

## Audio 流水线

audio 流水线以 bvid 为单位，每个 bvid 产出一个 WorkItem（携带其 page 列表）：

| item_type | source_endpoints | item_id | 备注 |
|-----------|------------------|---------|------|
| transcription | video_detail | bvid | 每个 bvid 一个工作项，包含所有分 P |

单个 bvid 的 audio 处理流程：
1. 从 fetching.query 获取 video_detail（cid 列表）
2. 对每个 page：`Video(bvid).get_download_url_data()` → `VideoDownloadURLDataDetecter.detect()` → 筛选 `AudioStreamDownloadURL`（64K）
3. CDN 下载 m4s → temp 目录
4. ffmpeg 转码 m4s → mp3（16kHz mono）；分段策略见下
5. 逐段调用 `MimoASRBackend.transcribe()` → 获取转录文本
6. 合并所有 page 结果，写入 `uid:{uid}:proc:audio:{bvid}`
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

audio pipeline 的工作项发现依赖 parsing 层产出的 `VideoDetail`（拿 cid 列表）和 fetching 层（拿 CDN URL）。如需新增工作项，应先重跑 fetching → parsing。processing 不写回 fetching 或 parsing 状态。

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

processing 维护独立于 fetching 的目录存储：

```
{BILI_PROCESSING_DATA_DIR}/{uid}/task.json                      处理任务状态
{BILI_PROCESSING_DATA_DIR}/{uid}/proc/{item_type}/{item_id}.json 单工作项处理结果
{BILI_PROCESSING_DATA_DIR}/{uid}/progress/{pipeline}.json       流水线进度（audio）

{BILI_PROCESSING_ERROR_DIR}/{uid}.json                          per-uid 错误记录
{BILI_PROCESSING_ERROR_DIR}/_null.json                          uid=None 错误
{BILI_PROCESSING_ERROR_DIR}/_counter.json                       自增 ID
```

processing 与 fetching 的 task key 同名（`uid:{uid}:task`），但因为 store 物理隔离（不同目录路径），不冲突。

> 历史目录 `proc/video_metadata/`、`proc/content_post/`、`proc/user_profile/` 已不再写入；旧数据保留在磁盘上，新代码读不到，下次手工清理。

### value 形状

**processing task**（`uid:{uid}:task`）：
```json
{
  "uid": 123,
  "status": "SUCCESS",
  "pipelines": {
    "audio": {
      "status": "SUCCESS",
      "items": {
        "transcription": { "total": 77, "completed": 77, "failed": 0, "skipped": 0 }
      }
    }
  },
  "created_at": 1718000000000,
  "updated_at": 1718000001000
}
```

**audio 处理结果**（`uid:{uid}:proc:audio:{bvid}`）：
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
    "total_chars": 5000
  },
  "source_endpoints": ["video_detail"],
  "processed_at": 1718000002000
}
```

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

异常层级（`bili_unit/processing/__init__.py`，与历史版本兼容；`TransformError` 等保留为基类，但当前没有任何 pipeline 抛出）：
```
ProcessingError
├── TransformError      # legacy；transform 子系统已删除（2026-06-14），保留以避免下游 import 断裂
│   ├── FieldExtractionError
│   └── FormatError
├── AudioError
│   ├── ASRConfigError
│   ├── DownloadError
│   ├── ConvertError
│   ├── ASRConnectionError
│   ├── ASRAPIError
│   └── AudioSizeError
├── QueueError
└── DataError
```

- 单个工作项失败不影响其他工作项；失败的工作项写 error store + processing data store（status=FAILED）。
- audio worker 包含 safety net：即使 `_process_audio_one` 内部异常未被捕获，worker 也能优雅降级（标记 FAILED）。
- 自动重试调度（per-work-item，单次 `process_uid` 内）：通过共享 `RetryDriver`（`bili_unit/_retry.py`）编排，可配置 `max_retries`（默认 3）+ 延迟间隔（默认 30/60/120 秒，通过 `BILI_PROCESSING_RETRY_DELAYS` 配置）。每次重试记录 error（`retryable="true"`）并更新 data store（`retry_count` 递增）。重试耗尽后写最终 error（`retryable="false"`）。`AudioError` 子类（DownloadError / ASRConnectionError / ConvertError / ASRAPIError / AudioSizeError）被视为可重试；其他异常（RuntimeError 等）不重试。`ASRConfigError` 归类为 PERMANENT，立即终止不重试。
- Command 不暴露 `retry_failed()` 接口；FAILED 的工作项在 `mode=incremental` 重新调用时也会被重新处理（作为额外重试入口）。

## CLI

统一 CLI（推荐）：

```bash
uv run python -m bili_unit process <uid>                       # incremental 处理 audio pipeline
uv run python -m bili_unit process <uid> -m full               # 全量重处理（覆盖所有 bvid 转录）
uv run python -m bili_unit process <uid> -b mock               # 临时把 ASR 后端覆盖为 mock
uv run python -m bili_unit query <uid>                         # 显示抓取 + 处理状态
uv run python -m bili_unit video-full <uid> <bvid>             # 联合 metadata（来自 parsing）+ transcription（来自 audio）
```

> processing 当前只有 audio pipeline，无需 handler 选择标志（旧版 `-t/-x/--item-types/--exclude-item-types` 已随 transform 一起删除）。要看视频元数据 / 内容帖 / UP 主画像，请用 `parse` 子命令产物，通过 `BiliQuery.parsing` 出口面消费。

向后兼容：`python -m bili_unit.processing` 仍可用（内部转发到统一 CLI），老脚本无需改动。

## 装配函数

processing 不再有独立的 `assemble()` 函数。统一通过顶层 `bili_unit.assemble()` 初始化：

```python
from bili_unit import assemble
cmd, qry, _data, _error = await assemble()
await cmd.fetch(uid)
await cmd.parse(uid)
await cmd.process(uid)
task = await qry.processing.get_task(uid)
```

`bili_unit.assemble()` 内部串调 fetching 的 `assemble()` 获取 `FetchingQuery` 注入 parsing + processing；`ParsingQuery` 注入 processing.query（视频联合视图的元数据来源）；根据
`BILI_PROCESSING_ASR_BACKEND` 创建对应的 ASR backend（mock / mimo）；返回
`BiliCommand` / `BiliQuery`（包装了 fetching + parsing + processing 的 command / query）。

### Command 接口

```python
async def process_uid(
    uid: int,
    mode: str = "incremental",            # "incremental" | "full"
) -> ProcessingCommandResult
```

### Query 接口

```python
async def get_task(uid: int) -> ProcessingTaskDTO | None
async def list_tasks() -> list[dict]
async def get_item(uid: int, item_type: str, item_id: str) -> ProcessingItemDTO | None
async def list_items(uid: int, item_type: str) -> list[ProcessingItemDTO]
async def get_video_full(uid: int, bvid: str) -> VideoFullDTO | None        # metadata 来自 parsing，transcription 来自 audio
async def list_all_videos(uid: int) -> list[VideoSummaryDTO]                # 元数据来自 parsing，transcription 状态来自 audio
async def list_errors(uid: int | None = None) -> list[ErrorDTO]
```

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_PROCESSING_DATA_DIR | data/bili/processing/data | 处理结果存储目录 |
| BILI_PROCESSING_TEMP_DIR | data/bili/processing/temp | 音频中间产物目录 |
| BILI_PROCESSING_ERROR_DIR | data/bili/processing/error | 处理错误存储目录 |
| BILI_PROCESSING_AUDIO_WORKERS | 2 | audio worker 数 |
| BILI_PROCESSING_QUEUE_MAXSIZE | 16 | 工作项队列上限 |
| BILI_PROCESSING_AUDIO_QUALITY | 64K | 音频清晰度 |
| BILI_PROCESSING_AUDIO_MAX_SEGMENT_MINUTES | 8 | 音频分段时长 |
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
| BILI_PROCESSING_MAX_RETRIES | 3 | 单工作项最大重试次数 |
| BILI_PROCESSING_RETRY_DELAYS | 30,60,120 | 重试间隔（秒），逗号分隔；超出列表长度时复用最后一个值 |
| BILI_PROCESSING_FFMPEG_PATH | auto | `auto`（系统优先 + imageio-ffmpeg fallback） / `system` / `imageio` / 显式路径 |

## 测试状态

- ≈408 pytest 总数全部通过（覆盖 fetching / parsing / processing audio / storage / CLI / retry；transform 删除后下降约 26 个用例）
- ruff lint 全部通过
- 无外部网络 / API 依赖；测试可在离线环境运行

### 测试矩阵

```
test_processing_data_error.py    data store + error store 单元测试
test_processing_audio.py         ASRBackend / MockASRBackend / MimoASRBackend / create_asr_backend / resolve_ffmpeg / token-budget 分段 / profile + auth_style + ASRConfigError + init-mimo 向导
test_processing_audio_cache.py   audio 段级缓存 + 断点续传
test_processing_audio_vad.py     VAD 切分 + 段间文本去重
test_processing_runner.py        runner / command / query 集成测试（audio pipeline + auto-retry + 多段 duration 回归 + video_full 联合视图）
fixtures/mimo_asr_response.json  MiMo 真实响应样本（uid:13991807 BV1o3YbzVEEo, 134s）
```

集成测试覆盖：
- audio pipeline 发现 + 处理（2 bvids，mock 转录）
- audio incremental 跳过已 SUCCESS（processed_at 不变）
- audio full 模式覆盖写（processed_at 推进）
- audio 下载失败 → item FAILED + 错误入库
- audio 无 video_detail → 优雅跳过
- VideoFullDTO 联合视图：metadata 来自 parsing，transcription 来自 audio
- `_is_retryable` 分类（AudioError → true，其他 → false）
- audio retryable 错误重试至耗尽 → FAILED + 3 条 error 记录（2 retry + 1 final）
- audio retryable 错误首次失败后第二次成功 → SUCCESS
- audio 非 retryable 错误（RuntimeError）→ 不重试，立即 FAILED
- max_retries=0 → 单次尝试，不重试

## 已知限制 / 开放工作项

- `MockASRBackend` 返回固定文本；用于测试 + 接口稳定保证。`MimoASRBackend` 已实装，
  设 `BILI_PROCESSING_ASR_BACKEND=mimo` + 配好 `BILI_PROCESSING_ASR_API_KEY` 即可使用。
- `bilibili-api-python` 17.x 在某些视频上 `VideoDownloadURLDataDetecter.detect_best_streams` 会抛
  `'NoneType' object has no attribute 'value'`；audio 下载器已采用
  `detect(audio_max_quality=...)` + `type(stream).__name__ == "AudioStreamDownloadURL"` workaround。
- `whisper` ASR 后端尚未实装（`create_asr_backend("whisper")` 仍抛 `NotImplementedError`）。
- `subtitle` / OCR pipeline 尚未实装；ingestion 待实装时如需视频元数据 / 内容帖 / UP 主画像，直读 `parsing.query` 出口面，processing 不再做"字段透传"层。
