# processing_feature — B站用户数据处理层代码现状

> 记录 `bili_unit/processing` 的实际代码能力。
> 对应设计文档：`docs/design/processing.md`
> 对应结构约束：`docs/structure/bili.md`

## 概述

processing 层负责把 fetching 抓取的 raw_payload 转换为结构化处理结果。当前实现覆盖两条完整流水线：

- **transform**：五个 handler（`video_metadata` / `dynamics` / `articles` / `opus` / `user_profile`），纯计算字段提取与结构化。
- **audio**：CDN 音频下载（bilibili-api）→ ffmpeg 转码（m4s → mp3，长视频自动分段）→ MiMo ASR 转录。

两条流水线通过 asyncio.Queue + worker pool 并发调度，支持 incremental / full 两种处理模式。

## 现网烟雾测试结果（uid:13991807，2026-06-11）

| handler | 工作项数 | 结果 | 备注 |
|---------|---------|------|------|
| video_metadata | 76 | 76/76 SUCCESS | desc 平均 47 字符（vs videos endpoint 截断 250）；13 个 multi-P 视频；平均 8.4 tags/video |
| dynamics | 868 | 868/868 SUCCESS | timestamp 868/868 填上；types: FORWARD 683 / AV 85 / DRAW(OPUS) 63 / WORD 29 / COMMON 7 / ARTICLE 1；683 个 forwarded 子动态全部识别 |
| articles | 1 | 1/1 SUCCESS | UP 主仅 1 篇专栏；image_urls / stats / ctime 完整 |
| user_profile | 1 | 1/1 SUCCESS | 实测 uid:3546785614137774（"反殖民警戒"，2026-06-12，无 ASR）：四端点全 SUCCESS → result 含 overview = {video:64, article:28, opus:54}；handler <20 ms 完成；该 UP 主 `jointime` 在 acc/info 接口返回 0（B 站对部分账号隐藏注册时间字段），handler 正确透传 |

CLI 执行（`uv run python -m bili_unit process 13991807 -t <handler>`）：每个 handler 完成时间均 <1 秒
（已抓取数据，无网络请求）。

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
├── __init__.py            # DTO + 异常（含 AudioError 子类）+ assemble()
├── __main__.py            # processing CLI 入口
├── command.py             # ProcessingCommand.process_uid
├── query.py               # ProcessingQuery（task / item / list / video_full / errors）
├── data.py                # ProcessingDataStore（文件目录 JSON）
├── error.py               # ProcessingErrorStore（per-uid JSON 文件）
├── env.py                 # ProcessingEnv (pydantic-settings)
├── keys.py                # 存储 key 生成
├── runner.py              # Phase 0/1/2 编排 + transform/audio worker pools
├── task.py                # ProcessingTaskValue / PipelineEntry
├── transform/
│   ├── __init__.py        # 注册表导出
│   ├── _base.py           # TransformHandler Protocol + WorkItem
│   ├── _registry.py       # HANDLERS 视图 + get_handler
│   ├── video_metadata.py  # video_detail → video_metadata handler
│   ├── dynamics.py        # dynamics → dynamics handler
│   ├── articles.py        # articles + article_detail → articles handler
│   ├── opus.py            # opus + opus_detail → opus handler
│   └── user_profile.py    # user_info + relation_info + up_stat (+ overview_stat) → user_profile handler
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
query → data, error
runner → task, transform, audio, data, error, env, fetching.query, fetching.auth
transform → 无外部 import（纯计算）
audio._mimo_backend → aiohttp（HTTP 调用 MiMo API）
audio._downloader → aiohttp（CDN 下载）, bilibili_api（URL 解析）
audio._converter → subprocess（ffmpeg 调用）
data/error → 不 import command/query/runner/transform/audio
env → 不 import data/error/task
```

processing 通过 `bili_unit.fetching.query.Query` 只读访问 fetching 数据；不直接访问 fetching 的
DataStore/ErrorStore，也不写回 fetching。

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

## 工作项与 handler

每个 transform handler 实现 `TransformHandler` Protocol（[_base.py](../../bili_unit/processing/transform/_base.py)）：

```python
class TransformHandler(Protocol):
    item_type: str
    source_endpoints: tuple[str, ...]
    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]: ...
    def transform(self, item: WorkItem) -> dict[str, Any]: ...
