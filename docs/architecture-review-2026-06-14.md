# Architecture review — 2026-06-14

> 本轮清理的总账。Branch `refactor/architecture-review-2026-06-14`，测试基线
> 431 → 441（+10），全程 0 失败。

## 主题

把 `docs/structure/bili.md` §6 §8 的边界声明从注释升级为代码层不变量；消除
测试架构倒灌生产代码（patch target hooks）；收敛跨阶段的多份重复单一真相源
（lifecycle、配置、model 名空间）。

## 已落地

| 项 | 性质 | 关键改动 |
|---|------|----------|
| **单点 lifecycle 出口** | bug | `BiliCommand.close()` 是唯一出口；删 `assemble()` 嵌套闭包；CLI handler 6 处统一 `await cmd.close()` |
| **跨阶段调用通过 Protocol** | 边界硬化 | 新增 `bili_unit/fetching/protocols.py:FetchingReadView`（4 方法）；parsing/processing 4 个文件改 type-annotate；`bili_unit/tests/test_fetching_protocol.py` 守住契约 |
| **凭据通过 DI 流入 processing** | 边界硬化 | `ProcessingCommand(credential_provider=...)`；删 `processing/runner/_audio.py` 内的 `from ...fetching.auth import get_credential` |
| **`fetch_endpoint` 通过构造注入** | DI | 删 `fetching/runner/__init__.py:46-48` 那个为测试准备的 wrapper；36 处 `patch("bili_unit.fetching.runner.fetch_endpoint", ...)` 改 `Runner(fetch_fn=mock)` |
| **`AudioDownloader` / `convert_single` 通过构造注入** | DI | 删 `processing/runner/__init__.py` 顶层 `# noqa: F401 — patch target` re-export；`ProcessingRunner(downloader_factory=..., convert_fn=...)`；18 处测试迁移 |
| **`asyncio.sleep` noop patch 清除** | 副产品 | 测试里 2 处 `patch("bili_unit.processing.runner.asyncio.sleep", ...)` 长期是 noop（包内根本无人调用，真实 `await asyncio.sleep` 在 `_retry.py`），直接删；测试稳健性靠 `retry_delays="0,0"` 撑着 |
| **parsing 双名空间收敛** | 清理 | 删 4 个 materializer thunk（`_parse_video_detail` 等）；删 `_PARSER_NAMES` 4 对 legacy alias；删 `query.py:MODEL_ALIASES` + `_canonical_model`；`ParsingQuery` 便利方法（`list_video_details` 等）保留为 stable public API，实现里直传 canonical |
| **env singleton 三合一** | 清理 | 新增 `bili_unit/_env.py:BiliSettings`（53 字段单一真相源）；三个 stage 的 `env.py` 改成 thin re-export；`get_retry_delays()` 重命名为 `get_fetching_retry_delays` / `get_processing_retry_delays` 消歧 |
| **`delete-uid` 名实相符** | bug | 三个 stage 的 `Command` 各自实现 `delete_uid()`；`BiliCommand.delete_uid()` 串起来；`_handle_delete_uid` 不再绕开统一 assemble；+9 测试 |
| **`ProcessingCommand.parsing_query` 死参数** | 清理 | 删（顺手） |
| **`ProcessingQuery._fetch_qry` 死字段** | 清理 | 接收但从未调用，删（顺手） |

## 已知遗留

按"未来要做时的触发条件"分类。每条都有 grep 起点，方便后续接手者定位。

### A. 边界硬化的最后一公里

#### A1. `BiliQuery.fetching` property 仍返回 `Query` 具体类

**位置**：`bili_unit/query/__init__.py:38-42`

```python
@property
def fetching(self) -> _FetchingQuery:
    return self._fetching
```

外部调用方（如 `bili_unit/__main__.py:_handle_fetch`）会写 `qry.fetching.list_tasks()` —— 跨 stage 边界还在，但走的是 unit 顶层 facade。Protocol 化的最后一步是 `BiliQuery` 也只暴露 `FetchingReadView`，让外部调用方拿不到 `Query` 上的非 Protocol 方法。

**触发条件**：当 query facade 出现第二个外部调用方（不只是 CLI），或者发现 CLI 在用 `Query` 上某个不在 Protocol 里的方法。当前外部调用方都是自家 CLI handler，可控；未做的成本低。

### B. 大文件拆分

