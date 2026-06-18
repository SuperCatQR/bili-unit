# parsing_feature — B站用户数据解析层代码现状

> 记录 `bili_unit/parsing` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`
> 对应数据契约：`docs/schema.md`

## 概述

parsing 层位于 fetching（raw payload）和 processing（结构化结果）之间，负责：

- **对象化**：把 fetching 抓到的 raw dict 筛选、归一为 6 个 typed dataclass（`UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`），并 `INSERT OR REPLACE` 写入主 DB 的对应内容表（`user_profile` / `video` + `video_page` / `video_subtitle` / `article` / `opus_post` / `dynamic_event`）。每行携带常查的列字段 + 完整 `to_dict()` 落到 `payload TEXT`。
- **图片下载**（可选）：并发下载头像、封面、动态/文章/图文图片，默认把图片二进制写入主 DB 的 `image_asset.data`，并回写每个对象的 `*_local` 逻辑路径字段。

两条流水线在 `ParsingCommand.parse_uid()` 中顺序执行：先对象化（必经），后图片下载（CLI `-i / --download-images` 标志触发）。

表层定义见 [docs/schema.md](../schema.md) §3；DDL 真相源 [main_v3.sql](../../bili_unit/_db/ddl/main_v3.sql)。

## 模块结构

```
bili_unit/parsing/
├── __init__.py            # 状态枚举 + 异常 + ParsingCommandResult + assemble()
├── _store.py              # ParsingStore（写主 DB 的 6 张内容表 + image_asset + stage_task）
├── _images.py             # ImageDownloader + ImageDownloadResult
├── command.py             # ParsingCommand.parse_uid()
├── materializer.py        # ParsingMaterializer（per-model raw → typed → save_*）
├── specs.py               # ParsingSpec registry；MODEL_ORDER；materializer_handler 分发
└── models/
    ├── __init__.py        # get_parser() 注册表 + all_parser_names()
    ├── _refs.py           # SourceRef / CrossRefs（跨 model 共享 id 类型）
    ├── up_profile.py      # UpProfile dataclass
    ├── video_detail.py    # VideoDetail + PageInfo / VideoStat / OwnerInfo
    ├── video_subtitle.py  # VideoSubtitle + SubtitlePage / SubtitleSegment
    ├── article.py         # Article + ArticleStats / ReadListMeta
    ├── opus.py            # OpusPost + OpusStats
    └── dynamic.py         # DynamicPost + ForwardedDynamic
```

import 边界：

```text
command      → materializer, _store, fetching._store, _db (UidContext), DTO
materializer → models, _store, _images, fetching._store
models       → 仅 stdlib + dataclasses（不 import store / db）
_store       → _db.UidContext (sqlite-backed 写)
_images      → aiohttp
```

parsing 通过 `bili_unit.fetching._store.FetchingStore`（同 `UidContext`）只读 raw DB 的 `raw_payload(endpoint, item_id, ...)` 行，不写回 fetching 层任何状态。

## Parsed Models

`ParsingCommand.parse_uid()` 通过 `bili_unit.parsing.specs.PARSING_SPECS` 分发 model。当前顺序为（`MODEL_ORDER`）：

```text
user_profile → video_work → video_subtitle → article_post → opus_post → dynamic_event
```

每个 spec 携带 `materializer_handler` 字符串方法名（`_parse_user_profile` 等），`parse_model()` 用 `getattr` 调用。每个 model 暴露一个 `@property is_complete`，由当前 source_refs / cross_refs / 字段计算得出，并随 `to_dict()` 落盘为顶层字段（`from_dict()` 不读取持久化值，rebuild 后由 property 自动重算）。

`materializer._SAVE_METHODS` 把 model 名映射到 `ParsingStore.save_*` 方法：

| model | 写入表 | save 方法 |
|---|---|---|
| `user_profile` | `user_profile` | `save_user_profile` |
| `video_work` | `video` + `video_page` | `save_video`（事务：DELETE video_page + INSERT OR REPLACE video + INSERT video_page*N） |
| `video_subtitle` | `video_subtitle` | `save_video_subtitle` |
| `article_post` | `article` | `save_article` |
| `opus_post` | `opus_post` | `save_opus` |
| `dynamic_event` | `dynamic_event` | `save_dynamic` |

### UpProfile（per-uid 单行）

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

入表 `user_profile`：列 `uid` ← `mid`，`name` / `sign` / `face_url` ← `avatar`，`level`，`follower` / `following` ← `social[...]`，整 dataclass 落 `payload`。

图片：`avatar` → `"avatar.jpg"`（1 张/uid）。

`is_complete`：3 个必填端点（`user_info` / `relation_info` / `up_stat`）都有 source_ref 时为 True；`overview_stat` 可选，不影响。

### VideoDetail（per-bvid，写 `video` + `video_page`）

来源端点：`video_detail`（item-level fan-out）

```python
@dataclass
class VideoDetail:
    bvid: str                    # info.bvid
    aid: int | None              # info.aid
    title: str                   # info.title
    desc: str                    # info.desc
    duration: int                # info.duration（秒）
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

