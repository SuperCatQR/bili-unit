# bili unit fetching design

> ⚠️ **状态：废弃（DEPRECATED）**
>
> 本文为早期设计稿，**不再维护**。代码现状以 [docs/feature/fetching.md](../feature/fetching.md) 为唯一真相源，
> 结构约束以 [docs/structure/bili.md](../structure/bili.md) 为准。本文与现状不一致时按 feature/structure 为准。
> 若需查阅最初的技术选型与设计推理，可保留参考；新增改动请直接更新 feature 文档。

> 性质：实现设计。本文记录 `bili` fetching 层的技术选型、设计决策与设计规则。
> 约束文档：`docs/structure/bili.md` 为绝对约束，本设计不与之冲突。
> 代码现状：`docs/feature/fetching.md` 为真相源，记录代码实际能力。

```text
信息归属
  模块划分、数据流方向、边界约束、状态归属    → docs/structure/bili.md
  技术选型理由、设计规则、设计决策            → 本文（docs/design/fetching.md）
  endpoint 表、状态枚举、DTO 字段、配置项、
  日志事件、value 形状、CLI、测试结果         → docs/feature/fetching.md
```

## 1. 位置

```text
docs/design → bili → fetching
```

```text
上游文档  docs/structure/unit.md
结构约束  docs/structure/bili.md
代码现状  docs/feature/fetching.md
外部依赖  docs/bili-api-info/（bilibili-api-python）
```

## 2. 设计定位

```text
对象      bili 的 fetching 层实现设计
单位      目标用户 uid
职责      认证 → 抓取（uid-level + item fan-out）→ 入库
服务      processing
不服务    index / reasoning / interaction
```

模块划分、调用方向、数据流见 `docs/structure/bili.md` §4/§6。

## 3. 运行时与依赖方向

```text
语言              Python 3.12
包管理            uv
异步              asyncio
外部库            bilibili-api-python
默认 HTTP 后端     aiohttp
可选 HTTP 后端     curl_cffi，安装 anti-detect extra 后优先使用
存储              文件目录 JSON 存储
限流              aiolimiter.AsyncLimiter
配置              pydantic-settings + python-dotenv
测试              pytest + pytest-asyncio + pytest-mock
语法检查          ruff (E/W/F/I/UP/B/SIM)
```

```text
bilibili-api-python 全异步，因此 fetching 采用 asyncio。
client 模块受 bilibili-api-python Python 生态约束；fetching 其余模块与 client 同进程通信，统一 Python。
```

```text
import 边界
  command → runner, DTO
  query → data, error, task
  runner → task, client, rate_limit, data, error, auth, env
  client → auth(Credential 注入), endpoint registry
  auth → env, error
  rate_limit → data/error(state persistence)
  data/error → 不 import command/query/runner/client
```

## 4. HTTP 请求设计

```text
默认后端          aiohttp         纯 Python；默认依赖，保证基础环境可运行
可选后端          curl_cffi       安装 anti-detect extra 后优先使用；用于伪装浏览器 TLS 指纹
客户端伪装        chrome131       默认值；通过 env/config 可覆盖
```

```text
后端选择逻辑
  1. 安装 curl_cffi 且配置启用 → select_client("curl_cffi")
  2. 否则 → select_client("aiohttp")
  3. curl_cffi 不可用时降级 aiohttp，记录 warning
```

```text
设计原则
  - HTTP 后端选择属于 client 启动配置，不属于业务语义。
  - curl_cffi 不作为基础运行前提；不可用时允许降级 aiohttp。
  - impersonate 默认值可变，不写死为不可调整代码常量。
```

## 5. 数据存储与事务设计

```text
data 与 error 分离的理由
  - 错误记录可独立清理、独立查看，不混入正常抓取数据。
  - 错误存储失败不应影响正常数据写入。
  - 当前各使用独立的目录路径，各自独立文件结构。
```

```text
文件存储策略
  - 每个 key 映射为独立 JSON 文件，目录结构反映 key 层级。
  - data 写操作通过内部 async lock 串行化。
  - error 写操作通过内部 async lock 串行化。
  - 测试中每个 test 使用 tmp_path 独立目录，teardown 自动清理。
```

```text
写入规则
  - fetch payload 写入与 progress 更新顺序执行（先 fetch，后 progress）。
  - progress 文件写入成功后才认为当前页完成（progress = commit marker）。
  - data 写入失败时不推进 progress。
  - progress 推进失败时不标记 endpoint 成功。
```

