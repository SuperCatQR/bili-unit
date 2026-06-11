# bili_unit

Bilibili 数据源单元（unit）：抓取（fetching）+ 处理（processing）两条流水线。

本仓库是 [Dialectica](https://github.com/ChosenEcho/Dialectica) 项目 `source_data` 层下的一个 unit，
按 Dialectica 的 `unit` 抽象（`抓取 → 处理`）实现。跨源归一化与清洗不在本仓库内完成，
由 Dialectica 的 `index.ingestion` 子层承担。

## 许可

本项目以 **GPL-3.0-only** 许可发行。依赖 [bilibili-api-python](https://github.com/nemo2011/bilibili-api)
（GPL-3.0），传染条款下本仓库及其衍生作品须保持同等许可。完整许可文本见
[LICENSE](LICENSE)。

## 环境与命令

Python 3.12（由 `.python-version` 锁定），依赖用 `uv` 管理。

```bash
uv sync                              # 创建/刷新 .venv

# 统一 CLI（python -m bili_unit）
uv run python -m bili_unit fetch <uid>                       # 抓取所有端点
uv run python -m bili_unit fetch -e user_info videos <uid>   # 指定端点
uv run python -m bili_unit fetch --mode full <uid>           # 全量重抓
uv run python -m bili_unit query <uid>                       # 查抓取结果
uv run python -m bili_unit login                             # 二维码登录
uv run python -m bili_unit list-uids                         # 列出已抓取 uid
uv run python -m bili_unit process <uid>                     # 处理（transform-only MVP；audio 见下）
uv run python -m bili_unit process <uid> -m full             # 全量重新处理
uv run python -m bili_unit process <uid> -t video_metadata   # 指定项类型

# 子模块独立 CLI
uv run python -m bili_unit.fetching --list-uids
uv run python -m bili_unit.fetching -q <uid>
uv run python -m bili_unit.processing query <uid>
uv run python -m bili_unit.processing video-full <uid> <bvid>

# 测试 & lint
uv run pytest -v
uv run ruff check
```

## 凭据与运行时数据

```bash
cp .env.example .env     # 然后填入凭据，或先运行 `uv run python -m bili_unit login`
```

运行时数据（`data/`、`error/`、`temp/`）默认放在工作目录下，已被 `.gitignore` 排除。
要覆盖路径，在 `.env` 写：

```
BILI_FETCHING_DATA_DIR=...
BILI_FETCHING_ERROR_DIR=...
BILI_PROCESSING_DATA_DIR=...
BILI_PROCESSING_ERROR_DIR=...
BILI_PROCESSING_TEMP_DIR=...
```

## 文档

| 类别 | 路径 |
|---|---|
| 结构（must-be） | [docs/structure/bili.md](docs/structure/bili.md) |
| 设计（should-be + how-to） | [docs/design/](docs/design/) |
| 现状（is） | [docs/feature/](docs/feature/) |
| 接口研究 | [docs/research/api_info/](docs/research/api_info/) |

Dialectica 体系级结构（`main` / `source-data` / `unit` / `index`）保留在
Dialectica 主仓库，本仓库不重复。详见 [docs/structure/bili.md §0](docs/structure/bili.md)。

## 与 Dialectica 主仓库的关系

主仓库通过 `[tool.uv.sources]` 把 bili_unit 作为 editable 依赖装入：

```toml
[project]
dependencies = ["bili-unit"]

[tool.uv.sources]
bili-unit = { path = "../bili_unit", editable = true }
```

CLI、import、tests 全部以 `bili_unit` 命名空间为准；旧的 `source_data.bili` 路径已废弃。
