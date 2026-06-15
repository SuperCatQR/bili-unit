# bili_unit

Bilibili 数据 SDK，附带 `python -m bili_unit` CLI。给定一个用户 uid，抓取 64 个 B 站读取端点的原始响应、对象化为 typed object、再对视频音频做 ASR 转录。**作为 Python 库被 import 嵌入**或作为 CLI 直接使用都受支持。

## 项目定位

- **抓取（fetching）**：异步循环 64 个读取端点，全局 + 端点级双层限流，412 自适应降速 + 冷却恢复，所有请求结果原样落盘。
- **解析（parsing）**：把 raw dict 筛选、对象化并写入主 DB 为 6 个 typed dataclass（`UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`），可选下载封面、头像、动态图片到本地 workdir。
- **处理（processing）**：对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。
- **存储**：**SQLite per uid** —— 一 uid 一 main DB + 一 raw DB + 一 workdir，消费端直接 `sqlite3.connect(bili_unit.db_path(uid))` 读取。
- **状态机**：抓取与处理共享 `incremental` / `refresh` / `full` 三档语义，`incremental` 跳过已成功项目、重试失败项目。

不做的事：跨源归一化、清洗、检索 — 那是上游 [Dialectica](https://github.com/ChosenEcho/Dialectica) 的事，本仓库只产出每个 uid 的原始与结构化数据单元。

- **接入形态**：写侧 SDK + 读侧 SQL。`async with bili_unit.session() as cmd:` 跑 fetch / parse / process；读侧用 `sqlite3` 直连 `bili_unit.db_path(uid)`，详见 [Embedding](#embedding) 与 [docs/schema.md](docs/schema.md)。

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

写侧三个核心命令 + 删除 + 凭据：

```bash
uv run python -m bili_unit fetch <uid>             # 抓 raw payload（→ {uid}.raw.db）
uv run python -m bili_unit parse <uid>             # 解析为 parsed objects（加 -i 下载图片）
uv run python -m bili_unit process <uid>           # ASR 转录视频音频
uv run python -m bili_unit delete-uid <uid> -y     # 删除某 uid 全部数据（不可逆）
```

读侧没有 CLI 子命令 —— 直接用 `sqlite3` 查 `data/bili/{uid}.db`：

```bash
sqlite3 data/bili/123456.db "SELECT * FROM manifest_summary"
sqlite3 data/bili/123456.db "SELECT bvid, title FROM video ORDER BY pubdate_ms DESC LIMIT 10"
```

各写侧命令的完整参数（mode 切换、端点过滤、ASR 后端选择等）见对应 feature 文档；表结构与常用查询见 [docs/schema.md](docs/schema.md)。

## Embedding

作为库嵌入 Python 应用 —— 写侧走 `session()`，读侧直连 SQLite：

```python
import asyncio
import sqlite3
import bili_unit

async def main() -> None:
    async with bili_unit.session() as cmd:
        await cmd.fetch(uid=123)
        await cmd.parse(uid=123)

    # Read side — open the db directly:
    conn = sqlite3.connect(bili_unit.db_path(123))
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT bvid, title FROM video LIMIT 5"):
        print(row["bvid"], row["title"])

asyncio.run(main())
```

`session()` 是推荐入口；它包了 `assemble()` 与 `cmd.close()` 的生命周期。要程序化构造配置（不走 `.env`）：

```python
from bili_unit import BiliSettings, session

settings = BiliSettings(
    bili_db_dir="/var/lib/bili",
    bili_processing_asr_backend="mock",  # 临时跳过 MiMo
)

async with session(settings=settings) as cmd:
    ...
```

要由宿主应用接管凭据：

```python
from bili_unit import CredentialProvider, session
from bilibili_api import Credential

async def my_provider() -> Credential | None:
    return Credential(sessdata=..., bili_jct=..., buvid3=...)

async with session(credential_provider=my_provider) as cmd:
    ...
```

稳定数据契约（表 / 视图 / 索引）见 [docs/schema.md](docs/schema.md)。

## 凭据与运行时数据

`.env` 由 `login` 命令写入；`.env.example` 列出所有可覆盖配置项。运行时目录默认在工作目录下，已被 `.gitignore` 排除：

```text
data/bili/{uid}.db        # 主 DB（消费契约：用户内容、解析结果、ASR 转录、任务/错误状态）
data/bili/{uid}.raw.db    # raw DB（生产私有：抓取层 raw_payload + cursor）
data/bili/{uid}/          # workdir（图片 / 音频缓存等二进制资产）
```

要换路径，在 `.env` 设置 `BILI_DB_DIR`（完整列表见 `.env.example`）。

## 开发

```bash
uv run pytest -v                                   # 全量测试（~7.5 分钟，无网络）
uv run ruff check                                  # lint
```

测试覆盖抓取 runner、限流、端点 schema 适配、解析 6 个 typed model + 数据层 + command 编排、ASR pipeline、SQLite store。所有外部 API 都被 mock。

## 文档

> 注意：下表所列各 stage 的 feature / structure 文档当前仍描述旧的 JSON-KV 实现，与 SQLite 重构后的代码已不一致；后续会单独清理（公开的 follow-up task）。

| 类别 | 路径 | 性质 |
| --- | --- | --- |
| **数据契约** | [docs/schema.md](docs/schema.md) | SQLite DDL、表/视图、5 个常用查询 |
| 领域语言 | [CONTEXT.md](CONTEXT.md) | 项目术语表（unit / stage / raw_payload 等） |
| 决策记录 | [docs/adr/](docs/adr/) | ADR：难逆转 + 需背景的架构决策 |
| 模块边界 | [docs/structure/bili.md](docs/structure/bili.md) | 各层职责约束（must-be，待更新） |
| 端点契约 | [docs/structure/fetching-contract.md](docs/structure/fetching-contract.md) | 64 个端点的 raw_payload schema |
| fetching 现状 | [docs/feature/fetching.md](docs/feature/fetching.md) | 端点注册表、限流、模式、CLI（待更新） |
| parsing 现状 | [docs/feature/parsing.md](docs/feature/parsing.md) | model 字段映射、图片下载、CLI（待更新） |
| processing 现状 | [docs/feature/processing.md](docs/feature/processing.md) | audio pipeline、ASR 后端、CLI（待更新） |
| 接口参考 | [docs/bili-api-info/](docs/bili-api-info/) | bilibili-api-python 速查（外部资料镜像） |

## 许可与依赖

本项目以 **GPL-3.0-only** 许可发行（见 [LICENSE](LICENSE)）。

抓取层基于 [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（GPL-3.0），其传染条款下本仓库及其衍生作品须保持同等许可。`docs/bili-api-info/` 是该库官方接口参考的本地镜像，方便离线查阅与 LLM 检索。

ASR 后端默认对接 [小米 MiMo ASR](https://api.xiaomimimo.com)（OpenAI-compatible chat completions 接口），可配置走 Token Plan / pay-as-you-go / 自托管中转。VAD 用 [pysilero-vad](https://github.com/rhasspy/pysilero-vad)。

## 与 Dialectica 的关系

bili_unit 是独立 SDK，独立可用、独立发版。Dialectica 是它的第一个消费者：[Dialectica](https://github.com/ChosenEcho/Dialectica) 项目通过 `[tool.uv.sources]` 把它装为 editable 依赖。SQLite 重构后两者间的读侧接缝从 Python query API 变为直接 SQL —— Dialectica 的 `index.ingestion` 子层用 `sqlite3.connect(bili_unit.db_path(uid))` 消费。跨源归一化、清洗、检索由 Dialectica 承担，不在本仓库范围内。
