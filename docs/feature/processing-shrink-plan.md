# processing 层瘦身方案：删除 transform，仅保留 audio

> 时间：2026-06-14
> 背景：parsing 重构后，`processing/transform/` 三个 handler（`video_metadata` / `content_post` / `user_profile`）退化为对 parsing dict 的字段透传 + 一行 `word_count`。ingestion 尚未实装，没有契约要保护。
> 决策：删除整个 transform 子系统，processing 缩成只有 audio 一条 pipeline；层名保留为 `processing`。

## 范围与不在范围

**在范围内**：
- 删除 `processing/transform/` 整个子目录
- 删除 `processing/runner/_transform.py`（transform mixin）
- 收缩 `processing/runner/__init__.py`：去掉 `_TransformMixin`，run() 不再接收 `pipelines` / `item_types` 参数
- 收缩 `processing/command.py`：`process_uid` 签名同上
- 修复跨模块依赖：`WorkItem` 从 `transform/_base.py` 搬到 `processing/runner/_audio.py`（仅 audio + executor 用）
- 收缩 `processing/query.py`：`get_video_full` / `list_all_videos` 改读 parsing 拿元数据 + processing 拿 transcription
- CLI 收缩：`process` 子命令去掉 `-t/-x` 标志（保留 `-m/-b`）
- env 收缩：删除 `bili_processing_transform_workers`
- 测试：删 `test_processing_transform.py`；剪 `test_processing_runner.py` 中 11 个 transform 用例；剪 `test_processing_data_error.py` 中 transform 名义用例
- 文档：`processing.md` / `bili.md` / `parsing-refactor-plan.md` / `README.md` / handoff 文档作废
- handoff 文档 `2026-06-11-bili-user-profile-handler.md` 顶上加大字状态："已废弃 / processing 不再持有 user_profile handler"

**不在范围内**：
- 不改 audio pipeline 任何逻辑（`_audio.py` / `_audio_work.py` / `_pipeline_executor.py` / `audio/` 子包）
- 不改 parsing 层任何文件
- 不改 fetching 层
- 不动数据迁移（已落盘的 `proc/video_metadata/*` / `proc/content_post/*` / `proc/user_profile/*` 目录留着，新代码读不到，下次手工清理）
- 不重命名 `processing` 层（已确认保留）
- 不实装 subtitle / OCR pipeline（先把现状清理干净再说）

## 变更清单（按依赖顺序）

### 1. 抽出 WorkItem，删除 transform 子包

`WorkItem` 当前住在 `processing/transform/_base.py`，被 audio mixin 和 pipeline_executor 用。删除 transform 后这个依赖会断。

**操作**：
- 把 `WorkItem` 搬到 `processing/runner/_audio.py` 顶部（audio 是唯一消费者；没必要单独建一个公共模块）
- 改 `processing/runner/_pipeline_executor.py:33`：`from ..transform._base import WorkItem` → `from ._audio import WorkItem`（注意循环：executor 被 audio import，audio 也 import executor。需要先把 WorkItem 提到 _pipeline_executor 顶部由 executor 拥有，audio 反过来 import）
- **修订**：把 `WorkItem` 定义放到 `processing/runner/_pipeline_executor.py`（executor 是底层），audio 反向 import

```python
# 新位置：processing/runner/_pipeline_executor.py
@dataclass(frozen=True)
class WorkItem:
    item_type: str
    item_id: str
    item_data: dict[str, Any]
```

- 删除整个 `bili_unit/processing/transform/` 目录（5 个文件）

### 2. 收缩 runner

**`processing/runner/_transform.py`**：整个文件删除。

**`processing/runner/__init__.py`**：
- 删 `from ._transform import _TransformMixin`
- 删 `from ..transform import HANDLERS`
- 删 `_TRANSFORM = "transform"`
- `class ProcessingRunner(_TransformMixin, _AudioMixin)` → `class ProcessingRunner(_AudioMixin)`
- `__init__` 删除 `parsing_query` 参数（audio 不需要）
- `run()` 签名简化：

```python
async def run(self, uid: int, mode: str = "incremental") -> ProcessingTaskStatus:
    ...
```

- 删 `_select_pipelines` / `_select_item_types`
- `_load_or_init_task` 直接用 `["audio"]` 初始化 pipelines
- 不再有 transform 分支；只保留 audio 调用

### 3. 收缩 command

