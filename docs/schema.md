# schema —— bili_unit SQLite 数据契约

> 真相源：[main_v1.sql](../bili_unit/_db/ddl/main_v1.sql)、[raw_v1.sql](../bili_unit/_db/ddl/raw_v1.sql)
> 适用版本：`schema_version = 1`（main DB 与 raw DB 各自独立编号）

bili_unit 自 Phase 3 起将自己重新定位为「被动持久化数据存储」：写侧通过 `BiliCommand` 编排三 stage，读侧 **直接 SQL 查询**——不再有 Python query facade。本文件描述消费方需要的 SQL 表面。

## 1. 概念

每个 uid 落盘三个工件：

| 路径 | 用途 | 受 SemVer 保护 |
|------|------|----------------|
| `{db_dir}/{uid}.db` | 主 DB（消费方契约） | ✓ 见 §3 / §5 |
| `{db_dir}/{uid}.raw.db` | 原始端点响应（producer-private） | ✗ 内部 |
| `{db_dir}/{uid}/` | 工作目录（image / audio 二进制文件，DB 内存相对路径） | 部分 |

`{db_dir}` 默认 `data/bili`，由 `BILI_DB_DIR` 覆盖。

### 连接示例

```python
import sqlite3, bili_unit
conn = sqlite3.connect(bili_unit.db_path(123456))
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT bvid, title FROM video ORDER BY pubdate_ms DESC LIMIT 5"):
    print(row["bvid"], row["title"])
```

```bash
sqlite3 data/bili/123456.db "SELECT * FROM manifest_summary;"
```

只读用 stdlib `sqlite3` 即可，无需安装 `aiosqlite` / ORM。建议 `conn.row_factory = sqlite3.Row`，访问列名更稳。

### Schema versioning

`meta.schema_version` 当前为 `'1'`。迁移策略：DDL 不兼容修改时 bump major（`schema_version = 2` 等），并附带 `tools/migrate_*` 脚本；新增列、新增 view、新增 index 不算不兼容。运行时连接器检测到 `schema_version` 高于自己支持的最大版本会抛 `SchemaMismatchError`。

## 2. Path helpers

`bili_unit.__init__` 公开 4 个 helper：

| 名字 | 签名 | 说明 |
|------|------|------|
| `db_path` | `(uid: int, settings=None) -> Path` | 主 DB 路径，文件可能尚未存在 |
| `raw_db_path` | `(uid: int, settings=None) -> Path` | 原始 DB 路径（仅 re-parse 场景用） |
| `list_uids` | `(root: str \| Path) -> list[int]` | 扫描目录列出所有已落盘 uid，按升序返回 |
| `UidContext` | class | 低阶：成对开关 (main, raw) Connection，给测试 / 迁移工具使用 |

`UidContext` 不在 SDK 推荐入口面里——常规消费方用 stdlib `sqlite3.connect(db_path(uid))` 就够；只有需要在 SDK 内部复用 retry / DDL 校验逻辑时再走它。

## 3. 主 DB 表（消费方契约）

所有时间字段命名以 `_ms` 结尾，存 INTEGER ms-epoch；`payload TEXT NOT NULL` 持有该行对应 dataclass 的完整 `to_dict()` JSON，是字段未提升为类型化列时的 escape hatch。

### 3.1 `meta` —— KV 元信息

| 列 | 类型 | 说明 |
|----|------|------|
| `key` | TEXT PK | 见下方约定 |
| `value` | TEXT NOT NULL | 字符串化的值（`schema_version='1'`、`uid='123456'`、ms-epoch 的 `'1718000000000'` 等） |

约定 keys：`schema_version`、`uid`、`created_at_ms`、`last_fetched_at_ms`、`last_parsed_at_ms`、`last_processed_at_ms`。后三个是 stage 入口在写入收尾时刷新的「最近一次成功跑完时间」。

### 3.2 `user_profile` —— UP 主资料

PK：`uid`。典型列：`name` / `sign` / `face_url` / `level` / `follower` / `following`，加 `payload` 与 `parsed_at_ms`。

### 3.3 `video` + `video_page` —— 视频与分 P

