# bili_unit

Bilibili 数据持久化单元。给定目标用户 uid，把 B 站读取端点的原始响应落到本地 SQLite，再对视频音频做 ASR 转录、把转写结果写进同一个文件。本项目独立可用、独立发版；跨源归一化、字段提升、检索不在仓库范围内。

## Language

### 项目定位

**unit**:
一个可独立运行的数据持久化单元；本仓库 = Bilibili 用户数据 unit。一个 unit 围绕一个外部源（B 站）组织抓取、ASR 与落盘。
_Avoid_: source, connector, integration。

**consumer**:
通过 SQLite 读取本项目产物的任意调用方或宿主应用。consumer 直接 `sqlite3.connect` 查询 raw DB；本项目不提供 Python query facade。
_Avoid_: downstream。

### 两 stage

**fetching**:
第一 stage。异步抓取 63 个 B 站读取端点的原始响应，双层限流（global + endpoint QPS）+ 412 自适应降速，所有请求结果原样落盘到 `raw_payload` 表。不做字段筛选、不做对象化。
_Avoid_: crawler, scraper, collector。

**asr**:
第二 stage。对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。bvid 与分页元信息直接从 `raw_payload(endpoint='video_detail')` 抽，不依赖中间层。当前仅 audio pipeline。落盘到 `audio_transcription` 系列表。
External command naming and the DB stage key are `asr`
(`stage_task.stage`, `stage_error.stage`, and related payloads). `process`
remains only a Python/internal backward-compatible alias; the CLI does not
expose it.
_Avoid_: handler, worker, transformer。

> 历史：早先存在过一个 `parsing` 阶段，把 raw payload 物化为 typed dataclass + image_asset。该阶段在 schema_v3 中整体删除，原因是它在「被动持久化」定位下属于多余的半归一化层；典型字段提升、视图、跨表 join 都改由 consumer 在自己的查询层做。

### 数据形态

**uid**:
目标用户。B 站用户 ID（整数）。unit 的工作单位——所有抓取 / ASR 按 uid 隔离。
_Avoid_: user_id, account, mid（mid 是 B 站 API 内部字段名，本仓库对外用 uid）。

**raw_payload**:
fetching 信封内的未加工字段。非分页端点 = B 站 API 原始响应 dict；分页端点 = `{"pages": [page1, ...]}`，每页是一次 API 响应的原始 dict。fetching 不做字段筛选；consumer 用 `json_extract` 自取。
_Avoid_: response, data, result。

**audio_transcription**:
asr 产物。每个 bvid 一行，列含 `status` / `transcript` / `seconds` / `audio_tokens` / `cache_hits` 与完整 ProcessingItem dict 的 `payload`；派生表 `audio_transcription_page` / `audio_transcription_segment` 提供查询友好的 page / segment 视角。
_Avoid_: transcript_record, asr_result, transcription。

### 端点与抓取

**endpoint**:
B 站一个读取接口的注册单元，`EndpointSpec` dataclass 描述其 callable / 分页策略 / 限流 key / item_id_path。共 63 个（33 uid-level + 30 item-level），在 `_endpoint_catalog.py` 声明。
_Avoid_: api, route, source。

**uid-level endpoint**:
直接按 uid 抓取的端点（如 `user_info` / `videos` / `dynamics`）。33 个。
_Avoid_: user endpoint, top-level endpoint。

**item-level endpoint / item-level fan-out**:
从父端点的 raw_payload 派生 item ID 列表，逐个抓取的端点（如 `video_detail` 自 `videos` 派生 bvid；`article_detail` 自 `articles` 派生 cvid）。30 个。`extract_items` callable 负责 ID 提取。
_Avoid_: detail endpoint, sub-endpoint, child endpoint。

**profile**:
CLI `--profile {all,minimal}` 选端点子集。`all`=63、`minimal`=5（smoke / CI）。早先存在过的 `parsing` profile 已随 parsing 阶段一并删除；想精挑端点用 `--include` / `--exclude`。
_Avoid_: preset, mode（mode 指抓取模式，不同概念）。

**fetch run scope**:
一次 fetching 调用在进入 runner 执行前解析出的运行范围：目标 uid、endpoint 集合、fresh/resume 语义和 mode。它集中表达 `run_task` / `resume_task` / `run_or_resume` 的状态决策。
_Avoid_: task wrapper, execution context。

### B 站 ID

**bvid**:
视频 BV 号（如 `BV1xx411x7xx`）。`videos` / `video_*` 端点的 item ID。
_Avoid_: video_id, aid（aid 是 B 站内部 aid，不对外）。

**cvid**:
专栏文章 ID（如 `cv123456`）。`articles` / `article_detail` 端点的 item ID。
_Avoid_: article_id。

**opus_id**:
图文帖 ID。`opus` / `opus_detail` 端点的 item ID。
_Avoid_: opus cv（图文不是专栏）。

**dynamic_id**:
动态 ID（B 站称 `id_str`）。`dynamics` 端点的 item ID。
_Avoid_: dyn_id, dynamic_str。

**rlid**:
文集（readlist）ID。`article_list` / `article_list_detail` 端点的 item ID。
_Avoid_: list_id。

**season_id / series_id**:
合集 / 列表 ID。`channel_list` 派生 `channel_videos_season` / `channel_videos_series`。

### 状态机

**mode（incremental / refresh / full）**:
fetching 与 asr 共享部分执行语义。`incremental` 跳过已成功、重试失败；`refresh` 介于两者间（fetching 的 item-level 检查 7 天 freshness window；asr 不支持 refresh）；`full` 忽略已有数据全量重跑。
_Avoid_: strategy, run type。

### 出口面

**command / SQL read side**:
bili unit 以命令行和内部写侧 command 组织流程；`command`（`BiliCommand`）驱动 fetch / asr / delete。stage 子模块（fetching client / runner / audio worker）不对外，藏在 command 后。
_Avoid_: service, facade, api。

## Notes

- 详细结构约束见 `docs/architecture.md`；数据契约见 `docs/schema.md`。
- 提交前自检 / CI 门禁（顺序一致，见 `.github/workflows/ci.yml`）：
  `uv run ruff check` → `uv run ruff format --check` → `uv run mypy bili_unit` → `uv run pytest`。
  类型检查首次接入，`bili_unit/` 源码须零 error；测试目录暂不纳入门禁。