入表映射（`save_video` 单事务）：

- `video.bvid / aid / title / description ← desc / cover_url ← pic / duration_s ← duration / pubdate_ms ← pubdate*1000`
- 统计列 `view_count / danmaku / reply / favorite / coin / share / like_count ← stat[...]`
- `payload` 持有完整 `to_dict()`；`parsed_at_ms` 写入当前 ms-epoch
- `video_page` 先 DELETE 再 INSERT，按 `pages` 顺序生成 `(bvid, page_no, cid, part, duration_s)`

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
    is_ai: bool                  # lan.startswith("ai-")
    segments: list[SubtitleSegment]

@dataclass
class VideoSubtitle:
    _schema_version: int = 3     # v1: 无 is_ai；v3: 字段名显式标明 B 站平台字幕来源
    bvid: str
    pages: list[SubtitlePage]    # 仅包含至少一种 lang 命中 body 的 page
    available_languages: list[str]  # 跨 page 出现过 body 的 lang 全集（去重，发现序）

    @property
    def is_complete(self) -> bool: ...   # retained subtitle pages 都至少有一种 lang
    @property
    def is_ai_only(self) -> bool: ...    # 至少 1 个 page 且每个 page 都是 AI
```

入表 `video_subtitle`：`bvid` PK，`has_bilibili_human_uploaded_or_official_subtitle` ← `any(not p.is_ai for p in pages)` ∈ {0,1}，`has_bilibili_platform_ai_generated_subtitle` ← `any(p.is_ai for p in pages) or any(lan.startswith("ai-") for lan in available_languages)` ∈ {0,1}，`payload` 持有完整字幕段（含 B 站平台 AI 生成字幕）。FK CASCADE → `video.bvid`，因此 video 行被删时字幕同步消失。

每个 page 的 lang 选择优先级（按 `lan` 字符串前缀匹配）：

```text
zh-CN > zh-Hans > zh-HK > ai-zh > en > 第一个有 body 的非空
```

`_fetch_error` 标记的项在选择前被排除；body 为空也跳过。当一个 page 的所有 lang 都不可用时，该 page 不会出现在 `pages` 里；需要判断整条视频是否覆盖所有分 P 时，调用方必须再对照 `video_page` 的 page index。

内部属性 `is_ai` 标记单 page 的字幕是否由 B 站平台 AI 自动生成（`lan` 以 `ai-` 开头）；对外 `payload` 使用 `is_selected_bilibili_subtitle_platform_ai_generated` 与 `is_only_bilibili_platform_ai_generated_subtitle_available` 这样的长字段名，保证只看 SQLite JSON 也能知道它来自 B 站平台 AI 字幕而不是本项目 ASR。

processing audio runner 只有在字幕覆盖所有分 P、非 AI、且有非空文本时才短路：B 站平台 AI 字幕（`is_selected_bilibili_subtitle_platform_ai_generated=true` / `selected_bilibili_subtitle_language_code` 以 `ai-` 开头）会保留在 `video_subtitle` / `video_subtitle_page` / `video_subtitle_segment` 中供查看，同时 `audio_transcription` / `audio_transcription_page` / `audio_transcription_segment` 仍可独立保存 `MIMO-ASR` 结果。`ParsingStore.video_subtitle_is_complete(bvid)` 只表达 retained payload 自洽性；最终能否短路由 runner 对照 `video_page` 决定。

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

入表 `article`：`cvid` ← `id`，`pubdate_ms` ← `ctime*1000`，`view_count / like_count / reply ← stats.*`，`payload` 持有完整 dataclass。

辅助函数：`_dedup_urls(*sources)` 多源 URL 去重合并；`_build_cvid_to_lists(list_details)` 反索引 cvid → 文集归属。

图片：`image_urls` → `[("article/{cvid}_{i:02d}.jpg") for i, url in enumerate(image_urls)]`（1~10 张/cvid）。

`is_complete`：source_refs 含 `article_detail` → True。仅有列表端点（`articles`）的 Article 缺正文 markdown，视为不完整。

### OpusPost（per-opus_id）

来源端点：`opus`（必填）、`opus_detail`（可选）

```python
@dataclass
class OpusPost:
    _schema_version: int = 2     # v1: list_images / detail_images / image_locals 三字段；from_dict 自动迁移
    id: str                      # str(opus list item opus_id)
    title: str                   # 列表级 title
    summary: str                 # 列表级 summary（fallback: modules 内 opus.summary.text）
    cover: str                   # 列表级 cover；dict 形态（{url, width, height}）由 _url_from_value 取出 url
    jump_url: str                # 列表级 jump_url
    stats: OpusStats             # {view, favorite, like, reply, share, coin}
    ctime: int | None            # pub_time（fallback: ctime）
    markdown: str                # opus_detail.markdown 去掉 YAML frontmatter 后的正文
    images: list[dict]           # [{url, width?, height?, local_path?}, ...]；优先 opus_detail.images，
                                 # 缺失时回落到 modules.module_dynamic.major.opus.pics[*].url
    cover_local: str = ""        # 图片下载后填充：images/opus/{id}_cover.jpg
