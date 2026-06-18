# bili_unit

Bilibili 数据持久化单元。给定目标用户 uid，把 B 站读取端点的原始响应落到本地、对象化为 typed object、再对视频音频做 ASR 转录。本项目独立可用、独立发版；跨源归一化、清洗、检索不在仓库范围内。

## Language

### 项目定位

**unit**:
一个可独立运行的数据持久化单元；本仓库 = Bilibili 用户数据 unit。一个 unit 围绕一个外部源（B 站）组织抓取、解析、处理与落盘。
_Avoid_: source, connector, integration。

**consumer**:
通过 SQLite 读取本项目产物的任意调用方或宿主应用。consumer 直接 `sqlite3.connect` 查询主库；本项目不提供 Python query facade。
_Avoid_: downstream。

### 三 stage

**fetching**:
第一 stage。异步抓取 64 个 B 站读取端点的原始响应，双层限流（global + endpoint QPS）+ 412 自适应降速，所有请求结果原样落盘到 fetching store。不做字段筛选。
_Avoid_: crawler, scraper, collector。

**parsing**:
第二 stage。读 fetching 的 raw_payload，筛选 / 归并 / 对象化为 typed object，可选下载图片到本地。落盘到 parsing store。
_Avoid_: parser, transform, mapper。

**asr**:
第三 stage。对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。当前仅 audio pipeline。落盘到 processing store。
External command/API naming and the DB stage key are `asr`
(`stage_task.stage`, `stage_error.stage`, and related payloads). `process` is
only a backward-compatible CLI alias.
_Avoid_: handler, worker, transformer。

### 数据形态

**uid**:
目标用户。B 站用户 ID（整数）。unit 的工作单位——所有抓取 / 解析 / 处理按 uid 隔离。
_Avoid_: user_id, account, mid（mid 是 B 站 API 内部字段名，本仓库对外用 uid）。

**raw_payload**:
fetching 信封内的未加工字段。非分页端点 = B 站 API 原始响应 dict；分页端点 = `{"pages": [page1, ...]}`，每页是一次 API 响应的原始 dict。fetching 不做字段筛选。
_Avoid_: response, data, result。

**typed object / parsed object**:
parsing 产物的统称。落盘为主 SQLite DB 中的内容表行；`payload` 列保留完整 JSON dict。当前 6 个 model：`UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`。
_Avoid_: entity, record, document。

### 端点与抓取

**endpoint**:
B 站一个读取接口的注册单元，`EndpointSpec` dataclass 描述其 callable / 分页策略 / 限流 key / item_id_path。共 64 个（34 uid-level + 30 item-level），在 `_endpoint_catalog.py` 声明。
_Avoid_: api, route, source。

**uid-level endpoint**:
直接按 uid 抓取的端点（如 `user_info` / `videos` / `dynamics`）。34 个。
_Avoid_: user endpoint, top-level endpoint。

**item-level endpoint / item-level fan-out**:
从父端点的 raw_payload 派生 item ID 列表，逐个抓取的端点（如 `video_detail` 自 `videos` 派生 bvid；`article_detail` 自 `articles` 派生 cvid）。30 个。`extract_items` callable 负责 ID 提取。
_Avoid_: detail endpoint, sub-endpoint, child endpoint。

**profile**:
CLI `--profile {all,parsing,minimal}` 选端点子集。`all`=64、`parsing`=13（parsing 实际消费的）、`minimal`=5（smoke / CI）。
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
fetching 与 processing 共享的三档执行语义。`incremental` 跳过已成功、重试失败；`refresh` 介于两者间（fetching 的 item-level 检查 7 天 freshness window；processing 不支持 refresh）；`full` 忽略已有数据全量重跑。parsing 只支持 `full` / `incremental` 两档。
_Avoid_: strategy, run type。

### 出口面

**command / SQL read side**:
bili unit 以命令行和内部写侧 command 组织流程；`command`（`BiliCommand`）驱动 sync / asr（`process` 仅兼容 alias）。读侧由调用方直接 SQL 查询。stage 子模块（client / runner / materializer / audio）不对外，藏在 command 后。
_Avoid_: service, facade, api。

## Notes

- 详细结构约束见 `docs/structure/bili.md`；实现现状见 `docs/feature/{fetching,parsing,processing}.md`（后者是真相源）。
