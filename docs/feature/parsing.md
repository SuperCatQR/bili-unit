# parsing_feature — B站用户数据解析层代码现状

> 记录 `bili_unit/parsing` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`
> 实施计划：`parsing-plan.md`

## 概述

parsing 层位于 fetching（raw dict）和 processing（结构化 result）之间，负责：

- **对象化**：把 28 个端点的 raw dict 归纳为 5 个 typed dataclass（`UpProfile` / `VideoDetail` / `Article` / `OpusPost` / `DynamicPost`），JSON 落盘。
- **图片下载**（可选）：并发下载头像、封面、动态/文章/图文图片到本地，回填 `*_local` 字段。

两条流水线在 `ParsingCommand.parse_uid()` 中顺序执行：先对象化（必经），后图片下载（CLI `--download-images` 标志触发）。

## 模块结构

```
bili_unit/parsing/
├── __init__.py            # DTO + 异常 + 状态枚举 + ParsingTaskValue
├── command.py             # ParsingCommand.parse_uid()
├── query.py               # ParsingQuery（task / typed object 只读视图）
├── data.py                # ParsingKeyMapper + ParsingDataStore（JsonKVStore wrapper）
├── env.py                 # ParsingEnv (pydantic-settings)
├── keys.py                # 存储 key 生成
├── _images.py             # ImageDownloader + ImageDownloadResult
└── models/
    ├── __init__.py        # get_parser() 注册表 + all_parser_names()
    ├── up_profile.py      # UpProfile dataclass
    ├── video_detail.py    # VideoDetail + PageInfo / VideoStat / OwnerInfo
    ├── article.py         # Article + ArticleStats / ReadListMeta
    ├── opus.py            # OpusPost + OpusStats
    └── dynamic.py         # DynamicPost + ForwardedDynamic
```

import 边界：
```text
command → models (via get_parser), _images, data, keys, DTO
query → data, keys, DTO
models → fetching.query (TYPE_CHECKING only), data (TYPE_CHECKING only)
_images → aiohttp
data → _storage (JsonKVStore), DTO
env → 不 import data/command/query
```

parsing 通过 `bili_unit.fetching.query.Query` 只读访问 fetching 数据；不直接访问 fetching 的 DataStore/ErrorStore，也不写回 fetching。

## 5 个 Typed Model

### UpProfile（per-uid 单文件）

来源端点：`user_info`（必填）、`relation_info`（必填）、`up_stat`（必填）、`overview_stat`（可选）

```python
@dataclass
class UpProfile:
    mid: int | None              # user_info.mid
    name: str                    # user_info.name
    sex: str                     # user_info.sex
    sign: str                    # user_info.sign
    avatar: str                  # user_info.face
    birthday: str                # user_info.birthday
    level: int                   # user_info.level
    jointime: int                # user_info.jointime
    vip: dict                    # {type, status, label} — 从 user_info.vip 归一化
    social: dict                 # {following, follower, whisper, black} — relation_info
    stats: dict                  # {archive_view, article_view, likes} — up_stat
    overview: dict | None        # {video_count, article_count, opus_count} — overview_stat（可选）
    avatar_local: str = ""       # 图片下载后填充：images/avatar.jpg
```

图片：`avatar` → `"avatar.jpg"`（1 张/uid）。

### VideoDetail（per-bvid）

来源端点：`video_detail`（item-level fan-out）

```python
@dataclass
class VideoDetail:
    bvid: str                    # info.bvid
    aid: int | None              # info.aid
    title: str                   # info.title
    desc: str                    # info.desc
    duration: int                # info.duration
    ctime: int | None            # info.ctime
    pubdate: int | None          # info.pubdate
    pic: str                     # info.pic（封面 URL）
    pages: list[PageInfo]        # [{cid, part, duration, dimension, first_frame}]
    tags: list[str]              # tags[*].tag_name
    stat: VideoStat              # {view, danmaku, reply, favorite, coin, share, like}
    owner: OwnerInfo             # {mid, name, face}
    rights: dict                 # info.rights
    subtitle: dict               # info.subtitle
    label: dict                  # info.label
    pic_local: str = ""          # 图片下载后填充：images/video/{bvid}_cover.jpg
