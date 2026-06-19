# schema —— bili_unit SQLite 数据契约

> 真相源：[raw_v3.sql](../bili_unit/_db/ddl/raw_v3.sql)
> 适用版本：DB `schema_version = 3`

bili_unit 自 schema v3 起把整个产物收敛到单 SQLite 文件：写侧通过 `BiliCommand` 编排两 stage（fetching → asr），读侧 **直接 SQL 查询**——不再有 Python query facade，也不再有 typed-object 物化层。本文件描述消费方需要的 SQL 表面。

## 1. 概念

每个 uid 落盘一个文件 + 一个工作目录：

| 路径 | 用途 | 稳定性 |
|------|------|----------------|
| `{db_dir}/{uid}.raw.db` | 唯一 DB（消费方读取面） | 见 §3 |
| `{db_dir}/{uid}/` | 工作目录（audio 缓存 / 临时文件） | 部分 |

`{db_dir}` 默认 `output/bili`，由 `BILI_DB_DIR` 覆盖。

> 历史遗留：早先版本会同时写一个 `{uid}.db`（typed-object 主 DB）。该文件现已废弃，新版本不再读写；目录里如果还有可手动删除。

### 连接示例

```python
import json, sqlite3, bili_unit
conn = sqlite3.connect(bili_unit.db_path(123456))
conn.row_factory = sqlite3.Row
for row in conn.execute(
    "SELECT item_id, payload FROM raw_payload "
    "WHERE endpoint = 'video_detail' LIMIT 5"
):
    payload = json.loads(row["payload"])
    print(row["item_id"], payload["info"]["title"])
```

```bash
sqlite3 output/bili/123456.raw.db "SELECT * FROM manifest_summary;"
```

只读用 stdlib `sqlite3` 即可，无需安装 `aiosqlite` / ORM。建议 `conn.row_factory = sqlite3.Row`，访问列名更稳。

### Schema versioning

`meta.schema_version` 当前为 `'3'`。迁移策略：DDL 不兼容修改时 bump major；新增列、新增 view、新增 index 不算不兼容。运行时连接器检测到 `schema_version` 不等于自己支持的版本时会抛 `SchemaMismatchError`。

## 2. Path helpers

`bili_unit.__init__` 公开 3 个 helper：

| 名字 | 签名 | 说明 |
|------|------|------|
| `db_path` | `(uid: int, settings=None) -> Path` | 唯一 DB 路径，文件可能尚未存在 |
| `list_uids` | `(root: str \| Path) -> list[int]` | 扫描目录列出所有已落盘 uid，按升序返回 |
| `UidContext` | class | 低阶：开关单 Connection，给测试 / 迁移工具使用 |

`UidContext` 是内部连接 helper；常规消费方用 stdlib `sqlite3.connect(path)` 就够。

## 3. 表（消费方契约）

所有时间字段命名以 `_ms` 结尾，存 INTEGER ms-epoch；`payload TEXT NOT NULL` 持有原始 JSON。

### 3.1 `meta` —— KV 元信息

| 列 | 类型 | 说明 |
|----|------|------|
| `key` | TEXT PK | 见下方约定 |
| `value` | TEXT NOT NULL | 字符串化的值（`schema_version='3'`、`uid='123456'`、ms-epoch 的 `'1718000000000'` 等） |

约定 keys：`schema_version`、`uid`、`created_at_ms`、`last_fetched_at_ms`、`last_processed_at_ms`。后两个是 stage 入口在写入收尾时刷新的「最近一次成功跑完时间」。

### 3.2 `raw_payload` —— 原始端点响应（核心数据）

复合 PK `(endpoint, item_id)`：

- `item_id = ''` —— endpoint 级响应（uid-level 端点），分页端点把合并后的 `{pages: [...]}` dict 整体存为单行
- `item_id = bvid / cvid / opus_id / dynamic_id / rlid / ...` —— item-level 端点的 fan-out 子项

| 列 | 类型 | 说明 |
|----|------|------|
| `endpoint` | TEXT NOT NULL | 端点名，见 docs/endpoint-contract.md |
| `item_id` | TEXT NOT NULL DEFAULT `''` | fan-out 项 ID 或空串 |
| `payload` | TEXT NOT NULL | 原始 API 响应 JSON dict |
| `fetched_at_ms` | INTEGER NOT NULL | 抓取时间 |

索引 `idx_raw_endpoint(endpoint, fetched_at_ms)` 加速按端点 / 时间过滤。

### 3.3 `fetch_progress` —— 分页游标 / 进度

PK `endpoint`；`runner` 在写完 `raw_payload` 后把游标推进这里，崩溃落在中间时游标是旧值，下次 resume 从旧游标重抓（payload 幂等覆盖）。列：`cursor` / `total` / `fetched` / `updated_at_ms`。

### 3.4 `audio_transcription` —— ASR 转写

PK：`bvid`。（注意：不再 FK 到任何 video 表——bvid 来源自 `raw_payload(endpoint='video_detail')`。）

