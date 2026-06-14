# fetching_feature — B站用户数据抓取层代码现状

> 记录 `bili_unit/fetching` 的实际代码能力。
> 对应结构约束：`docs/structure/bili.md`
> 对应数据契约：`docs/structure/fetching-contract.md`

## 概述

fetching 层负责从 B站 API 异步抓取指定用户的数据，支持 64 个端点类型、6 种抓取模式（none / page / cursor / anchor / legacy_offset / oid）、全局与端点级限流、增量扫描和 item-level fan-out。底层使用 `bilibili-api-python` 异步封装，HTTP 后端优先 `curl_cffi`，备选 `aiohttp`。

## 模块结构

```
bili_unit/
├── fetching/
│   ├── __init__.py      # DTO、异常、assemble() 装配
│   ├── __main__.py      # thin backward-compat wrapper（转发到统一 CLI）
│   ├── auth.py          # 凭据管理（环境变量读取、QR 登录、保存）
│   ├── client.py        # EndpointSpec 注册表、API 调用、item-level 抓取、_user_method helper
│   ├── command.py       # 写入口：fetch_uid()
│   ├── data.py          # FetchingKeyMapper + RMW helper（底层走 _storage.JsonKVStore）
│   ├── env.py           # 配置管理（pydantic-settings，.env + 环境变量）
│   ├── error.py         # FetchingErrorStore（底层走 _storage.JsonErrorStore）
│   ├── keys.py          # 存储 key 生成函数
│   ├── query.py         # 只读查询接口（含 list_fanout_payloads）
│   ├── rate_limit.py    # 限流控制器（QPS + 412 恢复）
│   ├── runner/          # 两阶段执行引擎（mixin 拆分）
│   │   ├── __init__.py      # Runner 类、编排、helpers
│   │   ├── _item_ids.py     # item ID 提取（纯函数）
│   │   ├── _endpoint.py     # _EndpointMixin._run_endpoint（走 RetryDriver）
│   │   └── _item_fanout.py  # _ItemFanoutMixin._run_item_endpoint / _process_single_item（_ItemFanoutResult StrEnum）
│   ├── task.py          # TaskValue 内部状态模型
│   ├── data/            # 运行时数据目录（JSON 文件）
│   └── error/           # 运行时错误目录（JSON 文件）
├── _retry.py            # 共享 RetryDriver（RetryPolicy + RetryClassification）
├── _storage/            # 共享存储抽象（JsonKVStore + JsonErrorStore，asyncio.to_thread IO）
└── tests/               # pytest 单测 + 集成（mock 网络）
```

### import 边界

```text
command → runner, DTO
query → data, error, task
runner → task, client, rate_limit, data, error, auth, env, _retry
client → auth(Credential 注入), endpoint registry, _user_method helper
auth → env, error
rate_limit → data/error(state persistence)
data/error → _storage (JsonKVStore + KeyMapper)
```

## 端点注册表

实际注册 64 个端点（34 uid-level + 30 item-level），其中 parsing 层目前消费 12 个；其余端点抓取后落盘但暂无消费方，可通过 CLI `--profile parsing` 跳过以缩短运行时间（issue #2）。

64 个端点分为两类：uid-level（直接按 uid 抓取）和 item-level（从源端点提取 items 后逐个抓取）。

### 扩展后端点总览（当前真相）

```text
uid-level（34 个）
  user_info videos access_id relation_info up_stat overview_stat articles
  subscribed_bangumi opus dynamics dynamics_legacy audios channel_list
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
| dynamics_legacy | legacy_offset | dynamics_legacy | cards[*].desc.dynamic_id | |
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

### MVP 范围与实测状态

```text
MVP endpoint（已实测）
  user_info         none 分页
  videos            page(pn/ps) 分页

已扩展 endpoint（已实测）
  relation_info     none
  up_stat           none
  dynamics          cursor(offset)
  audios            page(pn/ps)
  channel_list      page(pn/ps, max 20)

T1 endpoint（已注册，已实测）
  overview_stat     none ✓
  articles          page(pn/ps) ✓  — 响应结构 Shape 4（顶层 count）
  opus              cursor(offset) ✓  — 64 items, 4 pages
  subscribed_bangumi  page(pn/ps, ps=15) — 隐私受限时返回 53013

