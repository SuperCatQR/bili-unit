# 重构计划：SQLite 持久化数据单元

> 状态：草案 / 待审 — 2026-06-15
>
> 决议性质：不可逆架构转向。以本文件锁定决策，落地后再升级为多份 ADR。

## 0. 目标与定位转变

**当前定位**：Bilibili 数据 SDK——同时是抓取器、解析器、ASR 处理器与对外 Python Query API。

**目标定位**：Bilibili 用户数据**持久化数据单元**。完全独立维护，被动暴露。
对消费者只暴露一件事：一个或多个 SQLite 数据库文件，消费者用 SQL 查。

非目标：
- 不为消费者提供 Python query 包装、DTO、聚合视图函数
- 不实现跨 uid 的查询
- 不实现 manifest 计算（替代为 SQL VIEW）

## 1. 已锁定决策（不可调整）

| 决策 | 选择 | 理由 |
|---|---|---|
| DB 引擎 | **SQLite** (WAL) | 单文件 = deliverable；无部署；JSON1/FTS5 在此体量足够 |
| 拓扑 | **一 uid 一库** | 匹配 data unit 语义；delete_uid = rm file；天然隔离 |
| raw 处理 | **独立分库** `{uid}.raw.db` | 主库只放结构化契约，消费者可只挂主库 |
| Schema 风格 | **混合**：常查字段提列 + `payload` JSON 列 | SQLite 惯用法；规避 model 演化导致的迁移噩梦 |
| 写侧 SDK | **保留** `session()` | host 应用通过 SDK 触发抓取/解析/处理 |
| 读侧 SDK | **完全删除** | 消费者直接 `sqlite3.connect(db_path)` |
| 已有 JSON 数据 | **不迁移** | `data/bili/` 视为可重新抓取；如需保留另写一次性脚本 |
| 错误记录 | 进主库 `*_errors` 表 | 减少 deliverable 数量；同 uid 不超过几十条，体积忽略 |

## 2. 磁盘布局（目标态）

```
data/bili/
├── {uid}.db              ← 消费者契约：6 类典型对象 + 任务/进度/错误
└── {uid}.raw.db          ← B站 64 endpoint 的原始 JSON（保留 re-parse 能力）

data/bili/{uid}/          ← 工作文件，DB 存路径引用
├── images/               ← 头像、封面、动态图片（download_images=true 时填充）
└── audio/{bvid}/         ← ASR 段缓存、ffmpeg 中间产物（成功后清理）
```

`delete_uid` ≡ 删两个 `.db` + `rmtree({uid}/)`。

## 3. Schema：`{uid}.db`（消费契约）

> 完整 DDL 落到 `bili_unit/_db/ddl/main_v1.sql`。下面是关键定义。