```

嵌套 dataclass：`PageInfo`（cid / part / duration / dimension / first_frame）、`VideoStat`（7 个 int 指标）、`OwnerInfo`（mid / name / face）。

图片：`pic` → `"video/{bvid}_cover.jpg"`（1 张/bvid）。

### Article（per-cvid）

来源端点：`articles`（必填）、`article_detail`（可选）、`article_list_detail`（可选）

```python
@dataclass
class Article:
    id: str                      # str(articles list item id)
    title: str                   # 列表级 title
    summary: str                 # 列表级 summary
    image_urls: list[str]        # image_urls + origin_image_urls + banner_url（去重合并）
    stats: ArticleStats          # {view, favorite, like, reply, share, coin}
    ctime: int | None            # 列表级 ctime
    lists: list[ReadListMeta]    # [{rlid, name}] — 从 article_list_detail 反索引
    markdown: str                # article_detail.markdown（可选；缺失时 ""）
    content_json: list           # article_detail.content_json（可选；缺失时 []）
    image_locals: list[str]      # 图片下载后填充：images/article/{cvid}_{i:02d}.jpg
```

嵌套 dataclass：`ArticleStats`（6 个 int 指标）、`ReadListMeta`（rlid / name）。

辅助函数：`_dedup_urls(*sources)` 多源 URL 去重合并；`_build_cvid_to_lists(list_details)` 反索引 cvid → 文集归属。

图片：`image_urls` → `[("article/{cvid}_{i:02d}.jpg") for i, url in enumerate(image_urls)]`（1~10 张/cvid）。

### OpusPost（per-opus_id）

来源端点：`opus`（必填）、`opus_detail`（可选）

```python
@dataclass
class OpusPost:
    id: str                      # str(opus list item opus_id)
    title: str                   # 列表级 title
    summary: str                 # 列表级 summary（fallback: modules 内 opus.summary.text）
    cover: str                   # 列表级 cover
    jump_url: str                # 列表级 jump_url
    stats: OpusStats             # {view, favorite, like, reply, share, coin}
    ctime: int | None            # pub_time（fallback: ctime）
    list_images: list[str]       # modules.module_dynamic.major.opus.pics[*].url
    markdown: str                # opus_detail.markdown（可选）
    detail_images: list[dict]    # opus_detail.images（可选；[{url, width, height}]）
    cover_local: str = ""        # 图片下载后填充：images/opus/{id}_cover.jpg
    image_locals: list[str]      # 正文图片本地路径列表
```

嵌套 dataclass：`OpusStats`（6 个 int 指标）。

辅助函数：`_modules_dict(raw)` 归一化 modules 块（dict / list 双形态）；`_extract_opus_summary_text(modules)` 深层路径提取；`_extract_opus_pic_urls(modules)` 图片 URL 提取。

图片：cover → `"opus/{id}_cover.jpg"` + detail_images（优先）或 list_images → `"opus/{id}_{i:02d}.jpg"`。

### DynamicPost（per-dynamic_id）

来源端点：`dynamics`

```python
@dataclass
class DynamicPost:
    id_str: str                  # 动态稳定字符串 ID
    type: str                    # DYNAMIC_TYPE_DRAW / FORWARD / AV / ARTICLE / WORD / ...
    text: str                    # modules.module_dynamic.desc.text
    timestamp: int | None        # modules.module_author.pub_ts
    major: dict                  # {type, ...} — 结构因 major.type 而异
    forwarded: ForwardedDynamic | None  # FORWARD 类型时 orig 递归展开
    image_urls: list[str]        # 从 major 提取的所有图片 URL
    image_locals: list[str]      # 图片下载后填充：images/dynamic/{id_str}_{i:02d}.jpg
```

嵌套 dataclass：`ForwardedDynamic`（id_str / type / text / timestamp / major）。

辅助函数：`_flatten_dynamic(d)` 展平 raw dict；`_normalise_major(major_raw)` 归一化 major 块（DRAW / ARTICLE / ARCHIVE / OPUS 四种类型）；`_extract_image_urls_from_major(major)` 按 type 提取图片 URL。

图片来源因 `major.type` 而异：

| major.type | URL 来源 |
|---|---|
| `MAJOR_TYPE_DRAW` | `draw.items[*].src` |
| `MAJOR_TYPE_ARTICLE` | `article.covers` |
| `MAJOR_TYPE_ARCHIVE` | `archive.cover` |
| `MAJOR_TYPE_OPUS` | `opus.pics[*].url` |
| `FORWARD` | 原动态 `orig` 中递归提取（去重合并） |

## 图片协议

每个 model 实现两个方法，构成统一的图片下载协议（duck typing，无显式 Protocol 基类）：

```python
def collect_image_jobs(self, uid: int) -> list[tuple[str, str]]:
    """返回 [(url, dest_rel), ...] 供 ImageDownloader 下载。"""

