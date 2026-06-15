# bili unit

> 性质：结构设计。本文描述 `bili` unit。
> 术语：**`A 服务 B` ≡ A 是基础，B 调用 A**（服务方向 A→B，调用方向 B→A）。

## 0. Dialectica 体系定位

bili_unit 是 [Dialectica](https://github.com/ChosenEcho/Dialectica) 项目
`source_data` 层下的一个 unit。Dialectica 的体系级结构约束（`main` /
`source-data` / `unit` / `index`）保留在 Dialectica 主仓库，**本文件不重复**：

- [main.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/main.md) — 四层总结构 + 服务方向不变量
- [source-data.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/source-data.md) — `unit (1..N) → index.ingestion`
- [unit.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/unit.md) — unit 抽象（`抓取 → 处理`）
- [index.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/index.md) — `ingestion → indexing → storage`

本仓库主管 bili 这个 unit 的内部结构（本文件 §1+）与现状（[docs/feature/](../feature/)）。
下文出现的 `docs/structure/unit.md`、`docs/structure/source-data.md` 等引用，
均指 Dialectica 主仓库的对应文件。

## 1. 位置

```text
source_data → unit → bili
```

```text
上游文档  unit
下游文档  无
结构位置  Bilibili 外部源 → bili → index.ingestion
```

## 2. 定位

```text
对象   Bilibili 数据源
单位   目标用户 uid
输入   目标用户 uid + 认证信息 + 任务
输出   处理结果与状态
服务   index.ingestion
```

## 3. 管线

```text
抓取 → 解析 → 处理
```

```text
抓取   认证 → 调用 Bilibili API → 原始数据入库
解析   读取原始数据 → 对象化为 typed dataclass → 可选图片下载 → typed object 入库
处理   读取 typed object → ASR 转录 → 处理结果入库
```

## 4. 模块

### 抓取

```text
auth         获取 / 校验 / 提供可用认证；认证异常写入 stage_error
env          保存认证配置（与 unit-level _env 共享）
client       抓取脚本；Credential 由 auth 提供；只依据 bili-api-info 调用 bilibili-api-python
rate_limit   控制请求频率与并发；限流状态进程内驻留，限流异常写入 stage_error
_store       FetchingStore；写 raw_payload + fetch_progress（raw DB）+ stage_task / fetch_endpoint_state / stage_error（main DB）
runner       根据 stage_task / fetch_endpoint_state / stage_error 编排抓取执行 / 重试
```

### 解析

```text
models       6 个 typed dataclass（UpProfile / VideoDetail / VideoSubtitle / Article / OpusPost / DynamicPost）；from_raw() / to_dict() / from_dict() + 图片协议。跨 model 共享的 SourceRef / CrossRefs 落在 models/_refs.py。
_images      ImageDownloader；aiohttp 并发下载 + skip-existing + 失败隔离
specs        ParsingSpec registry；MODEL_ORDER；materializer_handler 分发
materializer ParsingMaterializer；per-model raw → typed → save_*
_store       ParsingStore；写主 DB 的 6 张内容表 + image_asset + stage_task[stage='parsing']
command      ParsingCommand；parse_uid() 编排 6 个 model + 可选图片下载
```

### 处理

```text
audio        音频下载 + ASR 转录逻辑；调用外部 CDN 与 ASR 引擎（处理阶段唯一外部调用模块，依据 unit §3 显式登记）
_store       ProcessingStore；写主 DB 的 audio_transcription + stage_task[stage='processing'] + stage_error[stage='processing']
runner       根据 stage_task / audio_transcription / stage_error 编排处理执行 / 重试；驱动 audio pipeline
command      ProcessingCommand；process_uid() 编排 audio pipeline + retry
```

```text
跨源归一化 / 清洗不在 bili unit 内部完成。
  bili.processing 仅产出"bili 形态"的结构化数据；
  归一化为 index 文档形态由 index.ingestion 承担。
```

### 存储

```text
_db          SQLite 持久化层（paths / connection / context / DDL）；按 uid 派生 main DB / raw DB / workdir
main DB      {bili_db_dir}/{uid}.db —— 消费方契约；6 张内容表 + image_asset + stage_task / fetch_endpoint_state / stage_error + manifest_summary / video_full views
raw DB       {bili_db_dir}/{uid}.raw.db —— producer-private；raw_payload + fetch_progress
workdir      {bili_db_dir}/{uid}/ —— images（parsing 下载）/ audio temp & ASR cache（processing 中间产物）；DB 内只存相对路径
```

```text
SQLite 是唯一稳定 deliverable；消费方用 stdlib sqlite3 直连主 DB 查 SQL。
具体表与字段语义见 docs/schema.md，落盘格式见 docs/feature/*.md 与 docs/structure/fetching-contract.md。
```

### 入口

```text
command      写侧入口；驱动抓取、解析与处理管线
读侧         消费方直接 sqlite3.connect(bili_unit.db_path(uid))；不再有 Python query facade
```

## 5. 管线对象

```text
来源   docs/bili-api-info/modules/user.md + docs/bili-api-info/modules/video.md
范围   bili-api-info 中和目标用户 uid 有关的读取接口（uid-level），以及从 uid 抓取结果派生的 item-level 读取接口
单位   目标用户 uid
分类   如下
```

```text
User(uid)
用户基础信息           → parsing
用户发布内容（视频）   → parsing + audio
用户发布内容（文章）   → parsing
用户空间内容           → parsing
用户关系内容           → parsing
用户列表内容           → parsing
用户状态 / 统计内容    → parsing
```

## 6. 数据流

```text
fetching.command  → fetching.runner → auth → _env
                                    ↘ client → rate_limit
                                    ↘ FetchingStore → _db.UidContext
                                                       ├─ raw DB (raw_payload + fetch_progress)
                                                       └─ main DB (stage_task + fetch_endpoint_state + stage_error)
parsing.command   → parsing.materializer → FetchingStore (read raw_payload)
                                          ↘ models[*].from_raw() → typed object
                                          ↘ ImageDownloader（可选）→ workdir/images/
                                          ↘ ParsingStore → main DB (user_profile / video / video_subtitle /
                                                                     article / opus_post / dynamic_event /
                                                                     video_page / image_asset / stage_task)
processing.command → processing.runner → audio
                                       ↘ FetchingStore (read raw_payload for video_detail CDN URL)
                                       ↘ ParsingStore (read video / video_subtitle for cid / 字幕短路)
                                       ↘ ProcessingStore → main DB (audio_transcription + stage_task + stage_error)
                                       ↘ workdir/audio/ (temp + ASR cache; 收尾后清理)
消费方 → sqlite3.connect(db_path(uid)) → SELECT 内容表 / video_full / manifest_summary view
```

```text
bili 主动调用 Bilibili 外部源
fetching 通过 raw_payload 行向 parsing / processing 暴露 raw 数据；不直接共享 dataclass
audio 主动调用外部源（CDN 下载、ASR 转录）；其余模块不调用外部 API
index.ingestion 通过 SQL 只读访问 main DB；不打开 raw DB（除非要 re-parse）
```

## 7. 状态归属

```text
目标用户 uid
认证状态
认证配置
处理配置
任务状态（stage_task[stage] 一行一 stage）
抓取状态（fetch_endpoint_state 行 / raw_payload 是否存在）
抓取进度（fetch_progress.cursor）
解析状态（stage_task[stage='parsing'].payload.models[*]）
解析图片下载状态（image_asset 行 + stage_task.payload.images）
处理状态（audio_transcription.status）
处理进度（stage_task[stage='processing'].payload.pipelines[*].items）
请求状态
限流状态（进程内驻留；不持久化）
重试状态（fetch_endpoint_state.retry_count / RetryDriver 内部）
失败状态（stage_error 行）
抓取结果（raw_payload）
解析结果（user_profile / video / video_subtitle / article / opus_post / dynamic_event）
处理结果（audio_transcription）
raw 存储（{uid}.raw.db）
temp / asr_cache 存储（workdir 二进制目录）
parsing 存储（typed objects 行 + workdir/images/）
main DB 存储（{uid}.db）
错误状态（stage_error）
抓取时间（meta.last_fetched_at_ms / fetched_at_ms）
解析时间（meta.last_parsed_at_ms / parsed_at_ms）
处理时间（meta.last_processed_at_ms / processed_at_ms）
```

## 8. 边界

```text
不处理抓取结果语义（在处理阶段处理）
不固定用户相关接口清单
不使用 bili-api-info 之外的抓取能力
不跨 unit 聚合
不做跨源归一化 / 清洗（归 index.ingestion）
不推送
不服务 index.ingestion 之外的调用方
不直接服务 index / reasoning / interaction
不暴露 Python query facade（消费方走 SQL）
audio 不直接读取 raw_payload 之外的字段
_env 不写入 DB
runner 编排 client / audio
runner 根据 stage_task / stage_error 编排重试
command 不直接调用 client / audio
command 不写 raw / workdir / DB（write 走 store）
stage 内 store 之间互不直接调用（共享 UidContext，由 command 注入）
读侧不暴露 DB 内部生产状态语义（stage_task / fetch_endpoint_state / stage_error 仅 debug）
读侧不读取 raw DB（除非显式 re-parse）
stage_error 不编排重试
调用方不直接写 main DB / raw DB / workdir
调用方读 main DB 时按 §10 稳定性承诺消费内容表 / view
workdir/audio temp 处理完成后删除
```

## 9. 外部依赖

```text
bilibili-api-python [GitHub](https://github.com/nemo2011/bilibili-api)
bili-api-info 作为抓取脚本唯一依据
Credential 认证
Bilibili CDN        视频音频流下载
ASR 引擎             语音转文字
异步调用
关键字参数调用
请求后端优先级  curl_cffi → aiohttp
412 风险         请求过快触发；由限流控制处理
```

## 10. 目录骨架

```text
bili_unit/                # Python 包根（pyproject 里 packages = ["bili_unit"]）
├── __init__.py           # 公共 API：session() / db_path() / raw_db_path() / list_uids() / 写侧 result DTO + 异常
├── __main__.py           # 统一 CLI 入口；python -m bili_unit <subcommand>
├── _env.py               # BiliSettings (pydantic-settings)；bili_db_dir 是唯一存储根
├── _retry.py             # 共享 RetryDriver（fetching / processing 通用）
├── _db/                  # SQLite 持久化层（paths / connection / context / DDL）
├── command/              # 写侧入口；BiliCommand 包装三 stage 的 command
├── fetching/             # 抓取阶段（auth / _bilibili_adapter / rate_limit / runner/ / _store / command）
├── parsing/              # 解析阶段（models / _images / specs / materializer / _store / command）
├── processing/           # 处理阶段（audio/ / runner/ / _store / command）
└── tests/                # pytest 测试目录
```

运行时数据默认落在工作目录下的 `data/bili/...`，由 `BILI_DB_DIR` 覆盖；不在 Python 包目录内。

```text
代码现状（结构 vs 实现）
  上表为 bili_unit 仓库当前的实际布局，包级形态稳定。
  fetching / parsing / processing 作为阶段子包存在；三个 stage 通过共享 UidContext
  在同一对 SQLite DB 上协作，bili_unit/command/ 作为跨阶段写侧统一入口。
  消费方通过 sqlite3.connect(bili_unit.db_path(uid)) 直接消费主 DB，
  不再有 bili_unit/query/ 包；manifest 是 manifest_summary SQL view，不是独立 stage。
  raw / workdir / DB 的物理目录由 BILI_DB_DIR 控制，结构上不属于代码包。
```