```text
存储演化
  已从 SQLite（aiosqlite）迁移为文件目录 JSON 存储。
  key 模式与 value 形状见 docs/feature/fetching.md §存储层。
```

```text
item-level 存储设计（video_detail 等 kind="item" endpoint）
  每个 item 独立 key：uid:{uid}:fetch:{endpoint}:{item_id}
  fetch value 含 uid、endpoint、item_id、status、raw_payload、fetched_at、updated_at
  progress value 模式为 "item_fanout"，含 total_items、completed_items、remaining_items、done
  item-level endpoint 在 task.endpoints 中独立条目，含 item_progress {total, completed, failed}
```

## 6. Payload 与校验设计

```text
raw_payload        原样保存 API 返回 JSON，不丢弃字段
validated_view     可选结构校验视图，仅用于基础形状检查、分页字段提取、DTO 生成
```

```text
允许
  - 结构校验
  - 类型矫正
  - 序列化

不允许
  - 丢弃 API 返回字段
  - 解释字段业务语义
  - 跨 endpoint 关联
  - 生成 processing 结果
```

```text
Pydantic 使用原则
  - raw_payload 不依赖严格 Pydantic model 才能入库。
  - DTO / validated_view 可使用 Pydantic。
  - 需要保留未知字段时使用 extra="allow"。
```

## 7. 认证设计

```text
配置来源          .env 文件；auth 首次调用时延迟加载
刷新              credential.refresh() 只在内存中生效
过期处理          写入 error；runner 检测认证异常后暂停或失败
必要性            认证是 fetching 的必要前置条件；无 credential 时 runner 直接 FAILED_PERMANENT
```

```text
数据流
  env → auth → client
```

```text
认证原则
  - 认证是必要项，不是可选项。runner 每次执行均先获取 credential。
  - 缺失 SESSDATA 时 AuthError → FAILED_PERMANENT，不进入抓取流程。
  - env 只读。
  - auth 不写回 env。
  - client 不直接读取 env；Credential 由 auth 提供，可由 runner 注入 client。
  - 所有 endpoint 均接收 credential，无论其 API 文档是否强制要求。
  - env 不写入 data。
  - 缺失认证字段不阻塞模块 import，只在 auth 首次调用时报错。
  - Credential / Cookie 不进入日志。
```

配置字段与 auth 接口见 docs/feature/fetching.md §配置项 / §装配函数。

## 8. 限流设计

```text
限流层级
  global limiter     账号 / IP / 设备级总请求控制
  endpoint limiter   单接口请求控制
```

```text
video_detail 独立限流
  rate_limit_key = "video_detail"
  推荐 QPS 0.2（每 5 秒 1 请求），300 视频约 50 分钟完成
  独立于其他 endpoint 限流 key，不抢占 uid-level endpoint 的 QPS 配额
  video_detail 请求同时过 global limiter 和 endpoint limiter
```

```text
rate_limit 职责
  - 提供请求准入控制。
  - 保存 / 调整限流参数。
  - 根据 412 事件更新限流状态。
  - 可提供建议等待时间。
  - 不编排 retry，不决定 task 状态。
```

```text
412 恢复机制
  - 收到 412 后，对应 limiter 的 QPS 减半，进入 cooldown 期（默认 300 秒）。
  - cooldown 结束后 QPS 翻倍恢复，但不超过原始配置值（original_global_qps / original_endpoint_qps）。
  - 连续多个 412 会持续降低 QPS。
  - 限流状态持久化到 data store，跨进程重启后可恢复。
  - 原始 QPS 值保存在 rate_limit value 中，作为恢复上限。
```

```text
runner 职责
  - 根据 RateLimitError / Http412Error 决定等待、重试、暂停或失败。
  - 根据 task/error/progress 状态恢复 endpoint 抓取。
```

限流配置值与 rate_limit value 形状见 docs/feature/fetching.md §配置项 / §存储层。

## 9. 错误设计

```text
异常层级
  FetchingError
  ├── AuthError
  ├── RateLimitError
  ├── RequestError
  │   ├── Http412Error
  │   └── Http5xxError
  └── DataError
```

```text
错误记录原则
  retryable=true       临时网络错误、可恢复 412、服务端临时错误
  retryable=false      明确不可重试错误，如认证失效、权限明确不足
  retryable=unknown    无法判断是否可重试，runner 按保守策略处理
```

