# 需求交接 — bili processing 补 `user_profile` transform handler

> ⚠️ **本文档已废弃（2026-06-14）**
>
> processing 层不再持有 user_profile transform handler。该 handler 连同整个 transform 子系统已删除（理由：parsing 重构后退化为字段透传，且 ingestion 仍未实装，没有契约要保护）。
>
> ingestion 待实装时直接消费 `parsing.query.get_user_profile(uid)` 即可，不需要在 processing 加层。
>
> 历史背景保留供参考；下方"落点 / 测试 / 输出 schema / CLI 验证"等具体要求**不再适用**。

> 提出时间：2026-06-11
> 提出方：index.ingestion 设计（向 bili unit 提需求）
> 接收方：bili unit（source_data/bili/processing/）
> 状态：已废弃（详见上方）
> 2026-06-14 后更新：handler 注册表只剩 `video_metadata / content_post / user_profile`（旧的 articles / opus / dynamics handler 已合并为 content_post）；user_profile handler 实施细节见 `docs/feature/processing.md`。
> 关联文档：
>   - 上游约束 `docs/structure/bili.md` §4/§5/§6/§8
>   - 上游设计 `docs/design/bili/processing.md`（实施时回写 §6.6 + §16 + §19）
>   - 代码现状 `docs/feature/bili/processing.md`（实施后回写 handler 表 + 测试矩阵）

## 1. 背景与动机

### 1.1 上游需求来源

`index.ingestion` 在设计阶段需要把 BiliQuery 输出归一化为"跨源统一文档"形态。已经确定的文档清单：

| 文档类型 | doc_id | 现成 BiliQuery 出口 |
|---|---|---|
| video（metadata + audio 合一篇） | `bili:video:{bvid}` | `processing.get_video_full(uid, bvid)` |
| dynamic（含 FORWARD 子结构） | `bili:dynamic:{id_str}` | `processing.list_items(uid, "dynamics")` |
| **user**（UP 主画像） | `bili:user:{uid}` | **❌ 缺失** |

bili fetching 已经实装 user_info / relation_info / up_stat / overview_stat 四个 uid-level endpoint（见 `docs/feature/bili/fetching.md` T0/T1 端点表），但 bili processing 没有对应的 transform handler。

### 1.2 为什么必须补在 processing，而不是让 ingestion 直读 fetching

`index.ingestion` 的设计原则：**输入面 = `BiliQuery.processing` 的出口面**。

- 若 ingestion 同时依赖 `BiliQuery.processing` 与 `BiliQuery.fetching`，"耦合 BiliQuery、职责清晰"的取向立刻破。
- 若 ingestion 直读 fetching 的 raw_payload，相当于 ingestion 替 bili 做了字段抽取——这是 bili processing 的职责，不该跨层。
- 现有 video / dynamics / articles 都遵循"raw_payload → handler → 结构化结果"的统一模式，user 维度应当一致。

结论：在 bili processing 补一个 `user_profile` handler，让 ingestion 走 `processing.list_items(uid, "user_profile")` 的统一接口。

## 2. 范围与边界

### 2.1 落点

- 新文件：`source_data/bili/processing/transform/user_profile.py`
- 注册：`source_data/bili/processing/transform/_registry.py` 加入 HANDLERS
- 接口：实现 `TransformHandler` Protocol（与 video_metadata / dynamics / articles 同级）

### 2.2 不在范围内

- **fetching 端任何代码**：四个 endpoint 已实装且 T0/T1 实测 SUCCESS（uid:13991807），不需要改 client / runner / spec
- **bili 顶层 BiliCommand / BiliQuery facade**：handler 落地后，`BiliQuery.processing.list_items(uid, "user_profile")` 自然生效，不需要额外暴露
- **跨源归一化、字段重命名**：归 ingestion，不在本需求
- **audio / opus / subscribed_bangumi 等其它 handler**：见 §11，单独提

## 3. handler 规格

```text
item_type            user_profile
source_endpoints     ("user_info", "relation_info", "up_stat", "overview_stat")
                       前 3 个必填；overview_stat 可选
extract_items        每个 uid 产出 1 个 WorkItem（uid 仅出现一次）
item_id              str(uid)
store key            uid:{uid}:proc:user_profile:{uid}
```