```

入表 `opus_post`：`opus_id` ← `id`，`pubdate_ms` ← `ctime*1000`，`payload` 持有完整 dataclass。

辅助函数：

- `_strip_yaml_frontmatter(md)` 剥掉 `bilibili-api` 的 `Opus.markdown()` 默认前置的 raw modules YAML（avatar 层 / vip 徽章 / layer_config 等渲染元数据，实测占 markdown 字节 ~90%；defensive，找不到闭合 `---` 时原样返回）。
- `_url_from_value(v)` 把 `cover` 字段在「字符串 URL」与「`{url, width, height}` dict」两种形态间归一化（B 站 listing 端点偶发返回 dict 形态）。
- `_merge_images(detail_images, listing_pic_urls)` 合并 detail（rich `{url, width, height}`）与 listing 的 pic URL，URL 去重保序，键白名单 `{url, width, height}`。
- `_modules_dict(raw)` 归一化 modules 块（dict / list 双形态）；`_extract_opus_summary_text(modules)`、`_extract_opus_pic_urls(modules)` 深层路径提取。

图片：cover → `"opus/{id}_cover.jpg"`（仅当 cover 非空），后接 `images[i].url` → `"opus/{id}_{i:02d}.jpg"`。`apply_image_results` 按 URL 配对（不再按下标）：cover 下载失败时不会把第一张正文图错配为 `cover_local`；每张正文图的 `local_path` 写回 `images[i]["local_path"]`。

`is_complete`：source_refs 含 `opus_detail` → True。仅有列表端点（`opus`）的 OpusPost 缺正文 markdown，视为不完整。

### DynamicPost（per-dynamic_id）

来源端点：`dynamics`

> 动态本质上是「信封」：`DYNAMIC_TYPE_AV` / `DYNAMIC_TYPE_OPUS` / `DYNAMIC_TYPE_ARTICLE` / `DYNAMIC_TYPE_DRAW` 等类型 `text` 通常为空，正文体在 `target_ref` 指向的 `video_work` / `opus_post` / `article_post` 里。`DYNAMIC_TYPE_FORWARD` 转发：用户重发的 caption 在 `text`，原作者的 caption 在 `forwarded.text`。视频复用类型上游 feed 不带 `archive.desc`，要看视频说明需走 `video_detail`。空 `text` 不代表数据丢失，是上游真相。

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

入表 `dynamic_event`：`dynamic_id` ← `dynamic_id or id_str`，`type` 列保存动态类型 string，`pubdate_ms` ← `timestamp*1000`，`payload` 持有完整 dataclass。

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

`to_dict()` 把它们落到 `_source_refs` / `_cross_refs` 顶层字段，并随 `payload` 列一起入库；`from_dict()` 时还原。
跨内容身份的关联（一篇文章既有 cvid 也是某条动态的 major、一个 opus 也是某条 video 的转载等）由 `_cross_refs` 承载，下游需要把 article / opus / dynamic / video 关联起来时按 `cvid` / `opus_id` / `dynamic_id` / `bvid` 自行 join（直接 `json_extract(payload, '$._cross_refs.bvid')` 即可）。

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
| OpusPost | `[(cover, "opus/{id}_cover.jpg")]` + `(images[i].url, "opus/{id}_{i:02d}.jpg")` | `cover_local` + `images[i].local_path`（按 URL 配对） |
| DynamicPost | `[(url, "dynamic/{id}_{i:02d}.jpg") ...]` | `image_locals` |

下载完成后 materializer 一次性 `INSERT OR REPLACE` 写回 typed object（payload 里 `*_local` 已更新），并按对应 model 的 `_IMAGE_SOURCE_KINDS` 在 `image_asset` 表登记一行（`url_hash = md5(url)`，图片内容写入 `data BLOB`，详见 [docs/schema.md §3.9](../schema.md#39-image_asset--图片缓存索引)）。

## ImageDownloader（`_images.py`）

并发图片下载器，设计参考 `processing/audio/_downloader.py`。

```python
@dataclass
class ImageDownloadResult:
    url: str
    local_path: str           # 逻辑相对路径，写回 parsed payload
    status: str               # "ok" | "skipped" | "failed"
    error: str = ""
    data: bytes | None = None # 成功下载或 DB 缓存命中时的图片内容