```text
error 只记录错误状态，不编排重试。
重试、暂停、接续由 runner 决策。
```

## 10. Endpoint registry 设计

```text
位置              client 模块
性质              抓取接口描述表；不是固定最终接口清单
来源              docs/bili-api-info/modules/user.md 中 User(uid) 相关读取接口（uid-level）
                  docs/bili-api-info/modules/video.md 中 Video(bvid) 相关读取接口（item-level fan-out）
```

```text
每个 endpoint 描述项包含
  name                  endpoint 名称
  callable              bilibili-api-python 调用目标
  credential_required   API 文档是否强制要求 Credential（fetching 始终注入 credential，此字段仅作文档标记）
  params_strategy       初始参数策略
  pagination_strategy   none | page | cursor | anchor | oid | custom
  rate_limit_key        限流 key
  item_id_path          增量模式 item ID 提取路径（可选，单路径）
  item_id_paths         增量模式 item ID 提取路径列表（可选，覆盖 item_id_path）
  items_path            分页检测用的列表定位路径（可选，dot-path，无 [*]）
  kind                  endpoint 类型（可选）："uid"（默认）| "item"（item-level fan-out）
  source_endpoint       kind=item 时必填；提供 item 列表的上游 endpoint 名称
  extract_items         kind=item 时必填；从上游 raw_payload 提取 item ID 列表的函数
```

```text
endpoint 类型
  uid-level（kind="uid"，默认）
    以 uid 为输入，直接调用 User(uid) 相关接口。
    支持 none / page / cursor 分页。
    存储 key: uid:{uid}:fetch:{endpoint}

  item-level fan-out（kind="item"）
    以上游 endpoint 产出的 item 列表为输入，逐项调用 item 级接口。
    依赖 source_endpoint 完成后才执行（两阶段编排）。
    每个 item 独立存储，支持独立增量/重试。
    存储 key: uid:{uid}:fetch:{endpoint}:{item_id}
```

```text
边界
  - endpoint registry 只描述如何抓取。
  - 不解释返回字段语义。
  - 不做跨 endpoint 关联。
  - 不代表 source_data 对 processing / index.ingestion 的输出契约。
  - item-level endpoint 依赖 source_endpoint 的 raw_payload，但不修改其数据。
```

已注册端点清单见 docs/feature/fetching.md §端点注册表。
完整候选清单（含 T1/T2）见 docs/design/endpoint-inventory.md。

### 10.1 video_detail 端点设计

**动机**：`videos` endpoint（`User.get_videos()`）只返回视频列表摘要（bvid、title、截断 description、play、video_review、comment）。以下字段缺失或不够完整：

```text
列表 API 缺失字段
  desc              完整视频简介（列表仅给截断 description）
  pages             分 P 信息（cid, part 名, duration, dimension）
  stat 完整统计      reply, favorite, coin, share, like, danmaku（列表仅有 play, video_review, comment）
  label             稿件活动标签
  subtitle          字幕信息
  rights            版权与权限标志
  owner             UP 主信息

列表 API 不含的能力
  tags              视频标签列表（tag_id, tag_name, likes, hates, is_atten）
```

video_detail 作为 item-level fan-out endpoint，从 videos 结果中提取 bvid 列表，逐个调用 `Video(bvid)` 方法获取完整信息。

**API 范围**：`docs/bili-api-info/modules/video.md` → `Video(bvid, credential=...)`

入选 API：
- `get_info() → dict` — 视频完整信息（desc, stat, pages, label, subtitle, rights, owner 等）
- `get_tags() → List[dict]` — 视频标签列表（tag_id, tag_name, cover, likes, hates, is_atten, subscribed）

排除 API：
- `get_pages()` — 分 P 信息已含于 get_info() 的 pages 字段
- `get_detail()` — 包含相关推荐与用户关系，非纯元数据
- `get_related()` — 推荐视频，非本视频自身属性
- `get_online()` — 实时在线人数，时效性数据，非稳定元数据
- `get_chargers()` / `get_pay_coins()` — 用户互动维度数据
- 弹幕/评论相关 — 体量大、独立数据域，留作后续独立 endpoint
- 下载/写接口 — 非元数据抓取范围

每视频请求量：2 次 API 调用（get_info + get_tags），需独立限流。

**Callable 设计**：

