# fetching_feature — B站用户数据抓取层代码现状

> 记录 `bili_unit/fetching` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`
> 对应数据契约：`docs/structure/fetching-contract.md`、`docs/schema.md`

## 概述

fetching 层负责从 B站 API 异步抓取指定用户的数据，支持 64 个端点、6 种抓取模式（none / page / cursor / anchor / legacy_offset / oid）、全局与端点级限流、增量扫描和 item-level fan-out。原始响应直接落盘到 SQLite 的 `{uid}.raw.db`（`raw_payload` + `fetch_progress` 两张表）；任务/端点状态机与错误单独记录在 `{uid}.db` 的 `stage_task` / `fetch_endpoint_state` / `stage_error` 三张表。底层使用 `bilibili-api-python` 异步封装，HTTP 后端优先 `curl_cffi`，备选 `aiohttp`。

## 模块结构

```
bili_unit/
├── fetching/
│   ├── __init__.py            # status enum、异常、CommandResult / TaskResult、assemble()
│   ├── _adapter_core.py       # bilibili-api-python 错误映射、JSON-safe、列表 shape helper
│   ├── _adapters/             # 领域 adapter（video 等），_bilibili_adapter 作为兼容 facade
│   ├── _bilibili_adapter.py   # bilibili-api-python facade + fetch_endpoint
│   ├── _endpoint_catalog.py   # 合并 endpoint groups + PROFILES + resolve_profile()
│   ├── _endpoint_groups/      # user / video / content / channel_upower 端点分组
│   ├── _endpoint_spec.py      # EndpointSpec dataclass
│   ├── _store.py              # FetchingStore（写 raw / main DB；唯一存储入口）
│   ├── auth.py                # 凭据管理（环境变量读取、QR 登录、保存）
│   ├── command.py             # Command.fetch_uid()
│   ├── rate_limit.py          # 限流控制器（QPS + 412 恢复；进程内内存）
│   └── runner/
│       ├── __init__.py        # Runner 类、编排、helpers
│       ├── _failure.py        # fetching retry 分类 + endpoint 失败状态写入
│       ├── _item_ids.py       # item ID 提取（纯函数）
│       ├── _run_scope.py      # fetch run scope：endpoint 集合、resume 决策、task 准备
│       ├── _endpoint.py       # _EndpointMixin._run_endpoint（走 RetryDriver）
│       └── _item_fanout.py    # _ItemFanoutMixin._run_item_endpoint / _process_single_item
├── _db/                       # SQLite 持久化层（paths / connection / context / DDL）
├── _retry.py                  # 共享 RetryDriver（fetching / processing 通用）
└── tests/                     # pytest 单测 + 集成（mock 网络）
```

### import 边界

```text
command → runner, _store, _db, rate_limit, _env, fetching DTO
runner  → _store, rate_limit, _bilibili_adapter, auth, _endpoint_catalog, _retry
_store  → _db (UidContext)
auth    → _env, fetching DTO（AuthError）
rate_limit → 仅 stdlib + aiolimiter（不再持久化）
```

## 端点注册表

实际注册 64 个端点（34 uid-level + 30 item-level），其中 parsing 层目前消费 13 个；其余端点抓取后落盘但暂无消费方，可通过 CLI `--profile parsing` 跳过以缩短运行时间（issue #2）。

64 个端点分为两类：uid-level（直接按 uid 抓取）和 item-level（从源端点提取 items 后逐个抓取）。

### 扩展后端点总览（当前真相）

```text
uid-level（34 个）
  user_info videos access_id relation_info up_stat overview_stat articles
  subscribed_bangumi opus dynamics audios channel_list
  channels media_list user_medal live_info user_relation reservation
  uplikeimg top_followers space_notice all_followings followings followers
  same_followers top_videos masterpiece article_list cheese elec_monthly
  user_fav_tag album upower_qa

item-level（30 个）
  video_detail video_pages video_detail_full video_ai_conclusion
  video_danmaku_snapshot video_danmaku_view video_danmaku_xml video_danmakus
  video_online video_pay_coins video_pbp video_player_info video_private_notes
  video_public_notes video_related video_relation video_special_dms video_subtitle
  video_up_mid video_snapshot video_download_url video_is_episode
  video_is_forbid_note video_chargers article_detail opus_detail
  article_list_detail channel_videos_season channel_videos_series upower_qa_detail