```

| item_type | source_endpoints | item_id | 输入路径 | 备注 |
|-----------|------------------|---------|---------|------|
| video_metadata | video_detail | bvid | `info` + `tags` | item-level fan-out；只处理 SUCCESS items |
| dynamics | dynamics | id_str | `pages[*].items[*]` | 覆盖 5 类型：WORD / DRAW / AV / ARTICLE / FORWARD（外加 OPUS / COMMON major）；转发型保留 `forwarded` 子结构 |
| articles | articles + article_detail（可选） | str(cvid) | `pages[*].articles[*]` 列表项 + `{cvid → {info, markdown, content_json}}` | 列表级字段（meta / 摘要 / 封面 / stats）+ 正文 markdown + content_json 节点树 + word_count；`optional_endpoints=("article_detail",)`，缺失时仅输出列表级字段 |
| opus | opus + opus_detail（可选） | str(opus_id) | `pages[*].items[*]` 列表项 + `{opus_id → {info, markdown, images}}` | 列表级字段（title / summary / 封面 / stats / pub_time）+ 正文 markdown + 图片清单（width/height）+ word_count；`optional_endpoints=("opus_detail",)`，缺失时仅输出列表级字段；与 articles **不去重**——`is_article()` 为 true 的 opus 会同时出现在两条 item_type 下 |
| user_profile | user_info + relation_info + up_stat (+ overview_stat 可选) | str(uid) | 四端点 raw_payload 平铺 | UP 主画像；`optional_endpoints=("overview_stat",)`，缺失时 `result.overview` 整段省略 |

> dynamics 的 `id_str` 是 B 站动态稳定字符串 ID（与 fetching 端 item_id_path 一致；见 design §19 已决）。
> articles 端原始 `id` 是 int，store key 占位符 `{article_id}` 统一为 string。
> opus 端原始 `opus_id` 数值上可能是 int 也可能是 string，extractor 统一为 string；store key 形如 `uid:{uid}:proc:opus:{opus_id}`。
> user_profile 每个 uid 仅产出 1 个工作项；store key 形如 `uid:{uid}:proc:user_profile:{uid}`。

## 处理模式

`process_uid(uid, mode)` 支持两档：

| mode | 行为 |
|------|------|
| incremental（默认） | 已 SUCCESS 的工作项跳过；已 FAILED 的工作项重试一次；新增的抓取结果入队处理 |
| full | 忽略已有 processing 结果，对所有可处理工作项重新处理并覆盖写入 |

处理模式不向 fetching 传播；processing 不会因 mode=full 而触发 fetching refresh / full。

## fetching 状态消费规则（不阻塞）

processing 不要求 fetching task 整体 SUCCESS。runner 按 endpoint 粒度逐项判断（见 design §10.1）：

| endpoint 类型 | endpoint 状态 | 行为 |
|-------------|--------------|------|
| uid-level | SUCCESS | 入队所有 extract_items 产出的工作项 |
| uid-level | 其它 | 跳过该 endpoint，本次不处理 |
| video_detail | PARTIAL_ITEM | 仅处理已 SUCCESS 的 item |
| video_detail | SUCCESS | 处理全部 item |
| video_detail | 其它 | 跳过该 handler |

processing 不写回 fetching 状态。被跳过的 endpoint 在下次 `process_uid` 时按当前 fetching 状态重新评估。

## 两阶段编排

```
Phase 0  扫描     load_or_init_task → transform handler 发现工作项 + audio 发现 bvid
Phase 1  分发执行 transform worker pool + audio worker pool 并行处理
Phase 2  收尾     reload_task → derive_task_status → save_task → cleanup temp
```

worker 配置：
- `BILI_PROCESSING_TRANSFORM_WORKERS` 默认 4
- `BILI_PROCESSING_AUDIO_WORKERS` 默认 2
- `BILI_PROCESSING_QUEUE_MAXSIZE` 默认 16

## 存储层

processing 维护独立于 fetching 的两组目录存储：

```
{BILI_PROCESSING_DATA_DIR}/{uid}/task.json                      处理任务状态
{BILI_PROCESSING_DATA_DIR}/{uid}/proc/{item_type}/{item_id}.json 单工作项处理结果
{BILI_PROCESSING_DATA_DIR}/{uid}/progress/{pipeline}.json       流水线进度（audio）
{BILI_PROCESSING_DATA_DIR}/{uid}/progress/{pipeline}/{item_type}.json  per-item-type 进度（transform）