| 列 | 类型 | 说明 |
|----|------|------|
| `bvid` | TEXT PK | |
| `status` | TEXT NOT NULL CHECK IN (`'pending'`,`'running'`,`'success'`,`'failed'`,`'skipped'`) | 状态机 |
| `transcription_source` | TEXT | 文本来源（`MIMO-ASR` / `mock` 等） |
| `transcript` | TEXT | 转写全文（`success` 时非空） |
| `audio_tokens` | INTEGER | LLM tokens 累计（计费用） |
| `seconds` | REAL | 实际处理音频秒数 |
| `cache_hits` | INTEGER | 复用缓存命中数 |
| `payload` | TEXT NOT NULL | 完整 ProcessingItem dict |
| `processed_at_ms` | INTEGER NOT NULL | 处理完成时间 |

`audio_transcription.payload` 是 ASR 处理结果的 canonical JSON。下面两张表是从 `payload.result.pages` 物化出来的查询表：

- `audio_transcription_page`：每个 ASR page 一行，PK `(bvid, page_no)`。`page_no` 是 SQL 侧 1-based 页号；`page_index` 是 payload 里的 0-based index。常用列包括 `language` / `asr_model` / `transcript_text` / `transcript_char_count` / `segment_count`。
- `audio_transcription_segment`：每个 ASR segment 一行，PK `(bvid, page_no, segment_no)`。除 `start_seconds` / `end_seconds` / `duration_s` / `transcript_text` 外，还保留 `is_empty_transcript_skip` / `is_high_risk_audio_skip` / `error_message`，用来区分「本段本来无文本」「高风险跳过」和普通识别文本。

`failed` / `skipped` / `pending` / `running` 写入会清空旧的 ASR page/segment 派生行，避免状态回退后还残留旧 transcript。

## 4. Producer state 表（仅 debug 用）

> ⚠️ 这些表 **不属于消费方契约**，可能在 minor 版本调整列。需要稳定的话只读 §3 / §5。
> Naming note: the user-facing command/event name and DB stage key are `asr`;
> the old `process` CLI command is no longer exposed.

### 4.1 `stage_task`

PK：`stage`，CHECK IN (`'fetching'`,`'asr'`)。一个 stage 一行，覆盖式写入。`payload` JSON 内含 `endpoints` / `pipelines` 子状态、`failed_item_ids`、`item_progress` 等运行时细节。

### 4.2 `fetch_endpoint_state`

PK：`endpoint`。每端点一行，列：`status` / `retry_count` / `last_error_id` / `item_progress`（fan-out 计数）/ `progress`（分页游标）/ `updated_at_ms`。`item_progress` 与 `progress` 都是 JSON。item fan-out 的 `item_progress` 常用键包括 `total` / `completed` / `failed`，以及缓存、过滤或终态跳过时的 `skipped` / `skipped_existing` / `skipped_fresh` / `skipped_unavailable` / `skipped_filtered`。

### 4.3 `stage_error`

`id INTEGER PK AUTOINCREMENT`，CHECK `stage IN ('fetching','asr')`。列：`endpoint` / `pipeline` / `item_type` / `item_id` / `error_type` / `message` / `retryable` / `detail` / `occurred_at_ms`。索引 `idx_stage_error_stage(stage, occurred_at_ms)`。

### 4.4 `stage_run`

内部 run-history 表，供 observability 层使用。一行代表一次写侧命令运行。

列：`run_id`（TEXT PK）/ `uid` / `command` / `status` /
`started_at_ms` / `ended_at_ms` / `args_json` / `summary_json`。

`status` CHECK 包含 `PENDING` / `RUNNING` / `SUCCESS` / `PARTIAL` / `FAILED`
/ `CANCELLED` / `DRY_RUN`。`DRY_RUN` 表示命令完成了候选发现和估算，但没有更新 stage task 或写入 ASR 结果。

索引：`idx_stage_run_uid_started(uid, started_at_ms DESC, run_id DESC)`。

### 4.5 `stage_event`

`stage_run` 的 append-only 语义事件时间线。

列：`id` / `run_id` / `ts_ms` / `level` / `stage` / `event` /
`endpoint` / `pipeline` / `item_type` / `item_id` / `message` /
`data_json`。

稳定事件前缀为 `fetch.*` / `asr.*`。实现细节留在结构化字段中，例如
`event='asr.item.failed'` 搭配 `pipeline='audio'`。

索引：

- `idx_stage_event_run_id(run_id, id DESC)`
- `idx_stage_event_item(stage, endpoint, pipeline, item_type, item_id)`

Run Summary 和 CLI 使用方式见 [observability.md](observability.md)。

## 5. Views（manifest 替代品）

### 5.1 `manifest_summary`

单行聚合视图。列：

| 列 | 来源 |
|----|------|
| `uid` / `schema_version` / `last_fetched_at_ms` / `last_processed_at_ms` | meta 表对应 key |
| `endpoint_count` | `COUNT(DISTINCT endpoint) FROM raw_payload` |
| `raw_payload_count` | `COUNT(*) FROM raw_payload` |
| `video_count` | `COUNT(*) FROM raw_payload WHERE endpoint = 'video_detail'` |
| `transcribed_count` | `audio_transcription WHERE status='success'` |
| `transcription_failed_count` | `audio_transcription WHERE status='failed'` |
| `total_audio_tokens` / `total_audio_seconds` / `total_cache_hits` | `COALESCE(SUM(...), 0)` over audio_transcription |
| `fetching_error_count` / `asr_error_count` | `stage_error WHERE stage=…` |