```text
uid-level callable 签名：(uid: int, cred: Credential | None, **kw) → dict
item-level callable 签名：(item_id: str, cred: Credential | None, **kw) → dict

fetch_video_detail_item(bvid, cred) 职责：
  1. Video(bvid, credential=cred).get_info() → info
  2. Video(bvid, credential=cred).get_tags() → tags
  3. 返回 {"info": info, "tags": tags}
```

runner 根据 spec.kind 选择调用方式：kind="uid" 用 uid 调用，kind="item" 用 item_id 调用。

**端点的 EndpointSpec 注册**：

```text
EndpointSpec(
    name="video_detail",
    kind="item",
    source_endpoint="videos",
    extract_items=_extract_bvids_from_videos,
    callable=fetch_video_detail_item,
    credential_required=False,
    pagination_strategy="none",
    rate_limit_key="video_detail",
)
```

**增量模式**：与 uid-level 的逐页 ID 检测不同，video_detail 增量通过对比 `videos` 产出的 bvid 集合与已存储的 bvid 集合进行：

```text
增量流程
  1. current_bvids = extract_items(videos.raw_payload)   # 当前视频列表中的所有 bvid
  2. stored_bvids  = {已有 uid:{uid}:fetch:video_detail:{bvid} 的 bvid 集合}
  3. new_bvids     = set(current_bvids) - stored_bvids
  4. 仅对 new_bvids 执行详情抓取
  5. new_bvids 为空时直接 SUCCESS
```

增量模式不检测已有 bvid 的数据变化（tag 更新、stat 变化）。需要全量刷新时使用 `--mode full`。

**单 item 错误处理**：

```text
单个 bvid 的 get_info/get_tags 失败时：
  retryable 错误  → 重试该 bvid（最多 max_retries 次），不阻塞其他 bvid
  permanent 错误  → 写入 error，跳过继续下一个 bvid

整体状态：全部成功 → SUCCESS，部分成功 → PARTIAL_ITEM
```

**边界**：video_detail 不抓取弹幕、评论、视频流/下载、相关推荐；不跨视频聚合；不解释字段语义。

## 11. Query DTO 设计

```text
query 读取范围     抓取结果、抓取状态、任务状态、抓取进度、错误状态
query 返回         DTO；不返回 store key、内部文件路径结构
query 可判断       endpoint 是否完成、是否有错误、是否有可读取 raw_payload
query 不判断       API 字段业务含义、不做 processing 语义处理、不跨 endpoint 关联
```

```text
available 表示结构级可读：
  - 有成功写入的 raw_payload
  - 当前 endpoint 状态允许读取
  - 不代表 processing 语义上有效
```

DTO 字段定义、Query 接口签名见 docs/feature/fetching.md §DTO 字段 / §装配函数。

## 12. Runner 状态设计

```text
task 级 _derive_status 视 PARTIAL_ITEM 为部分成功（等同 PARTIAL）。
```

```text
状态原则
  - SUCCESS 表示全部 endpoint 成功。
  - PARTIAL 表示部分 endpoint 成功，部分 endpoint 未成功。
  - FAILED_RETRYABLE 表示临时失败，可继续重试。
  - FAILED_EXHAUSTED 表示重试次数耗尽，需要用户重新触发。
  - FAILED_PERMANENT 表示明确不可重试，如认证失效、权限明确不足。
```

```text
断点续传原则
  - progress 记录下一次请求参数与最后完成请求参数。
  - 只有当前页 payload 成功写入 data 后，才更新 progress。
  - 重试时从 progress.next_request 继续。
  - item-level fan-out: progress 记录已完成 item 列表与剩余 item 列表。
```

```text
两阶段编排
  Phase 1: 所有 kind="uid" 的 endpoint 并行执行（现有行为不变）。
  Phase 2: 在 Phase 1 完成后，检查 kind="item" 的 endpoint：
           - 若其 source_endpoint 状态为 SUCCESS → 启动 item-level fan-out
           - 若 source_endpoint 未 SUCCESS → 该 item-level endpoint 标记 FAILED_PERMANENT
  Phase 2 中各 item-level endpoint 之间可并行。
  item-level endpoint 的失败不影响 Phase 1 已完成的 uid-level endpoint 状态。
```

任务状态枚举、endpoint 状态枚举、task/endpoint value 形状见 docs/feature/fetching.md §存储层。