`video` PK：`bvid`。

| 列 | 类型 | 说明 |
|----|------|------|
| `bvid` | TEXT PK | B 站 bvid |
| `aid` | INTEGER | 旧 av 号 |
| `title` / `description` / `cover_url` | TEXT | |
| `duration_s` | INTEGER | 秒 |
| `pubdate_ms` | INTEGER | 发布时间 |
| `view_count` / `danmaku` / `reply` / `favorite` / `coin` / `share` / `like_count` | INTEGER | 统计快照 |
| `payload` | TEXT NOT NULL | 完整 dict |

索引 `idx_video_pubdate` 在 `pubdate_ms DESC` 上，按时间排序无需 sort。

`video_page` 用复合 PK `(bvid, page_no)`，FK `bvid → video(bvid) ON DELETE CASCADE`。列：`cid`、`part`、`duration_s`。

### 3.4 `video_subtitle` —— 字幕

PK：`bvid`，FK CASCADE 到 `video`。

| 列 | 类型 | 说明 |
|----|------|------|
| `has_official` | INTEGER NOT NULL CHECK IN (0,1) | 是否有官方/UP 主上传字幕 |
| `has_ai` | INTEGER NOT NULL CHECK IN (0,1) | 是否有 B 站 AI 字幕 |
| `payload` | TEXT NOT NULL | 字幕全文 + lang 列表，参见 parsing 层 SubtitleData |

### 3.5 `article` —— 专栏

PK：`cvid`。列：`title` / `summary` / `pubdate_ms` / `view_count` / `like_count` / `reply` / `payload`。索引 `idx_article_pubdate ON article(pubdate_ms DESC)`。

### 3.6 `opus_post` —— Opus 长图文动态

PK：`opus_id`。仅 `pubdate_ms` 提为类型化列，正文 / 图片 URL 列表都在 `payload` 内。索引 `idx_opus_pubdate`。

### 3.7 `dynamic_event` —— 动态事件

PK：`dynamic_id`。`type` 列保存动态类型（如 `DYNAMIC_TYPE_AV` / `DYNAMIC_TYPE_DRAW`），便于按类型过滤。索引 `idx_dynamic_pubdate`。

### 3.8 `audio_transcription` —— ASR 转写

PK：`bvid`，FK CASCADE 到 `video`。

| 列 | 类型 | 说明 |
|----|------|------|
| `bvid` | TEXT PK | |
| `status` | TEXT NOT NULL CHECK IN (`'pending'`,`'running'`,`'success'`,`'failed'`,`'skipped'`) | 状态机 |
| `transcription_source` | TEXT | 后端名（`mock` / `mimo` / `whisper` / `subtitle` 等） |
| `transcript` | TEXT | 转写全文（`success` / `skipped` 时非空） |
| `audio_tokens` | INTEGER | LLM tokens 累计（计费用） |
| `seconds` | REAL | 实际处理音频秒数 |
| `cache_hits` | INTEGER | 复用缓存命中数 |
| `payload` | TEXT NOT NULL | 完整 ProcessingItem dict |
| `processed_at_ms` | INTEGER NOT NULL | 处理完成时间 |

`status='skipped'` 表示该视频走字幕直出而非真 ASR；`'failed'` 时 `transcript` 通常为 NULL，原因看 `stage_error`。

### 3.9 `image_asset` —— 图片缓存索引

PK：`url_hash`（即 url 的 md5，便于按 url 唯一去重）。

| 列 | 类型 | 说明 |
|----|------|------|
| `url_hash` | TEXT PK | md5(url) |
| `source_kind` | TEXT NOT NULL | `'video_cover'` / `'opus_image'` / `'article_image'` 等 |
| `source_id` | TEXT NOT NULL | 来源行的 PK（bvid / opus_id / cvid…） |
| `url` | TEXT NOT NULL | 原 URL |
| `file_path` | TEXT | `{uid}/images/<hash>.jpg` 相对路径，下载失败时可能为 NULL |
| `bytes` | INTEGER | 文件大小 |
| `status` | TEXT NOT NULL | 下载状态 |
| `downloaded_at_ms` | INTEGER NOT NULL | |

