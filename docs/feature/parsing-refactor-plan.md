# parsing 重构方案：从 raw endpoint 到可消费对象

## 实施进度（截至 2026-06-14）

| Slice | 内容 | 状态 |
|-------|------|------|
| Slice A | 基础设施（ParsingSpec registry / generic query / incremental） | 完成 |
| Slice B | ContentPost + selectors（纯函数 + 单测） | 完成 |
| Slice C | 接入 Article / Opus / Dynamic + processing 切换 | 完成（2026-06-14） |
| 后续清理 | processing transform 删除（仅保留 audio） | 完成（2026-06-14） |
| Slice D | Video / User / Collection / Social | 未开始 |

**Slice C 关键交付**（2026-06-14）：
- parsing 层新增 `article_post / opus_post / dynamic_event / content_post` 四个 spec，落盘目录按新名（旧名保留为 alias）
- processing 层完全切换到 ContentPost-centric：旧的 `articles / opus / dynamics` 三个 transform handler 已删除，新增 `content_post` handler
- `transform/_registry.py` 现在只注册 `video_metadata / content_post / user_profile` 三个 handler
- legacy `Article / OpusPost / DynamicPost` typed dataclass 保留，作为 ContentPost 的 candidate 来源（`_content_candidates_from_parsed`），不可删除
- 测试：142 个 parsing + processing + CLI 指定测试 passed；全套 434 passed；ruff clean

**后续清理**（2026-06-14，紧接 Slice C 之后）：
- processing transform 子系统整体删除，processing 层只剩 audio pipeline。理由：Slice C 完成后 transform handler 已退化为字段透传 + 一行 `word_count`，且 ingestion 仍未实装，没有契约要保护。
- ingestion 待实装时直读 parsing 出口面（`ParsingQuery.get_video_detail / list_articles / list_opus / list_dynamics / list_items(uid, "content_post") / get_user_profile`），不再经 processing 中转。
- Slice D 规划不变。

## 背景

fetching 层现在负责尽量抓全 B 站读取端点的 raw payload。parsing 层的职责不应是把每个 raw payload 原样包一层，而是从 raw 中筛选、抽取、归一、合并出后续 processing/query 能稳定消费的对象。

当前 parsing 仍停留在 5 个硬编码 typed dataclass：

- `UpProfile`
- `VideoDetail`
- `Article`
- `OpusPost`
- `DynamicPost`

这已经不匹配 fetching 的 64 个端点，也没有理清 `Article / Opus / Dynamic` 的交叉身份。

## 目标职责

parsing 层只负责四件事：

1. **筛选**：决定哪些 raw endpoint 进入 parsed 世界。
2. **抽取**：从 endpoint raw shape 中提取稳定 ID、正文、图片、时间、统计、引用关系。
3. **合并**：同一内容从多个入口出现时合成对象或建立 cross-ref。
4. **落盘**：保存可查询、可处理、带来源追踪的 parsed object。

parsing 层不负责：

- 网络请求。
- ASR / 音频处理。
- 最终分析结果。
- 当前登录账号视角的 `InteractionState`。
- 时效性强、体积大的 `PlaybackInfo`。
- 把所有 raw 字段无筛选照搬进 typed object。

## 第一批对象

第一批只处理高价值对象：

| 对象 | Key | 作用 |
|---|---|---|
| `ContentPost` | `article:{cvid}` / `opus:{opus_id}` / `dynamic:{dynamic_id}` | Article / Opus / Dynamic 的统一内容视图 |
| `ArticlePost` | `cvid` | 专栏原生对象，保留 cvid、正文、文集归属 |
| `OpusPost` | `opus_id` | 图文原生对象，保留 opus modules、图片、正文 |
| `DynamicEvent` | `dynamic_id` / `id_str` | 动态流事件，保留时间线、major、转发、target ref |
| `VideoWork` | `bvid` | 视频作品聚合对象 |
| `VideoPage` | `bvid:cid` | 视频分 P 对象 |
| `Subtitle` | `bvid:cid:lang` | 字幕对象 |
| `Danmaku` | `bvid:cid` | 弹幕集合对象 |
| `UserProfile` | `uid` | UP 主聚合画像 |
| `Collection` | typed collection id | 合集/频道/文集 |
| `SocialEdge` | `relation_type:target_uid` | 关注/粉丝关系 |

本轮先实现 infrastructure 与 `ContentPost` 相关纯函数；接入主流程时优先处理 `ArticlePost / OpusPost / DynamicEvent / ContentPost`。

## Article / Opus / Dynamic 关系

这三者不是互斥类型，而是不同维度：

```text
Dynamic = 时间线事件 / 动态流卡片
Opus    = 图文内容形态
Article = 专栏内容形态
```

建模规则：