**`processing/command.py`**：
- `process_uid(uid, pipelines, item_types, mode)` → `process_uid(uid, mode)`
- 删 `parsing_query` 参数（不再透传给 runner）

### 4. 收缩 query

**`processing/query.py`** 的 `get_video_full` / `list_all_videos` 当前依赖 `processing.list_items(uid, "video_metadata")`。改成走 parsing：

- 在 `ProcessingQuery.__init__` 增加 `parsing_query: ParsingQuery | None = None` 参数
- `get_video_full(uid, bvid)`：
  - 先 `self._parse_qry.get_video_detail(uid, bvid)` 拿元数据 dict（含 title/duration/tags）
  - 再 `self.get_item(uid, "audio", bvid)` 拿 transcription
  - 用 parsing dict 的字段构造 `VideoFullDTO.metadata` 的 result（直接用 parsing 出口面）
- `list_all_videos(uid)`：
  - 用 `self._parse_qry.list_video_details(uid)` 列出所有元数据
  - 每条用 bvid 去 processing 查 transcription 状态
- 删 fetching fallback 分支（`get_video_full` 里那段 `fetching_query.get_video_detail` 兜底用不上了，parsing 能覆盖）

> **注**：`VideoFullDTO.metadata` 仍然是 `ProcessingItemDTO`。parsing dict 没有 status / processed_at 字段，需要构造一个虚拟 DTO（status=SUCCESS, processed_at=parsing 的 updated_at）。也可以直接把 `VideoFullDTO.metadata` 的类型改成 `dict | None`，但那是 DTO 改造，外部 caller 要跟着改。**选简单的**：构造虚拟 ProcessingItemDTO，pipeline 字段填 "parsing"。

### 5. 收缩 CLI

**`bili_unit/__main__.py`**：
- `process` 子命令删除 `--exclude-item-types/-x` 与 `--item-types/-t` 两个互斥参数
- 删 `from bili_unit.processing.transform import HANDLERS` + `_resolve_subset` 的 item_type 调用
- `cmd.process(args.uid, mode=args.mode, item_types=item_types)` → `cmd.process(args.uid, mode=args.mode)`
- `BiliCommand.process` 同步去掉 `pipelines` / `item_types` 参数

**`bili_unit/processing/__main__.py`**（legacy 包装）：
- legacy `process` 命令保留 uid 处理，去掉对不再存在的 `-t` / `-x` flag 的转发；用户传了不识别参数让 argparse 自然报错

### 6. 收缩 env / data / keys / task

- **env.py**：删 `bili_processing_transform_workers` 字段
- **data.py**：无需改动（`update_task_pipeline` 是通用的）
- **keys.py**：`_proc_key` docstring 把 `item_type ∈ {...}` 改成 `item_type ∈ {"audio"}`；`_progress_key` docstring 删除 transform 分支说明（保留 pipeline-only 形态）
- **task.py**：无需改动（`PipelineEntry` 是通用的）
- **__init__.py**：`ProcessingPipelineStatus` 注释里"transform / audio"改为"audio"；其它枚举/异常/DTO 不动

### 7. 测试改造

- **`test_processing_transform.py`**：整个文件删除（15 个测试）
- **`test_processing_runner.py`**：
  - 删除 `_seed_parsing_user_profile` / `_seed_parsing_content_posts` 两个 helper
  - 删除以下用例（共 11 个）：
    - `test_processing_video_metadata_happy_path`
    - `test_processing_content_posts`
    - `test_processing_content_post_with_markdown`
    - `test_processing_content_post_with_images`
    - `test_processing_content_post_with_cross_refs`
    - `test_processing_user_profile_happy_path`
    - `test_processing_incremental_skip_existing`
    - `test_processing_full_mode_overwrites`
    - `test_processing_endpoint_unavailable_skips_handler`
    - `test_processing_handler_failure_records_error`
    - `test_processing_video_full_view`（改造后保留：用 parsing seed 验证联合视图）
  - 改造 `test_processing_video_full_view`：seed parsing video_detail，audio 处理一遍，断言 `qry.get_video_full` 返回包含 metadata（来自 parsing）+ transcription（来自 audio）
  - 把 5 处 `from bili_unit.processing.transform._base import WorkItem` 改成新位置（`from bili_unit.processing.runner._pipeline_executor import WorkItem`）
