# parsing_feature — B站用户数据解析层代码现状

> 记录 `bili_unit/parsing` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`

## 概述

parsing 层位于 fetching（raw dict）和 processing（结构化 result）之间，负责：

- **对象化**：把 fetching raw dict 筛选、归一并落盘为 6 个 typed dataclass（`UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`）。每个 model 一个目录，per-item 一个 JSON 文件。
- **图片下载**（可选）：并发下载头像、封面、动态/文章/图文图片到本地，回填 `*_local` 字段。

两条流水线在 `ParsingCommand.parse_uid()` 中顺序执行：先对象化（必经），后图片下载（CLI `--download-images` 标志触发）。

## 模块结构

```
bili_unit/parsing/
├── __init__.py            # DTO + 异常 + 状态枚举 + ParsingTaskValue
├── command.py             # ParsingCommand.parse_uid()
├── query.py               # ParsingQuery（task / typed object 只读视图）
├── data.py                # ParsingKeyMapper + ParsingDataStore（JsonKVStore wrapper）
├── keys.py                # 存储 key 生成
├── specs.py               # ParsingSpec registry；parse_uid 按 registry 分发
├── materializer.py        # ParsingMaterializer（per-model raw → typed 对象化）
├── _images.py             # ImageDownloader + ImageDownloadResult
└── models/
    ├── __init__.py        # get_parser() 注册表 + all_parser_names()
    ├── _refs.py           # SourceRef / CrossRefs / content_key_for_refs（跨 model 共享 id 类型）
    ├── up_profile.py      # UpProfile dataclass
    ├── video_detail.py    # VideoDetail + PageInfo / VideoStat / OwnerInfo
    ├── video_subtitle.py  # VideoSubtitle + SubtitlePage / SubtitleSegment
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

## Parsed Models

`ParsingCommand.parse_uid()` 通过 `bili_unit.parsing.specs.PARSING_SPECS` 分发 model。当前顺序为（`MODEL_ORDER`）：

```text
user_profile → video_work → video_subtitle → article_post → opus_post → dynamic_event
```

历史命名（`video_detail / article / opus / dynamic`）作为 `MODEL_ALIASES` 映射到新名，落盘目录已统一改为新名。

每个 model 暴露一个 `@property is_complete`，由当前 source_refs / cross_refs / 字段计算得出，并随 `to_dict()` 落盘为顶层字段（`from_dict()` 不读取持久化值，rebuild 后由 property 自动重算）。具体语义见各 model 段落末尾。

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

`is_complete`：3 个必填端点（`user_info` / `relation_info` / `up_stat`）都有 source_ref 时为 True；`overview_stat` 可选，不影响。

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

`is_complete`：source_refs 含 `video_detail` 且 `bvid` 非空 → True。

### VideoSubtitle（per-bvid）

来源端点：`video_subtitle`（item-level fan-out）

```python
@dataclass
class SubtitleSegment:
    start: float                 # body[*].from（秒，相对 page 起点）
    end: float                   # body[*].to
    content: str

@dataclass
class SubtitlePage:
    page_index: int
    cid: int
    lan: str                     # 选中的默认 lang；"" 表示无可用 body
    lan_doc: str
    segments: list[SubtitleSegment]

@dataclass
class VideoSubtitle:
    bvid: str
    pages: list[SubtitlePage]    # 仅包含至少一种 lang 命中 body 的 page
    available_languages: list[str]  # 跨 page 出现过 body 的 lang 全集（去重，发现序）

    @property
    def is_complete(self) -> bool: ...   # 所有 page 都至少有一种 lang
```

每个 page 的 lang 选择优先级（按 `lan` 字符串前缀匹配）：

```text
zh-CN > zh-Hans > zh-HK > ai-zh > en > 第一个有 body 的非空
```

`_fetch_error` 标记的项在选择前被排除；body 为空也跳过。当一个 page 的所有 lang 都不可用时，该 page 不会出现在 `pages` 里，`is_complete` 即为 `False`。

`is_complete` 是 processing audio runner 的字幕短路开关：完整字幕的 bvid 直接由字幕段拼出 audio result（`transcription_source: "subtitle"`），跳过 ASR。

无图片产物（`collect_image_jobs` 返回 `[]`）。

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

`is_complete`：source_refs 含 `article_detail` → True。仅有列表端点（`articles`）的 Article 缺正文 markdown，视为不完整。

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

`is_complete`：source_refs 含 `opus_detail` → True。仅有列表端点（`opus`）的 OpusPost 缺正文 markdown，视为不完整。

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

`is_complete`：动态没有 detail 端点，只要 `id_str` 非空就算完整。

## SourceRef / CrossRefs（跨 model 共享 id 类型）

