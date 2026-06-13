# bili_unit

Bilibili 数据抓取与处理工具。给定一个用户 uid，把 28 个 B 站接口的原始响应落到本地，再把它们规整成结构化记录（视频元数据、动态、专栏正文、图文正文、UP 主画像）外加视频音频转录。

## 项目定位

- **抓取（fetching）**：异步循环 28 个端点，全局 + 端点级双层限流，412 自适应降速 + 冷却恢复，所有请求结果原样落盘。
- **处理（processing）**：从抓取结果派生五类结构化记录（`video_metadata` / `dynamics` / `articles` / `opus` / `user_profile`），并对视频音频做 ASR 转录（VAD 切分 + 段级断点续传 + 段间文本去重拼接）。
- **存储**：纯文件 JSON KV，无数据库依赖；任何时刻按 `uid` 列出 / 删除 / 重跑。
- **状态机**：抓取与处理都用同一套 `incremental` / `refresh` / `full` 三档语义，`incremental` 跳过已成功项目、重试失败项目。

不做的事：跨源归一化、清洗、检索 — 那是上游 [Dialectica](https://github.com/ChosenEcho/Dialectica) 的事，本仓库只产出每个 uid 的原始与结构化数据单元。

## 安装与运行

依赖 Python 3.12，用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
git clone <repo-url> bili_unit
cd bili_unit
uv sync                                            # 创建 .venv 并安装依赖
cp .env.example .env                               # 准备凭据文件（凭据用 login 命令写入）
uv run python -m bili_unit login                   # 二维码登录，凭据写到 .env
```

ASR 转录默认走 MiMo 云端，按需配置：

```bash
uv run python -m bili_unit init-mimo               # MiMo ASR 配置向导（profile / api key / 中转站）
```

> Token Plan key（`tp-*`）与 pay-as-you-go key（`sk-*`）在 MiMo 不通用；按你买的方案选 profile。

`ffmpeg` 由 `imageio-ffmpeg` 自带兜底；Silero VAD 走 `pysilero-vad`（ONNX，不拉 torch）。

## 用法

```bash
# 抓取：把 uid 的 raw_payload 落到 data/bili/fetching
uv run python -m bili_unit fetch <uid>                             # 全部端点（默认增量）
uv run python -m bili_unit fetch <uid> -x video_detail             # 跳过最耗时的 video_detail（推荐用法）
uv run python -m bili_unit fetch <uid> -x video_detail upower_qa   # 跳多个用空格分隔
uv run python -m bili_unit fetch <uid> -e user_info relation_info  # 只跑指定端点（调试，与 -x 互斥）
uv run python -m bili_unit fetch <uid> -m refresh                  # 增量 + 重抓 stale 项（N 见 .env）
uv run python -m bili_unit fetch <uid> -m full                     # 全量重抓

# 处理：把 raw_payload 转换为结构化 result
uv run python -m bili_unit process <uid>                           # 全部 5 个 handler，按 .env 选 ASR 后端
uv run python -m bili_unit process <uid> -x video_metadata         # 跳过指定 handler
uv run python -m bili_unit process <uid> -t user_profile           # 只跑指定 handler（调试，与 -x 互斥）
uv run python -m bili_unit process <uid> -m full                   # 重处理所有项目（不支持 refresh）
uv run python -m bili_unit process <uid> -b mock                   # 临时跳过真实 ASR（CI / 不烧 token）

# 查询 / 管理
uv run python -m bili_unit query <uid>                             # 抓取任务 / 端点状态
uv run python -m bili_unit list-uids                               # 列出所有抓过的 uid
uv run python -m bili_unit delete-uid <uid> -y                     # 删除某 uid 全部数据（不可逆）
uv run python -m bili_unit video-full <uid> <bvid>                 # 单视频联合视图（metadata + transcription）
```

`fetch` 与 `process` 默认全跑：`-x` 排除少数项是日常用法，`-e/-t` 指定子集是调试用法。

### 向后兼容子模块入口

旧版 `python -m bili_unit.fetching` 和 `python -m bili_unit.processing` 仍然可用，内部自动转发到统一 CLI，老脚本无需改动。新脚本建议直接用 `python -m bili_unit <sub>` 格式。

### 抓取端点清单

| 类型 | 端点 |
|---|---|
| 用户基本信息 | `user_info` `relation_info` `up_stat` `overview_stat` `user_medal` `space_notice` `elec_monthly` |
| 投稿/创作 | `videos` `articles` `opus` `dynamics` `audios` `top_videos` `masterpiece` |
| 列表/合集 | `channel_list` `article_list` `subscribed_bangumi` `cheese` `album` `user_fav_tag` `all_followings` `upower_qa` |
| item-level fan-out | `video_detail`（自 `videos` 派生）`article_detail`（自 `articles` 派生，提供专栏 markdown 正文）`opus_detail`（自 `opus` 派生，提供图文 markdown + 图片清单）`article_list_detail`（自 `article_list` 派生，提供文集 → 文章 cvid 清单）`channel_videos_season` `channel_videos_series`（自 `channel_list` 派生） |

需要凭据的端点：`user_medal` / `all_followings` / `elec_monthly` / `upower_qa`（其它端点匿名也可抓）。

完整说明（分页策略、限流 key、是否需凭据）见 [docs/feature/fetching.md](docs/feature/fetching.md) §端点注册表。

### 处理 handler 清单（5 个）

| item_type | 输入端点 | 输出（result 形状） |
|---|---|---|
| `video_metadata` | `video_detail` | 单视频元数据（title / duration / tags / 数据指标） |
| `dynamics` | `dynamics` | 单条动态（type + 文本 / 媒体 + repost 链） |
| `articles` | `articles` + `article_detail`（可选）+ `article_list_detail`（可选） | 单篇专栏（meta + 摘要 + markdown 正文 + content_json 节点树 + word_count + 文集归属 `lists`） |
| `opus` | `opus` + `opus_detail`（可选） | 单条图文（meta + 摘要 + markdown 正文 + 图片清单 + word_count） |
| `user_profile` | `user_info` + `relation_info` + `up_stat` + `overview_stat`（可选） | UP 主画像（vip / social / stats / overview） |

`audio` pipeline 在 transform 之外独立运行：`video_metadata` 写入后由 ASR 后端转录视频音频。详见 [docs/feature/processing.md](docs/feature/processing.md)。

## 凭据与运行时数据

`.env` 由 `login` 命令写入；`.env.example` 列出所有可覆盖配置项。运行时目录默认放在工作目录下，已被 `.gitignore` 排除：

```
data/bili/fetching/        # 抓取 raw_payload + task / progress
data/bili/processing/      # 结构化 result + ASR 缓存 + temp（自动清理）
error/bili/                # 失败请求与可重试状态
```

要换路径，在 `.env` 写：

```
BILI_FETCHING_DATA_DIR=...
BILI_FETCHING_ERROR_DIR=...
BILI_PROCESSING_DATA_DIR=...
BILI_PROCESSING_ERROR_DIR=...
BILI_PROCESSING_TEMP_DIR=...
BILI_PROCESSING_ASR_CACHE_DIR=...
```

## 开发

```bash
uv run pytest -v                                   # 全量测试（~3.5 分钟）
uv run ruff check                                  # lint
```

测试覆盖抓取 runner、限流、所有 28 个端点的 schema 适配、处理 transform、ASR pipeline（VAD 切分、段级缓存、文本拼接）。无网络调用 — 所有外部 API 都被 mock。

## 文档

| 类别 | 路径 | 性质 |
|---|---|---|
| 结构（must-be） | [docs/structure/bili.md](docs/structure/bili.md) | 模块边界与职责约束 |
| 现状（is，**真相源**） | [docs/feature/](docs/feature/) | 实现现状；新增改动直接更新这里 |
| 接口研究 | [docs/bili-api-info/](docs/bili-api-info/) | bilibili-api-python 的接口参考速查（外部资料镜像） |
| 设计（**已废弃**） | [docs/design/](docs/design/) | 早期设计稿，仅作历史参考 |

## 许可与依赖

本项目以 **GPL-3.0-only** 许可发行（见 [LICENSE](LICENSE)）。

抓取层基于 [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（GPL-3.0），其传染条款下本仓库及其衍生作品须保持同等许可。`docs/bili-api-info/` 是该库官方接口参考的本地镜像，方便离线查阅与 LLM 检索。

ASR 后端默认对接 [小米 MiMo ASR](https://api.xiaomimimo.com)（OpenAI-compatible chat completions 接口），可配置走 Token Plan / pay-as-you-go / 自托管中转。VAD 用 [pysilero-vad](https://github.com/rhasspy/pysilero-vad)（Silero ONNX 封装，无 torch 依赖）。

## 与 Dialectica 的关系

bili_unit 是独立仓库，独立可用。同时它也是 [Dialectica](https://github.com/ChosenEcho/Dialectica) 项目 `source_data` 层下的一个 unit — Dialectica 主仓库通过 `[tool.uv.sources]` 把它装为 editable 依赖：

```toml
[project]
dependencies = ["bili-unit"]

[tool.uv.sources]
bili-unit = { path = "../bili_unit", editable = true }
```

跨源归一化、清洗、检索由 Dialectica 的 `index.ingestion` 子层承担，不在本仓库范围内。
