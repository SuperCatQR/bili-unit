# upstream —— bilibili-api-python

本项目抓取层基于
[`Nemo2011/bilibili-api`](https://github.com/Nemo2011/bilibili-api)
发布的 `bilibili-api-python` 包。它是一个 Python 异步 Bilibili API
调用库，覆盖视频、音频、直播、动态、专栏、用户、番剧等常用能力。

## 上游定位

| 项 | 说明 |
| --- | --- |
| GitHub | <https://github.com/Nemo2011/bilibili-api> |
| 开发文档 | <https://nemo2011.github.io/bilibili-api/> |
| PyPI 包 | `bilibili-api-python` |
| License | GPL-3.0 |
| 请求后端 | 上游支持 `aiohttp` / `httpx` / `curl_cffi`；本项目优先使用 `curl_cffi`，回退 `aiohttp` |

## 本项目如何使用

```text
bilibili-api-python
  → bili_unit.fetching._bilibili_adapter / _adapters
  → EndpointSpec catalog
  → FetchingRunner
  → raw_payload SQLite rows
  → parsing
  → main.db
  → asr
```

- `bili_unit.fetching._endpoint_catalog` 是本项目的 endpoint 真相源。
- `docs/endpoint-contract.md` 记录本项目实测和消费的 raw payload 形状。
- 上游文档用于查 callable、参数和返回大致结构；本项目的 SQLite schema 和 typed object 才是 consumer 契约。

## 维护规则

1. 上游接口可能变化；升级 `bilibili-api-python` 后优先跑 fetching / parsing / asr 全量测试（`processing` 是当前 ASR 实现包名）。
2. 新增 endpoint 时，先在 `_endpoint_catalog.py` 注册，再补 `docs/endpoint-contract.md`。
3. 不把上游文档再次整包镜像进本仓库；需要查完整 API 时直接访问上游 GitHub / 开发文档。
4. 任何绕过 `bilibili-api-python` 的直接 HTTP 调用，都要在 feature 文档中说明原因和返回形状。