`extract_items` 接收的 `raw_payloads` 是 `dict[endpoint_name, raw_payload_dict]`。三个必填 endpoint 任一缺失或 raw_payload 为空时，返回空列表（不入队），与现有 handler 处理 endpoint 缺失的方式一致。

## 4. 输出 schema

```json
{
  "uid": 13991807,
  "pipeline": "transform",
  "item_type": "user_profile",
  "item_id": "13991807",
  "status": "SUCCESS",
  "result": {
    "uid": 13991807,
    "name": "...",
    "sex": "男 | 女 | 保密",
    "sign": "...",
    "avatar": "https://...",
    "birthday": "MM-DD",
    "level": 6,
    "vip": { "type": 1, "status": 1, "label": "..." },
    "join_time": 1500000000,

    "social": {
      "following": 120,
      "follower": 50000,
      "whisper": 0,
      "black": 0
    },
    "stats": {
      "archive_view": 12345678,
      "article_view": 1234,
      "likes": 654321
    },
    "overview": {
      "video_count": 77,
      "article_count": 1,
      "opus_count": 64
    }
  },
  "source_endpoints": ["user_info", "relation_info", "up_stat", "overview_stat"],
  "processed_at": 1718000001000,
  "updated_at": 1718000001000
}
```

### 4.1 字段挑选原则

进 `result` 的字段必须满足下面任一条件：

- **身份**：name / sex / sign / birthday / avatar
- **画像**：level / vip / join_time
- **社交关系数量**：social
- **创作总量统计**：stats（来自 up_stat） + overview（来自 overview_stat）

不进 `result` 的字段（fetching 已存，ingestion 需要时再补，不在本需求）：

- 实时排行、临时文本（`space_notice` 这类）
- 装饰类（pendant / nameplate / honours）
- 隐私受限类（user_medal / all_followings 已是独立 endpoint，不并入 user_profile）

### 4.2 容错规则

- `result.overview` 在 `overview_stat` 缺失或为空时**整段省略**（不写 `null`、不写 `{}`），让 ingestion 通过键存在性判断
- `result.vip` 在 user_info 中缺失字段时降级为 `{"type": 0, "status": 0}`
- `result.birthday`、`result.sign` 等字符串字段缺失时使用空字符串 `""`，与现有 video_metadata handler 保持一致风格
- 数值字段（社交计数、stats）缺失时使用 `0`，不使用 `null`

## 5. fetching 状态消费规则（不阻塞）

沿用 `docs/design/bili/processing.md` §10.1：

| endpoint 组合状态 | 行为 |
|---|---|
| user_info / relation_info / up_stat 任一非 SUCCESS | 跳过 user_profile 工作项（本次不处理；下次 process_uid 重新评估） |
| 三者全 SUCCESS + overview_stat 非 SUCCESS | 入队，`result.overview` 省略 |
| 三者全 SUCCESS + overview_stat SUCCESS | 入队，全字段 |

不写回 fetching 状态。

## 6. 处理模式语义

与现有 transform handler 一致：

- `incremental`：已 SUCCESS 跳过；已 FAILED 重试一次；新 uid 自动入队
- `full`：忽略已有结果，重处理并覆盖写入

不需要新模式。

## 7. 测试要求

测试文件：`source_data/bili/tests/test_processing_transform.py`（沿用现有文件，追加 5 条）。

| # | 用例 | 断言 |
|---|---|---|
| 1 | 四 endpoint 全 SUCCESS happy path | result 含全部字段，含 `overview` |
| 2 | overview_stat 缺失 / 非 SUCCESS | result **不含** `overview` 键，handler 仍返回 SUCCESS |
| 3 | 必填 endpoint 之一缺失 | `extract_items` 返回空列表（不产生 WorkItem） |
| 4 | raw_payload 字段缺失容错 | name / sign / birthday 等使用默认值，handler 不抛 |
| 5 | runner 集成（参照 `test_processing_runner_happy_path`） | `process_uid(uid)` 后 `query.get_item(uid, "user_profile", str(uid))` 返回 SUCCESS DTO |