## 13. 幂等设计

```text
重复 command(uid) 依赖抓取模式

  增量模式（incremental，默认）
    分页 endpoint       正向逐页扫描，通过 item ID 检测新内容；遇到全页已知时停止。
                        本次请求的所有页覆盖写入 raw_payload。
    非分页 endpoint     直接覆盖写入。
    task SUCCESS        不跳过，进入增量扫描。
    首次运行（task 不存在）退化为全量抓取。

  全量模式（full）
    所有 endpoint        忽略已有数据，从第 1 页抓完所有页，覆盖写入。
    task SUCCESS         忽略已有数据，全量重抓。
    task PARTIAL / FAILED_RETRYABLE / FAILED_EXHAUSTED
                         重置 endpoint 状态后全量重抓。

  刷新模式（refresh）
    uid-level endpoint   行为同增量模式。
    item-level endpoint  检查每个 item 的 fetched_at 时间戳，
                         超过 REFRESH_AFTER_DAYS（默认 7 天）的 item 重新抓取，
                         未过期的 item 跳过。
    适用场景             定期调度时刷新时效性字段（播放量、标签等）。

  item-level fan-out（video_detail 等）
    增量模式   对比 source_endpoint 产出 item 集合与已存储 item 集合，仅抓取新增 item。
    全量模式   对所有 item 重新抓取，覆盖写入各 item 的存储。
    刷新模式   检查每个 item 的 fetched_at，超过 freshness window 的重新抓取。
    单 item 失败不阻塞其余 item。
```

```text
状态约束
  - task RUNNING：默认不启动第二个 runner，返回当前状态
  - task FAILED_PERMANENT：两种模式均不自动重跑
  - resume（断点续传）始终使用增量模式
```

```text
覆盖原则
  - 两种模式均以 endpoint 为粒度覆盖 raw_payload；不做 page 级差分合并。
  - 增量模式：本次运行请求的页累积为新 raw_payload，未请求的旧页不保留。
  - 全量模式：全部页覆盖。
  - 覆盖写入遵守数据事务规则（payload 写入成功后才推进 progress）。
```

幂等规则完整对照表见 docs/feature/fetching.md §幂等规则。

## 14. 资源生命周期

```text
data / error store
  - store 提供 async close()
  - 测试 teardown 必须 close store

client / session
  - client 负责设置 bilibili-api-python 请求后端
  - session 由 bilibili-api-python 管理

runner
  - 不创建不可关闭的全局资源
  - endpoint task 结束后收敛异常并写入 error
  - cancellation 时不推进 progress
```

## 15. 结构约束对照

结构约束完整列表见 `docs/structure/bili.md` §8。以下条目在本设计中有细化：

```text
约束                                       本设计细化位置
不处理抓取结果语义                          §6 Payload 与校验设计
不固定用户相关接口清单                       §10 Endpoint registry 设计
调用方不直接写 data / error                 §5 数据存储与事务设计
调用方不直接依赖 data / error 内部存储结构    §5 + §11 Query DTO 设计
runner 编排 client                         §3 import 边界
runner 根据任务状态与错误状态编排重试         §8 限流设计 + §9 错误设计
rate_limit 不编排重试                       §8 限流设计
error 不编排重试                            §9 错误设计
client 不直接读取 env                       §7 认证设计
env 不写入 data                            §7 认证设计
```

```text
item-level endpoint 特有约束
  item-level endpoint 不修改 uid-level endpoint 的数据
  item-level endpoint 不触发 uid-level endpoint 重新抓取
  video_detail 的失败不影响 videos 的状态
```

## 16. bili-api-info 对照