- `Article(cvid)` 是专栏身份。
- `Opus(opus_id)` 是图文身份。
- `Dynamic(id_str)` 是动态流事件身份。
- 同一内容可能同时拥有 `cvid / opus_id / dynamic_id`。
- parsing 不依赖 ID 数值相等推断关系，必须通过 raw 字段或转换结果显式建立 cross-ref。

统一来源结构：

```json
{
  "_source_refs": [
    {"endpoint": "articles", "item_id": "123"},
    {"endpoint": "article_detail", "item_id": "123"},
    {"endpoint": "opus", "item_id": "987"},
    {"endpoint": "dynamics", "item_id": "456"}
  ],
  "_cross_refs": {
    "cvid": "123",
    "opus_id": "987",
    "dynamic_id": "456",
    "bvid": null
  }
}
```

`ContentPost` canonical key 规则：

1. 有 `cvid` 时用 `article:{cvid}`。
2. 否则有 `opus_id` 时用 `opus:{opus_id}`。
3. 否则用 `dynamic:{dynamic_id}`。

`DynamicEvent.target_ref` 规则：

- `MAJOR_TYPE_ARTICLE` 指向 `article:{cvid}`。
- `MAJOR_TYPE_OPUS` 指向 `opus:{opus_id}`。
- `MAJOR_TYPE_ARCHIVE` 指向 `video:{bvid}`。
- `MAJOR_TYPE_DRAW` 产出动态图文内容。
- `DYNAMIC_TYPE_FORWARD` 保留 `forwarded_ref`，并递归抽取 forwarded major 的引用。

## Architecture

目标结构：

```text
bili_unit/parsing/
  specs.py
  materializer.py
  query.py
  selectors/
    article.py
    opus.py
    dynamic.py
    merge.py
  models/
    content_post.py
    article.py
    opus.py
    dynamic.py
    video_detail.py
    up_profile.py
```

`ParsingSpec` 负责把 parsing runner 的 interface 收窄：

```python
@dataclass(frozen=True)
class ParsingSpec:
    name: str
    model: str
    source_endpoints: tuple[str, ...]
    required_endpoints: tuple[str, ...]
    handler: Callable[[ParsingMaterializer, int, str], Awaitable[int]]
    priority: int = 100
```

短期目标是把现有 5 个 `_parse_*` 方法注册为 spec handler，先解除 `if/elif` 分发和硬编码 query。

## 实施切片

### Slice A：基础设施

- 新增 `bili_unit/parsing/specs.py`。
- 将现有 5 个 model 注册成 `ParsingSpec`。
- `ParsingMaterializer.parse_model()` 改为 registry 分发。
- `ParsingQuery` 新增：
  - `get_item(uid, model, item_id)`
  - `list_items(uid, model)`
- 旧 query 方法保留，代理到 generic 方法。
- 实现真实 `incremental`：目标 parsed key 已存在时跳过该 item。

### Slice B：内容对象与 selectors

- 新增 `ContentPost`。
- 新增 `SourceRef` / `CrossRefs`。
- 新增纯函数 selectors：
  - `select_article_posts`
  - `select_opus_posts`
  - `select_dynamic_content`
  - `merge_content_posts`
- 本 slice 不接入主 `parse_uid`，只做纯函数和单测。

### Slice C：接入 Article / Opus / Dynamic

- 新增 specs：
  - `article_post`
  - `opus_post`
  - `dynamic_event`
  - `content_post`
- 将旧 `Article / OpusPost / DynamicPost` 兼容输出保留一轮，避免 processing 立即断裂。
- 新对象与旧对象可并行落盘。

### Slice D：处理 video/user/collection/social

- `VideoWork / VideoPage / Subtitle / Danmaku`
- `UserProfile` 聚合新画像端点
- `Collection`
- `SocialEdge`

## 测试策略

每个 slice 都必须有局部测试：

- registry 分发测试。
- generic query 测试。
- incremental skip 测试。
- Article / Opus / Dynamic selector raw shape 测试。
- merge 去重和 canonical key 测试。
- 旧 parsing/processing 测试保持通过。

建议验证命令：

```powershell
.\.venv\Scripts\python -m pytest bili_unit/tests/test_parsing_models.py bili_unit/tests/test_parsing_data.py bili_unit/tests/test_parsing_command.py -q
.\.venv\Scripts\python -m pytest bili_unit/tests/test_parsing_content_posts.py -q
.\.venv\Scripts\python -m ruff check bili_unit\parsing bili_unit\tests\test_parsing_content_posts.py
```

## 当前 subagent 分工

- Explorer：只读梳理 Article / Opus / Dynamic raw shape 与 cross-ref。
- Worker A：实现 `ParsingSpec`、generic query、incremental。
- Worker B：实现 `ContentPost`、selectors、merge 纯函数与单测。

主线程负责审查、集成、修正冲突和最终验证。
