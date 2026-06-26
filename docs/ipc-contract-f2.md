# F2 进程隔离 — IPC 契约(阶段 1,契约先行)

> 对应 Multica issue CHO-36(父 CHO-27,reporter 已选 **F2 进程隔离**)。
> 本文是**阶段 1 的唯一交付物**:worker 进程协议契约。**过审后**才进阶段 2 实现。
> 本项目对外是 CLI(无 HTTP),术语以 `CONTEXT.md` 为准;`api-contract-spec` 的
> HTTP 响应信封/错误码段位**不直接适用**,本文套用其精神(契约先定、变更同步双方、不单方改字段)。
>
> **GPL 合规前提(灰色区,需法务背书)**:F2 成立的条件是 worker 必须是**可独立分发的 GPL-3.0 组件**,
> 主仓 `bili_unit` 仅通过**通用数据协议**(stdio + JSON)与之通信,**不 import、不链接**任何 GPL 代码。
> 本契约的每一条都以"保持 arm's-length 距离"为约束。**实现合入/发版前建议过一次法务确认。**

---

## 1. 目标与边界

**目标**:把 GPL-3.0 的 `bilibili-api`(`bilibili-api-python~=17.0`)调用从主进程剥离到独立 worker 进程。
主进程 `grep -r "import bilibili" bili_unit/` **零命中**(或仅命中已废弃的兼容边界);
现有 63 端点 + 登录 + 音频下载经 IPC 全部可用;现有测试在 worker spawn 下全绿。

**不改对外 CLI 契约**:`bili-unit fetch / asr / delete` 的参数与输出语义不变,仅内部进程边界变化。

**本契约定义**:
1. 进程边界 — 哪些符号留主进程、哪些进 worker(§3)。
2. 传输层 — stdio 帧格式、并发、关联 ID(§4)。
3. 操作集(op)— 请求/响应字段、示例(§5–§6)。
4. **可序列化错误包** — 主进程据此判 retryable/permanent/unavailable(§7)。
5. 凭据传递 — 不落盘明文、不过 IPC(§8)。
6. worker 生命周期/并发/超时/重启(§9)。
7. 下载大对象流式策略(§10)。
8. 三态验收映射(§11)、红线(§12)、待确认问题(§13)。

---

## 2. 现状符号面(实测,过审基准)

`bilibili_api` 被 **9 个生产模块** import(test/conftest 不计):

| 模块 | import 的符号 | 用途 |
|---|---|---|
| `fetching/auth.py` | `Credential`, `login_v2` | 构造凭据 / 扫码登录 |
| `fetching/_bilibili_adapter.py` | `Credential`, `request_settings`, `select_client`, `user`, `article.{Article,ArticleList}`, `channel_series.ChannelOrder`, `exceptions.{ApiException,InitialStateException}`, `opus.Opus`, `video.Video` | 端点 callable 主体 + HTTP 后端 bootstrap |
| `fetching/_adapter_core.py` | `exceptions.{ApiException,ArgsException,CredentialNoBiliJctException,CredentialNoSessdataException,NetworkException,ResponseCodeException}` | SDK 异常 → fetching 异常映射 |
| `fetching/_adapters/_video.py` | `Credential`, `video.Video` | video_detail 系列 callable |
| `fetching/_adapters/_subtitle.py` | `Credential`, `video.Video` | video_subtitle callable |
| `fetching/_endpoint_groups/_user.py` | `user` | uid-level User.* 端点封装 |
| `fetching/_endpoint_groups/_channel_upower.py` | `user` | channel/upower 端点封装 |
| `processing/audio/_downloader.py` | `video.{AudioQuality,Video,VideoDownloadURLDataDetecter}` | 音频 CDN URL 解析 |
| `_types.py` | `Credential`(仅 `TYPE_CHECKING`) | 类型别名 `CredentialProvider` |

**端点注册表(实测核对 PRD 数字,准确)**:`_endpoint_catalog.py` 注册 **63 个 `EndpointSpec`**
= **33 uid-level + 30 item-level**。callable 三种形态:`_user_method(name, **defaults)` 闭包(28)、
具名 `fetch_*` async 函数(31)、lambda 闭包(4–5,含 `access_id`/`masterpiece`/`album`/`channel_videos_season|series`)。
**17/63 端点 `credential_required=True`。**

**关键:纯 Python、不碰 `bilibili_api` 的逻辑(留主进程)**:
- 分页推进 `_PAGINATION_STRATEGIES`(`_adapters/_pagination.py`,纯 dict 走);
- item-ID 提取 `extract_items` / `_extract_*`(纯 dict 走);
- 限流 `RateLimitController`(`rate_limit.py`,纯 aiolimiter,**主进程**);
- 重试 `RetryDriver` / `RetryClassification`(`_retry.py`,**主进程**);
- 错误分类 `classify_fetching_exception`(`runner/_failure.py`,**主进程**);
- 落盘 `FetchingStore` / SQLite(**主进程**)。