注意 meta 来源列保持 TEXT（因 `meta.value` 是 TEXT），数值列做 SUM/COUNT 后是 INTEGER/REAL；消费方拿到 `last_*_at_ms` 时按需 `int(row["last_fetched_at_ms"])`。

按需的「typed view」（如以前的 `video_full`、按内容类型聚合的 view 等）请由消费方在自己的查询层用 `json_extract` 派生；本仓库不再提供。

## 6. SQL 食谱

### 6.1 列出某 uid 的视频，按发布时间倒序

```sql
SELECT item_id AS bvid,
       json_extract(payload, '$.info.title')   AS title,
       json_extract(payload, '$.info.duration') AS duration_s,
       json_extract(payload, '$.info.pubdate') * 1000 AS pubdate_ms
FROM raw_payload
WHERE endpoint = 'video_detail'
ORDER BY pubdate_ms DESC
LIMIT 50;
```

> `pubdate` 在 B 站原始响应里是秒；用 `json_extract * 1000` 自取毫秒。

### 6.2 取单个视频 + 完整 ASR 转写

```sql
SELECT r.item_id AS bvid,
       json_extract(r.payload, '$.info.title') AS title,
       t.status, t.transcription_source, t.transcript, t.seconds
FROM raw_payload r
LEFT JOIN audio_transcription t ON t.bvid = r.item_id
WHERE r.endpoint = 'video_detail' AND r.item_id = ?;
```

### 6.3 找出尚未转写的视频

```sql
SELECT r.item_id AS bvid
FROM raw_payload r
LEFT JOIN audio_transcription t ON t.bvid = r.item_id
WHERE r.endpoint = 'video_detail'
  AND (t.status IS NULL OR t.status = 'failed')
ORDER BY json_extract(r.payload, '$.info.pubdate') DESC;
```

可以直接喂给下次 `asr` run 的 `--only-bvids`。

### 6.4 一次拿到 manifest 摘要

```sql
SELECT * FROM manifest_summary;
```

返回单行，类似旧 `manifest/{uid}.json` 内容。

### 6.5 列出最近的 fetching 错误

```sql
SELECT endpoint, error_type, message, occurred_at_ms
FROM stage_error
WHERE stage = 'fetching'
ORDER BY id DESC
LIMIT 20;
```

`idx_stage_error_stage(stage, occurred_at_ms)` 加速。`detail` 列存 JSON，需要更多上下文时再 SELECT 出来。

## 7. JSON 列使用

所有 `payload` 列是 UTF-8 JSON，搭配 `sqlite3.Row` + `json.loads()` 即可：

```python
import json, sqlite3, bili_unit
conn = sqlite3.connect(bili_unit.db_path(uid))
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT payload FROM raw_payload "
    "WHERE endpoint = 'video_detail' AND item_id = ?",
    (bvid,),
).fetchone()
data = json.loads(row["payload"])  # 完整 video_detail 原始响应
```

需要在 SQLite 侧做过滤 / 排序时用 JSON1（Python 自带 sqlite 默认编译开启）：

```sql
SELECT item_id, json_extract(payload, '$.info.owner.name') AS up_name
FROM raw_payload
WHERE endpoint = 'video_detail'
LIMIT 5;
```

`json_extract` 支持 dot path 与 `$[0]` 索引；返回值是 TEXT/INTEGER/REAL/NULL，可直接用作 WHERE 条件。

## 8. WAL 文件

DB 开启 `PRAGMA journal_mode = WAL`，运行时会附带 `{uid}.raw.db-wal` / `{uid}.raw.db-shm`。

约定：

- 由 SQLite 自动管理，正常关闭连接 / `PRAGMA wal_checkpoint(TRUNCATE)` 后清空。
- 备份时可忽略（先 `wal_checkpoint`）或与主文件一并打包；二者一致即可。
- `BiliCommand.delete_uid(uid)` 会同步删除 `-wal` / `-shm` 伴生文件，无需手动清理。

## 9. 稳定性承诺

- §3.1（`meta`）、§3.2（`raw_payload`）、§3.4（`audio_transcription` 系列）的列名 / 类型 / PK / CHECK 约束，以及 §5 的 view 列，是稳定读取面；不兼容修改会 bump schema major。
- 新增列、新增 view、新增 index 视为 minor 兼容变更，消费方应宽容（用列名而非列序读取）。
- §4 的 `stage_task` / `fetch_endpoint_state` / `stage_error` 内部列可在 minor 版本演化；如需跨版本读取，看 `payload` JSON 的 schema 注释。
- raw_payload 内部的 B 站响应 schema 由上游决定，本仓库不归一化；用 `docs/endpoint-contract.md` 当导航。
