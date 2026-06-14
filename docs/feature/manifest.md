# Manifest（每 uid 跨阶段摘要）

## 概述

每个 uid 在 `{BILI_MANIFEST_DIR}/{uid}.json`（默认 `data/bili/manifest/{uid}.json`）有一份**跨阶段聚合摘要**，把 fetching / parsing / processing 三 stage 的 task 状态、count、cost、completeness、最近运行时间合并成一个 dict。

下游消费方不需要再扫三个目录就能回答 "这个 uid 现在跑到哪一步、有多少完整数据、ASR 花了多少 token"。

manifest 是 fetching / parsing / processing 任意一阶段跑完后自动刷新写入；CLI `manifest <uid>` 只是读盘打印，不重新计算。

## 字段定义

```json
{
  "uid": 12345,
  "schema_version": 1,
  "computed_at": 1718000000000,

  "fetching": {
    "status": "SUCCESS",
    "endpoint_count": 64,
    "success_count": 60,
    "failed_count": 2,
    "failed_item_ids": [...],
    "updated_at": 1718000001000
  },

  "parsing": {
    "status": "SUCCESS",
    "models": {
      "user_profile":  {"count": 1,  "complete_count": 1,  "status": "SUCCESS"},
      "video_work":    {"count": 76, "complete_count": 70, "status": "SUCCESS"},
      "article_post":  {"count": 12, "complete_count": 12, "status": "SUCCESS"},
      "opus_post":     {"count": 30, "complete_count": 28, "status": "SUCCESS"},
      "dynamic_event": {"count": 80, "complete_count": 80, "status": "SUCCESS"},
      "video_subtitle":{"count": 30, "complete_count": 28, "status": "SUCCESS"}
    },
    "images": {"total": 47, "ok": 43, "skipped": 2, "failed": 2},
    "failed_item_ids": [],
    "updated_at": 1718000010000
  },

  "processing": {
    "status": "SUCCESS",
    "pipelines": {
      "audio": {
        "status": "SUCCESS",
        "transcription": {
          "total": 76, "completed": 70,
          "failed": 6, "skipped": 0,
          "subtitle_source": 30, "asr_source": 40
        }
      }
    },
    "failed_item_ids": ["audio:transcription:BV1abc"],
    "updated_at": 1718000020000
  },

  "cost": {
    "total_audio_tokens": 12345,
    "total_seconds": 1900,
    "asr_calls": 40,
    "cache_hits": 20,
    "subtitle_count": 30
  },

  "completeness": {
    "user_profile": 1.0,
    "video_work":   0.92,
    "article_post": 1.0,
    "opus_post":    0.93,
    "dynamic_event":1.0,
    "video_subtitle": 0.40
  }
}
```

字段说明：

- **fetching.success_count / failed_count**：endpoint 级状态汇总，只数 `SUCCESS` 与 `FAILED_*`/`PARTIAL_ITEM`。
- **parsing.models[name].complete_count**：经过 `is_complete` 计算后视为 "数据完整" 的 typed object 数（参见 W3.1 / `docs/feature/parsing.md`）。
- **processing.pipelines.audio.transcription.subtitle_source / asr_source**：成功的转写按来源切分。`subtitle` 是字幕短路、`asr` 是 ASR 实际调用。两者之和应当 ≤ `completed`。
- **cost.\***：跨所有 audio item 累加。`subtitle_count` 是字幕路径数量（cost 一定为 0），`asr_calls` 是 ASR 路径数量，`cache_hits` 是命中 ASR 缓存的段计数（缓存命中 token 不重新计费但保留累计值）。
- **completeness.\***：`is_complete=True` 的 item 占该 model 总数的比例。`video_subtitle` 是覆盖率（拿到字幕的视频 / 全部视频）。当某 model 数量为 0 时该 key 省略。

任意一个 stage 没跑过 → 对应 slot 是 `null`。

## 触发写盘

| 时机 | 谁触发 | 说明 |
|---|---|---|
| `BiliCommand.fetch()` 跑完 | unit 顶层 | fetching 完成后立即重算 + 写盘 |
| `BiliCommand.parse()` 跑完 | unit 顶层 | parsing 完成后重算 |
| `BiliCommand.process()` 跑完 | unit 顶层 | processing 完成后重算 |
| `BiliCommand.delete_uid()` | unit 顶层 | 删 manifest 文件 |
| 直接调 stage 内部 command | **不写** | 仅在 `BiliCommand` 这一层挂钩，stage 内部 command 不感知 manifest |

写盘是**best-effort**：抛错只 warn 日志，不阻塞主流程（避免 manifest 故障带翻 fetch / parse / process 整体）。

## 何时不写

- `BiliCommand` 在 narrow 测试里被手工构造（不传 `query=` / `settings=`），manifest 持久化跳过——保留向后兼容。
- 任何阶段 raise 之前的早退路径（runner 抛错被命令层捕获重抛）也会跳过；只有正常返回 `*CommandResult` 时才会跑刷新钩子。

## 消费方

- CLI: `python -m bili_unit manifest <uid>`（默认是简短摘要；`--json` 输出完整 JSON）。
- 嵌入：直接读 `{BILI_MANIFEST_DIR}/{uid}.json`。`compute_manifest / read_manifest / write_manifest / delete_manifest` 是 internal 函数，签名可能在小版本变化，不要直接 import。

## 配置

- `BILI_MANIFEST_DIR`（默认 `data/bili/manifest`）。