> 这条边界是 F2 的核心判断:**编排(限流/重试/分页/落盘)留主进程,只有"碰 SDK 的那一次调用"过 IPC**。
> 这样主进程零 `bilibili_api`,而所有现有控制流(§已实测 `runner/_endpoint.py` `_item_fanout.py`)几乎不动。

---

## 3. 进程边界

```
┌─────────────────── 主进程 bili_unit (NON-GPL 目标) ──────────────────┐
│  CLI / Command / Runner                                              │
│   ├─ RateLimitController   (限流, 主进程, 不动)                        │
│   ├─ RetryDriver           (重试, 主进程, 不动)                        │
│   ├─ _PAGINATION_STRATEGIES / extract_items  (纯 dict, 主进程, 不动)   │
│   ├─ FetchingStore / SQLite落盘             (主进程, 不动)            │
│   ├─ classify_fetching_exception            (主进程, 据错误包重建)     │
│   └─ WorkerClient  ←——— 适配层, 替换今天的 _bilibili_adapter ———→      │
│        · fetch_fn(uid, spec, cred_ref, params) → 一次 IPC 调用        │
│        · item callable(item_id, cred_ref, _uid?) → 一次 IPC 调用      │
│        · resolve_audio_url(bvid, ...) → 一次 IPC 调用                  │
│        · 流式下载: 拿到 URL 后主进程用纯 aiohttp 自取字节(§10)         │
└──────────────────────────────┬───────────────────────────────────────┘
                    stdio (stdin/stdout) + 换行分隔 JSON 帧
                    凭据只传 credential_ref(不传明文, §8)
┌──────────────────────────────┴─────── worker 进程 (独立 GPL-3.0 包) ──┐
│  bili_worker (独立分发, 自带 LICENSE=GPL-3.0)                          │
│   ├─ import bilibili_api ...        (GPL, 只在这里)                     │
│   ├─ 凭据池: credential_ref → Credential (worker 内, 从 .env 自读)     │
│   ├─ op 分发: fetch_page / fetch_item / resolve_audio_url / auth.*    │
│   ├─ SDK 异常 → 可序列化错误包(§7, worker 侧完成映射语义)              │
│   └─ 端点 catalog (callable 主体, 留 worker 侧)                        │
└───────────────────────────────────────────────────────────────────────┘
```

**为什么 callable/catalog 留 worker 侧**:`EndpointSpec.callable` 是 Python 函数对象,不可序列化;
且 callable 主体直接 `import bilibili_api`(执行 SDK 调用)。主进程只需要 `EndpointSpec` 的**可序列化元数据**
(name/kind/pagination_strategy/rate_limit_key/item_id_path(s)/source_endpoint/needs_parent_uid/credential_required/params_strategy),
用于编排;真正的 callable 通过 op + endpoint name 在 worker 侧查表执行。

**`extract_items` / `skip_item` 留主进程(不过 IPC)**:虽然它们当前与 callable 同住在
`import bilibili_api` 的模块里(`_adapters/_video.py`、`_bilibili_adapter.py` 等),但**函数体本身不碰 SDK**
——`_extract_bvids_from_videos` 等全是纯 dict 取值,且由主进程 runner(`runner/_item_fanout.py`)在已
`json_safe` 的 payload 上调用。阶段 2 把这些纯提取/跳过 helper **抽到主进程不 import SDK 的模块**即可,
**它们在主进程跑、不过 IPC**;只有"碰 SDK 的那一次调用"(callable)才过 IPC。

**端点元数据清单(manifest)**:worker 启动后经 `describe_catalog` op 把 63 端点的上述可序列化字段一次性
交给主进程,主进程据此建本地 spec 视图(`callable` 字段为 None,仅编排用;`extract_items`/`skip_item`
绑定主进程本地实现)。**主进程的 `extract_items`/`skip_item`/分页推进仍在主进程跑**(纯 dict),不经 worker。

---

## 4. 传输层

- **通道**:worker 作为主进程的子进程(`subprocess` / `asyncio.create_subprocess_exec`)。
  请求经 worker `stdin`,响应经 worker `stdout`。**`stderr` 仅用于 worker 日志**,不参与协议。