class ImageDownloader:
    def __init__(self, base_dir: Path, concurrency: int = 8, timeout: float = 30.0): ...
    async def download_one(self, url: str, dest_rel: str) -> ImageDownloadResult: ...
    async def download_many(self, jobs: list[tuple[str, str]]) -> list[ImageDownloadResult]: ...
```

关键行为：

- **跳过已存在**：主 DB 已有同 URL 的成功 `image_asset.data` 时返回 `"skipped"`
- **并发控制**：`asyncio.Semaphore(concurrency)`
- **HTTP headers**：`Referer: https://www.bilibili.com`、`User-Agent: Chrome`
- **扩展名推断**：URL 路径提取 + Content-Type 兜底（`_CONTENT_TYPE_EXT` 映射）
- **失败隔离**：单张失败返回 `"failed"` + error，不影响其他图片
- **DB 优先**：下载器只返回 bytes；materializer 负责写入 `image_asset.data`

## 磁盘布局

```
{bili_db_dir}/
├── {uid}.db                     # 主 DB；6 张内容表 + image_asset + stage_task 等
├── {uid}.raw.db                 # raw DB（fetching 写）
└── {uid}/                       # workdir：音频缓存、临时文件等；图片默认在主 DB
```

解析结果直接 INSERT OR REPLACE 进 SQLite 表，没有独立的 JSON 目录。