def apply_image_results(self, results: list[ImageDownloadResult]) -> None:
    """下载完成后回填 *_local 字段。"""
```

| Model | collect_image_jobs | apply_image_results 回填字段 |
|---|---|---|
| UpProfile | `[(avatar, "avatar.jpg")]` | `avatar_local` |
| VideoDetail | `[(pic, "video/{bvid}_cover.jpg")]` | `pic_local` |
| Article | `[(url, "article/{cvid}_{i:02d}.jpg") ...]` | `image_locals` |
| OpusPost | `[(cover, "opus/{id}_cover.jpg")]` + 正文图片 | `cover_local` + `image_locals` |
| DynamicPost | `[(url, "dynamic/{id}_{i:02d}.jpg") ...]` | `image_locals` |

## ImageDownloader（`_images.py`）

并发图片下载器，设计参考 `processing/audio/_downloader.py`。

```python
@dataclass
class ImageDownloadResult:
    url: str
    local_path: str           # 相对于 images/ 目录的路径
    status: str               # "ok" | "skipped" | "failed"
    error: str = ""

class ImageDownloader:
    def __init__(self, base_dir: Path, concurrency: int = 8, timeout: float = 30.0): ...
    async def download_one(self, url: str, dest_rel: str) -> ImageDownloadResult: ...
    async def download_many(self, jobs: list[tuple[str, str]]) -> list[ImageDownloadResult]: ...
```

关键行为：
- **跳过已存在**：目标文件存在且 `size > 0` 时返回 `"skipped"`
- **并发控制**：`asyncio.Semaphore(concurrency)`
- **HTTP headers**：`Referer: https://www.bilibili.com`、`User-Agent: Chrome`
- **扩展名推断**：URL 路径提取 + Content-Type 兜底（`_CONTENT_TYPE_EXT` 映射）
- **失败隔离**：单张失败返回 `"failed"` + error，不影响其他图片
- **I/O 隔离**：文件写入 `asyncio.to_thread`

## 磁盘布局

```
data/bili/parsing/{uid}/
├── task.json                        # 解析状态（含 images 下载进度）
├── user_profile/
│   └── {uid}.json                   # per-uid 单文件
├── video_detail/
│   └── {bvid}.json                  # per-bvid
├── article/
│   └── {cvid}.json                  # per-cvid
├── opus/
│   └── {opus_id}.json               # per-opus_id
├── dynamic/
│   └── {dynamic_id}.json            # per-dynamic_id
└── images/                          # 图片本地存储（--download-images 触发）
    ├── avatar.jpg
    ├── video/{bvid}_cover.jpg
    ├── article/{cvid}_{i:02d}.jpg
    ├── opus/{id}_cover.jpg + {id}_{i:02d}.jpg
    └── dynamic/{id_str}_{i:02d}.jpg
```

## Key 方案

```
uid:{uid}:task                          → {uid}/task.json
uid:{uid}:parse:{model}:{item_id}       → {uid}/{model}/{item_id}.json
```

与 fetching 的 `uid:{uid}:task` 同名但物理隔离（不同目录路径），不冲突。

### task.json 形状

```json
{
  "uid": 3494380472109167,
  "status": "SUCCESS",
  "models": {
    "user_profile": {"status": "SUCCESS", "count": 1},
    "video_detail": {"status": "SUCCESS", "count": 76},
    "article": {"status": "SUCCESS", "count": 1},
    "opus": {"status": "SUCCESS", "count": 54},
    "dynamic": {"status": "SUCCESS", "count": 868}
  },
  "images": {
    "total": 47, "ok": 43, "skipped": 2, "failed": 2,
    "failed_urls": ["https://..."]
  },
  "created_at": 1718000000000,
  "updated_at": 1718000001000
}
```