- **帧格式**:**换行分隔 JSON**(NDJSON)——每帧一行 UTF-8 JSON,以 `\n` 结尾,帧内不含裸 `\n`。
  **每帧必须是单行紧凑 JSON(`json.dumps(..., ensure_ascii=False)`,无 `indent`、无内嵌换行)**;payload 文本里的
  真实换行由 `json.dumps` 自动转义为 `\\n`,因此一帧恒为一行,接收侧按 `\n` readline 即可分帧。
  (理由:实现简单、可读、调试友好;大对象不走此通道,见 §10,故无需长度前缀。)
- **编码**:UTF-8。worker 启动必须 `PYTHONIOENCODING=utf-8` 且 stdout 不做行缓冲转换
  (Windows 下显式 `reconfigure(encoding="utf-8", newline="\n")`,避免 CRLF 污染帧)。
- **并发与关联**:每帧带 `id`(主进程单调递增整数)。worker 可乱序返回;主进程按 `id` 关联。
  worker 内部用 asyncio 并发处理多个在途请求(并发上限见 §9)。
- **方向**:请求恒为 主→worker;响应恒为 worker→主。worker **不主动推送**(扫码登录的进度也走"主问 worker 答"的轮询,见 §6.4),保证协议单向请求-响应,易测。

### 4.1 请求信封

```json
{ "id": 42, "op": "fetch_page", "params": { } }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 主进程分配,单调递增,响应原样回带 |
| `op` | string | 操作名(§5 枚举),未知 op → 错误包 `protocol_error` |
| `params` | object | op 专属参数;缺字段/类型错 → `protocol_error` |

### 4.2 响应信封(成功 / 失败二选一)

```json
{ "id": 42, "status": "ok",    "data": { } }
{ "id": 42, "status": "error", "error": { "type": "...", "classification": "...", "code": null, "message": "..." } }
```

- `status` ∈ `{"ok","error"}`。`ok` 时有 `data`(object),无 `error`;`error` 时有 `error`(§7),无 `data`。
- **协议不变量**:每个请求 `id` 恰有一条响应;`status` 必为二者之一;字段不混用。
  协议不匹配(未知 op、缺字段、`status` 非法、JSON 解析失败)→ 主进程按 `protocol_error` 处理并明确报错(§11 异常态)。

---

## 5. 操作集(op 枚举)

| op | 方向 | 用途 | 对应今天的代码 |
|---|---|---|---|
| `handshake` | 主→worker | 协议版本/能力协商,worker 就绪确认 | (新增) |
| `describe_catalog` | 主→worker | 取 63 端点可序列化元数据 manifest | `_endpoint_catalog.py` |
| `init_http_backend` | 主→worker | 配置 HTTP 后端(aiohttp/curl_cffi) | `init_http_backend()` |
| `credential_open` | 主→worker | 从 .env 构造凭据,返回 `credential_ref` | `auth.get_credential()` |
| `credential_status` | 主→worker | 校验当前凭据是否可用(doctor 预检) | doctor preflight |
| `fetch_page` | 主→worker | 抓 uid-level 端点的**一页** | `_bilibili_adapter.fetch_endpoint()` |
| `fetch_item` | 主→worker | 抓 item-level 端点的**单个 item** | `spec.callable(item_id, cred, ...)` |
| `resolve_audio_url` | 主→worker | 解析音频 CDN URL(不下载字节) | `AudioDownloader.get_audio_url()` |
| `login_qr_start` | 主→worker | 生成二维码,返回终端可打印文本 | `auth.qr_login()`(拆分) |
| `login_qr_poll` | 主→worker | 轮询扫码状态;成功返回 `credential_ref` | `auth.qr_login()`(拆分) |
| `login_save_env` | 主→worker | 把已登录凭据写回 .env(worker 侧写,§8) | `auth.save_credential_to_env()` |
| `shutdown` | 主→worker | 优雅退出 | (新增) |

> **op 命名稳定性**:op 名是契约的一部分,一旦审过不得改名(改名 = 破契约,走 §13 变更同步)。
> 新增 op 向后兼容(老 worker 收到未知 op 回 `protocol_error`,主进程据 `handshake` 的能力位避免发不支持的 op)。

---

## 6. 各 op 详细契约

### 6.1 `handshake`

请求 `params`:`{"protocol_version": "1.0", "client": "bili_unit/0.1.0"}`
响应 `data`:
```json
{ "protocol_version": "1.0", "worker": "bili_worker/0.1.0",
  "bilibili_api_version": "17.x.y",
  "capabilities": ["fetch_page","fetch_item","resolve_audio_url","login_qr","credential_ref"] }