## stage_task[stage='parsing'] 形状

parsing 复用主库 `stage_task` 表（PK：`stage`），`payload` JSON 形如：

```json
{
  "models": {
    "user_profile":   {"status": "SUCCESS", "count": 1},
    "video_work":     {"status": "SUCCESS", "count": 76},
    "video_subtitle": {"status": "SUCCESS", "count": 76},
    "article_post":   {"status": "SUCCESS", "count": 1},
    "opus_post":      {"status": "SUCCESS", "count": 54},
    "dynamic_event":  {"status": "SUCCESS", "count": 868}
  },
  "images": {
    "total": 47, "ok": 43, "skipped": 2, "failed": 2,
    "failed_urls": ["https://..."]
  }
}
```

`status` 列存任务级状态（`PENDING` / `RUNNING` / `SUCCESS` / `PARTIAL` / `FAILED`）。`init_task()` idempotent —— 重跑保留已有 model 的 status / count；`update_task_model_status(model, status, count)` 是 read-modify-write（asyncio.Lock 串行）。`stage_error` 表对 parsing 不写（CHECK 仅允许 `'fetching'` / `'asr'`）—— 单个 model 的失败只通过 `models[name].status="FAILED"` 表达。

## 状态枚举

`ParsingTaskStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / PARTIAL / FAILED

`ParsingModelStatus`（StrEnum）：PENDING / RUNNING / SUCCESS / FAILED / SKIPPED

任务级状态由 model 状态聚合（`ParsingCommand.parse_uid`）：

- full 模式下全部 model count > 0 且 SUCCESS → SUCCESS
- full 模式下任一 model count == 0 或 FAILED → PARTIAL
- incremental 模式下 count == 0 但该 model 的内容表已存在行 → 视为跳过已有数据，不强制 PARTIAL
- 所有 model 失败 → PARTIAL（非 FAILED，保留容错语义）

## 解析模式

`parse_uid(uid, mode)` 支持两档：

| mode | 行为 |
|------|------|
| full（默认） | 重新解析所有项并 `INSERT OR REPLACE` 覆盖写入 |
| incremental | 跳过已存在的 typed object（`ParsingStore.get_existing_item_ids(model)` 返回内容表中已有 PK 集合） |

注意：incremental 模式只跳过「行存在」的 item；列字段 / payload 形状变化时仍需 `parse <uid> -m full` 触发重写。

## CLI

常规路径由 `sync <uid>` 在 fetching 后自动触发 parsing；手动 `parse` 主要用于 re-parse 已有 raw DB 或调试 parser：

```bash
uv run python -m bili_unit sync <uid>                          # 常用：抓取后解析
uv run python -m bili_unit sync <uid> -i                       # 同步 + 下载图片
uv run python -m bili_unit parse <uid>                         # 6 个 model，full 模式
uv run python -m bili_unit parse <uid> -i                      # 解析 + 下载图片
uv run python -m bili_unit parse <uid> -m incremental          # 增量模式（跳过已入库的 item）
```

读侧用 `sqlite3` 直连 `bili_unit.db_path(uid)`：

```sql
SELECT bvid, title, view_count, pubdate_ms FROM video ORDER BY pubdate_ms DESC LIMIT 50;
SELECT cvid, title, summary FROM article;
SELECT opus_id, json_extract(payload, '$.title') AS title FROM opus_post;
SELECT * FROM video_full WHERE bvid = ?;        -- video + transcription LEFT JOIN view
SELECT * FROM stage_task WHERE stage = 'parsing';
```

## 装配

parsing 的 `assemble(settings)` 返回单值 `ParsingCommand`：

```python
async def assemble(settings: BiliSettings) -> ParsingCommand
```

`ParsingCommand` 不持有 store；每次 `parse_uid()` 自开自关 `UidContext`，再绑定 `ParsingStore` + `FetchingStore` + `ParsingMaterializer` 跑一遍 6 个 model + 可选图片下载，最后写 `stage_task[stage='parsing'].status`。

### Command 接口

```python
async def parse_uid(
    uid: int,
    mode: str = "full",              # "full" | "incremental"
    download_images: bool = False,   # 是否下载图片
) -> ParsingCommandResult
async def delete_uid(uid: int) -> dict[str, int]    # no-op；BiliCommand 删 db 文件
async def close() -> None                            # no-op
```

`ParsingCommandResult` 字段：`uid: int`、`status: ParsingTaskStatus`、`run_id: str | None`。
CLI 使用 `run_id` 精确读取本次 run 的 Run Summary；缺失时才退回 uid 最新 run。

### ParsingStore 关键方法

`bili_unit/parsing/_store.py` 的写侧表面：

```python
# typed object writes
async def save_user_profile(profile)
async def save_video(video)              # video + video_page，单事务
async def save_video_subtitle(sub)
async def save_article(art)
async def save_opus(opus)
async def save_dynamic(dyn)