## 状态枚举

`ParsingTaskStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / PARTIAL / FAILED

`ParsingModelStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / FAILED / SKIPPED

任务级状态由 model 状态聚合（ParsingCommand.parse_uid）：
- 全部 model count > 0 且 SUCCESS → SUCCESS
- 任一 model count == 0 或 FAILED → PARTIAL
- 所有 model 失败 → PARTIAL（非 FAILED，保留容错语义）

## 解析模式

`parse_uid(uid, mode)` 支持两档：

| mode | 行为 |
|------|------|
| full（默认） | 重新解析所有项并覆盖写入 |
| incremental | 跳过已存在的 typed object（按 key 检查） |

## CLI

```bash
uv run python -m bili_unit parse <uid>                         # 全部 5 个 model，full 模式
uv run python -m bili_unit parse <uid> -i                      # 解析 + 下载图片
uv run python -m bili_unit parse <uid> -m incremental          # 增量模式
```

## 装配

parsing 通过顶层 `bili_unit.assemble()` 统一装配：

```python
from bili_unit import assemble
cmd, qry, _data, _error = await assemble()
await cmd.fetch(uid)
await cmd.parse(uid)                       # 解析
await cmd.parse(uid, download_images=True) # 解析 + 下载图片
await cmd.process(uid)                     # 处理
task = await qry.parsing.get_task(uid)
profile = await qry.parsing.get_user_profile(uid)
videos = await qry.parsing.list_video_details(uid)
```

### Command 接口

```python
async def parse_uid(
    uid: int,
    mode: str = "full",              # "full" | "incremental"
    download_images: bool = False,   # 是否下载图片
) -> ParsingCommandResult
```

### Query 接口

```python
async def get_task(uid: int) -> ParsingTaskDTO | None
async def list_tasks() -> list[dict]
async def get_user_profile(uid: int) -> dict | None
async def list_video_details(uid: int) -> list[dict]
async def get_video_detail(uid: int, bvid: str) -> dict | None
async def list_articles(uid: int) -> list[dict]
async def get_article(uid: int, cvid: str) -> dict | None
async def list_opus(uid: int) -> list[dict]
async def get_opus(uid: int, opus_id: str) -> dict | None
async def list_dynamics(uid: int) -> list[dict]
async def get_dynamic(uid: int, dynamic_id: str) -> dict | None
```

## 异常层级

```
ParsingError
├── ModelParseError        # raw dict → typed object 失败
├── DataError              # 存储 / 序列化失败
└── ImageDownloadError     # 图片下载失败（未被 _images.py 内部隔离时抛出）
```

单张图片下载失败不阻塞整体流程；失败 URL 记录在 task.json 的 `images.failed_urls` 中。单个 model 解析失败标记该 model 为 FAILED，不影响其他 model。

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_PARSING_DATA_DIR | data/bili/parsing | 解析结果存储目录 |
| BILI_PARSING_IMAGE_CONCURRENCY | 8 | 图片下载并发数 |
| BILI_PARSING_IMAGE_TIMEOUT | 30 | 单张图片下载超时（秒） |

## 测试状态

- 93 个 parsing 测试全部通过（含在 419 总数内）
- ruff lint 全部通过
- 无外部网络 / API 依赖；测试可在离线环境运行

### 测试矩阵

```
test_parsing_models.py       5 个 model 单元测试（60 tests：from_raw / to_dict / from_dict / image protocol / edge cases）
test_parsing_data.py         ParsingKeyMapper + ParsingDataStore 单元测试（13 tests）
test_parsing_command.py      ParsingCommand + ParsingQuery 集成测试（20 tests）
```

集成测试覆盖：
- parse_uid 正常流程（5 个 model 全部 SUCCESS）
- parse_uid 部分 model 返回 0（PARTIAL 状态）
- parse_uid 单个 model 抛异常（FAILED + PARTIAL）
- parse_uid 带 download_images=True（_download_images 被调用）
- parse_uid 带 download_images 失败（不影响整体状态）
- ParsingQuery 所有 typed object accessor（get / list / none）
- ParsingQuery task DTO 含/不含 images 块
- ParsingKeyMapper key ↔ path 双向映射
- ParsingDataStore CRUD + 原子 helper（update_task_model_status / update_task_images）