```
- 版本不兼容(major 不同)→ worker 回 `error` `protocol_error`,主进程明确报错并不再发后续 op。

### 6.2 `describe_catalog`

请求 `params`:`{}`。响应 `data`:
```json
{ "endpoints": [
    { "name": "videos", "kind": "uid", "credential_required": false,
      "pagination_strategy": "page", "rate_limit_key": "videos",
      "params_strategy": {"pn": 1, "ps": 30},
      "item_id_path": null, "item_id_paths": null, "items_path": "list.vlist",
      "source_endpoint": null, "needs_parent_uid": false },
    { "name": "video_detail", "kind": "item", "credential_required": false,
      "pagination_strategy": "none", "rate_limit_key": "video_detail",
      "params_strategy": {}, "item_id_path": null,
      "source_endpoint": "videos", "needs_parent_uid": false }
  ],
  "count": 63, "uid_level": 33, "item_level": 30 }
```
- **不可序列化的函数字段不出现在 manifest**:`callable` 留 worker 侧(主进程不需要);
  `extract_items` / `skip_item` 虽在主进程跑,但由主进程**本地实现绑定**(见 §3),同样不经 manifest 传输。
- 主进程**校验** `count==63 && uid_level==33 && item_level==30`,否则视为 worker 与契约不一致 → 报错。

### 6.3 `fetch_page` / `fetch_item`

**`fetch_page`** 请求 `params`:
```json
{ "uid": 123, "endpoint": "videos", "credential_ref": "cred-1",
  "request_params": {"pn": 2, "ps": 30}, "timeout_s": 30.0 }
```
- `credential_ref`:`null` 表示匿名;非空表示用该凭据(§8)。`endpoint` 必须在 manifest 中。
- worker 内:查表得 `spec.callable`,执行 `spec.callable(uid, cred=<resolved>, **request_params)`,
  对结果做现有 `json_safe` 归一(scalar→`{"value":..}`、bare list→`{"list":..}` 等保持不变)。
- **worker 不做分页推进、不判 is_last_page**:worker 只返回这一页的 `raw_payload`;
  主进程用本地 `_PAGINATION_STRATEGIES[spec.pagination_strategy]` 算 `is_last/next_request`(逻辑不动)。

响应 `data`:
```json
{ "raw_payload": { } }
```
> 对比今天的 `FetchPageResult(uid, endpoint, raw_payload, is_last_page, next_request)`:
> `is_last_page`/`next_request` 由主进程本地分页策略算,**不过 IPC**,所以 `data` 只回 `raw_payload`。
> 适配层把它包成 `FetchPageResult` 喂给现有 runner,**runner 代码零改动**。

**`fetch_item`** 请求 `params`:
```json
{ "endpoint": "video_detail", "item_id": "BV1xx", "credential_ref": null,
  "parent_uid": 123, "timeout_s": 30.0 }