# image bookkeeping
async def save_image_asset(*, url, source_kind, source_id, file_path, bytes, status,
                           data=None, downloaded_at_ms=None)
async def get_image_asset(url) -> dict | None
async def list_image_assets() -> list[dict]

# read-side helpers (consumed by materializer / processing audio)
async def get_existing_item_ids(model) -> set[str]
async def get_video_payload(bvid) -> dict | None
async def get_video_subtitle_payload(bvid) -> dict | None
async def video_subtitle_is_complete(bvid) -> bool         # processing 字幕短路判定
async def get_user_profile_payload(uid) -> dict | None
async def get_article_payload(cvid) -> dict | None
async def get_opus_payload(opus_id) -> dict | None
async def get_dynamic_payload(dynamic_id) -> dict | None

# task state
async def init_task(models)
async def update_task_model_status(model, status, count=0)
async def update_task_images(images_summary)
async def update_task_status(status)
async def get_task() -> dict | None
```

`save_*` 方法都会让重新解析结果覆盖旧内容；普通内容表多用 `INSERT OR REPLACE`，带子表的 `video` / `video_subtitle` 使用事务清理并重建派生行。`payload = json.dumps(obj.to_dict(), ensure_ascii=False)`，中文不转义。

## 异常层级

```
ParsingError
├── ModelParseError        # raw dict → typed object 失败
└── ImageDownloadError     # 图片下载失败（未被 _images.py 内部隔离时抛出）
```

单张图片下载失败不阻塞整体流程；失败 URL 记录在 `stage_task.payload.images.failed_urls` 中。单个 model 解析失败标记该 model 为 FAILED，其他 model 继续。

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_DB_DIR | output/bili | SQLite DB 根目录（main + raw + workdir 全部派生；图片默认写入 main DB 的 `image_asset.data`） |
| BILI_PARSING_IMAGE_CONCURRENCY | 8 | 图片下载并发数 |
| BILI_PARSING_IMAGE_TIMEOUT | 30 | 单张图片下载超时（秒） |

> 解析结果和图片内容都默认进入 `{BILI_DB_DIR}/{uid}.db` 主库；workdir 主要保留给音频缓存与临时文件。

## 测试状态

测试位于 `bili_unit/tests/`，覆盖 6 个 model 单元测试（from_raw / to_dict / from_dict / image protocol / is_complete）、`ParsingSpec` registry、`ParsingStore` SQLite 契约测试（`test_parsing_store_sqlite.py`）、generic incremental、command + materializer 集成。无外部网络，离线可跑：`uv run pytest`。
