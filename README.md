# bili_unit

Bilibili 用户数据本地持久化工具。给定一个用户 uid，抓取 B 站读取端点的原始响应、对象化为 typed object、再对视频音频做 ASR 转录。项目定位是独立 CLI 工具：运行命令产出 per-uid SQLite 文件，后续分析、索引或检索由使用方直接读取这些文件完成。

## 项目定位

- **抓取（fetching）**：异步循环 64 个读取端点，全局 + 端点级双层限流，412 自适应降速 + 冷却恢复，所有请求结果原样落盘。
- **解析（parsing）**：把 raw dict 筛选、对象化并写入主 DB 为 6 个 typed dataclass（`UpProfile` / `VideoDetail` / `VideoSubtitle` / `Article` / `OpusPost` / `DynamicPost`），可选下载封面、头像、动态图片并默认存入主 DB。
- **ASR（asr）**：对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）；`processing` 仅是当前内部实现包名。
- **存储**：**SQLite per uid** —— 一 uid 一 main DB + 一 raw DB + 一 workdir，消费端直接用 `sqlite3` 读取；图片内容在 `image_asset.data`。
- **状态机**：fetching 支持 `incremental` / `refresh` / `full`；parsing 支持 `incremental` / `full`，`incremental` 只跳过已物化且不早于 raw payload 的项目；asr 支持 `incremental` / `full`，`incremental` 跳过已成功项目、重试失败项目。

不做的事：跨源归一化、清洗、检索。本仓库只产出每个 uid 的原始与结构化数据单元；更上层的分析、索引或检索由使用方自行完成。

## 安装

依赖 Python 3.12，用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
git clone <repo-url> bili_unit
cd bili_unit
uv sync                                            # 创建 .venv 并安装依赖
cp .env.example .env                               # 准备凭据文件
uv run python -m bili_unit login                   # 二维码登录，凭据写到 .env
uv run python -m bili_unit init-mimo --test        # （可选）配置并探测 MiMo ASR
```

> Token Plan key（`tp-*`）与 pay-as-you-go key（`sk-*`）在 MiMo 不通用，按你买的方案选 profile。
> `ffmpeg` 由 `imageio-ffmpeg` 自带兜底；Silero VAD 走 `pysilero-vad`（ONNX，不拉 torch）。

## 用法

常用写侧命令 + 删除 + 凭据：

```bash
uv run python -m bili_unit sync <uid>              # 抓 raw payload 并解析为 parsed objects（加 -i 下载图片）
uv run python -m bili_unit asr <uid>           # ASR 转录视频音频（有完整字幕时会直接短路）
uv run python -m bili_unit delete-uid <uid> -y     # 删除某 uid 全部数据（不可逆）
```

Naming note: `asr` is the user-facing command and DB stage name. The older
`process` command is no longer exposed by the CLI.

高级/调试入口仍保留：`fetch <uid>` 只抓 raw DB，`parse <uid>` 只把已有 raw DB 重新物化到主 DB。

读侧没有 CLI 子命令 —— 直接用 `sqlite3` 查 `output/bili/{uid}.db`：

```bash
sqlite3 output/bili/123456.db "SELECT * FROM manifest_summary"
sqlite3 output/bili/123456.db "SELECT bvid, title FROM video ORDER BY pubdate_ms DESC LIMIT 10"
```

各写侧命令的完整参数（mode 切换、端点过滤、ASR 后端选择等）见对应 feature 文档；表结构与常用查询见 [docs/schema.md](docs/schema.md)。

## 读取结果

本项目不提供 Python query facade。命令跑完后，直接读取 SQLite：

```python
import sqlite3

conn = sqlite3.connect("output/bili/123456.db")
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT bvid, title FROM video ORDER BY pubdate_ms DESC LIMIT 5"):
    print(row["bvid"], row["title"])
```

表 / 视图 / 索引见 [docs/schema.md](docs/schema.md)。Python 包内仍有少量 helper 供 CLI 和调试脚本复用，但不按通用库承诺稳定 API。

## 凭据与运行时数据

`.env` 由 `login` 命令写入；`.env.example` 列出所有可覆盖配置项。运行时目录默认在工作目录下，已被 `.gitignore` 排除：

```text
output/bili/{uid}.db        # 主 DB（消费契约：用户内容、解析结果、ASR 转录、任务/错误状态）
output/bili/{uid}.raw.db    # raw DB（生产私有：抓取层 raw_payload + cursor）
output/bili/{uid}/          # workdir（音频缓存、临时文件等）
```

要换路径，在 `.env` 设置 `BILI_DB_DIR`（完整列表见 `.env.example`）。

## 开发

```bash
uv run pytest -v                                   # 全量测试（~30 秒，无网络）
uv run ruff check                                  # lint
```

测试覆盖抓取 runner、限流、端点 schema 适配、解析 6 个 typed model + 数据层 + command 编排、ASR pipeline、SQLite store。所有外部 API 都被 mock。

## 文档

新读者建议先看：[docs/README.md](docs/README.md) → [docs/schema.md](docs/schema.md) → [docs/architecture.md](docs/architecture.md)。

| 类别 | 路径 | 性质 |
| --- | --- | --- |
| **文档入口** | [docs/README.md](docs/README.md) | 当前真相的阅读路线 |
| **数据契约** | [docs/schema.md](docs/schema.md) | SQLite DDL、表/视图、5 个常用查询 |
| 领域语言 | [CONTEXT.md](CONTEXT.md) | 项目术语表（unit / stage / raw_payload 等） |
| 模块边界 | [docs/architecture.md](docs/architecture.md) | 各层职责约束（must-be） |
| 端点契约 | [docs/endpoint-contract.md](docs/endpoint-contract.md) | 64 个端点的 raw_payload schema |
| 运行状态 | [docs/observability.md](docs/observability.md) | run events、Run Summary、TUI 读模型 |
| 上游 API | [docs/upstream.md](docs/upstream.md) | bilibili-api-python 的角色、链接与维护规则 |

## 许可与依赖

本项目以 **GPL-3.0-only** 许可发行（见 [LICENSE](LICENSE)）。

抓取层基于 [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（GPL-3.0），其传染条款下本仓库及其衍生作品须保持同等许可。上游项目与文档入口见 [docs/upstream.md](docs/upstream.md)。

ASR 后端默认对接 [小米 MiMo ASR](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)（OpenAI-compatible chat completions 接口），可配置走 Token Plan / pay-as-you-go / 自托管中转。VAD 用 [pysilero-vad](https://github.com/rhasspy/pysilero-vad)。