```
- `parent_uid` 仅当 manifest 标 `needs_parent_uid=true`(`channel_videos_season|series`、`upower_qa_detail`)时必填;
  worker 侧映射为现有 `**{"_uid": parent_uid}`。其余 item 端点忽略 `parent_uid`。
- worker 执行 `spec.callable(item_id, <resolved cred>, **extra)`,extra 含 `timeout` 与可选 `_uid`。

> **SDK 枚举参数约定**:多个 callable 把 `bilibili_api` 枚举当默认值
> (`user.MedialistOrder`/`channel_series.ChannelOrder`/`VideoOrder`/`OrderType` 等,如 `_bilibili_adapter.py:225/595`)。
> **IPC 只传 JSON 原语(int/str)**,枚举不过 IPC。因为 callable 整体留 worker 侧,枚举的还原也在 worker 侧:
> worker 收到 `request_params` 里的原语后,按现有 `MedialistOrder(int)`(`_bilibili_adapter.py:244`)等逻辑还原为枚举
> 再调 SDK。主进程/契约层**不构造、不传递任何 SDK 枚举**;省略该参数时由 worker 用 SDK 默认值。

响应 `data`:`{ "raw_payload": { } }`(单 item 的原始 dict,语义同今天 `_process_single_item` 拿到的 `result`)。

### 6.4 `resolve_audio_url`

请求 `params`:`{ "bvid": "BV1xx", "page_index": 0, "quality": "64K", "credential_ref": "cred-1" }`
响应 `data`:`{ "url": "https://...", "quality": "64K", "duration": 1234.5 }`
- worker 内执行今天 `AudioDownloader.get_audio_url` 的 SDK 部分
  (`Video.get_download_url` + `VideoDownloadURLDataDetecter.detect` + 流筛选 + duration 提取)。
- **worker 只回 URL + 元信息,绝不下载字节**;字节由主进程自取(§10)。
- 找不到音频流 → `error` 包 `download_error` / classification `permanent`(语义同今天 `DownloadError`)。

### 6.5 凭据 op:`credential_open` / `credential_status`

- `credential_open` 请求 `params`:`{ "reload_env": false }`。worker 从自己进程内的 `.env`/环境读字段
  构造 `Credential`,存入 worker 内凭据池,返回 `data`:`{ "credential_ref": "cred-1", "has_sessdata": true }`。
  **明文 sessdata/bili_jct 不出现在响应里**(§8)。缺 `BILI_SESSDATA` → `error` 包 `auth_error`/`permanent`。
- `credential_status` 请求 `params`:`{ "credential_ref": "cred-1" }`,响应 `data`:`{ "valid": true, "detail": "..." }`,
  供 `doctor` 预检用。

### 6.6 扫码登录:`login_qr_start` / `login_qr_poll` / `login_save_env`

扫码登录在 F2 下需拆成请求-响应轮询(worker 无 TTY):
- `login_qr_start` `params`:`{}` → `data`:`{ "login_ref": "qr-1", "qrcode_terminal": "<可直接 print 的二维码文本>" }`。
  **主进程负责把 `qrcode_terminal` 打到用户终端**(保持现有"终端打印二维码"的 CLI 行为)。
- `login_qr_poll` `params`:`{ "login_ref": "qr-1" }` → `data`:`{ "state": "SCAN|CONF|TIMEOUT|DONE", "credential_ref": "cred-2"|null }`。
  主进程按现有节奏轮询(~1s/次),`DONE` 时拿到 `credential_ref`;`TIMEOUT` → `auth_error`/`permanent`。
- `login_save_env` `params`:`{ "credential_ref": "cred-2", "env_path": ".env" }` → `data`:`{ "written": true, "path": ".env" }`。
  **写 .env 在 worker 侧做**(worker 持有明文凭据,主进程不持有),保持明文不过 IPC、不进主进程内存。

---

## 7. 可序列化错误包(核心)

**这是 F2 的关键契约**:SDK 异常不能跨进程,所以 **worker 侧完成"SDK 异常 → fetching 语义"的映射**
(复用今天 `_adapter_core.map_bilibili_errors` 的全部分支),把结果序列化成错误包;
主进程据错误包**重建 fetching 异常实例**,喂给**不变的** `classify_fetching_exception` / `RetryDriver`。

### 7.1 错误包结构

```json
{ "type": "Http412Error",
  "classification": "retryable",
  "code": 412,
  "message": "videos: 412",
  "retryable_hint": true }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | fetching 异常类名(枚举见 7.2),主进程据此重建确切异常类型 |
| `classification` | string | `retryable` / `permanent` / `unavailable`,见 7.3 |
| `code` | int \| null | 业务码或 HTTP status(412 / -400 / 404 ...);无则 null |
| `message` | string | 人读说明,**已脱敏**(不含 sessdata 等),原样进 `store.record_error` |
| `retryable_hint` | bool | worker 给的便捷位;**以 `type`/主进程分类为准**,hint 仅诊断用 |

### 7.2 `type` 枚举(与主进程 fetching 异常类一一对应)

主进程的 fetching 异常类(`fetching/__init__.py`,**不 import bilibili_api**)保持不变:
`FetchingError`(基) / `AuthError` / `RequestError` / `InvalidRequestError` /
`Http412Error`(:RequestError)/ `Http5xxError`(:RequestError)/ `ResourceUnavailableError`。
worker 侧映射表(等价于今天 `map_bilibili_errors`,实测核对):

| worker 检测到的 SDK 情形 | 错误包 `type` | `classification` | `code` |
|---|---|---|---|
| `TimeoutError`(`asyncio.wait_for` 超时) | `Http5xxError` | retryable | null |
| `ResponseCodeException` code==412 | `Http412Error` | retryable | 412 |
| `ResponseCodeException` code∈{-400,22115,22118,53013,53016,88214} | `ResourceUnavailableError` | unavailable | <code> |
| `ResponseCodeException` 其他 code | `RequestError` | retryable | <code> |
| `NetworkException` status==404 | `ResourceUnavailableError` | unavailable | 404 |
| `NetworkException` 400≤status<500 | `RequestError` | retryable | <status> |
| `NetworkException` 其他(5xx/0) | `Http5xxError` | retryable | <status> |
| `CredentialNoSessdata/NoBiliJct` | `AuthError` | permanent | null |
| `ArgsException` | `InvalidRequestError` | permanent | null |
| `InitialStateException`/`KeyError`(article/opus 取下架) | `ResourceUnavailableError` | unavailable | null |
| `ApiException`(opus_id 不正确 / fallback) | `ResourceUnavailableError` | unavailable | null |
| 其他 `ApiException` | `RequestError` | retryable | null |
| 其他未预期 `Exception` | `RequestError` | retryable | null |
| 下载无音频流 / CDN 失败 | `DownloadError`(processing 侧) | permanent | null |
| 未知 op / 帧错误 / 字段缺失 | `protocol_error` | permanent | null |