credential_required
  video_pay_coins video_private_notes video_relation user_medal user_relation
  top_followers all_followings same_followers elec_monthly upower_qa
  upower_qa_detail
```

新增扩展端点目前以 mock 测试锁定注册、分页和 fan-out 行为，真实站点可用性取决于 B 站权限、风控和 bilibili-api-python 对应接口状态。

### uid-level 端点（34 个）

真相源：`bili_unit/fetching/_endpoint_catalog.py`。

| 端点 | 分页策略 | 限流 key | item_id_path | 需凭据 |
|------|---------|----------|-------------|------|
| access_id | none | access_id | — | |
| album | page (page_num/page_size) | album | — | |
| all_followings | none | all_followings | — | ✓ |
| article_list | none | article_list | — | |
| articles | page (pn/ps) | articles | articles[*].id | |
| audios | page (pn/ps) | audios | data[*].id | |
| channel_list | page (pn/ps) | channel_list | items_lists.seasons_list[*].meta.season_id, items_lists.series_list[*].meta.series_id | |
| channels | none | channels | — | |
| cheese | none | cheese | — | |
| dynamics | cursor (offset) | dynamics | items[*].id_str | |
| elec_monthly | none | elec_monthly | — | ✓ |
| followers | page (pn/ps) | followers | list[*].mid | |
| followings | page (pn/ps) | followings | list[*].mid | |
| live_info | none | live_info | — | |
| masterpiece | none | masterpiece | — | |
| media_list | oid | media_list | media_list[*].bvid, list[*].bvid, items[*].bvid | |
| opus | cursor (offset) | opus | items[*].opus_id | |
| overview_stat | none | overview_stat | — | |
| relation_info | none | relation_info | — | |
| reservation | none | reservation | — | |
| same_followers | page (pn/ps) | same_followers | list[*].mid | ✓ |
| space_notice | none | space_notice | — | |
| subscribed_bangumi | page (pn/ps) | subscribed_bangumi | list[*].season_id | |
| top_followers | none | top_followers | — | ✓ |
| top_videos | none | top_videos | — | |
| up_stat | none | up_stat | — | |
| uplikeimg | none | uplikeimg | — | |
| upower_qa | anchor | upower_qa | list[*].qa_id | ✓ |
| user_fav_tag | page (pn/ps) | user_fav_tag | — | |
| user_info | none | user_info | — | |
| user_medal | none | user_medal | — | ✓ |
| user_relation | none | user_relation | — | ✓ |
| videos | page (pn/ps) | videos | list.vlist[*].bvid | |

> 已知 library quirk：`album` 使用 `page_num`/`page_size` 参数名（client 层从 `pn`/`ps` 映射）；`masterpiece` 原始响应是 list，经 `_wrap_list_result` 包装为 `{"list": [...]}`；`user_fav_tag` 在 bilibili-api-python 内 `pn`/`ps` 被注释，实际只返回第一页。

### item-level 端点（30 个）

真相源：`bili_unit/fetching/_endpoint_catalog.py`。所有 item-level 端点 `pagination_strategy="none"`，分页/总量逻辑在 callable 内部完成。

| 端点 | 源端点 | 限流 key | extract_items | 需凭据 |
|------|--------|---------|-------------|------|
| article_detail | articles | article_detail | _extract_cvids_from_articles | |
| article_list_detail | article_list | article_list_detail | _extract_rlids_from_article_list | |
| channel_videos_season | channel_list | channel_videos_season | _extract_season_ids | |
| channel_videos_series | channel_list | channel_videos_series | _extract_series_ids | |
| opus_detail | opus | opus_detail | _extract_opus_ids_from_opus | |
| upower_qa_detail | upower_qa | upower_qa_detail | _extract_qa_ids_from_upower_qa | ✓ |
| video_ai_conclusion | videos | video_ai_conclusion | _extract_bvids_from_videos | |
| video_chargers | videos | video_chargers | _extract_bvids_from_videos | |
| video_danmaku_snapshot | videos | video_danmaku_snapshot | _extract_bvids_from_videos | |
| video_danmaku_view | videos | video_danmaku_view | _extract_bvids_from_videos | |
| video_danmaku_xml | videos | video_danmaku_xml | _extract_bvids_from_videos | |
| video_danmakus | videos | video_danmakus | _extract_bvids_from_videos | |
| video_detail | videos | video_detail | _extract_bvids_from_videos | |
| video_detail_full | videos | video_detail_full | _extract_bvids_from_videos | |
| video_download_url | videos | video_download_url | _extract_bvids_from_videos | |
| video_is_episode | videos | video_is_episode | _extract_bvids_from_videos | |
| video_is_forbid_note | videos | video_is_forbid_note | _extract_bvids_from_videos | |
| video_online | videos | video_online | _extract_bvids_from_videos | |
| video_pages | videos | video_pages | _extract_bvids_from_videos | |
| video_pay_coins | videos | video_pay_coins | _extract_bvids_from_videos | ✓ |
| video_pbp | videos | video_pbp | _extract_bvids_from_videos | |
| video_player_info | videos | video_player_info | _extract_bvids_from_videos | |
| video_private_notes | videos | video_private_notes | _extract_bvids_from_videos | ✓ |
| video_public_notes | videos | video_public_notes | _extract_bvids_from_videos | |
| video_related | videos | video_related | _extract_bvids_from_videos | |
| video_relation | videos | video_relation | _extract_bvids_from_videos | ✓ |
| video_snapshot | videos | video_snapshot | _extract_bvids_from_videos | |
| video_special_dms | videos | video_special_dms | _extract_bvids_from_videos | |
| video_subtitle | videos | video_subtitle | _extract_bvids_from_videos | |
| video_up_mid | videos | video_up_mid | _extract_bvids_from_videos | |

item-level 端点共享独立的低 QPS（默认 0.5，即 2 秒间隔），避免逐项 fan-out 烧光全局配额触发 B站 412。除 `video_detail`（get_info + get_tags）/ `article_detail`（get_info + fetch_content + markdown + json）/ `opus_detail`（get_info + markdown + get_images_raw_info）/ `article_list_detail`（get_content）/ `channel_videos_{season,series}`（内部游标分页合并）/ `upower_qa_detail`（get_upower_qa_detail）有专门 callable 外，其余 23 个 video_* 端点由 `_video_item_method(name, per_page, page_arg, result_key)` 工厂统一构造。

## 抓取范围（Profile）

CLI `--profile {all,parsing,minimal}` 控制本次抓取的端点子集（issue #2）。`-p` 与 `-e` / `-x` 互斥。

| Profile | 端点数 | 典型耗时 (中等账号) | 用途 |
|---------|--------|----------------------|------|
| all (默认) | 64 | ~17 分钟 | 完整存档，向后兼容 |
| parsing | 13 | ~2-3 分钟 | parsing 层实际消费的端点；推荐 |
| minimal | 5 | <1 分钟 | smoke / CI / 调试 |

`parsing` 集合：`user_info` `relation_info` `up_stat` `overview_stat` `articles` `article_detail` `article_list_detail` `opus` `opus_detail` `dynamics` `videos` `video_detail` `video_subtitle`。

`minimal` 集合：`user_info` `videos` `articles` `opus` `dynamics`。

实现位于 `bili_unit/fetching/_endpoint_catalog.py` 的 `PROFILES` 常量与 `resolve_profile()`；新增 parsing 消费方时同步更新该常量即可。

## 抓取模式

### incremental（默认）

首次运行时等同于全量抓取。对已成功的任务进入增量扫描：

- 分页端点：从第 1 页开始检查，用 item_id_path 提取 ID 与已知集合对比；遇到 boundary（首页全已知）时取一个 safety page 后停止
- 无分页端点：直接覆盖
- item-level 端点：跳过已存储的 items（`FetchingStore.list_completed_items(endpoint)` 直接 SELECT raw_payload），仅抓取新增

增量模式的边界检测意味着：如果 UP 主没有新投稿，增量运行通常只需 1-2 页 API 调用。

### full

完全重新抓取所有端点，忽略已有数据。分页端点从头到尾全部重新拉取。

### refresh

介于 incremental 和 full 之间：对 item-level 端点检查 freshness window（默认 7 天），过期的 items 重新抓取，未过期的跳过（`FetchingStore.list_item_ages_ms(endpoint)` 返回 `{item_id: fetched_at_ms}`，runner 据此过滤）。uid-level 端点行为与 incremental 相同。

**作用场景**：video_detail 中包含播放量、点赞数、标签等随时间变化的字段。incremental 模式只抓取新增视频，旧视频的数据停留在首次抓取时的状态；refresh 模式会自动刷新超过 7 天的旧数据，保持时效性字段的相对新鲜。适用于定期调度的场景（如每日/每周跑一次任务）。

**当前状态**：video_detail 目前仅存储 get_info + get_tags 的结果，其中 tags 基本不变，播放量等时效性字段尚未被上层消费。因此现阶段 refresh 与 incremental 效果差异不大，待后续处理层开始使用播放量等动态字段后，refresh 的价值会充分体现。

配置项：`BILI_FETCHING_REFRESH_AFTER_DAYS`（默认 7 天）。

### 幂等规则

重复调用 `fetch_uid(uid)` 时，runner 根据已有 `stage_task[stage='fetching'].status` 决策：

| task 状态 | incremental / refresh | full |
|-----------|----------------------|------|
| 不存在 | 创建新 task，全量抓取 | 创建新 task，全量抓取 |
| RUNNING (recent) | 直接返回，不启动第二个 runner | 直接返回，不启动第二个 runner |
| RUNNING (stale, > BILI_FETCHING_STALE_RUNNING_THRESHOLD_SECONDS) | 接续未完成端点（同 PARTIAL） | 全量重抓（同 PARTIAL） |
| SUCCESS | 进入增量扫描 | 忽略已有数据，全量重抓 |
| PARTIAL | 接续未成功的 endpoint | 重置所有 endpoint，全量重抓 |
| FAILED_RETRYABLE | 继续重试 | 重置所有 endpoint，全量重抓 |
| FAILED_EXHAUSTED | 重置 retry_count，接续未成功 endpoint | 重置所有 endpoint，全量重抓 |
| FAILED_PERMANENT | 不自动重跑，直接返回 | 不自动重跑，直接返回 |

覆盖规则：两种模式均以 endpoint 为粒度覆盖 `raw_payload(endpoint, item_id='')`，分页端点在该行内存合并后的 `{pages: [...]}` dict；item-level fan-out 则按 `(endpoint, item_id)` 分行写入。

### 增量模式算法

增量扫描的核心是 **known_ids 集合**：从 `raw_payload[endpoint, item_id=''].pages` 中用 `item_id_path` 提取所有已知 item ID，然后逐页检查新页面的 ID 是否全部已知。

**ID 提取函数** `_extract_item_ids(raw_payload, path)`：

- 路径格式：dot-path + `[*]` 展开，如 `list.vlist[*].bvid`
- `[*]` 前的段做 dict 键访问，遇到 `[*]` 时对当前列表逐元素展开剩余路径
- `[*]` 后支持多段 dict 键（如 `meta.season_id`）
- 任一段缺失或类型不匹配时返回空列表并记录 warning
- 无 `[*]` 的路径直接做 dict 键访问

**多路径聚合** `_extract_item_ids_multi(raw_payload, paths)`：

逐路径调用 `_extract_item_ids` 并拼接结果。channel_list 使用此机制聚合 `seasons_list` 和 `series_list` 的 ID。

**known_ids 构建**：

1. 从 raw DB 读取 `raw_payload(endpoint, item_id='')` 的 payload（调用 `FetchingStore.get_raw_payload(endpoint)`）
2. 遍历 `payload.pages`，对每页调用 `_extract_item_ids_multi` 提取 ID
3. 所有 ID 加入 `set`（自然去重）
4. 无存储数据时 `known_ids = None`，退化为全量抓取

**增量扫描流程**：

1. 从第 1 页开始逐页抓取
2. 每页提取 page_ids，计算 `new_ids = page_ids - known_ids`
3. `new_ids` 非空 → 加入 known_ids → 继续下一页
4. `new_ids` 为空且 page_ids 非空 → **boundary hit**：再抓一页兜底（safety page），然后停止
5. 分页自然结束（is_last_page）→ 正常停止
6. 本次运行请求的所有页累积为新 `payload.pages`，覆盖写入 `raw_payload`

增量模式下，分页 listing 端点的 `raw_payload(endpoint, item_id='')`
表示“最近一次增量扫描窗口”，不承诺保存全历史 listing 页。完整 item
事实以 item-level fan-out 行为准，例如 `raw_payload('video_detail', bvid)`
会按 bvid 长期保留并被后续 parsing 使用。

`items_path` 与 `item_id_path` 的区别：`items_path` 在 client 模块用于**分页终止检测**（判断当前页是否为最后一页）；`item_id_path` 在 runner 模块用于**增量 ID 对比**（判断 item 是否已知）。两者可指向不同位置。

## 两阶段执行引擎

`runner` 包采用 mixin + fetch run scope 拆分模式，将原 966 行的单体文件分解为多个内部模块：

- `__init__.py` — Runner 类、公共 API（run_task / resume_task / run_or_resume）、编排逻辑（_run）、helpers
- `_item_ids.py` — `_extract_item_ids` / `_extract_item_ids_multi` 纯函数
- `_run_scope.py` — `FetchRunPlanner` / `FetchRunScope`，集中处理 endpoint 集合、fresh/resume、stale RUNNING 接管、SUCCESS 后 incremental/full 决策
- `_endpoint.py` — `_EndpointMixin._run_endpoint`：单端点抓取 + 增量扫描 + 重试
- `_item_fanout.py` — `_ItemFanoutMixin._run_item_endpoint` / `_process_single_item`：item-level fan-out

Runner 类通过 `class Runner(_EndpointMixin, _ItemFanoutMixin)` 组合 mixin。重试逻辑已抽取到顶层 `bili_unit/_retry.py`（`RetryDriver` + `RetryPolicy`），fetching / processing 共享同一套实现；`_endpoint.py` 和 `_item_fanout.py` 通过 `RetryDriver.run()` 编排重试，回调处理 412 advice 和 AuthError 中止。`fetch_endpoint` 由 `Command` 构造时注入 Runner（`fetch_fn=`），默认走 `_bilibili_adapter.fetch_endpoint`，方便测试 patch。

两阶段并行执行：

**Phase 1**：所有 uid-level 端点并行抓取（`asyncio.gather`）。对 item-level 端点检查其 source_endpoint 是否在 task 中，如果不在则自动添加并运行。

**Phase 2**：所有 item-level 端点并行执行 fan-out。读取源端点的 `raw_payload`，提取 items，按 `item_concurrency`（默认 3）并发抓取每个 item。

每个阶段内部有独立的错误处理：AuthError → 整个 fan-out 立即终止（FAILED_PERMANENT）；412 → 重试（指数退避，最多 max_retries 次）；其他 FetchingError → 重试；非预期异常 → FAILED_PERMANENT。

## 限流机制

`RateLimitController` 实现双层限流：

- **全局 QPS**（默认 1.0）：所有端点共享
- **端点 QPS**（默认 0.5）：每个端点独立
- **video_detail QPS**（默认 0.5）：独立且更低

412 恢复机制：收到 412 后将对应 QPS 减半，进入 cooldown 期（默认 300 秒）；cooldown 结束后 QPS 翻倍恢复（不超过原始值）；连续多个 412 会持续降低 QPS。

> 限流状态**仅保存在进程内内存**（不再持久化）。重启后 cooldown 重算 —— 这是 SQLite 重构时刻意做的简化（见 `docs/history/refactor-plan-sqlite.md` §11 决策点）。

## 日志事件

runner 模块使用结构化日志（`logger.info` / `logger.warning`），不输出完整 Cookie / Credential。

**任务入口事件**：

| 事件 | 级别 | 字段 | 说明 |
|------|------|------|------|
| command_received | info | uid, mode | Command 收到抓取请求 |
| task_already_running | info | uid, age_ms | task RUNNING 且 updated_at 在阈值内，跳过 |
| task_stale_running_takeover | warning | uid, age_ms, threshold_ms | RUNNING 任务 updated_at 超过 stale 阈值，作为 PARTIAL 接管 |
| task_failed_permanent_skip | info | uid | task FAILED_PERMANENT，跳过 |
| task_incremental_scan | info | uid, mode | SUCCESS → 进入增量扫描 |
| task_full_refetch | info | uid | SUCCESS → 全量重抓 |

**增量扫描事件**：

| 事件 | 级别 | 字段 | 说明 |
|------|------|------|------|
| incremental_scan_started | info | uid, endpoint, known_id_count | 扫描开始 |
| incremental_no_stored_data | info | uid, endpoint | 无存储数据，退化为全量 |
| incremental_page_checked | info | uid, endpoint, new_count, known_count, total_page_ids | 页 ID 检查完成 |
| incremental_boundary_hit | info | uid, endpoint | 全页已知，边界命中 |
| incremental_safety_page | info | uid, endpoint | 兜底页抓取完成 |
| incremental_safety_page_failed | warning | uid, endpoint, error | 兜底页抓取失败 |
| incremental_completed | info | uid, endpoint, total_pages_fetched, new_item_count, mode | 扫描完成 |

**重试与端点事件**：

| 事件 | 级别 | 字段 | 说明 |
|------|------|------|------|
| retry_scheduled | info | uid, endpoint, wait_s, retry | 重试等待（412 / FetchingError） |
| endpoint_page_saved | info | uid, endpoint | 当前页写入完成 |

**item-level fan-out 事件**：

| 事件 | 级别 | 字段 | 说明 |
|------|------|------|------|
| item_endpoint_source_failed | info | uid, endpoint, source_endpoint | source 数据不可用 |
| item_endpoint_no_items | info | uid, endpoint | 无 items 可抓取 |
| item_endpoint_completed | info | uid, endpoint, status, completed, failed, total | fan-out 全部完成 |
| item_endpoint_retry | info | uid, endpoint, item_id, wait_s, retry | 单 item 重试 |
| item_endpoint_item_exhausted | warning | uid, endpoint, item_id, retry | 单 item 重试耗尽 |
| item_endpoint_item_saved | info | uid, endpoint, item_id | 单 item 写入完成 |

**path 解析 warning**（`_extract_item_ids` 容错时发出）：

- `item_id_path: expected list at [*], got %s`
- `item_id_path: key %r not found`
- `item_id_path extraction failed for path %r: %s`

## 存储层

fetching 写两个 SQLite 库（DDL：[main_v2.sql](../../bili_unit/_db/ddl/main_v2.sql)、[raw_v1.sql](../../bili_unit/_db/ddl/raw_v1.sql)）。表语义见 [docs/schema.md](../schema.md)。常用 CLI 入口是 `sync <uid>`，它会先跑 fetching 再跑 parsing；`fetch <uid>` 保留为 raw-only / 调试入口。

### Raw DB（`{bili_db_dir}/{uid}.raw.db`）

**`raw_payload(endpoint, item_id, payload, fetched_at_ms)`**

- `item_id=''` —— uid-level 响应（分页端点合并后的 `{pages: [...]}` dict 整体存为单行）
- `item_id=bvid / cvid / opus_id / dynamic_id / rlid / season_id / ...` —— item-level fan-out 子项
- `payload` 是 UTF-8 JSON 字符串；`fetched_at_ms` 是 ms-epoch
- 索引 `idx_raw_endpoint(endpoint, fetched_at_ms)` 加速「最近抓取的 N 行」查询

**`fetch_progress(endpoint, cursor, total, fetched, updated_at_ms)`**

每端点一行，作为 commit marker：runner 通过 `FetchingStore.save_raw_page_and_progress(...)` 在单事务内先写 `raw_payload` 再写 `fetch_progress`，崩溃落在中间时 `progress` 是旧值，下次 resume 从旧游标重新拉（payload 幂等覆盖）。

`cursor` 是 TEXT，可能是裸字符串（`"30"`）或 JSON 序列化后的请求字典（`{"pn": 2, "ps": 30}`）。`FetchingStore.get_progress()` 在读出时根据 `[`/`{` 前缀尝试 JSON 解码。

### Main DB（`{bili_db_dir}/{uid}.db`）

**`stage_task[stage='fetching']`** —— 任务包络：`status` + `payload` JSON（`{"endpoints": [...]}`） + `created_at_ms` / `updated_at_ms`。`init_task(endpoints)` idempotent（INSERT OR IGNORE）。

**`fetch_endpoint_state(endpoint, status, retry_count, last_error_id, item_progress, progress, updated_at_ms)`** —— 每端点一行的状态机视图。`item_progress` 与 `progress` 都是 JSON：

- `item_progress`（item-level 端点）：`{"total": 77, "completed": 50, "failed": 0}`
- `progress`（分页端点）：与 `fetch_progress.cursor` 对应的内层结构（裸字符串 / 请求字典）

**`stage_error[stage='fetching']`** —— 错误 sink。`record_error(...)` 通过 `INSERT ... RETURNING id` 一次性写入并取回新行 id，写回 `fetch_endpoint_state.last_error_id`。`retryable` 三态：1 / 0 / NULL（unknown）。`detail` 是 JSON dict，对 item-level 失败会带 `{"item_id": ...}`，便于 `list_failed_items(endpoint)` 反查。

> 失败 item 不再持久化为 task 字段；终结状态时由 `FetchingStore.list_failed_items(endpoint)` 现算（读 `stage_error` 取出 detail 中的 item_id，再减去 `raw_payload` 已成功项），retry-to-success 自动从结果中剔除（参见 commit `f29a42e`）。

### FetchingStore 关键方法

`bili_unit/fetching/_store.py` 暴露的全部方法都通过持有的 `UidContext` 同时操作 main + raw DB：

```python
# raw DB writes
async def save_raw_payload(endpoint, item_id, payload, *, fetched_at_ms=None) -> None
async def save_raw_page_and_progress(endpoint, item_id, payload, progress, *, fetched_at_ms=None) -> None
async def save_progress(endpoint, progress, *, updated_at_ms=None) -> None

# raw DB reads
async def get_raw_payload(endpoint, item_id="") -> dict | None
async def get_progress(endpoint) -> dict | None
async def list_completed_items(endpoint) -> list[str]      # raw_payload WHERE item_id <> ''
async def list_fanout_payloads(endpoint) -> dict[str, dict]
async def list_item_ages_ms(endpoint) -> dict[str, int]    # for refresh-mode

# main DB writes
async def init_task(endpoints) -> None
async def prepare_task_run(endpoints, *, fresh, mode) -> None
async def update_task_status(status) -> None
async def update_endpoint_state(endpoint, *, status, retry_count=0, last_error_id=None,
                                item_progress=None, progress=None) -> None

# main DB reads
async def get_task_status() -> str | None
async def get_task_updated_at() -> int | None
async def list_endpoint_names() -> list[str]
async def list_endpoint_statuses(endpoints=None) -> dict[str, str]
async def get_endpoint_status(endpoint) -> str | None
async def get_endpoint_state(endpoint) -> dict | None
async def list_failed_items(endpoint) -> list[str]

# error sink
async def record_error(*, endpoint, error_type, message, retryable, detail=None,
                       occurred_at_ms=None) -> int
async def list_errors(endpoint=None) -> list[dict]
```

写操作通过 `Connection` 内部的 `asyncio.Lock` 串行；多语句事务（如 `save_raw_page_and_progress`、`init_task`）走 `run_transaction`。

## CLI 用法

只有写侧子命令；读侧用 `sqlite3` 直连 `db_path(uid)`：

```bash
uv run python -m bili_unit sync <uid>                              # 常用：增量抓取后立即解析
uv run python -m bili_unit sync <uid> --fetch-mode full            # 常用：全量抓取后解析
uv run python -m bili_unit sync <uid> -p parsing                   # 常用：仅抓 parsing 层消费端点后解析
uv run python -m bili_unit sync <uid> -i                           # 同步后下载图片
uv run python -m bili_unit fetch <uid>                             # 高级：只抓 raw payload，不解析
uv run python -m bili_unit fetch <uid> -e user_info videos         # 高级：仅指定端点（调试用）
uv run python -m bili_unit login                                   # QR 扫码登录
uv run python -m bili_unit delete-uid <uid>                        # 删除指定 uid 全部数据（交互确认）
uv run python -m bili_unit delete-uid <uid> -y                     # 删除（跳过确认）

# 只读（无 CLI 子命令；直接 SQL）：
sqlite3 output/bili/{uid}.db "SELECT * FROM manifest_summary;"
sqlite3 output/bili/{uid}.db "SELECT endpoint, status, retry_count FROM fetch_endpoint_state;"
sqlite3 output/bili/{uid}.db "SELECT id, endpoint, error_type, message FROM stage_error WHERE stage='fetching' ORDER BY id DESC LIMIT 20;"
```

CLI 常用入口是 `python -m bili_unit sync ...`；`fetch ...` 是 raw-only / 调试入口。不再保留 `python -m bili_unit.fetching` shim。

## 装配函数

`bili_unit.fetching.assemble(settings)` 是 stage 装配入口，**返回单值** `Command`（读侧消费方直接 SQL，不再有 Python query facade）：

```python
async def assemble(settings: BiliSettings | None = None) -> Command
```

读取设置 → 初始化 HTTP 后端 → 创建限流控制器 → 返回 `Command`。`Command` 不持有 store；每次 `fetch_uid` 自开自关 `UidContext` + `FetchingStore`。

### Command 接口

```python
async def fetch_uid(uid: int, endpoints: list[str] | None = None,
                    mode: str = "incremental") -> CommandResult
async def delete_uid(uid: int) -> dict[str, int]    # no-op；BiliCommand 层做文件删除
async def close() -> None                            # no-op；store 是请求级
```

`CommandResult` 字段：`uid: int`、`status: TaskStatus`、`run_id: str | None`。
CLI 使用 `run_id` 精确读取本次 run 的 Run Summary；缺失时才退回 uid 最新 run。

### Runner 接口

```python
async def run_task(uid, endpoints=None, mode="incremental") -> TaskResult
async def resume_task(uid, endpoints=None) -> TaskResult
async def run_or_resume(uid, endpoints=None, mode="incremental") -> TaskResult
```

`run_or_resume` 是 Command 调用的入口：检查已有 `stage_task` 状态后决定 run 还是 resume。`run_task` 创建全新 task。`resume_task` 从断点续传（沿用现有 `fetch_endpoint_state` 行的 `progress`）。

## 异常层级

```
FetchingError
├── AuthError                   # 凭据缺失 / 过期 / 拒绝；fan-out 触发立即中止
├── RequestError
│   ├── Http412Error            # B站 412 限流 → runner 触发 QPS 减半 + cooldown
│   └── Http5xxError
└── ResourceUnavailableError    # 已下架 / 隐私受限等已知终态业务码（53013 / 88214 等）
```

`ResourceUnavailableError` 与 `AuthError` 区分：前者只让单端点 / 单 item 进入 `FAILED_PERMANENT`，不会带翻整个 fan-out。

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_SESSDATA | "" | 认证凭据（必填，缺失则 AuthError） |
| BILI_JCT | "" | 认证凭据（可选） |
| BILI_BUVID3 | "" | 认证凭据（可选） |
| BILI_BUVID4 | "" | 认证凭据（可选） |
| BILI_DEDEUSERID | "" | 认证凭据（可选） |
| BILI_AC_TIME_VALUE | "" | 认证凭据（可选） |
| BILI_DB_DIR | output/bili | SQLite DB 根目录（main + raw + workdir 全部派生） |
| BILI_FETCHING_GLOBAL_QPS | 1.0 | 全局 QPS 上限 |
| BILI_FETCHING_ENDPOINT_QPS | 0.5 | 端点级 QPS 上限 |
| BILI_FETCHING_VIDEO_DETAIL_QPS | 0.5 | video_detail 独立 QPS |
| BILI_FETCHING_RECOVERY_COOLDOWN | 300 | 412 恢复冷却秒数 |
| BILI_FETCHING_MAX_RETRIES | 3 | 最大重试次数 |
| BILI_FETCHING_RETRY_DELAYS | 30,60,120 | 重试间隔（秒，逗号分隔） |
| BILI_FETCHING_REQUEST_TIMEOUT | 30 | 单次请求超时秒数 |
| BILI_FETCHING_ITEM_CONCURRENCY | 3 | item fan-out 并发数 |
| BILI_FETCHING_REFRESH_AFTER_DAYS | 7 | refresh 模式过期天数 |
| BILI_FETCHING_STALE_RUNNING_THRESHOLD_SECONDS | 900 | RUNNING 任务 updated_at 超过此秒数视为 stale，自动接管（issue #3） |
| BILI_FETCHING_HTTP_BACKEND | aiohttp | HTTP 后端（curl_cffi / aiohttp） |
| BILI_FETCHING_IMPERSONATE | chrome131 | curl_cffi 指纹伪装 |

> 旧的文件目录 JSON 存储已删除——所有抓取产物落 `{BILI_DB_DIR}/{uid}.raw.db` 与 `{BILI_DB_DIR}/{uid}.db`。

## 测试状态

测试位于 `bili_unit/tests/`，覆盖 store / env / auth / rate_limit / client / runner / item fan-out / command / SQLite 契约 / 集成。`pytest-mock` mock 网络层；通过 `uv run pytest` 跑全套（无网络）。`uv run ruff check` 通过。