{BILI_PROCESSING_ERROR_DIR}/{uid}.json                          per-uid 错误记录
{BILI_PROCESSING_ERROR_DIR}/_null.json                          uid=None 错误
{BILI_PROCESSING_ERROR_DIR}/_counter.json                       自增 ID
```

processing 与 fetching 的 task key 同名（`uid:{uid}:task`），但因为 store 物理隔离（不同目录路径），不冲突。

### value 形状

**processing task**（`uid:{uid}:task`）：
```json
{
  "uid": 123,
  "status": "SUCCESS",
  "pipelines": {
    "transform": {
      "status": "SUCCESS",
      "items": {
        "video_metadata": { "total": 77, "completed": 77, "failed": 0, "skipped": 0 }
      }
    },
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

**transform 处理结果**（`uid:{uid}:proc:{item_type}:{item_id}`）：
```json
{
  "uid": 123,
  "pipeline": "transform",
  "item_type": "video_metadata",
  "item_id": "BV1xxxxxxxxxx",
  "status": "SUCCESS",
  "result": { /* transform-specific 结构化结果 */ },
  "source_endpoints": ["video_detail"],
  "processed_at": 1718000001000,
  "updated_at": 1718000001000
}
```

**transform 进度**（`uid:{uid}:progress:transform:{item_type}`）：
```json
{
  "pipeline": "transform",
  "item_type": "video_metadata",
  "total_items": 77,
  "completed_items": 50,
  "failed_items": 0,
  "skipped_items": 0,
  "remaining_items": 27,
  "done": false
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

异常层级：
```
ProcessingError
├── TransformError
│   ├── FieldExtractionError
│   └── FormatError
├── AudioError
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
- 自动重试调度（per-work-item，单次 `process_uid` 内）：可配置 `max_retries`（默认 3）+ 指数退避延迟（默认 30/60/120 秒）。每次重试记录 error（`retryable="true"`）并更新 data store（`retry_count` 递增）。重试耗尽后写最终 error（`retryable="false"`）。`AudioError` 子类（DownloadError / ASRConnectionError / ConvertError / ASRAPIError / AudioSizeError）被视为可重试；其他异常（TransformError / RuntimeError 等）不重试。
- Command 不暴露 `retry_failed()` 接口；FAILED 的工作项在 `mode=incremental` 重新调用时也会被重新处理（作为额外重试入口）。

## CLI

```bash
# 顶层统一入口
uv run python -m bili_unit process <uid>                       # incremental 处理（默认跑全部 5 个 handler）
uv run python -m bili_unit process <uid> -m full              # 全量重处理
uv run python -m bili_unit process <uid> -x video_metadata    # 排除指定 handler（推荐：跳过 ASR 重的 video_metadata）
uv run python -m bili_unit process <uid> -t video_metadata    # 仅指定 handler（调试用，与 -x 互斥）
uv run python -m bili_unit process <uid> -b mock              # 临时把 ASR 后端覆盖为 mock

# 独立 processing CLI
uv run python -m bili_unit.processing process <uid> [-m full] [-t TYPES... | -x TYPES...]
uv run python -m bili_unit.processing query <uid>             # 显示处理状态
uv run python -m bili_unit.processing list-uids               # 列出有 processing task 的 uid
uv run python -m bili_unit.processing video-full <uid> <bvid> # 联合 metadata + transcription
```

> 处理范围默认是「全部已注册 transform handler」。`-x/--exclude-item-types` 是推荐的剪裁方式，
> `-t/--item-types` 仅作调试时只跑指定 handler 用，二者互斥。

## 装配函数

```python
from bili_unit.processing import assemble
cmd, qry, data, error = await assemble()
```

`assemble()` 内部串调 `fetching.assemble()` 获取 `FetchingQuery` 注入 processing；根据
`BILI_PROCESSING_ASR_BACKEND` 创建对应的 ASR backend（mock / mimo）；返回
ProcessingCommand / ProcessingQuery / ProcessingDataStore / ProcessingErrorStore。

bili 顶层 `bili_unit.assemble()` 已经把 processing 包进 `BiliCommand` / `BiliQuery`：

```python
from bili_unit import assemble
cmd, qry, _data, _error = await assemble()
await cmd.fetch(uid)
await cmd.process(uid)
task = await qry.processing.get_task(uid)
```

### Command 接口

```python
async def process_uid(
    uid: int,
    pipelines: list[str] | None = None,   # ["transform", "audio"] 默认全部
    item_types: list[str] | None = None,  # 指定工作项类型
    mode: str = "incremental",            # "incremental" | "full"
) -> ProcessingCommandResult
```

### Query 接口

```python
async def get_task(uid: int) -> ProcessingTaskDTO | None
async def list_tasks() -> list[dict]
async def get_item(uid: int, item_type: str, item_id: str) -> ProcessingItemDTO | None
async def list_items(uid: int, item_type: str) -> list[ProcessingItemDTO]
async def get_video_full(uid: int, bvid: str) -> VideoFullDTO | None
async def list_all_videos(uid: int) -> list[VideoSummaryDTO]
async def list_errors(uid: int | None = None) -> list[ErrorDTO]
```

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_PROCESSING_DATA_DIR | data/bili/processing/data | 处理结果存储目录 |
| BILI_PROCESSING_TEMP_DIR | data/bili/processing/temp | 音频中间产物目录 |
| BILI_PROCESSING_ERROR_DIR | data/bili/processing/error | 处理错误存储目录 |
| BILI_PROCESSING_TRANSFORM_WORKERS | 4 | transform worker 数 |
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

- 79 processing 单元测试 + 集成测试全部通过（pytest）
- ruff lint 全部通过
- 212 pytest 总数：133 fetching + 79 processing
- 无外部网络 / API 依赖；测试可在离线环境运行

### 测试矩阵

```
test_processing_transform.py     transform handler 纯函数测试（15 tests，含 5 个 dynamics 真实形态 + 4 个 user_profile）
test_processing_data_error.py    data store + error store 单元测试（6 tests）
test_processing_audio.py         ASRBackend / MockASRBackend / MimoASRBackend / create_asr_backend / resolve_ffmpeg / token-budget 分段 / profile + auth_style + ASRConfigError + init-mimo 向导（39 tests）
test_processing_runner.py        runner / command / query 集成测试 + audio pipeline + auto-retry + 多段 duration 回归（20 tests，含 user_profile 集成）
fixtures/mimo_asr_response.json  MiMo 真实响应样本（uid:13991807 BV1o3YbzVEEo, 134s）
```

集成测试覆盖：
- video_metadata happy path（3 bvids 全部 SUCCESS）
- dynamics + articles 双 handler
- user_profile（四端点 SUCCESS / overview_stat 缺失 / 必填端点缺失三态）
- incremental 跳过已 SUCCESS（processed_at 不变）
- full 模式覆盖写（processed_at 推进）
- PARTIAL_ITEM 仅处理 SUCCESS items
- endpoint 不可用时跳过 handler，不影响其他工作
- transform 抛异常 → item FAILED + 错误入库
- VideoFullDTO 联合视图（含 transcription）
- audio pipeline 发现 + 处理（2 bvids，mock 转录）
- audio incremental 跳过已 SUCCESS
- audio 下载失败 → item FAILED + 错误入库
- audio 无 video_detail → 优雅跳过
- `_is_retryable` 分类（AudioError → true，其他 → false）
- audio retryable 错误重试至耗尽 → FAILED + 3 条 error 记录（2 retry + 1 final）
- audio retryable 错误首次失败后第二次成功 → SUCCESS
- audio 非 retryable 错误（RuntimeError）→ 不重试，立即 FAILED
- max_retries=0 → 单次尝试，不重试

## 已知限制 / 开放工作项

- `MockASRBackend` 返回固定文本；用于测试 + 接口稳定保证。`MimoASRBackend` 已实装，
  设 `BILI_PROCESSING_ASR_BACKEND=mimo` + 配好 `BILI_PROCESSING_ASR_API_KEY` 即可使用。
- `dynamics` 当前覆盖 WORD / DRAW / AV / ARTICLE / FORWARD 五类（含 OPUS / COMMON major）；其余长尾 type
  仍能产出工作项（带空 `text` / `major: {}`），后续可扩展更细致的字段抽取。
- `articles` 默认输出列表级字段；当 fetching 也跑了 `article_detail`（item-level fan-out 自 `articles` 派生），handler 会附带 markdown 正文 + content_json 节点树 + word_count。`article_detail` 是 `optional_endpoints`，所以它没跑过的项依然能正常 transform，只是没有正文字段。
- `opus` 与 `articles` 同模式：默认列表级字段，跑了 `opus_detail`（item-level fan-out 自 `opus` 派生）才会附带 markdown 正文 + 图片清单（含 width/height）+ word_count；`opus_detail` 也是 `optional_endpoints`。两条 handler **不去重**：B 站 `is_article()` 为 true 的 opus 同时出现在 `articles`（cvid 视角）与 `opus`（opus_id 视角）下，下游需要去重时自行合并。
- `bilibili-api-python` 17.x 在某些视频上 `VideoDownloadURLDataDetecter.detect_best_streams` 会抛
  `'NoneType' object has no attribute 'value'`；audio 下载器已采用
  `detect(audio_max_quality=...)` + `type(stream).__name__ == "AudioStreamDownloadURL"` workaround。
- `whisper` ASR 后端尚未实装（`create_asr_backend("whisper")` 仍抛 `NotImplementedError`）。
