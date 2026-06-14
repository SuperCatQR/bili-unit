# bili_unit

Bilibili 数据 SDK，附带 `python -m bili_unit` CLI。给定一个用户 uid，抓取 64 个 B 站读取端点的原始响应、对象化为 typed object、再对视频音频做 ASR 转录。**作为 Python 库被 import 嵌入**或作为 CLI 直接使用都受支持。

## 项目定位

- **抓取（fetching）**：异步循环 64 个读取端点，全局 + 端点级双层限流，412 自适应降速 + 冷却恢复，所有请求结果原样落盘。
- **解析（parsing）**：把 raw dict 筛选、对象化并合并为 parsed objects；包含 5 个 legacy typed dataclass（`UpProfile` / `VideoDetail` / `Article` / `OpusPost` / `DynamicPost`）和统一内容视图 `ContentPost`，可选下载封面、头像、动态图片到本地。
- **处理（processing）**：对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。
- **存储**：纯文件 JSON KV，无数据库依赖；任何时刻按 `uid` 列出 / 删除 / 重跑。
- **状态机**：抓取与处理共享 `incremental` / `refresh` / `full` 三档语义，`incremental` 跳过已成功项目、重试失败项目。

不做的事：跨源归一化、清洗、检索 — 那是上游 [Dialectica](https://github.com/ChosenEcho/Dialectica) 的事，本仓库只产出每个 uid 的原始与结构化数据单元。

- **接入形态**：SDK 优先，CLI 是其薄包装。`async with bili_unit.session() as (cmd, qry):` 是推荐入口，详见 [Embedding](#embedding) 与 [docs/api.md](docs/api.md)。

## 安装

依赖 Python 3.12，用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
git clone <repo-url> bili_unit
cd bili_unit
uv sync                                            # 创建 .venv 并安装依赖
cp .env.example .env                               # 准备凭据文件
uv run python -m bili_unit login                   # 二维码登录，凭据写到 .env
uv run python -m bili_unit init-mimo               # （可选）配置 MiMo ASR
```

> Token Plan key（`tp-*`）与 pay-as-you-go key（`sk-*`）在 MiMo 不通用，按你买的方案选 profile。
> `ffmpeg` 由 `imageio-ffmpeg` 自带兜底；Silero VAD 走 `pysilero-vad`（ONNX，不拉 torch）。

## 用法

```bash
# 三个核心命令
uv run python -m bili_unit fetch <uid>             # 抓 raw payload
uv run python -m bili_unit parse <uid>             # 解析为 parsed objects（加 -i 下载图片）
uv run python -m bili_unit process <uid>           # ASR 转录视频音频

# 查询与管理
uv run python -m bili_unit query <uid>             # 抓取任务 / 端点状态
uv run python -m bili_unit list-uids               # 列出所有抓过的 uid
uv run python -m bili_unit delete-uid <uid> -y     # 删除某 uid 全部数据（不可逆）
uv run python -m bili_unit video-full <uid> <bvid> # 单视频联合视图（metadata + transcription）
uv run python -m bili_unit manifest <uid>          # 跨阶段聚合摘要（每跑一阶段自动刷新）
```

各命令的完整参数（mode 切换、端点过滤、ASR 后端选择等）见对应 feature 文档。

## Embedding

作为库嵌入 Python 应用：

```python
import asyncio
import bili_unit

async def main() -> None:
    async with bili_unit.session() as (cmd, qry):
        await cmd.fetch(uid=123)
        task = await qry.fetching.get_task(uid=123)
        if task is not None:
            for ep_name, ep_dto in task.endpoints.items():
                print(ep_name, ep_dto.status.value)

asyncio.run(main())
```

`session()` 是推荐入口；它包了 `assemble()` 与 `cmd.close()` 的生命周期。要程序化构造配置（不走 `.env`）：

```python
from bili_unit import BiliSettings, session

settings = BiliSettings(
    bili_fetching_data_dir="/var/lib/bili/fetching",
    bili_processing_data_dir="/var/lib/bili/processing",
    bili_processing_asr_backend="mock",  # 临时跳过 MiMo
)

async with session(settings=settings) as (cmd, qry):
    ...
```

要由宿主应用接管凭据：

```python
from bili_unit import CredentialProvider, session
from bilibili_api import Credential

async def my_provider() -> Credential | None:
    return Credential(sessdata=..., bili_jct=..., buvid3=...)

async with session(credential_provider=my_provider) as (cmd, qry):
    ...
```

稳定 API 边界与扩展点见 [docs/api.md](docs/api.md)。

## 凭据与运行时数据

`.env` 由 `login` 命令写入；`.env.example` 列出所有可覆盖配置项。运行时目录默认在工作目录下，已被 `.gitignore` 排除：

```
data/bili/fetching/        # 抓取 raw_payload + task / progress
data/bili/parsing/         # 解析 typed objects + images（可选）
data/bili/processing/      # 结构化 result + ASR 缓存 + temp（自动清理）
data/bili/manifest/        # 每个 uid 一份跨阶段聚合摘要
error/bili/                # 失败请求与可重试状态
```

要换路径，在 `.env` 设置 `BILI_FETCHING_DATA_DIR` / `BILI_PARSING_DATA_DIR` / `BILI_PROCESSING_DATA_DIR` 等（完整列表见 `.env.example`）。

## 开发

```bash
uv run pytest -v                                   # 全量测试（~7.5 分钟，无网络）
uv run ruff check                                  # lint
```

测试覆盖抓取 runner、限流、端点 schema 适配、解析 typed model + `ContentPost` + 数据层 + command/query 编排、ASR pipeline。所有外部 API 都被 mock。

## 文档

| 类别 | 路径 | 性质 |
|---|---|---|
| **稳定 API** | [docs/api.md](docs/api.md) | SDK 公开面、内部边界、SemVer 承诺 |
| 领域语言 | [CONTEXT.md](CONTEXT.md) | 项目术语表（unit / stage / raw_payload / ContentPost 等） |
| 决策记录 | [docs/adr/](docs/adr/) | ADR：难逆转 + 需背景的架构决策 |
| 模块边界 | [docs/structure/bili.md](docs/structure/bili.md) | 各层职责约束（must-be） |
| 数据契约 | [docs/structure/fetching-contract.md](docs/structure/fetching-contract.md) | 64 个端点的 raw_payload schema |
| fetching 现状 | [docs/feature/fetching.md](docs/feature/fetching.md) | 端点注册表、限流、模式、CLI |
| parsing 现状 | [docs/feature/parsing.md](docs/feature/parsing.md) | model 字段映射、图片下载、CLI |
| processing 现状 | [docs/feature/processing.md](docs/feature/processing.md) | audio pipeline、ASR 后端、CLI |
| manifest 摘要 | [docs/feature/manifest.md](docs/feature/manifest.md) | 跨阶段聚合 JSON、字段、刷新触发 |
| 接口参考 | [docs/bili-api-info/](docs/bili-api-info/) | bilibili-api-python 速查（外部资料镜像） |

## 许可与依赖

本项目以 **GPL-3.0-only** 许可发行（见 [LICENSE](LICENSE)）。

抓取层基于 [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（GPL-3.0），其传染条款下本仓库及其衍生作品须保持同等许可。`docs/bili-api-info/` 是该库官方接口参考的本地镜像，方便离线查阅与 LLM 检索。

ASR 后端默认对接 [小米 MiMo ASR](https://api.xiaomimimo.com)（OpenAI-compatible chat completions 接口），可配置走 Token Plan / pay-as-you-go / 自托管中转。VAD 用 [pysilero-vad](https://github.com/rhasspy/pysilero-vad)。

## 与 Dialectica 的关系

bili_unit 是独立 SDK，独立可用、独立发版。Dialectica 是它的第一个消费者：[Dialectica](https://github.com/ChosenEcho/Dialectica) 项目通过 `[tool.uv.sources]` 把它装为 editable 依赖，bili_unit 的 query 出口面服务 Dialectica 的 `index.ingestion` 子层。跨源归一化、清洗、检索由 Dialectica 承担，不在本仓库范围内。