```text
全异步                                          asyncio 运行时                          [bili-api-info: README]
412 限流风险                                    rate_limit + runner                    [bili-api-info: README FAQ]
curl_cffi > aiohttp > httpx 优先级              anti-detect extra + aiohttp 默认依赖     [bili-api-info: request_client.md §1]
_prepare_request 自动 WBI 加密                  rate_limit 不重复此逻辑                  [bili-api-info: request_client.md §2.1]
impersonate 默认空值                            client 默认 chrome131，可配置覆盖         [bili-api-info: README L165-171]
wbi_retry_times 默认 3                          不干预库的 WBI 重试                      [bili-api-info: configuration.md]
enable_auto_buvid 默认 True                     接受默认                                [bili-api-info: configuration.md]
enable_bili_ticket 默认 False                   暂不开启                                [bili-api-info: configuration.md]
request_log AsyncEvent                          logging 标准库同技术栈                   [bili-api-info: configuration.md]
Cookie 从浏览器 F12 手动复制                    env 存 .env，用户手动维护                [bili-api-info: get-credential.md]
Credential 字段包括 dedeuserid/buvid4 等         env 预留字段                             [bili-api-info: get-credential.md]
credential.refresh() + check_refresh()          auth 会话级刷新                          [bili-api-info: refresh_cookies.md]
asyncio.sleep(1) 防 412 示例                    初始 QPS 保守                            [bili-api-info: examples/user.md L100-101]
get_videos(pn, ps) 页码分页                     progress 存 next_request                 [bili-api-info: examples/user.md]
get_dynamics_new(offset) 游标分页               progress 存 next_request                 [bili-api-info: examples/user.md]
库不自动翻页                                    client + endpoint registry 负责循环       [bili-api-info: examples/user.md 分页示例]
proxy / timeout / verify_ssl 可设               保留可配置，初始默认                      [bili-api-info: configuration.md]
```

## 17. 开发顺序

```text
 1. 项目初始化        uv init，Python 3.12 + uv 工程
 2. 目录与模块骨架    fetching/ 下 command/query/auth/env/data/task/runner/client/rate_limit/error
 3. data / error      文件目录 JSON KV 存储 + per-uid 错误文件（已从 SQLite 迁移）
 4. env / auth        .env 只读加载 + Credential 构造 + QR 扫码登录 + 凭据写入 .env
 5. client registry   endpoint 注册
 6. rate_limit        RateLimitController：global limiter + endpoint limiter (aiolimiter)
 7. runner 状态机     task / endpoint 状态、重试、退避、断点续传、增量扫描、全量覆盖
 8. command           写侧入口 fetch_uid(uid, endpoints, mode) → runner.run_or_resume()
 9. query             只读入口 get_task / get_endpoint / list_errors → DTO
10. 测试              pytest + pytest-asyncio + pytest-mock
11. CLI               python -m bili_unit.fetching
12. ruff              E/W/F/I/UP/B/SIM 规则集，target py312，line-length 120
```

## 18. 完成标准

```text
MVP 完成标准
  - uv run pytest 全部通过
  - uv run ruff check 全部通过
  - mock integration 完成 command → query 闭环
  - CLI 入口可用
  - 默认测试无真实网络依赖
  - logs 不泄露 Credential
```

## 19. 待定

```text
令牌桶参数          global QPS 0.5，endpoint QPS 0.2，video_detail QPS 0.2；持续实测调参
.env 模板           待部署环境确定后产出 .env.example
CI/CD               待代码仓库建立后配置
video_detail QPS    0.2 为保守初始值，待实测调整
```

## 20. 已决

| 议题 | 决定 | 理由 |
|------|------|------|
| tag 变化检测 | 不引入软增量或字段级过期机制 | 视频 tag 在发布后基本不会更新，增量"已存在就跳过"已够用 |
| 接口清单 | 完整清单已设计，含 24 个候选端点，分 T0/T1/T2 三级；收藏夹、channels 已排除 | 详见 `docs/design/endpoint-inventory.md` |
| endpoint registry | T1 批次 4 个端点待实现（articles, opus, overview_stat, subscribed_bangumi）；channels、channel_videos_season/series 归入 T2 | 同上 |
| 端点间内容重叠 | 条目取并集，字段值去重：fetching 层独立抓取并存储各端点 raw_payload，不做去重；processing 层合并时保留所有唯一条目，但相同实体上值完全相同的字段只保留一份 | fetching 层边界：不解释字段语义、不跨 endpoint 关联；去重与实体对齐是 processing 层职责 |
| cursor(anchor) 分页 | 新增 `pagination_strategy="anchor"` 分支，硬编码 anchor 字段名 | upower_qa 是目前唯一的 anchor 端点且属 T2，不值得泛化为可配置方案 |
| 存储层选型 | 文件目录替代 SQLite（aiosqlite） | fetching 层只用 KV 操作（get/put/delete/list_prefix），无 join、无聚合、无 schema 约束；文件方案更简单、透明（可直接查看 JSON）、可 git diff 追踪数据变化、rsync 同步；单进程 asyncio 的并发控制用 asyncio.Lock 即可，不需要数据库级事务 |