> `_PERMANENT_BUSINESS_CODES` 与 `passthrough`(InitialState/KeyError/ApiException)逻辑**整体搬到 worker 侧**,
> 因为它们依赖 `bilibili_api.exceptions` 的具体类型。**这正是 CHO-32(D,统一错误分类)落地的对象**:
> D 在本项之后做,届时统一入口 `classify_error(exc, endpoint)` 的输入对象从 SDK 异常变为本错误包。

### 7.3 主进程侧重建与分类(零行为变更)

主进程适配层收到错误包后:
1. 按 `type` 实例化对应 fetching 异常(`Http412Error(message)` 等),用 `message` 作 args。
2. `raise` 它 → 进**今天就有的** `RetryDriver` + `classify_fetching_exception`:
   - `AuthError` / `InvalidRequestError` / `ResourceUnavailableError` → `PERMANENT`
   - 其余 `FetchingError`(含 `Http412Error`/`Http5xxError`/`RequestError`)→ `RETRYABLE`
3. `Http412Error` 仍触发主进程 `RateLimitController.record_412` 的自适应降速(限流在主进程,语义不变)。
4. 三态归宿不变:`unavailable`→item 跳过 / uid 端点 `FAILED_PERMANENT`;`permanent` AuthError→中止 fan-out;
   `retryable`→走重试预算。

**契约保证**:`classification` 字段是 worker 给主进程的"三态归类事实源",与 `type`→`classify_fetching_exception`
的结果**必须一致**;阶段 2 实现必须有对照测试证明"经 IPC 重建后的分类 == 改造前直接分类"(D 的回归基准)。

---

## 8. 凭据传递(不落盘明文、不过 IPC)

**红线:明文凭据(sessdata / bili_jct / ac_time_value 等)不跨 IPC、不进主进程内存。**

- **凭据归属**:`Credential` 对象**只存在于 worker 进程**。worker 自己读 `.env`(同今天 `auth.get_credential`
  读的字段 `BILI_SESSDATA/BILI_JCT/BILI_BUVID3/BILI_BUVID4/BILI_DEDEUSERID/BILI_AC_TIME_VALUE`)构造它。
- **主进程只持有 `credential_ref`**(不透明字符串句柄,如 `"cred-1"`)。所有 `fetch_*` op 传 `credential_ref` 而非明文。
  `credential_ref=null` = 匿名请求(对应今天 `credential=None`)。
- **worker 凭据池**:`credential_ref → Credential` 映射存 worker 内存,worker 退出即销毁。`credential_open` 申请,
  worker 重启后 ref 失效(主进程据 §9 重新 `credential_open`)。
- **.env 读写都在 worker 侧**:`login_save_env` 由 worker 写(它持有明文),主进程不碰明文。
  这与今天 `save_credential_to_env` 的行为一致,只是执行进程从主进程移到 worker。
- **日志脱敏**:错误包 `message`、worker stderr 日志都不得含明文凭据(沿用今天 `test_logging_redaction.py` 的口径)。
- **环境隔离**:worker 由主进程 spawn 时**继承**含 BILI_* 的环境/工作目录(以便读同一个 `.env`);
  这不构成明文过 IPC(环境变量是 OS 进程继承,非协议帧)。若审查认为继承环境也算风险,可改为
  `credential_open` 时主进程**只**告知 `.env` 路径,worker 自读——见 §13 待确认。

---

## 9. worker 生命周期 / 并发 / 超时 / 重启

- **启动**:主进程在 `assemble()`(或 doctor/asr 入口)时 spawn worker,做 `handshake` + `describe_catalog` +
  `init_http_backend` + `credential_open`。失败(spawn 不起来 / handshake 版本不符)→ 主进程明确报错并中止该命令,
  **不静默降级**。
- **并发**:worker 内 asyncio 并发处理在途请求;并发上限 = 主进程 `bili_fetching_item_concurrency`(沿用今天 item fan-out 的信号量语义)。
  **限流仍在主进程**`RateLimitController.acquire()`——主进程先过限流闸再发 op,worker 不重复限流。
- **超时**:每个 op 主进程侧设独立超时(沿用 `bili_fetching_request_timeout`,默认 30s;下载 URL 解析另算)。
  超时 = 主进程不再等该 `id` 的响应,按 `Http5xxError`/`retryable` 处理并计入重试;同时标记 worker 可能不健康。
  worker 侧 `asyncio.wait_for` 也保留(双重保险,语义同今天 `fetch_endpoint` 的 `timeout`)。