```sql
-- 元信息
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 必填 key: schema_version (=1), uid, created_at_ms,
--          last_fetched_at_ms, last_parsed_at_ms, last_processed_at_ms

-- 用户画像（每库一行）
CREATE TABLE user_profile (
    uid           INTEGER PRIMARY KEY,
    name          TEXT, sign TEXT, face_url TEXT,
    level         INTEGER, follower INTEGER, following INTEGER,
    payload       TEXT NOT NULL,             -- 完整 UpProfile JSON
    parsed_at_ms  INTEGER NOT NULL
);

-- 视频
CREATE TABLE video (
    bvid          TEXT PRIMARY KEY,
    aid           INTEGER,
    title         TEXT, description TEXT, cover_url TEXT,
    duration_s    INTEGER, pubdate_ms INTEGER,
    view_count INTEGER, danmaku INTEGER, reply INTEGER,
    favorite INTEGER, coin INTEGER, share INTEGER, like_count INTEGER,
    payload       TEXT NOT NULL,             -- VideoDetail 完整 JSON（含 owner/tags/rights/...）
    parsed_at_ms  INTEGER NOT NULL
);
CREATE INDEX idx_video_pubdate ON video(pubdate_ms DESC);

CREATE TABLE video_page (
    bvid       TEXT NOT NULL,
    page_no    INTEGER NOT NULL,
    cid        INTEGER, part TEXT, duration_s INTEGER,
    PRIMARY KEY (bvid, page_no),
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE video_subtitle (
    bvid          TEXT PRIMARY KEY,
    has_official  INTEGER NOT NULL CHECK (has_official IN (0,1)),
    has_ai        INTEGER NOT NULL CHECK (has_ai IN (0,1)),
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

CREATE TABLE article (
    cvid          TEXT PRIMARY KEY,
    title TEXT, summary TEXT, pubdate_ms INTEGER,
    view_count INTEGER, like_count INTEGER, reply INTEGER,
    payload TEXT NOT NULL, parsed_at_ms INTEGER NOT NULL
);

CREATE TABLE opus_post (
    opus_id       TEXT PRIMARY KEY,
    pubdate_ms    INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);

CREATE TABLE dynamic_event (
    dynamic_id    TEXT PRIMARY KEY,
    type          TEXT, pubdate_ms INTEGER,
    payload       TEXT NOT NULL,
    parsed_at_ms  INTEGER NOT NULL
);

-- 音频转录（processing 输出）
CREATE TABLE audio_transcription (
    bvid                  TEXT PRIMARY KEY,
    status                TEXT NOT NULL CHECK (status IN
                          ('pending','running','success','failed','skipped')),
    transcription_source  TEXT,            -- official / ai_subtitle / asr
    transcript            TEXT,            -- 全文（可空）
    audio_tokens          INTEGER, seconds REAL, cache_hits INTEGER,
    payload               TEXT NOT NULL,   -- ProcessingItemDTO 完整 JSON
    processed_at_ms       INTEGER NOT NULL,
    FOREIGN KEY (bvid) REFERENCES video(bvid) ON DELETE CASCADE
);

-- 图片资产清单（download_images=true 时填充）
CREATE TABLE image_asset (
    url_hash      TEXT PRIMARY KEY,        -- md5(url)
    source_kind   TEXT NOT NULL,           -- video.cover / opus.image / profile.face / dynamic.image
    source_id     TEXT NOT NULL,           -- bvid / opus_id / dynamic_id / uid
    url           TEXT NOT NULL,
    file_path     TEXT,                    -- 相对 {uid}/images/ 的路径
    bytes         INTEGER,
    status        TEXT NOT NULL,           -- ok / skipped / failed
    downloaded_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_image_source ON image_asset(source_kind, source_id);

-- ============================================================
-- 任务/进度/错误（生产侧状态，消费者一般不查，但放一起便于调试）
-- ============================================================

-- 三 stage 共用一张 task 表（用 stage 列区分）
CREATE TABLE stage_task (
    stage         TEXT PRIMARY KEY CHECK (stage IN ('fetching','parsing','processing')),
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL,           -- 各 stage 自己的 task 结构（endpoints/models/pipelines）
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

-- fetching endpoint 状态（uid:fetch:ep[:item] 的合并表）
CREATE TABLE fetch_endpoint_state (
    endpoint        TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error_id   INTEGER,
    item_progress   TEXT,                  -- JSON: {total, completed, failed}
    progress        TEXT,                  -- JSON: pagination cursor 等
    updated_at_ms   INTEGER NOT NULL
);

-- 错误（fetching + processing 合并；parsing 不产生错误记录）
CREATE TABLE stage_error (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage         TEXT NOT NULL,
    endpoint      TEXT,                    -- fetching only
    pipeline      TEXT, item_type TEXT, item_id TEXT, -- processing only
    error_type    TEXT NOT NULL,
    message       TEXT NOT NULL,
    retryable     INTEGER,                 -- 0/1/NULL(unknown)
    detail        TEXT,                    -- JSON
    occurred_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_stage_error_stage ON stage_error(stage, occurred_at_ms);

-- ============================================================
-- 视图：替代旧的 manifest / aggregates 计算
-- ============================================================

CREATE VIEW video_full AS
SELECT v.*,
       t.status AS transcription_status,
       t.transcription_source,
       t.transcript,
       t.audio_tokens, t.seconds, t.cache_hits
FROM video v
LEFT JOIN audio_transcription t USING (bvid);

CREATE VIEW manifest_summary AS
SELECT
    (SELECT value FROM meta WHERE key='uid') AS uid,
    (SELECT value FROM meta WHERE key='last_fetched_at_ms')   AS last_fetched_at_ms,
    (SELECT value FROM meta WHERE key='last_parsed_at_ms')    AS last_parsed_at_ms,
    (SELECT value FROM meta WHERE key='last_processed_at_ms') AS last_processed_at_ms,
    (SELECT COUNT(*) FROM video)               AS video_count,
    (SELECT COUNT(*) FROM article)             AS article_count,
    (SELECT COUNT(*) FROM opus_post)           AS opus_count,
    (SELECT COUNT(*) FROM dynamic_event)       AS dynamic_count,
    (SELECT COUNT(*) FROM audio_transcription WHERE status='success')
        AS transcribed_count,
    (SELECT COALESCE(SUM(audio_tokens),0) FROM audio_transcription)
        AS total_audio_tokens,
    (SELECT COALESCE(SUM(seconds),0)      FROM audio_transcription)
        AS total_audio_seconds,
    (SELECT COUNT(*) FROM stage_error WHERE stage='fetching')   AS fetching_errors,
    (SELECT COUNT(*) FROM stage_error WHERE stage='processing') AS processing_errors;
```