测试夹具：可直接用 uid:13991807 烟雾测试期间抓到的 raw_payload 子集（`source_data/bili/fetching/data/13991807/fetch/{user_info,relation_info,up_stat,overview_stat}.json`）做 mini fixture，无需新建大体量测试数据。

## 8. 文档回写清单（实施后必须更新）

| 文档 | 位置 | 内容 |
|---|---|---|
| `docs/design/bili/processing.md` | 新增 §6.6 | user_profile transform 的输入来源、处理逻辑、输出 schema、错误处理 |
| `docs/design/bili/processing.md` | §16 开发顺序 | 追加 "22. user_profile handler"（已 unblock，立即推进） |
| `docs/design/bili/processing.md` | §19 已决表 | 追加一行：user_profile 字段范围、source_endpoints 列表、overview 缺失策略 |
| `docs/feature/bili/processing.md` | "工作项与 handler" 表格 | 新增 user_profile 行 |
| `docs/feature/bili/processing.md` | "测试状态" / "测试矩阵" | transform handler 测试数 +5、processing 总测试数 +5 |
| `CLAUDE.md` | "What this project is" | 把 "video_metadata + dynamics + articles handlers" 更新为四个 |

## 9. CLI 验证

实施完成的判定标准：下列命令在 uid:13991807 上跑通且产出结构正确。

```bash
# 处理 user_profile
uv run python -m source_data.bili process 13991807 -t user_profile

# 查询任务状态，pipelines.transform.items 应含 user_profile 行
uv run python -m source_data.bili.processing query 13991807

# 查询单条结果
uv run python -m source_data.bili.processing query 13991807 \
  | grep -A2 user_profile

# 全量重处理
uv run python -m source_data.bili process 13991807 -t user_profile -m full
```

期望输出（关键片段）：

```text
pipelines:
  transform:
    user_profile: total=1, completed=1, failed=0, skipped=0
```

## 10. 工作量估计

| 项 | 估计 |
|---|---|
| handler 代码 | 80–100 行（参照 articles handler） |
| 注册表更新 | 1 行 |
| 测试 | 5 条 |
| 文档回写 | 3 个文件 6 处 |
| 总工期 | 半天 |

## 11. 显式排除（后续单独提需求）

下列处理器在 ingestion 后续阶段可能需要，**不阻塞本轮**，不在本需求内：

| 待补 handler | 来源 endpoint | 优先级 | 阻塞项 |
|---|---|---|---|
| audios | audios（T0 已抓） | 中 | 音频元数据 vs 音频转录的关系待定 |
| article_content（全文） | 待新增 fetching item-level fan-out | 中 | 需要 fetching 先扩展 article_content endpoint |
| opus | opus（T1 已抓） | 低 | 与 dynamics OPUS 类型可能重叠 |
| subscribed_bangumi | subscribed_bangumi（T1 已抓） | 低 | 用户兴趣画像维度，indexing 反馈驱动 |
| channel_videos | channel_videos_season/series（T2 已抓） | 低 | 合集结构对 KG 的价值待评估 |

这些 handler 的优先级应当由 indexing 阶段的实际查询反馈驱动，不在 ingestion 设计阶段决定。

## 12. 完成标准

- [ ] `source_data/bili/processing/transform/user_profile.py` 实装
- [ ] `_registry.py` 注册
- [ ] 5 条新测试通过
- [ ] `uv run pytest -v` 全部通过（应为 196 tests）
- [ ] `uv run ruff check` 全部通过
- [ ] CLI 验证（§9）在 uid:13991807 上产出 SUCCESS
- [ ] 文档回写（§8 清单 6 处）

## 13. 接收方签收

实施前请：

1. 确认 user_info / relation_info / up_stat / overview_stat 四个 endpoint 的实际 raw_payload shape（用 `uv run python -m source_data.bili.fetching -q 13991807` 看 fixture）
2. 若 §4 schema 与实际字段命名不一致，以实际 raw_payload 为准，handler 输出字段命名遵循"驼峰转下划线 + 保留 B 站语义"的现有约定（参照 video_metadata handler 处理 `pubdate` / `ctime` 的方式）
3. §4 schema 与实际偏离的部分，回写 `docs/design/bili/processing.md` §6.6 与 §19，以最终落地结果为准