- **崩溃检测**:worker stdout EOF / 进程退出码非 0 / 心跳超时 → 主进程判定 worker 死亡。
- **重启策略**:worker 死亡时,主进程
  1. 把所有在途 `id` 标记为 `retryable` 失败(`Http5xxError`),交给现有重试预算;
  2. 重启 worker(带退避,上限 N 次,N 可配),重做 handshake + describe_catalog + init_http_backend + credential_open(ref 重新申请);
  3. 重启失败超过上限 → 主进程优雅报错(对应 §11 空态:"worker 未启动/已退出 → 优雅报错并可重启,不崩、不静默吞")。
- **关闭**:命令结束时主进程发 `shutdown` 并 `await` worker 退出;超时则 `terminate`/`kill`。
  **主进程不在仍有在途请求时退出**(与 Multica「Background Task Safety」一致)。

---

## 10. 下载大对象的流式策略

音频字节可达数百 MB,**绝不走 JSON IPC 通道**(会撑爆 stdout 帧、内存翻倍)。拆成两步:

1. **URL 解析(过 worker)**:`resolve_audio_url` op → worker 用 SDK 解析得 CDN `url` + `quality` + `duration`,
   只把这三个小字段回主进程(§6.4)。
2. **字节下载(主进程,纯 aiohttp)**:主进程拿到 `url` 后,用**今天 `AudioDownloader.download_to_file` 的纯 aiohttp 路径**
   (`Referer`/`User-Agent` 头、`iter_chunked(8192)` 流式写盘、`max_size_bytes` 上限、超时)——**这段不 import bilibili_api**,
   可原样留在主进程。

> 结论:`_downloader.py` 拆为两半——`get_audio_url`(碰 SDK,逻辑搬 worker)与 `download_to_file`(纯 aiohttp,留主进程)。
> 大字节流走 CDN→主进程的 HTTP 直连,**完全不经 worker / 不经 IPC 帧**。这也避免了 GPL 代码接触下载字节流。

> **worker 侧 aiohttp 的两条例外(刻意保留)**:除上面音频字节由主进程直连 CDN 外,worker 侧自身也用到 aiohttp:
> `_adapters/_subtitle.py:30 _fetch_subtitle_body` 在 `video_subtitle` callable 内用 `aiohttp` 直取字幕 body JSON,
> 与 `Video.get_subtitle(cid)` 的 SDK 调用**交织**、并经 `raw_payload` 回传。字幕是**小文本**,过 IPC 没问题,
> 所以**整个 callable(含其 aiohttp 取字幕)随 callable 留 worker 侧**,worker 包需依赖 `aiohttp`。
> 区分:**音频字节**(数百 MB)→主进程直连 CDN、不过 IPC;**字幕 body**(小文本)→worker 内随 callable 取、经 raw_payload 过 IPC。

---

## 11. 三态验收映射(给阶段 2 + 质保)

沿用 PRD 三态(正常/空/异常)+ Given-When-Then;契约层的可验收点:

**正常态**
- Given 主进程 `grep -r "import bilibili" bili_unit/`,When 阶段 2 完成,Then 仅命中适配/worker 边界(或零命中)。
- Given worker 已起 + `describe_catalog` 校验通过,When 跑全部 63 端点 + 登录 + 音频下载,Then 经 IPC 全部可用;
  现有测试套件在 worker spawn 下全绿(test 侧 mock 从 patch `bilibili_api` 改为 patch `WorkerClient` 的 op,见 §13)。
- Given 一次 `fetch_page`,When worker 返回 `{raw_payload}`,Then 主进程本地分页策略算出的 `is_last/next_request` 与改造前逐一致。

**空态**
- Given worker 未启动 / 已退出,When 主进程发 op,Then 优雅报错 + 按 §9 重启 worker,**不崩、不静默吞**。
- Given item-level 源端点无数据,When fan-out,Then 行为同今天(`SUCCESS` total=0),与 worker 无关(extract_items 在主进程)。

**异常态**
- Given worker 返回 `error` 包,When 主进程重建异常,Then 按 `classification` 三态归类 + 走现有重试(§7.3)。
- Given worker 进程崩溃 / op 超时,When 在途请求,Then 标 `retryable` + 重启 worker,计入重试预算。
- Given 协议不匹配(未知 op / 缺字段 / JSON 坏帧 / handshake 版本不符),When 收到,Then 主进程明确报 `protocol_error`,不静默吞、不误判为业务失败。

**CI 门禁(A,CHO-29 已就位)**:阶段 2 代码 + worker 包必须过 `ruff` + `mypy`(主仓 `bili_unit/` 零 error)+ `pytest`。

---

## 12. 红线(契约层,阶段 2 必须守)