可选 FTS5：MVP 不做，等消费者明确需要再 ALTER 加。

## 4. Schema：`{uid}.raw.db`

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);  -- schema_version, uid

CREATE TABLE raw_payload (
    endpoint    TEXT NOT NULL,
    item_id     TEXT NOT NULL DEFAULT '',  -- '' = endpoint-level；fanout 时是 bvid/cvid/...
    payload     TEXT NOT NULL,             -- 整个响应 JSON
    fetched_at_ms INTEGER NOT NULL,
    PRIMARY KEY (endpoint, item_id)
);

CREATE TABLE fetch_progress (
    endpoint    TEXT PRIMARY KEY,
    cursor      TEXT,
    total       INTEGER, fetched INTEGER,
    updated_at_ms INTEGER NOT NULL
);
```

> 把 fetch progress 留在 raw 库的理由：progress = "raw 抓到哪了" 的指针，跟 raw payload 同生命周期；
> 主库的 `fetch_endpoint_state` 是状态机视图（status/retry/error），不重复 raw 指针。

## 5. 模块层级 Diff

### 5.1 删除（一次性）

完整删除以下文件/包：

```
bili_unit/query/                      ← 整包
bili_unit/fetching/query.py
bili_unit/parsing/query.py
bili_unit/processing/query.py
bili_unit/_aggregates.py
bili_unit/_manifest.py                ← 计算/读写都没了；改 SQL VIEW
bili_unit/_storage/                   ← 整包替换
```

公共 API 删除（在 `bili_unit/__init__.py` 中）：

```
BiliQuery, VideoFullDTO, VideoSummaryDTO,
TaskDTO, EndpointDTO, FetchingErrorDTO, EndpointStatus, TaskStatus,
ParsingTaskDTO, ParsingTaskStatus, ParsingModelDTO, ParsingModelStatus,
ParsingImageDTO,
ProcessingTaskDTO, ProcessingTaskStatus, ProcessingPipelineDTO,
ProcessingPipelineStatus, ProcessingItemDTO, ProcessingItemStatus,
ProcessingErrorDTO,
```

CLI 命令删除（`bili_unit/__main__.py`）：

```
query, list-uids, video-full, manifest
```

> `delete-uid` 保留（仍然是写侧操作）。
> `list-uids` 用户用 `ls data/bili/*.db` 即可。
> `manifest` 用户用 `sqlite3 data/bili/{uid}.db "SELECT * FROM manifest_summary"` 即可。

### 5.2 新增

```
bili_unit/_db/
├── __init__.py              # 公共导出: open_main(uid), open_raw(uid), db_paths(uid)
├── paths.py                 # uid → 三个路径（main / raw / workdir）
├── connection.py            # async wrapper 基于 aiosqlite；WAL pragma；schema_version 校验
├── ddl/
│   ├── main_v1.sql          # ↑ §3
│   ├── raw_v1.sql           # ↑ §4
│   └── __init__.py          # _read_ddl(name) 工具
└── migrate.py               # 检查 meta.schema_version；版本不匹配则报错（v1 阶段不做自动迁移）

tools/
└── migrate_jsonkv_to_sqlite.py   # 一次性脚本：扫描旧 data/bili/ 目录灌入新库（可选用）
```

### 5.3 改造（保接口形态，换底层）

每个 stage 的 `data.py` 整个重写——不再继承 `KvDataStore`，改为持有连接、暴露**语义化**写入方法：

#### `bili_unit/fetching/data.py` → `FetchingStore`

```python
class FetchingStore:
    def __init__(self, uid: int, settings: BiliSettings): ...
    async def open(self): ...   # opens main + raw connections
    async def close(self): ...

    # raw payload writes (raw db)
    async def save_raw_payload(self, endpoint: str, item_id: str, payload: dict): ...
    async def save_raw_page_and_progress(
        self, endpoint: str, item_id: str, payload: dict,
        progress: ProgressRecord,
    ): ...   # 单事务

    # state writes (main db, stage_task + fetch_endpoint_state)
    async def init_task(self, endpoints: list[str]): ...
    async def update_endpoint_state(
        self, endpoint: str, *, status: str,
        retry_count: int = 0, last_error_id: int | None = None,
        item_progress: dict | None = None,
    ): ...
    async def mark_task_status(self, status: str): ...

    # incremental-mode reads (the only reads runners need)
    async def get_endpoint_status(self, endpoint: str) -> str | None: ...
    async def get_progress(self, endpoint: str) -> dict | None: ...
    async def list_completed_items(self, endpoint: str) -> set[str]: ...
    async def list_failed_items(self, endpoint: str) -> list[str]: ...

    # error sink (main db, stage_error)
    async def record_error(self, ...) -> int: ...
```

> 关键：runner 层的"incremental check"逻辑过去靠扫 `list_prefix("uid:N:fetch:ep:")`，
> 现在变成 `list_completed_items(endpoint)` 直接出 `SELECT item_id FROM raw_payload WHERE endpoint=?`。
> 这个 API 变化要把 runner 里 ~25 处 `data.put / data.get / data.list_prefix` 调用站点改写。

#### `bili_unit/parsing/data.py` → `ParsingStore`

```python
class ParsingStore:
    async def save_user_profile(self, profile: UpProfile): ...
    async def save_video(self, video: VideoDetail): ...
    async def save_video_subtitle(self, sub: VideoSubtitle): ...
    async def save_article(self, art: Article): ...
    async def save_opus(self, opus: OpusPost): ...
    async def save_dynamic(self, dyn: DynamicPost): ...
    async def save_image_asset(self, asset: ImageAssetRecord): ...

    async def get_existing_item_ids(self, model: str) -> set[str]: ...
    async def update_task_model_status(self, model, status, count): ...
    async def update_task_images(self, summary): ...
    async def mark_task_status(self, status): ...
```

每个 `save_*` 内部把 dataclass.to_dict() 拆成「列字段 + payload JSON」插入。

#### `bili_unit/processing/data.py` → `ProcessingStore`

```python
class ProcessingStore:
    async def save_audio_transcription(
        self, bvid: str, status: str, source: str | None,
        transcript: str | None, cost: AudioCost, payload: dict,
    ): ...
    async def get_audio_status(self, bvid: str) -> str | None: ...
    async def list_failed_audio_bvids(self) -> list[str]: ...

    async def update_task_pipeline(self, pipeline, status, items): ...
    async def record_error(self, ...) -> int: ...
```

#### Command 层基本不动

`bili_unit/command/__init__.py` 只删 `_persist_manifest`（manifest 现在是 VIEW，不需要写）：

```python
class BiliCommand:
    async def fetch(self, uid, endpoints=None, mode="incremental") -> CommandResult: ...
    async def parse(self, uid, mode="full", download_images=False) -> ParsingCommandResult: ...
    async def process(self, uid, mode="incremental", **kw) -> ProcessingCommandResult: ...
    async def delete_uid(self, uid) -> dict: ...   # 改为：close conn + rm 两个 .db + rmtree workdir
    async def close(self): ...
```

`CommandResult` / `ParsingCommandResult` / `ProcessingCommandResult` 写侧 DTO 保留（不属于读侧）。

### 5.4 公共 API 终态（`bili_unit/__init__.py`）

```python
__all__ = [
    "BiliCommand",                # 写侧门面
    "BiliSettings", "get_settings", "reload_settings",
    "CredentialProvider",
    "session",                    # async ctx → BiliCommand
    "db_path",                    # uid → Path  (主库)
    "raw_db_path",                # uid → Path  (raw 库)

    # 写侧 result DTO（不属于读侧；给 host 应用看 fetch/parse/process 返回值）
    "CommandResult", "ParsingCommandResult", "ProcessingCommandResult",

    # 异常
    "FetchingError", "ParsingError", "ProcessingError", "AudioError",

    "__version__",
]

@asynccontextmanager
async def session(settings=None, *, asr_backend_override=None,
                  credential_provider=None) -> AsyncIterator[BiliCommand]:
    # 注意只返一个值 cmd，不再返 (cmd, qry)
    ...

def db_path(uid: int, settings: BiliSettings | None = None) -> Path: ...
def raw_db_path(uid: int, settings: BiliSettings | None = None) -> Path: ...
```

`session()` 签名改 = SemVer **major bump**（1.x → 2.0）。

## 6. 实施阶段

每个阶段都是一个 PR，全做完才 merge 到 main。中间分支可破坏；不再走 strangler，因为这是 deliberate breaking refactor。

### Phase 1 — DDL 与连接层骨架
- 写 `bili_unit/_db/`（paths / connection / ddl 文件 / migrate stub）
- 单元测试：`test_db_open.py` 验证开新库会跑 DDL、版本号写入 meta、重开能跳过 DDL
- 不动其他模块

### Phase 2 — Stores 重写
- 三个 `*/data.py` 整文件重写，不再继承 `KvDataStore`
- 单元测试：`test_fetching_store_sqlite.py` 等，针对每个 `save_*` / `get_*`
- 删除 `bili_unit/_storage/`（彻底）

### Phase 3 — Runner 适配
- fetching runner：把 `data.put("uid:N:fetch:ep:item", payload)` → `store.save_raw_payload(ep, item, payload)`
- 同理 parsing / processing
- 删除所有 key 字符串拼接（在 `*/keys.py` 里）；`keys.py` 整文件删
- 既有 runner 单测大改：fixture 给 `FetchingStore(uid)`，断言 `SELECT` 出来的行数/字段

### Phase 4 — Command 简化 & API 收紧
- `BiliCommand`：删 `_persist_manifest`、`delete_uid` 改为删两个 db 文件 + `rmtree`
- `bili_unit/__init__.py`：删 `BiliQuery` 导入、读侧 DTO 导入；`session()` 改返单值；新增 `db_path()`
- 删 `bili_unit/_aggregates.py`、`bili_unit/_manifest.py`、`bili_unit/query/`、三个 `*/query.py`

### Phase 5 — CLI 收口
- `bili_unit/__main__.py`：删 `query` / `list-uids` / `video-full` / `manifest` 子命令
- 保 `fetch` / `parse` / `process` / `delete-uid` / `login` / `init-mimo`
- 这四个子命令的 handler 改为只接 cmd 不接 qry

### Phase 6 — 测试大改
- 总数 ~70 测试文件需要扫一遍。具体策略见 §7
- 删除：`test_storage_kv_contract.py`、所有跟 manifest/query 相关测试（test_sdk_session 中读侧部分、test_cli_subset 的 query 命令、test_manifest.py）
- 新增：每个 store 一份契约测试 + SQL view 等价性测试

### Phase 7 — 迁移脚本（可选）
- `tools/migrate_jsonkv_to_sqlite.py`：读旧 `data/bili/fetching/data/{uid}/...` 灌入新 `data/bili/{uid}.db`
- 不进 packaging；放 `tools/`，需要的人手动跑
- 跑后人工验：随机抽 5 uid 跑等价检查（视频数、转录数、错误数对得上）

### Phase 8 — 文档
- 删 `docs/api.md`，新写 `docs/schema.md`：DDL 全文 + 字段语义表 + 4-5 个常用 SQL recipe（"列出某 uid 所有视频+转录"、"查 view>10000 的视频"、"列 ASR 失败的 bvid"）
- README 改写"用法/Embedding"两节
- 写 ADR：`0005-sqlite-as-deliverable.md`、`0006-drop-query-api.md`、`0007-one-uid-per-db.md`

## 7. 测试改写策略

按 ~70 测试统计的处理类别：

| 类别 | 数量 | 处理 |
|---|---|---|
| 直接 poke `KvDataStore` 内部 | ~15 | 改为 `conn.execute("SELECT ...")` 断言 |
| 通过 `Query` 验状态 | ~30 | 改为开主库连接做 SQL 断言 |
| 测 runner 逻辑 (mock 网络/ASR) | ~20 | fixture 换成 `FetchingStore(uid, tmp_settings)`；断言改 SQL |
| 测 manifest / aggregates | ~5 | **删** |
| 测 CLI subset | ~3 | 删 query/manifest 子命令相关用例 |

新增 conftest helper：
```python
def assert_row(conn, table, where: dict, expect: dict): ...
def count(conn, table, **where) -> int: ...
def fetch_one(conn, sql, *params) -> dict | None: ...
```

让旧测试改写量降到平均每文件 5-10 行。

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Dialectica 还在用 `BiliQuery` | 读侧切了直接 break | 同步起 PR，让 Dialectica 改用 `sqlite3.connect(bili_unit.db_path(uid))`；本仓库 PR 必须等 Dialectica 那边的 PR 一起 merge |
| 历史 `data/bili/` 数据不可读 | 用户损失抓取成果 | (a) ADR 标明 v2 不兼容 v1；(b) 提供 `tools/migrate_jsonkv_to_sqlite.py` 兜底 |
| `aiosqlite` vs 同步 sqlite3 | runner 是 async 框架；同步 IO 会 block event loop | 用 `aiosqlite`（已在 ecosystem，pure python）；写操作走单连接串行（每 uid 单写者，本来就不并发） |
| 单 uid 库的并发抓+处理 | 同 uid 同时跑 fetch & process 可能撞库 | WAL + 单连接 per stage；同 uid 不允许两个 cmd 并发（命令层加 advisory lock：`{uid}.db.lock` 文件） |
| FTS5 没了消费者用啥全文搜 | 转录文本搜索倒退 | MVP 不做；schema 留扩展位（添加 FTS5 不需要重建主表，`CREATE VIRTUAL TABLE ... USING fts5` 单独建即可） |
| schema_version 演化 | v1.1 改 schema 怎么办 | `_db/migrate.py` 的占位结构存在，v1 阶段强制版本相等才打开；v2 时再设计 ALTER 流程（YAGNI） |

## 9. 不做的事

- **跨 uid 查询封装** — 消费者要跨 uid 自己 `ATTACH DATABASE`
- **Python ORM** — 不引入 SQLAlchemy；写侧手写 SQL，读侧消费者写 SQL
- **schema 自动从 dataclass 生成** — 列定义手维护；payload JSON 列吸收变化
- **HTTP/RPC 暴露** — 完全不在范围内
- **跨进程并发抓取同 uid 的协调** — 用户自己保证不并发，库不做分布式锁

## 10. 工作量估算

| Phase | 文件 LoC 估计 | 时间 |
|---|---|---|
| 1. DB 骨架 | +400 | 0.5d |
| 2. Stores | +900 / -700 | 1.5d |
| 3. Runner 适配 | -200 ±300 | 1d |
| 4. Command/API 收紧 | -500 | 0.5d |
| 5. CLI 收口 | -150 | 0.25d |
| 6. 测试改写 | ~2000 行触动 | 2d |
| 7. 迁移脚本 | +300 | 0.5d |
| 8. 文档 | +600 / -400 | 1d |
| **合计** | **净减 ~500 LoC** | **~7 工作日** |

净减 LoC 因为：删 query 包 + 三个 query.py + 读侧 DTO + manifest compute + storage schema 引擎，
比新增的 SQLite store 多。

## 11. 决策点（已锁定 2026-06-15）

- [x] **错误表 → 进主库 `stage_error` 表**
- [x] **rate_limit → 进程内内存**（不持久化；重启 cooldown 重算）
- [x] **`session()` → 返单值 `BiliCommand`**（不再是 tuple；major bump）

---

按 §6 顺序起 PR，每 PR 单独提审。