item-level fan-out endpoint（已实现，已实测）
  video_detail         kind=item, source=videos（77 bvids, 76/76 SUCCESS）
  article_detail       kind=item, source=articles
  opus_detail          kind=item, source=opus
  article_list_detail  kind=item, source=article_list（文集 → cvid 清单）

T2 endpoint（已注册，已实测）
  user_medal        none ✓（credential_required）
  space_notice      none ✓
  all_followings    none ✓（credential_required）
  top_videos        none ✓
  masterpiece       none ✓（返回 list，包装为 dict）
  article_list      none ✓
  cheese            none ✓
  elec_monthly      none — FAILED_EXHAUSTED 88214（充电未开通）
  user_fav_tag      page(pn/ps) — FAILED_EXHAUSTED 53013（隐私受限）；library bug：pn/ps 被注释
  album             page(pn/ps→page_num/page_size) ✓ — Shape 5（total_count）
  channel_videos_season  kind=item, source=channel_list ✓
  channel_videos_series  kind=item, source=channel_list ✓
  upower_qa         anchor ✓（credential_required，1 页）
```

```text
暂不实现
  写接口（modify_relation / set_space_notice / 点赞投币收藏等副作用操作）
  当前登录账号 self 全局数据接口（get_self_history / get_toview_list 等，不属于目标 uid 空间采集）
  视频历史弹幕按日期全量回溯（当前只抓实时/当前可读弹幕相关接口）
```

```text
MVP 约束
  uid-level endpoint 只调用 bilibili_api.user.User(uid) 读取接口。
  item-level endpoint 调用 bilibili_api.video.Video(bvid) 读取接口。
  不调用写接口。