- 主进程**零 `import bilibili_api`**(含间接):适配层只调 `WorkerClient` 的 op,不 import SDK 任何符号。
- worker 是**可独立分发的 GPL-3.0 包**(自带 LICENSE、独立 `pyproject`、独立可 `pip install`);主仓**不 import、不链接** worker 代码,只 spawn 子进程 + 走 stdio JSON。
- **不改对外 CLI 契约**:`fetch/asr/delete` 参数与输出语义不变。
- **明文凭据不过 IPC、不进主进程内存**(§8)。
- **大字节流不过 IPC**(§10)。
- 错误包 `classification` 与主进程分类**一致**,且**零行为变更**(以现有测试为回归基准)。
- op 名 / 错误包字段一旦审过不单方面改(§13 变更同步)。

---

## 13. 待确认问题 / 未解决风险(不掩盖)

| # | 问题 | 影响 | 建议处置 |
|---|---|---|---|
| Q1 | **法务背书**:F2"独立进程 + arm's-length"是灰色区,非绝对安全。worker 继承含 BILI_* 的环境是否削弱"独立"论证? | 决定 F2 是否真成立 | **实现合入/发版前过法务**(reporter 选 F2 时已知此前提);法务结论入 CHO-34 / 父 issue。 |
| Q2 | **测试改造面**:`conftest.py` 今天 patch `bilibili_api.Credential` + `runner.get_credential`;测试目录 **39 个文件中,`from bilibili_api.exceptions import ...` 的有 5 个、引用 `bilibili_api`(任意符号)的共 9 个**,这些点直接构造 SDK 异常/对象做断言。worker 化后这些断言点要改为构造**错误包**/ patch `WorkerClient`。 | 阶段 2 改动量主要在此,非业务逻辑;改造面比"全量测试"小得多(仅 9/39 文件触 SDK) | 阶段 2 提供 `FakeWorker`(内存实现 op,返回固定错误包),test 从 patch SDK 转 patch worker;**不弱化断言**。 |
| Q3 | **worker 是否复用同一 event loop 抓多 uid**:今天 `fetch_uid` 每次开 `UidContext`;worker 是常驻还是每命令一个? | 性能 / 凭据池生命周期 | 建议**常驻 + 凭据 ref 池**,命令结束不杀 worker(asr/fetch 连用更快);进程退出才杀。 |
| Q4 | **`init_http_backend` 的 curl_cffi/impersonate** 现在主进程配;worker 化后由 worker 装 `curl_cffi`(GPL 无关,worker 包的可选依赖)。 | 反爬能力归属 worker | worker 包带 `anti-detect` extra;主仓移除该 SDK 相关 optional dep。 |
| Q5 | **`request_settings`/`select_client` 全局态**:SDK 用全局单例配置 HTTP 后端;worker 单进程内 OK,但若 Q3 选每命令一进程则每次重配。 | 与 Q3 绑定 | 随 Q3 定;常驻 worker 则 `init_http_backend` 一次即可。 |
| Q6 | **协议版本演进**:`handshake` 的 major/minor 兼容规则需在阶段 2 实现里固化(本契约定为 major 不同即拒)。 | 后续 worker 升级 | 阶段 2 写 `protocol_version` 兼容矩阵 + 测试。 |

**这些不算"已完成"**:Q1 是**阻塞实现合入的前置**(法务),Q2–Q6 是阶段 2 实现时必须定的设计点,本契约给了倾向性建议但**未拍板**,留审查 + Owner 阶段 2 共识。

---

# 流程状态

- 当前团队:后端
- 当前节点:整合(评审退回后修订)
- 上游输入是否充分:是(reporter 已选 F2;PRD 第 2 节 + CHO-34 决策齐备;我已 checkout 实测核对 9 模块 import 面 + 63 端点 manifest + 错误映射分支)
- 本节点是否完成:是(阶段 1 IPC 契约文档完成 + 已按后端审查意见修订:§3 extract_items/skip_item 归属改为"留主进程"、§4.1 NDJSON 单行紧凑约定、§6.3 SDK 枚举参数约定、§10 字幕 aiohttp 例外、§13 Q2 测试数字改准为 9/39)
- 是否需要 Battle:否
- 是否需要退回:否(已消化审查的 1 阻塞 + 3 建议 + 1 疑问,回审)
- 下一个交付对象:后端审查(复核修订;过审后才进阶段 2 worker 实现,实现再走后端审查 → 质量保障)
- 备注:**(1)** 本文是阶段 1 唯一交付物,**契约未过审不写实现、不合并**;**(2)** Q1 法务背书是阶段 2 合入/发版的前置阻塞项,不在本节点解决;**(3)** 错误包契约直接是 CHO-32(D 统一错误分类)的落地对象,D 在本项之后做;**(4)** 主进程零 `bilibili_api` + 大字节流/明文凭据不过 IPC 是不可让步的红线。