`grep -c '^$' file` 不算线，下面是 LOC：

| 文件 | LOC | 状态 |
|------|-----|------|
| `fetching/_bilibili_adapter.py` | 937 | 集中 60+ 个 endpoint 适配器 |
| `fetching/_endpoint_catalog.py` | 753 | 64 个 EndpointSpec |
| `fetching/runner/_item_fanout.py` | 434 | item-level fan-out 全流程 |
| `processing/runner/_audio.py` | 395 | audio pipeline 编排 |

#### B1. `_bilibili_adapter.py` / `_endpoint_catalog.py` 按业务域拆分

**触发条件**：新增 endpoint 时 PR 总在改这两个文件。当前节奏没出现冲突。

候选拆法：按业务域（`user.py` / `video.py` / `article.py` / `opus.py` / `dynamic.py` / `channel.py` / `upower.py`）。`EndpointSpec` 也按域拆，`ENDPOINTS` 在 catalog 顶层 import 后拼装。

#### B2. `_item_fanout.py` 拆 retry callback

**触发条件**：跟 fetching 的 uid-level retry callback (`_endpoint.py:136-244`) 出现第三处实现需求时。

当前 fetching uid-level / fetching item-level / processing 三处 `_on_attempt_failed` 骨架几乎一样（分类异常 → 记录错误 → 写状态 → 决定 retry 等待），差异只在错误记录字段。`_retry.py:RetryDriver` 已经抽了主循环，callback 那段可以再抽 `ErrorRecordingFailureHandler(error_store, status_writer, classify_fn)`。本轮没动是因为收益有限（三处而已），抽出来反而让 callback 跨文件追溯变难。

### C. 风格 / 类型收敛

#### C1. `mode: str` → enum

**位置**：跨三个 stage：

- fetching: `"incremental" | "refresh" | "full"`（`fetching/runner/__init__.py:run_or_resume`）
- parsing: `"full" | "incremental"`（`parsing/command.py:parse_uid`）
- processing: `"incremental" | "full"`（`processing/runner/__init__.py:run`）

合法值集合不一样，目前都是裸字符串校验。`StrEnum` 化能消除 typo 风险；可以三阶段各一个 enum（`FetchingMode` / `ParsingMode` / `ProcessingMode`）放在各自 `__init__.py`。

**触发条件**：CLI 增加 `mode` 选择面（比如批量任务里给不同 stage 传不同 mode）；或者出现一次 typo bug。

#### C2. `Runner(_EndpointMixin, _ItemFanoutMixin)` mixin 模式 → 组合

**位置**：`bili_unit/fetching/runner/__init__.py:55`

mixin 之间通过 MRO 互相调（`_update_endpoint_status` 在主类，被两个 mixin 调），mixin 内用 `_data: Any` 等 stub 表达隐式契约 —— 静态分析读不出调用图。改组合（`Runner` 持有 `_EndpointRunner` / `_ItemFanoutRunner` 两个 helper）会更清楚。

**触发条件**：新增第三个 mixin（如 cleanup pipeline）；或 mypy 在 mixin stub 上报误报。

DI 落地后这条收益变小（runner 不再需要为测试 mock 而把 mixin 暴露成可 patch 表面）。

### D. 状态机 / 死代码

#### D1. `ProcessingPipelineStatus.FAILED_RETRYABLE` 是死值

**位置**：`bili_unit/processing/__init__.py:36`

`_derive_pipeline_status()` 只输出 `SUCCESS / RUNNING / PARTIAL / FAILED_PERMANENT`，从无路径产出 `FAILED_RETRYABLE`。要么删 enum 值，要么补出 emit 路径并加 schema 升级注释（旧 JSON 里若曾有该值的恢复策略）。

**触发条件**：pipeline 级状态机要表达"整 pipeline 暂时失败、待 resume"语义时（item 级已经有 `FAILED` + retry 机制；pipeline 级目前不需要这个）。

### E. 性能 / 并发

#### E1. `JsonKVStore` 全 store 单锁

**位置**：`bili_unit/_storage/_kv.py:59`

所有 write 串行。`fetching/runner/_item_fanout.py` 给 video_detail 跑 200+ 并发 fan-out，每写一个 item 都拿全 store 锁。锁粒度可以收到 per-uid 或 per-key。

**触发条件**：跑大账号（5000+ 视频）时延迟可观察化、且 profile 显示 lock contention 是热点。