- **`test_processing_data_error.py`**：
  - `test_data_update_task_pipeline` 把 "transform" 字面量改成 "audio"（验证通用机制，不绑定具体 pipeline）
  - `test_data_progress_keys` 第二个 case 改成 audio progress 形态
  - `test_error_record_and_list` 把 `pipeline="transform"` 改成 `pipeline="audio"`（保留两条 audio 记录或一条 audio + 一条 None）
- audio 测试（`test_processing_audio.py` / `test_processing_audio_cache.py` / `test_processing_audio_vad.py`）不动

### 8. 文档

- **`docs/feature/processing.md`**：
  - 概述 / 模块结构 / 工作项与 handler / 装配 / CLI / 配置 / 测试矩阵 全部重写
  - 保留 audio pipeline 全部章节（CDN / 分段 / VAD / 缓存 / MiMo）
  - 顶上加一段说明："2026-06-14 起 processing 仅持有 audio pipeline；transform 子系统已删除（理由：parsing 重构后 transform 退化为字段透传，且 ingestion 未实装无契约要保护）。视频元数据 / 内容帖 / UP 主画像直接消费 `parsing.query` 出口面。"
- **`docs/structure/bili.md`**：
  - L70-78 处理段：删 `transform` 行；保留 `audio` / `env` / `task` / `runner`；说明改成"runner 编排单一 audio pipeline"
  - L255 树形结构：`processing/` 注释从"transform / audio / runner / ..."改成"audio / runner / ..."
  - 数据流图（§6）：删 `runner → transform ← parsing.data` 这条线
- **`docs/feature/parsing-refactor-plan.md`**：
  - 顶部"实施进度"加一行 / 一段："Slice C 完成后续：processing transform 删除（2026-06-14）"。Slice D 不变。
- **`docs/handoff/2026-06-11-bili-user-profile-handler.md`**：
  - 顶部加大字状态块：

```markdown
> ⚠️ **本文档已废弃（2026-06-14）**：processing 不再实现 user_profile handler。
> ingestion 待实装时直接消费 parsing.query.get_user_profile()。
> 历史背景保留供参考；下方"落点 / 测试 / 输出 schema"等具体要求不再适用。
```

- **`README.md`**：
  - L9 段："处理（processing）：从解析结果派生五类结构化记录..."改成"处理（processing）：对视频音频做 ASR 转录..."
  - L109 段：删除"audio pipeline 在 transform 之外独立运行：video_metadata 写入后由 ASR 后端转录视频音频"，改为单纯描述 audio pipeline
  - L143 测试覆盖描述：去掉"处理 transform"
  - 其它处用到 dynamics/articles/opus/user_profile 五类记录的地方同步修订（已经在 parsing 重构里改过，这次再扫一遍）

## 验证步骤

```bash
uv run pytest -q                       # 全部通过；transform 测试已删除
uv run ruff check bili_unit            # 无 lint
uv run python -m bili_unit process 13991807 -m incremental  # 走 audio
uv run python -m bili_unit query 13991807                   # 不报错
uv run python -m bili_unit video-full 13991807 BV1xxx       # metadata 来自 parsing，transcription 来自 audio
```

预期 pytest 数量从 434 降到 ~408（删 15 个 transform 单测 + 11 个 runner transform 用例，新增 0~1 个改造的 video_full 用例）。

## 后续清理（不在本轮）

- 删除已落盘的旧 `data/bili/processing/data/{uid}/proc/{video_metadata,content_post,user_profile}/` 目录（用户视情况执行；代码不读了）
- 等 ingestion 真正动工时，重新评估"是否要在 parsing 之上加一层稳定 view"——届时再决定是否复活 transform 或在别处建
- subtitle / OCR pipeline 单独提议，不绑在本轮

## 影响摘要

| 维度 | 变化 |
|---|---|
| 删除文件 | 6（transform/ 5 个 + runner/_transform.py） |
| 修改 .py 文件 | 9（runner/__init__.py, command.py, query.py, env.py, keys.py, processing/__init__.py, processing/__main__.py, query/__init__.py, command/__init__.py, __main__.py, _pipeline_executor.py, _audio.py） |
| 删除测试 | 26（transform 15 + runner 11） |
| 改造测试 | ~5（runner WorkItem import + video_full + data_error 3 处） |
| 文档变更 | 5（processing.md / bili.md / parsing-refactor-plan.md / README.md / handoff 标废） |
| 代码净减少 | ~600 行（transform 子包 ~240 + _transform.py 283 + 测试 ~250 = ~770；新增 ~50 行调整 query.py） |