复合索引 `idx_image_source ON image_asset(source_kind, source_id)`：按来源反查图片资产。

## 4. Producer state 表（仅 debug 用）

> ⚠️ 这三张表 **不属于消费方契约**，可能在 minor 版本调整列。需要稳定的话只读 §3 / §5。

### 4.1 `stage_task`

PK：`stage`，CHECK IN (`'fetching'`,`'parsing'`,`'processing'`)。一个 stage 一行，覆盖式写入。`payload` JSON 内含 `endpoints` / `models` / `pipelines` 子状态、`failed_item_ids`、`item_progress` 等运行时细节，结构与旧版 task.json 同源。

### 4.2 `fetch_endpoint_state`

PK：`endpoint`。每端点一行，列：`status` / `retry_count` / `last_error_id` / `item_progress`（fan-out 计数）/ `progress`（分页游标）/ `updated_at_ms`。`item_progress` 与 `progress` 都是 JSON。

### 4.3 `stage_error`

`id INTEGER PK AUTOINCREMENT`，CHECK `stage IN ('fetching','processing')`。列：`endpoint` / `pipeline` / `item_type` / `item_id` / `error_type` / `message` / `retryable` / `detail` / `occurred_at_ms`。索引 `idx_stage_error_stage(stage, occurred_at_ms)`。

`parsing` 阶段不写错误行——parsing 失败粒度到 model，且整体只读 raw DB，错误直接抛回 `ParsingError`。

## 5. Views（manifest 替代品）

### 5.1 `video_full`

`video LEFT JOIN audio_transcription USING (bvid)`，列：

- 来自 video：`bvid` / `aid` / `title` / `description` / `cover_url` / `duration_s` / `pubdate_ms` / `view_count` / `danmaku` / `reply` / `favorite` / `coin` / `share` / `like_count` / `video_payload`（即 video.payload，重命名避免与 transcription.payload 冲突）/ `parsed_at_ms`
- 来自 audio_transcription：`transcription_status` / `transcription_source` / `transcript` / `audio_tokens` / `seconds` / `cache_hits` / `processed_at_ms`

LEFT JOIN：尚未处理的视频也会出现，转写列均 NULL。

### 5.2 `manifest_summary`

单行聚合视图，**取代旧的 `data/bili/manifest/{uid}.json` 文件**。列：

| 列 | 来源 |
|----|------|
| `uid` / `schema_version` / `last_fetched_at_ms` / `last_parsed_at_ms` / `last_processed_at_ms` | meta 表对应 key |
| `video_count` / `article_count` / `opus_count` / `dynamic_count` | 各内容表 `COUNT(*)` |
| `transcribed_count` | `audio_transcription WHERE status='success'` |
| `transcription_failed_count` | `audio_transcription WHERE status='failed'` |
| `total_audio_tokens` / `total_audio_seconds` / `total_cache_hits` | `COALESCE(SUM(...), 0)` over audio_transcription |
| `fetching_error_count` / `processing_error_count` | `stage_error WHERE stage=…` |

注意 meta 来源列保持 TEXT（因 `meta.value` 是 TEXT），数值列做 SUM/COUNT 后是 INTEGER/REAL；消费方拿到 `last_*_at_ms` 时按需 `int(row["last_fetched_at_ms"])`。

## 6. Raw DB（producer-private）

> 多数消费方 **不应** 打开此文件。仅在 re-parse 场景（不重新抓 B 站，只用本地缓存的 raw 响应重新跑 parsing）才需要它。

DDL：[raw_v1.sql](../bili_unit/_db/ddl/raw_v1.sql)。两张表：

### 6.1 `raw_payload`

复合 PK `(endpoint, item_id)`：

- `item_id = ''` —— endpoint 级响应（uid-level 端点），分页端点把合并后的 `{pages: [...]}` dict 整体存为单行
- `item_id = bvid / cvid / opus_id / dynamic_id / rlid / ...` —— item-level 端点的 fan-out 子项

列：`payload TEXT NOT NULL`、`fetched_at_ms INTEGER NOT NULL`。索引 `idx_raw_endpoint(endpoint, fetched_at_ms)`。