```

## 抓取范围（Profile）

CLI `--profile {all,parsing,minimal}` 控制本次抓取的端点子集（issue #2）。`-p` 与 `-e` / `-x` 互斥。

| Profile | 端点数 | 典型耗时 (中等账号) | 用途 |
|---------|--------|----------------------|------|
| all (默认) | 64 | ~17 分钟 | 完整存档，向后兼容 |
| parsing | 12 | ~2-3 分钟 | parsing 层实际消费的端点；推荐 |
| minimal | 5 | <1 分钟 | smoke / CI / 调试 |

`parsing` 集合：`user_info` `relation_info` `up_stat` `overview_stat` `articles` `article_detail` `article_list_detail` `opus` `opus_detail` `dynamics` `videos` `video_detail`。

`minimal` 集合：`user_info` `videos` `articles` `opus` `dynamics`。

实现位于 `bili_unit/fetching/_endpoint_catalog.py` 的 `PROFILES` 常量与 `resolve_profile()`；新增 parsing 消费方时同步更新该常量即可。

## 抓取模式

### incremental（默认）

首次运行时等同于全量抓取。对已成功的任务进入增量扫描：

- 分页端点：从第 1 页开始检查，用 item_id_path 提取 ID 与已知集合对比；遇到 boundary（首页全已知）时取一个 safety page 后停止
- 无分页端点：直接覆盖
- item-level 端点：跳过已存储的 items，仅抓取新增

增量模式的边界检测意味着：如果 UP 主没有新投稿，增量运行通常只需 1-2 页 API 调用。

### full

完全重新抓取所有端点，忽略已有数据。分页端点从头到尾全部重新拉取。

### refresh

介于 incremental 和 full 之间：对 item-level 端点检查 freshness window（默认 7 天），过期的 items 重新抓取，未过期的跳过。uid-level 端点行为与 incremental 相同。

**作用场景**：video_detail 中包含播放量、点赞数、标签等随时间变化的字段。incremental 模式只抓取新增视频，旧视频的数据停留在首次抓取时的状态；refresh 模式会自动刷新超过 7 天的旧数据，保持时效性字段的相对新鲜。适用于定期调度的场景（如每日/每周跑一次任务）。

**当前状态**：video_detail 目前仅存储 get_info + get_tags 的结果，其中 tags 基本不变，播放量等时效性字段尚未被上层消费。因此现阶段 refresh 与 incremental 效果差异不大，待后续处理层开始使用播放量等动态字段后，refresh 的价值会充分体现。

配置项：`BILI_FETCHING_REFRESH_AFTER_DAYS`（默认 7 天）。

### 幂等规则

重复调用 `fetch_uid(uid)` 时，runner 根据已有 task 状态决策：

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

覆盖规则：两种模式均以 endpoint 为粒度覆盖 raw_payload，不做 page 级差分合并。增量模式本次请求的所有页累积为新 raw_payload，未请求的旧页不保留。

### 增量模式算法

增量扫描的核心是 **known_ids 集合**：从已存储的 `raw_payload.pages` 中用 `item_id_path` 提取所有已知 item ID，然后逐页检查新页面的 ID 是否全部已知。

**ID 提取函数** `_extract_item_ids(raw_payload, path)`：

- 路径格式：dot-path + `[*]` 展开，如 `list.vlist[*].bvid`
- `[*]` 前的段做 dict 键访问，遇到 `[*]` 时对当前列表逐元素展开剩余路径
- `[*]` 后支持多段 dict 键（如 `meta.season_id`）
- 任一段缺失或类型不匹配时返回空列表并记录 warning
- 无 `[*]` 的路径直接做 dict 键访问

**多路径聚合** `_extract_item_ids_multi(raw_payload, paths)`：

逐路径调用 `_extract_item_ids` 并拼接结果。channel_list 使用此机制聚合 `seasons_list` 和 `series_list` 的 ID。

**known_ids 构建**：

1. 从 data store 读取已存储的 fetch value（`uid:{uid}:fetch:{endpoint}`）
2. 遍历 `raw_payload.pages`，对每页调用 `_extract_item_ids_multi` 提取 ID
3. 所有 ID 加入 `set`（自然去重）
4. 无存储数据时 `known_ids = None`，退化为全量抓取

**增量扫描流程**：

1. 从第 1 页开始逐页抓取
2. 每页提取 page_ids，计算 `new_ids = page_ids - known_ids`
3. `new_ids` 非空 → 加入 known_ids → 继续下一页
4. `new_ids` 为空且 page_ids 非空 → **boundary hit**：再抓一页兜底（safety page），然后停止
5. 分页自然结束（is_last_page）→ 正常停止
6. 本次运行请求的所有页累积为 `raw_payload.pages`，覆盖写入

`items_path` 与 `item_id_path` 的区别：`items_path` 在 client 模块用于**分页终止检测**（判断当前页是否为最后一页）；`item_id_path` 在 runner 模块用于**增量 ID 对比**（判断 item 是否已知）。两者可指向不同位置。

## 两阶段执行引擎

`runner` 包采用 mixin 拆分模式，将原 966 行的单体文件分解为 4 个模块：

- `__init__.py`（324 行）— Runner 类、公共 API（run_task / resume_task / run_or_resume）、编排逻辑（_run）、helpers
- `_item_ids.py`（76 行）— `_extract_item_ids` / `_extract_item_ids_multi` 纯函数
- `_endpoint.py`（325 行）— `_EndpointMixin._run_endpoint`：单端点抓取 + 增量扫描 + 重试
- `_item_fanout.py`（345 行）— `_ItemFanoutMixin._run_item_endpoint` / `_process_single_item`：item-level fan-out

Runner 类通过 `class Runner(_EndpointMixin, _ItemFanoutMixin)` 组合 mixin。重试逻辑已抽取到顶层 `bili_unit/_retry.py`（`RetryDriver` + `RetryPolicy`），fetching / processing 共享同一套实现；`_endpoint.py` 和 `_item_fanout.py` 通过 `RetryDriver.run()` 编排重试，回调处理 412 advice 和 AuthError 中止。`fetch_endpoint` 委托包装保持测试 patch target 兼容（`bili_unit.fetching.runner.fetch_endpoint`）。

两阶段并行执行：

**Phase 1**：所有 uid-level 端点并行抓取（`asyncio.gather`）。对 item-level 端点检查其 source_endpoint 是否在 task 中，如果不在则自动添加并运行。

**Phase 2**：所有 item-level 端点并行执行 fan-out。读取源端点的 stored data，提取 items，按 `item_concurrency`（默认 3）并发抓取每个 item。

每个阶段内部有独立的错误处理：AuthError → 整个 fan-out 立即终止（FAILED_PERMANENT）；412 → 重试（指数退避，最多 max_retries 次）；其他 FetchingError → 重试；非预期异常 → FAILED_PERMANENT。

## 限流机制

`RateLimitController` 实现双层限流：

- **全局 QPS**（默认 1.0）：所有端点共享
- **端点 QPS**（默认 0.5）：每个端点独立
- **video_detail QPS**（默认 0.5）：独立且更低

412 恢复机制：收到 412 后将对应 QPS 减半，进入 cooldown 期（默认 300 秒）；cooldown 结束后 QPS 翻倍恢复（不超过原始值）；连续多个 412 会持续降低 QPS。

限流状态持久化到 data store，跨进程重启后可恢复。

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

> 已从 SQLite（aiosqlite）替换为文件目录 JSON 存储。DataStore / ErrorStore 公共接口不变。

两个独立的目录存储：

**data store**（`{data_dir}/`）：文件目录 KV 存储，key → 路径映射：
- `uid:{uid}:task` → `{uid}/task.json` — 任务状态（TaskValue JSON）
- `uid:{uid}:fetch:{endpoint}` → `{uid}/fetch/{endpoint}.json` — 端点抓取结果（status + raw_payload + fetched_at）
- `uid:{uid}:fetch:{endpoint}:{item_id}` → `{uid}/fetch/{endpoint}/{item_id}.json` — 单个 item 的详情结果
- `uid:{uid}:progress:{endpoint}` → `{uid}/progress/{endpoint}.json` — 分页进度（支持断点续传）
- `rate_limit:global` / `rate_limit:{key}` → `rate_limit/global.json` / `rate_limit/{key}.json` — 限流状态

**error store**（`{error_dir}/`）：per-uid JSON 文件，每个文件包含该 uid 的错误记录列表。
- `{uid}.json` — 指定 uid 的错误记录
- `_null.json` — uid=None 的错误记录
- `_counter.json` — 自增 ID 计数器

所有写操作通过 `asyncio.Lock` 序列化，保证单线程 asyncio 下的数据一致性。端点结果与进度通过 `write_fetch_page_and_progress()` 原子写入（先写 fetch 文件，后写 progress 文件，progress 作为 commit marker）。

### 内部 value 形状

所有时间字段使用 epoch_ms（整数），限流时间戳除外（epoch seconds 浮点）。

**task value**（`uid:{uid}:task`）：

```json
{
  "uid": 123,
  "status": "RUNNING",
  "endpoints": {
    "user_info": { "status": "SUCCESS", "retry_count": 0, "last_error_id": null },
    "video_detail": { "status": "RUNNING", "retry_count": 0, "last_error_id": null,
      "item_progress": { "total": 77, "completed": 50, "failed": 0 } }
  },
  "created_at": 1718000000000,
  "updated_at": 1718000001000,
  "failed_item_ids": ["video_detail:BV1xxxxxxxxxx"]
}
```

`item_progress` 仅 item-level 端点使用。`failed_item_ids` 在任务收尾时聚合写入：每条编码为 `"endpoint"`（uid-level 失败）或 `"endpoint:item_id"`（item-level fan-out 失败），方便消费方直接读 task.json 拿到失败清单，不必再 join 错误日志。空列表也会出现，保持字段稳定。

**fetch value — uid-level**（`uid:{uid}:fetch:{endpoint}`）：

```json
{
  "uid": 123, "endpoint": "videos", "status": "SUCCESS",
  "raw_payload": { "pages": [ { "list": { "vlist": [...] }, "page": { "count": 65 } } ] },
  "fetched_at": 1718000000000, "updated_at": 1718000001000
}
```

**fetch value — item-level 单条**（`uid:{uid}:fetch:video_detail:{bvid}`）：

```json
{
  "uid": 123, "endpoint": "video_detail", "item_id": "BV1xxxxxxxxxx",
  "status": "SUCCESS",
  "raw_payload": { "info": { "..." }, "tags": [ "..." ] },
  "fetched_at": 1718000000000, "updated_at": 1718000001000
}
```

**fetch value — item-level 聚合**（`uid:{uid}:fetch:video_detail`，供查询层读取状态）：

```json
{
  "uid": 123, "endpoint": "video_detail", "status": "SUCCESS",
  "raw_payload": null,
  "item_counts": { "total": 77, "completed": 77, "failed": 0 },
  "fetched_at": 1718000000000, "updated_at": 1718000001000
}
```

**progress value — 分页端点**（`uid:{uid}:progress:{endpoint}`）：

```json
{
  "mode": "page",
  "next_request": { "pn": 2, "ps": 30 },
  "last_completed_request": { "pn": 1, "ps": 30 },
  "done": false, "updated_at": 1718000000000
}
```

**progress value — item fan-out**（`uid:{uid}:progress:video_detail`）：

```json
{
  "mode": "item_fanout",
  "total_items": 77, "completed_items": 50, "failed_items": 0,
  "done": false, "updated_at": 1718000000000
}
```

两种 progress 结构不同：分页版用 `next_request` 支持断点续传；fan-out 版用计数器跟踪整体进度。

**rate_limit value**（`rate_limit:global` / `rate_limit:{key}`）：

```json
{
  "scope": "global", "endpoint": null,
  "qps": 1.5, "paused_until": null, "last_412_at": null,
  "updated_at": 1718000000000,
  "original_global_qps": 2.0
}
```

`original_global_qps`（全局）或 `original_endpoint_qps`（端点级）记录 412 降速前的原始 QPS，用于恢复上限。`paused_until` 和 `last_412_at` 使用 epoch seconds（浮点）。

## CLI 用法

统一 CLI（推荐）：

```bash
uv run python -m bili_unit fetch <uid>                             # 增量抓取所有端点（默认）
uv run python -m bili_unit fetch <uid> -m full                     # 全量抓取
uv run python -m bili_unit fetch <uid> -m refresh                  # 刷新模式
uv run python -m bili_unit fetch <uid> -x video_detail             # 排除指定端点（推荐：跳过最耗时的 video_detail）
uv run python -m bili_unit fetch <uid> -e user_info videos         # 仅指定端点（调试用，与 -x 互斥）
uv run python -m bili_unit login                                   # QR 扫码登录
uv run python -m bili_unit list-uids                               # 列出所有已抓取的目标用户
uv run python -m bili_unit delete-uid <uid>                        # 删除指定用户的所有数据（交互确认）
uv run python -m bili_unit delete-uid <uid> -y                     # 删除（跳过确认）
uv run python -m bili_unit query <uid>                             # 查询已有结果
```

> 抓取范围默认是「全部已注册端点」。`-x/--exclude-endpoints` 是推荐的剪裁方式，
> `-e/--endpoints` 仅作调试时只跑指定端点用，二者互斥。

向后兼容：`python -m bili_unit.fetching` 仍可用（内部转发到统一 CLI）。

## 装配函数

`assemble()` 是 fetching 层的统一初始化入口：

```python
cmd, qry, data, error = await assemble()
```

读取环境变量 → 初始化两个文件目录存储 → 创建限流控制器 → 返回 Command（写接口）、Query（读接口）和两个 store（供调用方关闭）。

### Command 接口

```python
async def fetch_uid(uid: int, endpoints: list[str] | None = None, mode: str = "incremental") -> CommandResult
```

外部调用方唯一的写侧入口。`endpoints=None` 时抓取所有注册端点。

### Query 接口

```python
async def get_task(uid: int) -> TaskDTO | None
async def get_endpoint(uid: int, endpoint: str) -> EndpointDTO | None
async def list_tasks() -> list[dict]
async def get_video_detail(uid: int, bvid: str) -> EndpointDTO | None
async def list_video_details(uid: int) -> list[tuple[str, EndpointStatus]]
async def list_errors(uid: int | None = None) -> list[ErrorDTO]
```

- `get_task` — 返回指定用户的完整任务 DTO（含所有端点状态与数据摘要）
- `get_endpoint` — 返回单个端点的抓取结果
- `list_tasks` — 扫描所有 `uid:*:task` key，返回已抓取用户列表
- `get_video_detail` — 返回单个 bvid 的详情 EndpointDTO（raw_payload 含 info + tags）
- `list_video_details` — 返回所有已抓取 bvid 的 (bvid, status) 列表，不含完整 payload
- `list_errors` — 返回错误记录列表，可按 uid 过滤

### Runner 接口

```python
async def run_task(uid: int, endpoints: list[str] | None = None, mode: str = "incremental") -> TaskResult
async def resume_task(uid: int, endpoints: list[str] | None = None) -> TaskResult
async def run_or_resume(uid: int, endpoints: list[str] | None = None, mode: str = "incremental") -> TaskResult
```

`run_or_resume` 是 Command 调用的入口：检查已有 task 状态后决定 run 还是 resume。`run_task` 创建全新 task。`resume_task` 从断点续传。

### DataStore 接口

```python
async def get(key: str) -> dict | None
async def put(key: str, value: dict) -> None
async def delete(key: str) -> None
async def list_prefix(prefix: str) -> list[tuple[str, dict]]
async def write_fetch_page_and_progress(fetch_key, fetch_value, progress_key, progress_value) -> None
async def update_task_endpoint(task_key, ep_name, status, retry_count, last_error_id, item_progress) -> None
async def close() -> None
```

`update_task_endpoint` 原子更新 task value 中单个端点的状态（read-modify-write 在同一个 lock hold 内完成）。

### ErrorStore 接口

```python
async def record(error: FetchingError, uid: int | None, endpoint: str | None, retryable: str, detail: dict | None = None) -> int
async def list_errors(uid: int | None = None) -> list[ErrorDTO]
async def list_by_uid(uid: int) -> list[ErrorDTO]
async def delete_by_uid(uid: int) -> int
async def close() -> None
```

`delete_by_uid` 返回删除的行数。

### DTO 字段

**EndpointDTO**：uid, endpoint, status, available, raw_payload, fetched_at, progress, errors

**TaskDTO**：uid, status, endpoints (dict[str, EndpointDTO]), created_at, updated_at, failed_item_ids (list[str])。`failed_item_ids` 与 task.json 同形：`"endpoint"` 或 `"endpoint:item_id"`，无需 join error 日志。

**ErrorDTO**：id, uid, endpoint, error_type, message, retryable, detail, timestamp

## 配置项（env / .env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILI_SESSDATA | "" | 认证凭据（必填，缺失则 AuthError） |
| BILI_JCT | "" | 认证凭据（可选） |
| BILI_BUVID3 | "" | 认证凭据（可选） |
| BILI_BUVID4 | "" | 认证凭据（可选） |
| BILI_DEDEUSERID | "" | 认证凭据（可选） |
| BILI_AC_TIME_VALUE | "" | 认证凭据（可选） |
| BILI_FETCHING_GLOBAL_QPS | 1.0 | 全局 QPS 上限 |
| BILI_FETCHING_ENDPOINT_QPS | 0.5 | 端点级 QPS 上限 |
| BILI_FETCHING_VIDEO_DETAIL_QPS | 0.5 | video_detail 独立 QPS |
| BILI_FETCHING_RECOVERY_COOLDOWN | 300 | 412 恢复冷却秒数 |
| BILI_FETCHING_MAX_RETRIES | 3 | 最大重试次数 |
| BILI_FETCHING_REQUEST_TIMEOUT | 30 | 单次请求超时秒数 |
| BILI_FETCHING_ITEM_CONCURRENCY | 3 | item fan-out 并发数 |
| BILI_FETCHING_REFRESH_AFTER_DAYS | 7 | refresh 模式过期天数 |
| BILI_FETCHING_STALE_RUNNING_THRESHOLD_SECONDS | 900 | RUNNING 任务 updated_at 超过此秒数视为 stale，自动接管（issue #3） |
| BILI_FETCHING_DATA_DIR | data/bili/fetching/data | 数据存储目录 |
| BILI_FETCHING_ERROR_DIR | data/bili/fetching/error | 错误存储目录 |
| BILI_FETCHING_HTTP_BACKEND | aiohttp | HTTP 后端（curl_cffi / aiohttp） |
| BILI_FETCHING_IMPERSONATE | chrome131 | curl_cffi 指纹伪装 |

## 测试状态

测试位于 `bili_unit/tests/`，覆盖 store / env / auth / rate_limit / client / runner / item fan-out / command / query / 集成。`pytest-mock` mock 网络层；通过 `uv run pytest` 跑全套（无网络）。`uv run ruff check` 通过。