定义在 `bili_unit/parsing/models/_refs.py`，每个 typed model 序列化时都会带这两类字段：

```python
@dataclass(frozen=True)
class SourceRef:
    endpoint: str   # 来源 fetching 端点名
    item_id: str    # 该端点下的 item id

@dataclass
class CrossRefs:
    cvid: str | None = None
    opus_id: str | None = None
    dynamic_id: str | None = None
    bvid: str | None = None
```

`to_dict()` 把它们落到 `_source_refs` / `_cross_refs` 顶层字段，`from_dict()` 时还原。
跨内容身份的关联（一篇文章既有 cvid 也是某条动态的 major、一个 opus 也是某条 video 的转载等）由 `_cross_refs` 承载，下游需要把 article / opus / dynamic / video 关联起来时按 `cvid` / `opus_id` / `dynamic_id` / `bvid` 自行 join。

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
├── video_work/
│   └── {bvid}.json                  # per-bvid（旧名 video_detail，已重命名）
├── video_subtitle/
│   └── {bvid}.json                  # per-bvid 字幕段（仅有 video_subtitle 端点时落盘）
├── article_post/
│   └── {cvid}.json                  # per-cvid（旧名 article，已重命名）
├── opus_post/
│   └── {opus_id}.json               # per-opus_id（旧名 opus，已重命名）
├── dynamic_event/
│   └── {dynamic_id}.json            # per-dynamic_id（旧名 dynamic，已重命名）
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
    "video_work": {"status": "SUCCESS", "count": 76},
    "video_subtitle": {"status": "SUCCESS", "count": 76},
    "article_post": {"status": "SUCCESS", "count": 1},
    "opus_post": {"status": "SUCCESS", "count": 54},
    "dynamic_event": {"status": "SUCCESS", "count": 868}
  },
  "images": {
    "total": 47, "ok": 43, "skipped": 2, "failed": 2,
    "failed_urls": ["https://..."]
  },
  "created_at": 1718000000000,
  "updated_at": 1718000001000,
  "failed_item_ids": []
}
```

`failed_item_ids` 在 `parse_uid` 收尾时聚合：parsing 没有 ErrorStore，只有 model 级别的 FAILED 状态，因此每条都是裸 model 名（如 `"article_post"`）。所有 model 都成功时为空列表。

## 状态枚举

`ParsingTaskStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / PARTIAL / FAILED

`ParsingModelStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / FAILED / SKIPPED

任务级状态由 model 状态聚合（ParsingCommand.parse_uid）：
- full 模式下全部 model count > 0 且 SUCCESS → SUCCESS
- full 模式下任一 model count == 0 或 FAILED → PARTIAL
- incremental 模式下 count == 0 但该 model 已有 parsed object → 视为跳过已有数据，不强制 PARTIAL
- 所有 model 失败 → PARTIAL（非 FAILED，保留容错语义）

## 解析模式

`parse_uid(uid, mode)` 支持两档：

| mode | 行为 |
|------|------|
| full（默认） | 重新解析所有项并覆盖写入 |
| incremental | 跳过已存在的 typed object（按 key 检查） |

## CLI

```bash
uv run python -m bili_unit parse <uid>                         # 6 个 model，full 模式
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

泛型方法（推荐）：

```python
async def get_item(uid: int, model: str, item_id: str) -> dict | None
async def list_items(uid: int, model: str) -> list[dict]
```

legacy 兼容方法（内部代理到泛型方法 + alias 映射）：

```python
async def get_task(uid: int) -> ParsingTaskDTO | None
async def list_tasks() -> list[dict]
async def get_user_profile(uid: int) -> dict | None
async def list_video_details(uid: int) -> list[dict]   # 读 video_work 目录
async def get_video_detail(uid: int, bvid: str) -> dict | None
async def list_video_subtitles(uid: int) -> list[dict]  # 读 video_subtitle 目录
async def get_video_subtitle(uid: int, bvid: str) -> dict | None
async def list_articles(uid: int) -> list[dict]         # 读 article_post 目录
async def get_article(uid: int, cvid: str) -> dict | None
async def list_opus(uid: int) -> list[dict]             # 读 opus_post 目录
async def get_opus(uid: int, opus_id: str) -> dict | None
async def list_dynamics(uid: int) -> list[dict]         # 读 dynamic_event 目录
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

测试位于 `bili_unit/tests/`，覆盖 6 个 model 单元测试（from_raw / to_dict / from_dict / image protocol / is_complete）、`ParsingSpec` registry、generic query / incremental、ParsingKeyMapper / DataStore CRUD、command + query 集成。无外部网络，离线可跑：`uv run pytest`。