### 6.2 `fetch_progress`

PK：`endpoint`。列：`cursor` / `total` / `fetched` / `updated_at_ms`。作为 commit marker：runner 先写 `raw_payload` 再写 `fetch_progress`，崩溃落在中间时 progress 是旧值，下次 resume 从旧游标重新拉（payload 幂等覆盖）。

raw DB 的 `meta` 只放 `schema_version` 与 `uid`，没有最近时间戳——抓取时间戳跟在 `raw_payload.fetched_at_ms`。

## 7. SQL 食谱

### 7.1 列出某 uid 的视频，按发布时间倒序

```sql
SELECT bvid, title, view_count, duration_s, pubdate_ms
FROM video
ORDER BY pubdate_ms DESC
LIMIT 50;
```

`idx_video_pubdate` 直接走索引，无 sort。

### 7.2 取单个视频 + 完整 ASR 转写

```sql
SELECT bvid, title, transcription_status, transcription_source, transcript, seconds
FROM video_full
WHERE bvid = ?;
```

`transcription_status IS NULL` 表示 processing 还没跑过这个视频。

### 7.3 找出尚未转写的视频

```sql
SELECT v.bvid, v.title
FROM video v
LEFT JOIN audio_transcription t USING (bvid)
WHERE t.status IS NULL OR t.status = 'failed'
ORDER BY v.pubdate_ms DESC;
```

可以直接喂给下次 `process` run 的 `--only-bvids`。

### 7.4 一次拿到 manifest 摘要

```sql
SELECT * FROM manifest_summary;
```

返回单行，对应旧 `manifest/{uid}.json` 内容。

### 7.5 列出最近的 fetching 错误

```sql
SELECT endpoint, error_type, message, occurred_at_ms
FROM stage_error
WHERE stage = 'fetching'
ORDER BY id DESC
LIMIT 20;
```

`idx_stage_error_stage(stage, occurred_at_ms)` 加速。`detail` 列存 JSON，需要更多上下文时再 SELECT 出来。

## 8. JSON 列使用

所有 `payload` 列是 UTF-8 JSON，搭配 `sqlite3.Row` + `json.loads()` 即可：

```python
import json, sqlite3, bili_unit
conn = sqlite3.connect(bili_unit.db_path(uid))
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT payload FROM video WHERE bvid = ?", (bvid,)).fetchone()
data = json.loads(row["payload"])  # full VideoData.to_dict()
```

需要在 SQLite 侧做过滤 / 排序时用 JSON1（Python 自带 sqlite 默认编译开启）：

```sql
SELECT bvid, json_extract(payload, '$.owner.name') AS up_name
FROM video
LIMIT 5;
```

`json_extract` 支持 dot path 与 `$[0]` 索引；返回值是 TEXT/INTEGER/REAL/NULL，可直接用作 WHERE 条件。

## 9. WAL 文件

main DB 与 raw DB 都开启 `PRAGMA journal_mode = WAL`，运行时会附带：

- `{uid}.db-wal` / `{uid}.db-shm`
- `{uid}.raw.db-wal` / `{uid}.raw.db-shm`

约定：

- 由 SQLite 自动管理，正常关闭连接 / `PRAGMA wal_checkpoint(TRUNCATE)` 后清空。
- 备份时可忽略（先 `wal_checkpoint`）或与主文件一并打包；二者一致即可。
- `BiliCommand.delete_uid(uid)` 会同步删除 `-wal` / `-shm` 伴生文件，无需手动清理。

## 10. 稳定性承诺

- §3 内容表的列名 / 类型 / PK / FK / CHECK 约束，以及 §5 的 view 列，是 SDK SemVer 契约的一部分——只在 schema major bump 时破坏。
- 新增列、新增 view、新增 index 视为 minor 兼容变更，消费方应宽容（用列名而非列序读取）。
- §4 的 `stage_task` / `fetch_endpoint_state` / `stage_error` 内部列可在 minor 版本演化；如需跨版本读取，看 `payload` JSON 的 schema 注释。
- §6 的 raw DB 是内部布局，schema_version 独立，可随 minor bump 调整。