#### E2. `Query.get_task` 多次读全量错误

**位置**：`bili_unit/fetching/query.py:36-47`

每个 endpoint 独立调 `get_endpoint`，内部又 `await self._error.list_by_uid(uid)`。endpoint 数 64 时单次 `get_task` 触发 64 次磁盘读全量错误。改成读一次按 endpoint groupby。

**触发条件**：CLI `query` 子命令延迟可观察、或顶层引用方做 list-all-uids 加载所有 task。

### F. CLI / 用户面

#### F1. legacy entry point shim

**位置**：`bili_unit/fetching/__main__.py` / `bili_unit/processing/__main__.py`

两个文件都是 `sys.argv = ["bili_unit", ...]` 重写后委托到统一 CLI。注释里写 "legacy / backward-compat"。

**触发条件**：要么补一个版本号 + `DeprecationWarning` 走正式废弃流程；要么直接删（如果调用方/CI 已经全部迁过去）。

## 测试体量与稳健性的注记

- DI 改造让 patch target hook 全部消失。`bili_unit/processing/runner/__init__.py` 的顶层 `# noqa: F401 — patch target` 注释和 `__all__` 里的 `AudioDownloader / convert_single` re-export 都已清除。

- **`asyncio.sleep` 在 retry 路径上没真正 mock 过**。所有相关测试靠 `retry_delays="0,0"` 让 sleep 实际无延迟。如果未来想测真正的 backoff（指数退避、412 advisory wait 覆盖等），需要显式 inject sleeper 或者 `patch("bili_unit._retry.asyncio.sleep", ...)`（这才是真正的调用点）。

- **`bili_unit/_storage/_errors.py:_next_id` 是进程内锁**。多进程并发会撞，但项目目前不跨进程；`delete_by_uid` 不动 counter（不会 reset），这是有意为之。

## 不在本轮范围（明确拒绝）

为避免范围蔓延，下列条目曾被考虑但**故意未做**：

- **fetching `EndpointStatus` 跨 stage 共享**：保留在 `fetching/__init__.py`；下游 `from ..fetching import EndpointStatus` 是合法的数据契约依赖，不是越界。
- **三个 stage 的 task / status enum 抽公共基类**：三套 `*TaskStatus` 词汇相似但合法值不一样（fetching 7 / parsing 5 / processing 7），合并有真实信息丢失风险。
- **parsing 模型物理文件名重命名**（`video_detail.py` → `video_work.py` 等）：canonical model name 是逻辑名，文件名是物理布局；保留差异不损害架构。

## 接手指引

按时间序读：

1. `docs/structure/bili.md` —— 设计声明与不变量
2. 本文件 —— 哪些不变量已落地为代码、哪些还停留在文档约定
3. `docs/adr/` —— 不可逆的架构决策（存储后端、transform 删除、pipeline_executor seam；ContentPost 共存 ADR 已 superseded —— PR #17 删了 ContentPost 与 selectors）
4. `docs/feature/` —— 各 stage 实现真相

读代码起点：

1. `bili_unit/__init__.py:assemble()` —— 装配根
2. `bili_unit/command/__init__.py` —— 写侧入口
3. `bili_unit/query/__init__.py` —— 读侧入口
4. `bili_unit/fetching/protocols.py` —— 跨 stage 契约
5. `bili_unit/_env.py` —— 配置真相源

## 2026-06-14 follow-up — output quality 补全

review 第一轮收尾后用户反馈"看起来缺了点什么"，开了第二轮专门补输出质量的洞。详见 `PLAN.md`。落地的主题：

| 波次 | 主题 | 关键改动 |
|---|---|---|
| W1.1 | subtitle 不只拉索引 | `fetch_video_subtitle_item` 并发拉每条 lan 的 JSON，raw_payload 升级为 `{pages, subtitle:[{..., content:[{lan, lan_doc, body:[...]}]}]}` |
| W1.2 | segments 实化 | `audio_transcribe_page` 维护 `segments_out` 列表，cache 命中也填，最终落到 `page_results[*].segments` |
| W1.3 | process CLI 过滤 + fixtures 扩展 | `--limit / --only-bvids / --retry-failed-only / --dry-run`；6 份新 fixtures |
| W2.1 | subtitle 优先 | 新模型 `video_subtitle` + parsing spec；audio runner 注入 `parsing.query`，subtitle 完整时跳 ASR、`transcription_source: "subtitle"` |
| W2.2 | 视频进 ContentPost | `video_posts_from_parsed`；ContentPost.kind 增加 `video`；canonical key `bvid > dynamic_id` |
| W3.1 | is_complete + cost | 5 个 typed dataclass + ContentPost + VideoSubtitle 都加 `is_complete` 计算属性；audio result 新增 `cost: {audio_tokens, seconds, model, cache_hits, fresh_segments}` |
| W3.2 | task ↔ error join | 三 stage TaskValue/DTO 加 `failed_item_ids`；CLI 直接打印 |
| W4 | 顶层 manifest.json | 新 `bili_unit/_manifest.py` + `BILI_MANIFEST_DIR`；`BiliCommand.fetch/parse/process/delete_uid` 自动维护 `data/bili/manifest/{uid}.json`；CLI `manifest <uid> [--json]` 只读打印 |

测试基线：545 → 555（+10）；ruff 0 错。文档同步更新：`docs/feature/{fetching,parsing,processing,manifest}.md`、`docs/structure/fetching-contract.md`、`docs/structure/bili.md` §6、`docs/api.md`、`docs/adr/0004-...`、`README.md`。

## 2026-06-14 follow-up #2 — content_post 删除与 parsing 输出质量

第二轮 follow-up 落地后，回头看 `content_post/` 这个统一内容视图——`index.ingestion` 仍未实装，没有任何下游消费它，**53% 的解析层落盘是为想象中的读者付的预付款**。同时检查实际抓回的数据，发现 `OpusPost.markdown` 90% 字节是 `Opus.markdown()` 自动塞的 raw modules YAML（avatar 层 / vip 徽章），`OpusPost.cover` 在 B 站 listing 端点偶发返回 `{url, width, height}` dict 时被 `_str_or_empty` 字符串化、导致 17/19 张封面下载失败，`VideoSubtitle` 没有显式 AI 字幕标记。

| 波次 | 主题 | 关键改动 | PR |
|---|---|---|---|
| F2.1 | content_post 删除 | 删 `selectors/` 包 / `content_post.py` model / `_parse_content_posts` materializer / 两份测试；`SourceRef` / `CrossRefs` / `content_key_for_refs` 移到 `models/_refs.py` 共享；解析层落盘从 6 model 降回 6 model（VideoSubtitle 替补，符合既往状态） | #17 |
| F2.2 | opus markdown 剥 frontmatter | `_strip_yaml_frontmatter` 在 `OpusPost.from_raw` 里去掉前置 YAML，输出尺寸：5097 KB → 278 KB（`opus_post/` 目录，−94.5%） | #18 |
| F2.3 | opus cover dict 形态修复 | 新 `_url_from_value` 助手；listing dict 形态归一；下游图片下载从 154/173 变 173/173 | #18 |
| F2.4 | opus images 字段合并 | `list_images` + `detail_images` 合并为单 `images: list[dict]` 带 `local_path`；`apply_image_results` 改 URL-keyed（修了 cover 失败时 `cover_local` 错配第一张正文图的位置 bug）；`_schema_version` 1 → 2，`from_dict` 兼容 v1 JSON | #18 |
| F2.5 | video_subtitle AI 标记 | `SubtitlePage.is_ai`（`lan.startswith("ai-")`）+ `VideoSubtitle.is_ai_only` @property；`_schema_version` 1 → 2 | #18 |
| F2.6 | dynamic_event docstring | 在 `DynamicPost` 加说明：动态是「信封」，非 FORWARD 类型 `text` 为空是上游 feed 的真相，正文体在 `target_ref` → `opus_post` / `video_work`；不写代码（无可恢复字段） | #18 |

文档同步更新：`docs/feature/parsing.md`（OpusPost / VideoSubtitle 字段表 + 图片协议表 + DynamicPost docstring）、`README.md`、`CONTEXT.md`、`docs/adr/0004-...`（标 superseded）、`docs/adr/README.md`、本文。

测试基线：PR #17 删 content_post 后 552 通过；PR #18 +22 个新测试到 588（其中 +9 frontmatter / +11 cover dict / +9 images merge / +7 subtitle is_ai；既有 OpusPost / VideoSubtitle 测试少量延伸）。ruff 0 错。

