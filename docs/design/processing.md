# bili unit processing design

> 性质：实现设计。本文记录 `bili` processing 层的技术选型、设计决策与设计规则。
> 约束文档：`docs/structure/bili.md` §4/§6/§8 为绝对约束，本设计不与之冲突。
> 代码现状：`docs/feature/processing.md` 为真相源，记录代码实际能力。
> 上游设计：`docs/design/fetching.md` 为 fetching 层设计参考。

```text
信息归属
  模块划分、数据流方向、边界约束、状态归属    → docs/structure/bili.md
  技术选型理由、设计规则、设计决策            → 本文（docs/design/processing.md）
  endpoint 处理清单、状态枚举、DTO 字段、配置项、
  日志事件、value 形状、CLI、测试结果         → docs/feature/processing.md
```

## 1. 位置

```text
docs/design → bili → processing
```

```text
上游文档  docs/structure/unit.md, docs/structure/bili.md
结构约束  docs/structure/bili.md §4（处理模块）、§6（数据流）、§8（边界）
代码现状  docs/feature/processing.md
上游实现  docs/feature/fetching.md（fetching 层代码现状）
上游设计  docs/design/fetching.md（fetching 层设计参考）
外部依赖  MiMo ASR API（https://platform.xiaomimimo.com）, Bilibili CDN（音频流下载）
```

## 2. 设计定位

```text
对象      bili 的 processing 层实现设计
单位      目标用户 uid
职责      读取抓取结果 → transform（结构化转换）+ audio（音频下载 + ASR 转录）→ 处理结果入库
服务      unit 对外接口（index.ingestion pull）
不服务    index / reasoning / interaction
```

processing 是 bili unit 管线的第二（也是末）阶段：`抓取 → 处理`。处理层读取 fetching 层的 raw_payload，通过两条独立流水线产出"bili 形态"的结构化数据。跨源归一化与清洗不在本层完成，归 `index.ingestion` 承担。

模块划分、调用方向、数据流见 `docs/structure/bili.md` §4/§6。

## 3. 运行时与依赖方向

```text
语言              Python 3.12
包管理            uv
异步              asyncio
HTTP（音频下载）    aiohttp（复用 fetching 的 HTTP 后端配置）
ASR               MiMo API（api.xiaomimimo.com, model=mimo-v2.5-asr）
存储              文件目录 JSON 存储（与 fetching 一致）
配置              pydantic-settings + python-dotenv
测试              pytest + pytest-asyncio + pytest-mock
语法检查          ruff（E/W/F/I/UP/B/SIM）
```

```text
import 边界
  command → runner, DTO
  query → data, error, task（processing 自有存储）
  query → fetching.query（读取抓取结果，只读）
  runner → task, transform, audio, data, error, env, fetching.query
  transform → 无外部 import（纯计算）
  audio → env（ASR 配置、下载配置）
  audio → aiohttp（HTTP 下载）, MiMo API client
  data/error → 不 import command/query/runner/transform/audio
  env → 不 import data/error/task
```

```text
跨模块依赖
  processing 通过 fetching.query.Query 只读访问抓取结果。
  processing 不直接访问 fetching 的 DataStore / ErrorStore 内部。
  processing 不触发 fetching 的写侧流程。
  fetching 不知道 processing 的存在（单向依赖）。
```

## 4. 处理管线

### 4.1 两条流水线

```text
transform 流水线
  输入   fetching.query 提供的 raw_payload
  处理   纯计算：字段提取、结构规范化、跨 endpoint 合并
  输出   处理结果 → data store
  特征   无外部调用、无 I/O 阻塞（纯 CPU）、确定性输出

audio 流水线
  输入   fetching.query 提供的 video_detail（cid, bvid）
  处理   CDN 音频下载 → temp → 音频预处理 → MiMo ASR API 调用
  输出   转录文本 → data store
  特征   有外部调用（CDN + ASR API）、有 I/O 阻塞、可能失败
```

两条流水线相互独立：transform 不依赖 audio 的输出，audio 不依赖 transform 的输出。它们共享相同的工作项源（fetching 的 endpoint 结果）和输出目标（processing 的 data store），但处理逻辑完全解耦。

### 4.2 MVP 范围

```text
MVP 流水线（第一批实现）
  transform:
    video_metadata      视频元数据合并（videos + video_detail → 结构化记录）
    dynamics            动态文本提取与结构化
    articles            专栏文章内容处理
  audio:
    （设计完成，实现推迟；提供抽象接口与 MiMo 后端参考实现）
```

```text
非 MVP 但已设计
  audio 完整流水线（CDN 下载 + MiMo ASR）
  opus 图文帖处理
  channel_videos 合集视频处理
  subscribed_bangumi 追番信息处理
```

### 4.3 工作项（Work Item）

processing 以 **工作项（work item）** 为处理单位。每个工作项对应一个独立的处理任务，由 runner 从 fetching 结果中提取并分发给 transform 或 audio worker。

```text
工作项来源与类型
  video_metadata    每个 bvid 一个工作项；读取 video_detail/{bvid} 的 raw_payload
  dynamics          每个 dynamic_id 一个工作项；读取 dynamics 的 raw_payload.pages
  articles          每个 article_id 一个工作项；读取 articles 的 raw_payload.pages
  audio             每个 bvid 一个工作项；读取 video_detail/{bvid} 获取 cid
```

工作项 ID 格式：`{pipeline}:{item_type}:{item_id}`，例如 `transform:video_metadata:BV1xxxxxxxxxx`。

每个工作项独立处理、独立存储、独立重试、独立跟踪状态。

## 5. 存储设计

### 5.1 存储分离

```text
processing 维护独立于 fetching 的三组存储：

data       处理结果 + 处理状态 + 处理进度
temp       临时文件（音频下载中间产物）；处理完成后删除
error      处理阶段错误记录
```

processing 的 data/error 与 fetching 的 data/error 物理隔离（不同目录路径）。两组存储使用相同的文件目录 JSON 技术方案。

```text
processing 与 fetching 的 task 完全独立：
  fetching 的任务状态 (`uid:{uid}:task`) 落在 fetching data store 内。
  processing 的任务状态 (`uid:{uid}:task`) 落在 processing data store 内。
  两个 key 同名但不冲突，因 store 物理隔离；指向各自阶段的独立任务。
  processing 通过 fetching.query 只读检查 fetching 状态，
    不读写 fetching 的 task / data / error，
    也不与 fetching task 共享生命周期。
```

```text
目录配置
  BILI_PROCESSING_DATA_DIR    data/bili/processing/data
  BILI_PROCESSING_TEMP_DIR    data/bili/processing/temp
  BILI_PROCESSING_ERROR_DIR   data/bili/processing/error
```

### 5.2 data store key 模式

```text
处理任务
  uid:{uid}:task                              处理任务状态（位于 processing data store；与 fetching 的 task 独立）

处理结果 — transform
  uid:{uid}:proc:video_metadata:{bvid}        单个视频的元数据处理结果
  uid:{uid}:proc:dynamics:{id_str}            单条动态的处理结果（id_str 取自 raw_payload.items[*].id_str）
  uid:{uid}:proc:articles:{article_id}        单篇文章的处理结果（article_id 取自 raw_payload.articles[*].id）

处理结果 — audio
  uid:{uid}:proc:audio:{bvid}                 单个视频的音频转录结果
  uid:{uid}:proc:audio:{bvid}:{page_index}    分 P 视频的按分 P 转录（多 P 场景）

处理进度
  uid:{uid}:progress:transform:{item_type}    transform 流水线进度
  uid:{uid}:progress:audio                    audio 流水线进度
```

### 5.3 value 形状

**处理任务（`uid:{uid}:task`）**：

```json
{
  "uid": 123,
  "status": "RUNNING",
  "pipelines": {
    "transform": {
      "status": "RUNNING",
      "items": {
        "video_metadata": { "total": 77, "completed": 50, "failed": 0, "skipped": 0 },
        "dynamics": { "total": 200, "completed": 200, "failed": 0, "skipped": 0 }
      }
    },
    "audio": {
      "status": "PENDING",
      "items": {
        "transcription": { "total": 77, "completed": 0, "failed": 0, "skipped": 0 }
      }
    }
  },
  "created_at": 1718000000000,
  "updated_at": 1718000001000
}
```

**处理结果 — video_metadata（`uid:{uid}:proc:video_metadata:{bvid}`）**：

```json
{
  "uid": 123,
  "pipeline": "transform",
  "item_type": "video_metadata",
  "item_id": "BV1xxxxxxxxxx",
  "status": "SUCCESS",
  "result": {
    "bvid": "BV1xxxxxxxxxx",
    "aid": 12345,
    "title": "视频标题",
    "desc": "完整视频简介",
    "duration": 600,
    "pages": [
      { "cid": 12345, "part": "P1", "duration": 300 },
      { "cid": 12346, "part": "P2", "duration": 300 }
    ],
    "tags": ["标签1", "标签2"],
    "stat": {
      "view": 10000, "danmaku": 500, "reply": 200,
      "favorite": 300, "coin": 150, "share": 50, "like": 800
    },
    "owner": { "mid": 123, "name": "UP主" },
    "ctime": 1700000000,
    "pubdate": 1700000000,
    "rights": {},
    "subtitle": {},
    "label": {}
  },
  "source_endpoints": ["video_detail"],
  "processed_at": 1718000001000,
  "updated_at": 1718000001000
}
```

**处理结果 — audio（`uid:{uid}:proc:audio:{bvid}`）**：

```json
{
  "uid": 123,
  "pipeline": "audio",
  "item_type": "transcription",
  "item_id": "BV1xxxxxxxxxx",
  "status": "SUCCESS",
  "result": {
    "bvid": "BV1xxxxxxxxxx",
    "pages": [
      {
        "page_index": 0,
        "cid": 12345,
        "duration": 300,
        "text": "完整转录文本...",
        "language": "zh",
        "asr_model": "mimo-v2.5-asr",
        "segments": [
          { "from": 0.0, "to": 5.2, "text": "大家好" },
          { "from": 5.5, "to": 12.0, "text": "今天我们来讲..." }
        ]
      }
    ],
    "total_duration": 300,
    "total_chars": 5000
  },
  "source_endpoints": ["video_detail"],
  "processed_at": 1718000002000,
  "updated_at": 1718000002000
}
```

**处理进度 — transform（`uid:{uid}:progress:transform:video_metadata`）**：

```json
{
  "pipeline": "transform",
  "item_type": "video_metadata",
  "total_items": 77,
  "completed_items": 50,
  "failed_items": 0,
  "skipped_items": 0,
  "remaining_items": 27,
  "done": false,
  "updated_at": 1718000000000
}
```

### 5.4 temp store

```text
temp 目录用于 audio 流水线的中间产物：

temp/{uid}/audio/{bvid}/{page_index}.m4s     下载的音频流（原始格式）
temp/{uid}/audio/{bvid}/{page_index}.mp3     转换后的音频（供 ASR 使用）

生命周期：
  创建   audio worker 下载音频流时写入
  使用   ASR 调用时读取
  删除   该 bvid 的 ASR 转录完成后删除对应 temp 文件
  清理   runner 在 audio 流水线完成后扫描并清除残留 temp 文件
```

temp 存储不参与状态管理（无 task / progress），仅作为文件系统的临时工作区。

## 6. Transform 流水线设计

### 6.1 设计原则

```text
transform 是纯计算模块：
  - 输入：raw_payload dict（从 fetching.query 读取）
  - 输出：result dict（写入 processing data store）
  - 无外部调用（无 HTTP、无文件 I/O、无 ASR）
  - 确定性：相同输入 → 相同输出
  - 可独立测试（不需要 mock HTTP / ASR）
```

### 6.2 video_metadata transform

```text
输入来源
  video_detail/{bvid} 的 raw_payload：{info: {...}, tags: [...]}
  videos 的 raw_payload：{pages: [{list: {vlist: [...]}}]}（可选，补充列表级字段）

处理逻辑
  1. 从 video_detail.info 提取核心字段：
     bvid, aid, title, desc, duration, ctime, pubdate
     pages: [{cid, part, duration, dimension}]
     stat: {view, danmaku, reply, favorite, coin, share, like}
     owner: {mid, name, face}
     rights, subtitle, label
  2. 从 video_detail.tags 提取标签名称列表
  3. 可选：从 videos 列表数据补充列表级字段（如 video_review）

输出
  结构化 dict（见 §5.3 value 形状）

错误处理
  字段缺失    使用默认值（null / 0 / []），记录 warning
  格式异常    写入 error，该工作项标记 FAILED
```

### 6.3 dynamics transform

```text
输入来源
  dynamics 的 raw_payload：{pages: [{items: [...], ...}]}

处理逻辑
  1. 遍历所有 pages 中的 items
  2. 每个 dynamic 提取：
     id_str（动态 ID，作为 item_id；store key 中的 {dynamic_id} 占位符 == id_str）
     type（动态类型）, modules（结构化内容）
     文本内容（从 modules 中提取纯文本）
     图片列表（如有）
     时间戳
  3. 按 id_str 产出独立工作项

输出
  每条动态一个结构化 dict

注意
  dynamics 的 raw_payload 结构复杂（B站动态有多种 module 类型），
  transform 需要处理不同动态类型（转发、图文、视频分享等）。
  MVP 阶段优先处理纯文本和图文类型，转发和视频分享可后续扩展。
```

### 6.4 articles transform

```text
输入来源
  articles 的 raw_payload：{pages: [{articles: [...], ...}]}

处理逻辑
  1. 遍历所有 pages 中的 articles
  2. 每篇 article 提取：
     id, title, summary, image_urls, stats, ctime
  3. 按 article_id 产出独立工作项

输出
  每篇文章一个结构化 dict

注意
  articles endpoint 返回的是文章列表摘要，不含全文。
  如需全文，可能需要额外的 item-level fan-out 抓取（类似 video_detail）。
  MVP 阶段仅处理列表级字段，全文抓取留作后续扩展。
```

### 6.5 transform 扩展机制

```text
新增 transform 类型时，需要：
  1. 在 transform 模块中注册新的 transform handler
  2. 定义输入来源（哪个 endpoint 的 raw_payload）
  3. 定义输出 result 结构
  4. runner 根据注册表自动发现并分发工作项

transform handler 接口：
  class TransformHandler:
      item_type: str                           工作项类型标识
      source_endpoints: list[str]              依赖的 fetching endpoint 列表
      extract_items(raw_payload) -> list[tuple[str, dict]]  从 raw_payload 提取 (item_id, item_data) 列表
      transform(item_id, item_data) -> dict    执行转换，返回 result dict
```

## 7. Audio 流水线设计

### 7.1 设计原则

```text
audio 是有外部调用的流水线：
  - 输入：bvid + cid（从 fetching.query 获取）+ credential
  - 处理：CDN 下载 → 音频预处理 → ASR 转录
  - 输出：转录文本 → processing data store
  - 有 I/O 阻塞（网络下载 + API 调用）
  - 可能失败（网络错误、CDN 过期、ASR 配额耗尽）
```

### 7.2 音频获取

```text
获取流程
  1. 从 fetching.query 获取 video_detail（包含 cid 列表）
  2. 调用 bilibili_api.video.Video(bvid).get_download_url(page_index=0)
  3. 使用 VideoDownloadURLDataDetecter 解析返回结果
  4. 提取 AudioStreamDownloadURL（选择 64K 或 132K 质量，ASR 不需要高保真）
  5. 通过 HTTP GET 下载音频流到 temp 目录

bilibili-api-python 提供的关键 API：
  Video.get_download_url(page_index, cid)  → 获取下载 URL
  VideoDownloadURLDataDetecter(data)       → 解析下载信息
  detecter.detect(audio_max_quality=AudioQuality._64K)  → 提取音频流 URL
  AudioStreamDownloadURL.url               → 音频流 CDN URL
  AudioStreamDownloadURL.audio_quality     → 音频清晰度

音频质量选择
  ASR 场景不需要高保真音频。默认选择 64K（最低质量），减少下载体积和 temp 占用。
  可通过 env 配置覆盖（BILI_PROCESSING_AUDIO_QUALITY）。
```

```text
HTTP 下载
  复用 bilibili-api-python 的 HTTP client（get_client()）
  或使用 aiohttp 直接下载
  需要携带正确的 Referer 和 User-Agent 头（CDN 鉴权）
  下载进度可选记录日志（大文件场景）
```

### 7.3 音频预处理

```text
文件格式
  B站 CDN 返回的音频流为 m4s 格式（DASH 音频段）。
  MiMo ASR 接受 wav 或 mp3 格式。
  需要格式转换：m4s → mp3/wav

转换工具
  首选 ffmpeg（通过 subprocess 调用）
  备选 pydub（Python 库，底层仍依赖 ffmpeg）

音频大小限制
  MiMo API 限制 10MB（base64 编码后约 7.5MB 原始文件）。
  对于长视频（>10min），需要分段处理：
    1. ffmpeg 切分为多个 <10min 的片段
    2. 逐段调用 ASR
    3. 合并转录结果（拼接文本 + 调整时间戳偏移）

分段策略
  目标段长   5-8 分钟（保守值，确保 base64 后 <10MB）
  分段工具   ffmpeg -i input.m4s -f segment -segment_time 480 -c copy output_%03d.mp3
  合并       按段序拼接文本，时间戳加上段起始偏移
```

### 7.4 MiMo ASR 集成

```text
API 信息
  Endpoint    POST {BASE_URL}/chat/completions
  Model       mimo-v2.5-asr
  Auth        Header: api-key: $MIMO_API_KEY
              （或 Authorization: Bearer $MIMO_API_KEY）
  格式兼容    OpenAI Chat Completions 兼容；ASR 走 chat completion 形态
              （`input_audio` 内容部分，base64 data URI）

Token Plan API Key（tp-*）的 BASE_URL（按区域绑定）
  cn  https://token-plan-cn.xiaomimimo.com/v1   ← 默认
  sgp https://token-plan-sgp.xiaomimimo.com/v1
  ams https://token-plan-ams.xiaomimimo.com/v1

按量付费 API Key（sk-*）使用 https://api.xiaomimimo.com/v1
两种 key 不可互换；用错域名返回 401 Invalid API Key（实测确认）。
```

```text
请求格式
  {
    "model": "mimo-v2.5-asr",
    "messages": [
      {"role":"user","content":[
        {"type":"input_audio",
         "input_audio":{"data":"data:audio/mp3;base64,$BASE64"}}
      ]}
    ],
    "asr_options": {"language": "auto"}
  }

响应格式（实测样本，134 秒英文歌曲音频）
  {
    "id": "bdf00cd7ab01422daae9f6593fdd8339",
    "choices": [{"finish_reason":"stop","index":0,
                 "message":{"content":"<完整转录文本>",
                            "role":"assistant",
                            "audio":null,
                            "tool_calls":null,
                            "audio_tokens":[]}}],
    "created": 1781157542,
    "model": "mimo-v2.5-asr",
    "object": "chat.completion",
    "usage": {
      "completion_tokens": 87,
      "prompt_tokens": 858,
      "total_tokens": 945,
      "completion_tokens_details": {"reasoning_tokens": 0},
      "prompt_tokens_details": {"audio_tokens": 837, "cached_tokens": 4},
      "seconds": 134
    }
  }

关键事实（实测确认）
  - choices[0].message.content   完整转录文本（无 segments / 时间戳）
  - usage.seconds                音频时长（向上取整为整数秒，134s 实例返回 134）
  - usage.prompt_tokens_details.audio_tokens  输入音频 token 数（计费维度）
  - language detection 无独立字段；语言由 asr_options.language 控制
  - 不返回 segments / words / 置信度；只能拿到完整 text
  - 流式响应（"stream": true）通过 SSE 输出 chat.completion.chunk，
    最终一条 chunk 携带 usage，最后 `data: [DONE]`
```

```text
计费
  按 audio token + 输出 token；usage.seconds 仅作时长展示
  Token Plan 用户按订阅额度结算；按量付费按 token 计价

语言支持
  普通话、英语、中文方言、中英混说、噪声环境
  language 参数：auto（自动检测）| zh | en
  音乐 / 引擎噪声等无人声场景实测会返回空文本或低 token 输出（属预期）
```

```text
ASR client 设计
  class MimoASRClient:
      __init__(api_key: str, base_url: str = "https://token-plan-cn.xiaomimimo.com/v1")
      async transcribe(audio_data: bytes, mime_type: str = "audio/mp3",
                       language: str = "auto") -> ASRResult

  ASRResult:
      text: str                 完整转录文本（来自 choices[0].message.content）
      language: str             调用时传入的 language（API 不回显检测结果）
      segments: list[dict]      固定为空（API 不返回 segments）
      duration: float | None    来自 usage.seconds
      model: str                "mimo-v2.5-asr"
      raw_response: dict        原始 API 响应（保留 choices/usage 全部字段）

HTTP 实现
  使用 aiohttp.ClientSession 直接调用 MiMo API。
  不依赖 openai SDK（减少依赖，API 足够简单）。
  超时配置：单次请求最长 5 分钟（长音频处理时间较长）。
```

### 7.5 ASR 抽象接口

```text
为后续扩展预留抽象：

class ASRBackend(Protocol):
    async def transcribe(self, audio_data: bytes, mime_type: str,
                         language: str) -> ASRResult: ...
    async def close(self) -> None: ...

实现
  MimoASRBackend       MiMo 云端 API（默认）
  WhisperBackend       本地 whisper / faster-whisper（后续）
  MockASRBackend       测试用 mock 实现

通过 env 配置切换：BILI_PROCESSING_ASR_BACKEND = "mimo" | "whisper" | "mock"
```

### 7.6 audio 流水线完整流程

```text
单个 bvid 的 audio 处理流程：

1. 读取 video_detail/{bvid}
   → 获取 cid 列表（可能多个分 P）

2. 对每个 page（分 P）：
   a. get_download_url(page_index=i)
   b. 解析 → AudioStreamDownloadURL（64K）
   c. 下载音频流 → temp/{uid}/audio/{bvid}/{i}.m4s
   d. 格式转换 m4s → mp3（ffmpeg）
   e. 检查文件大小：
      - ≤10MB → 直接提交 ASR
      - >10MB → 切分为多段 → 逐段提交 ASR → 合并结果
   f. 调用 ASR API → 获取转录文本
   g. 删除 temp 文件

3. 合并所有 page 的转录结果

4. 写入 processing data store
   key: uid:{uid}:proc:audio:{bvid}
   value: 包含所有 page 的转录文本 + 元信息

5. 清理该 bvid 的所有残留 temp 文件
```

### 7.7 分 P 视频处理

```text
B站视频支持多分 P（多集），每个分 P 有独立的 cid。
video_detail.info.pages 字段包含所有分 P 信息。

处理策略
  逐 P 处理：每个分 P 独立下载 + 独立转录
  存储粒度：一个 bvid 一个 key，value 中包含 pages 数组
  进度跟踪：以 page 为子单位，记录 completed_pages / total_pages

分 P 并发
  同一 bvid 的不同分 P 可并行下载和转录
  并发数受 BILI_PROCESSING_AUDIO_CONCURRENCY 控制
```

## 8. 队列解耦设计

### 8.1 架构概览

```text
                    ┌─────────────────────┐
                    │       Runner        │
                    │  (编排 / 调度)       │
                    └─────┬───────┬───────┘
                          │       │
              ┌───────────▼──┐ ┌──▼───────────┐
              │ Transform Q  │ │   Audio Q    │
              │ (asyncio.Queue)│ │(asyncio.Queue)│
              └──────┬───────┘ └──────┬───────┘
                     │                │
             ┌───────▼──────┐ ┌──────▼───────┐
             │ Transform    │ │   Audio      │
             │ Worker Pool  │ │ Worker Pool  │
             │ (N workers)  │ │ (M workers)  │
             └───────┬──────┘ └──────┬───────┘
                     │                │
                     ▼                ▼
              ┌──────────────────────────┐
              │    Processing Data       │
              │    Store (JSON files)    │
              └──────────────────────────┘
```

### 8.2 队列机制

```text
实现
  使用 asyncio.Queue 作为进程内队列。
  不引入外部消息队列（Redis / RabbitMQ），保持单进程部署。

工作项入队
  Runner 从 fetching.query 读取可用 endpoint 结果，
  提取工作项列表，分别入队到 transform_queue 和 audio_queue。
  每个工作项包含：item_type, item_id, source_data（或 source_data 引用）。

Worker 消费
  每个 worker 是一个 asyncio task，循环从队列中取工作项并处理。
  worker 数量通过 env 配置：
    BILI_PROCESSING_TRANSFORM_WORKERS  默认 4
    BILI_PROCESSING_AUDIO_WORKERS      默认 2（audio 受外部 API 限流）

完成信号
  所有工作项入队后，runner 向每个队列放入 sentinel（None）。
  worker 收到 sentinel 后退出循环。
  runner 通过 asyncio.gather 等待所有 worker 完成。
```

### 8.3 并发控制

```text
transform 并发
  纯 CPU 计算，无外部限流。
  worker 数可配置较高（默认 4）。
  实际瓶颈在 JSON 序列化/反序列化，非 API 调用。

audio 并发
  受 CDN 下载速度和 MiMo API 限流双重约束。
  worker 数默认 2，保守值。
  每个 worker 内部串行处理（下载 → 转换 → ASR）。
  MiMo API 的具体限流策略待实测后调整。
  可配置 BILI_PROCESSING_AUDIO_WORKERS 调整。

队列背压
  asyncio.Queue 可设置 maxsize。
  当队列满时，runner 的 put 操作会 await，自然形成背压。
  默认 maxsize 由 BILI_PROCESSING_QUEUE_MAXSIZE 直接给出（默认 16），
  不再用 worker_count * 4 公式动态计算，便于运维显式配置。
```

### 8.4 错误处理

```text
工作项级错误
  单个工作项失败不影响其他工作项。
  失败的工作项写入 error store，记录 item_type + item_id + 错误详情。
  工作项状态标记为 FAILED，不自动重试（MVP 阶段）。

流水线级错误
  MiMo API key 无效 → 所有 audio 工作项失败 → audio 流水线 FAILED_PERMANENT
  CDN 鉴权过期 → 所有下载失败 → audio 流水线 FAILED_RETRYABLE

MVP 重试入口
  MVP 阶段不引入自动重试调度；用户可重新调用 process_uid(uid) 触发重处理。
  Command 不暴露 retry_failed() 接口（见 §11.1），避免与"已存在结果跳过"
  的增量语义产生歧义。

重试策略（MVP 后）
  可配置单工作项最大重试次数
  指数退避
  与 fetching 的重试机制保持一致的设计理念
```

## 9. 错误设计

### 9.1 异常层级

```text
ProcessingError
├── TransformError          transform 阶段错误
│   ├── FieldExtractionError    字段提取失败
│   └── FormatError             格式异常
├── AudioError              audio 阶段错误
│   ├── DownloadError           CDN 下载失败
│   ├── ConvertError            格式转换失败（ffmpeg）
│   ├── ASRConnectionError      ASR API 连接失败
│   ├── ASRAPIError             ASR API 返回错误
│   └── AudioSizeError          音频超出大小限制
├── QueueError              队列操作错误
└── DataError               存储 / 序列化失败
```

### 9.2 错误记录

```text
error store 使用与 fetching 相同的 per-uid JSON 文件方案。
每条错误记录包含：
  id, uid, pipeline(transform/audio), item_type, item_id,
  error_type, message, retryable, detail, timestamp

processing 的 error store 与 fetching 的 error store 物理隔离。
```

## 10. Runner 设计

### 10.1 处理编排

```text
runner 职责
  1. 读取 fetching 任务状态，确认哪些 endpoint 已完成
  2. 根据已完成的 endpoint，生成工作项并入队
  3. 启动 worker pool（transform + audio）
  4. 等待所有 worker 完成
  5. 汇总结果，更新 processing task 状态
  6. 清理 temp 目录

runner 不直接执行 transform 或 audio 逻辑，
只负责调度（入队）和监控（等待完成 + 状态汇总）。
```

```text
fetching 状态消费规则（不阻塞）

processing 不要求 fetching task 整体 SUCCESS。runner 按 endpoint 粒度逐项判断：
  uid-level endpoint
    SUCCESS                              → 该 endpoint 的所有工作项入队
    FAILED_RETRYABLE / FAILED_EXHAUSTED  → 跳过该 endpoint，不阻塞其它工作项
    FAILED_PERMANENT                     → 跳过该 endpoint
    PENDING / RUNNING                    → 跳过该 endpoint（本次不处理；下次 process_uid 时再检查）

  item-level endpoint（如 video_detail）
    PARTIAL_ITEM                         → 仅处理已 SUCCESS 的 item，未成功的 item 跳过
    其它状态规则同 uid-level

processing 不写回 fetching 状态。被跳过的 endpoint 在下次 process_uid(uid)
触发时按当前 fetching 状态重新评估。

设计原则
  - 严格单向：processing 只读 fetching 状态，不阻塞 fetching、不触发 fetching 重抓
  - 不阻塞：fetching 部分成功时 processing 仍能尽量推进，与"半成功 fetching task"独立
```

### 10.2 两阶段编排

```text
processing runner 的编排分为两个阶段：

Phase 0 — 扫描
  读取 fetching task 状态。
  对每个已完成的 endpoint，检查是否已有对应的 processing 结果。
  确定需要处理的工作项列表。

Phase 1 — 分发 + 执行
  将所有 transform 工作项入队 transform_queue。
  将所有 audio 工作项入队 audio_queue。
  启动 transform worker pool 和 audio worker pool。
  两个 pool 并行执行，互不阻塞。
  等待所有 worker 完成。

Phase 2 — 收尾
  汇总所有工作项状态。
  更新 processing task 状态。
  清理 temp 目录。
```

### 10.3 处理模式（mode）

```text
process_uid(uid, mode="incremental") 支持两种 MVP 模式：

  incremental（默认）
    已 SUCCESS 的工作项跳过；
    已 FAILED 的工作项重试一次（覆盖写入）；
    新增的抓取结果（fetching 增量后新出现的 item）入队处理。

  full
    忽略已有 processing 结果，对所有可处理的工作项重新处理并覆盖写入。
    适用于 transform 逻辑变更后的全量回填。

两种模式都遵循 §10.1 "fetching 状态消费规则（不阻塞）"。
mode 参数不传播到 fetching；processing 不会因为自身 mode=full 而触发 fetching refresh / full。
```

```text
重复调用 process_uid(uid, mode="incremental") 时，runner 检查已有处理结果：

  已有 SUCCESS 的工作项    跳过（不重新处理）
  已有 FAILED 的工作项     MVP 阶段重试一次（覆盖写入）
  新增的抓取结果           自动处理（fetching 增量后新出现的 item）

判断"新增"的依据：
  对比 fetching data 中的 item 集合与 processing data 中已有的 item 集合。
  差集即为需要处理的新增工作项。

processing 不感知 fetching 的 refresh 模式（MVP 决策）。
  fetching refresh 模式只刷新 raw_payload（如 stat / play 等时效性字段），
  processing 仅按 bvid / id_str / article_id 集合差集判断；同一个 item 已有
  SUCCESS 结果时，processing 默认不重处理刷新后的 raw_payload。
  待处理结果开始消费这些时效性字段时再引入 "fetched_at vs processed_at" 比对策略。
```

### 10.4 状态枚举

```text
ProcessingTaskStatus（处理任务级）
  PENDING             尚未开始
  RUNNING             正在处理
  SUCCESS             所有工作项成功
  PARTIAL             部分工作项成功，部分失败
  FAILED_RETRYABLE    临时失败，可重试
  FAILED_EXHAUSTED    重试耗尽
  FAILED_PERMANENT    明确不可重试

ProcessingItemStatus（工作项级）
  PENDING             已入队，等待处理
  PROCESSING          正在处理
  SUCCESS             处理成功
  FAILED              处理失败
  SKIPPED             跳过（已存在结果 / 不满足前置条件）
```

## 11. Command / Query 接口设计

### 11.1 Command

```python
class ProcessingCommand:
    async def process_uid(
        uid: int,
        pipelines: list[str] | None = None,  # ["transform", "audio"] 默认全部
        item_types: list[str] | None = None,  # 指定工作项类型
        mode: str = "incremental",            # "incremental" | "full"
    ) -> ProcessingCommandResult:
        """触发处理流水线。"""
```

```text
MVP 不提供独立的 retry_failed() 入口。
用户对 FAILED 工作项的重处理依赖 incremental 模式的语义：
  process_uid(uid, mode="incremental") 时已 FAILED 的工作项会被重新处理。
后续若引入自动重试调度，再单独定义 retry 接口。
```

### 11.2 Query

```python
class ProcessingQuery:
    async def get_task(uid: int) -> ProcessingTaskDTO | None
    async def get_item(uid: int, pipeline: str, item_type: str, item_id: str) -> ProcessingItemDTO | None
    async def list_items(uid: int, pipeline: str, item_type: str) -> list[ProcessingItemDTO]
    async def list_tasks() -> list[dict]
    async def list_errors(uid: int | None = None) -> list[ErrorDTO]

    # 聚合查询
    async def get_video_full(uid: int, bvid: str) -> VideoFullDTO | None:
        """返回单个视频的完整处理结果（元数据 + 转录）。"""
    async def list_all_videos(uid: int) -> list[VideoSummaryDTO]
```

Query 只读访问 processing data/error。对于需要联合 fetching 数据的场景（如展示 raw_payload），Query 可额外读取 `fetching.query` 进行拼接，但不暴露 fetching 内部结构。

### 11.3 DTO

```text
ProcessingTaskDTO
  uid, status, pipelines(dict), created_at, updated_at

ProcessingItemDTO
  uid, pipeline, item_type, item_id, status, result, processed_at, errors

VideoFullDTO
  bvid, metadata(ProcessingItemDTO), transcription(ProcessingItemDTO | None)

VideoSummaryDTO
  bvid, title, status, has_transcription, duration
```

## 12. 配置设计

### 12.1 处理配置（env / .env）

```text
# 目录
BILI_PROCESSING_DATA_DIR      data/bili/processing/data
BILI_PROCESSING_TEMP_DIR      data/bili/processing/temp
BILI_PROCESSING_ERROR_DIR     data/bili/processing/error

# Worker 配置
BILI_PROCESSING_TRANSFORM_WORKERS    4
BILI_PROCESSING_AUDIO_WORKERS        2
BILI_PROCESSING_QUEUE_MAXSIZE        16

# 音频配置
BILI_PROCESSING_AUDIO_QUALITY        64K
BILI_PROCESSING_AUDIO_MAX_SEGMENT_MINUTES  8

# ASR 配置
BILI_PROCESSING_ASR_BACKEND          mimo
BILI_PROCESSING_ASR_API_KEY          ""
BILI_PROCESSING_ASR_BASE_URL         https://api.xiaomimimo.com/v1
BILI_PROCESSING_ASR_MODEL            mimo-v2.5-asr
BILI_PROCESSING_ASR_LANGUAGE         auto
BILI_PROCESSING_ASR_TIMEOUT          300
BILI_PROCESSING_ASR_MAX_FILE_SIZE_MB 10

# FFmpeg
BILI_PROCESSING_FFMPEG_PATH          ffmpeg
```

### 12.2 env 不写入 data

```text
env 模块只读。
配置通过 pydantic-settings 从 .env + 环境变量加载。
首次调用时延迟加载（与 fetching 一致）。
```

## 13. 与 fetching 层的集成

### 13.1 依赖方式

```text
processing 通过 fetching.query.Query 只读访问抓取结果。

具体集成点：
  runner 在 Phase 0 调用 fetching.query.get_task(uid) 获取抓取状态
  runner 对每个 SUCCESS 的 endpoint 调用 fetching.query.get_endpoint(uid, ep)
  transform handler 从 EndpointDTO.raw_payload 中提取数据
  audio worker 从 video_detail EndpointDTO 中提取 cid

装配时注入：
  processing assemble() 调用 fetching assemble() 获取 Query 实例，
  将其注入 processing runner。
```

### 13.2 Credential 传递

```text
audio 流水线需要 Credential 来调用 Video.get_download_url()。
Credential 由 fetching.auth 提供，processing runner 在启动时获取并注入 audio worker。
processing 不自行管理 Credential。
```

### 13.3 装配函数

```python
async def assemble() -> tuple:
    """读取 env, 初始化 stores, 装配 processing 组件。

    返回 (ProcessingCommand, ProcessingQuery, DataStore, ErrorStore)。
    """
    # 1. 初始化 fetching（获取 Query + Credential）
    fetch_cmd, fetch_qry, fetch_data, fetch_error = await fetching.assemble()

    # 2. 初始化 processing stores
    s = get_processing_settings()
    data = ProcessingDataStore(s.bili_processing_data_dir)
    error = ProcessingErrorStore(s.bili_processing_error_dir)
    temp_dir = s.bili_processing_temp_dir

    # 3. 初始化 ASR backend
    asr_backend = create_asr_backend(s)  # mimo / whisper / mock

    # 4. 装配 command + query
    cmd = ProcessingCommand(data, error, temp_dir, fetch_qry, asr_backend)
    qry = ProcessingQuery(data, error, fetch_qry)

    return cmd, qry, data, error
```

## 14. 资源生命周期

```text
data / error store
  - store 提供 async close()
  - 测试 teardown 必须 close store

temp 目录
  - audio 工作项完成后删除对应 temp 文件
  - runner 在 audio 流水线结束后执行全量 temp 清理
  - 异常退出时 temp 残留由下次运行的 cleanup 阶段清理

ASR backend
  - 维护 aiohttp.ClientSession
  - 在 assemble 的 close 阶段关闭 session

ffmpeg subprocess
  - 按需启动，无长驻进程
  - 超时保护：单次转换最长 5 分钟
```

## 15. 边界约束对照

```text
结构约束（docs/structure/bili.md §8）           本设计细化位置

transform 不调用外部 API                        §6 Transform 流水线设计
audio 不直接读取抓取存储内部                      §7.2 音频获取（通过 fetching.query）
env 不写入 data                                 §12.2 env 只读
task 不直接调用 transform / audio                §10 Runner 设计（通过队列）
runner 编排 transform / audio                    §10 Runner 设计
command 不直接调用 transform / audio              §11.1 Command
command 不写 data                               §11.1 Command（通过 runner）
command 不提供 data / error 读取                  §11.1 Command
query 不暴露 data / error 内部存储结构             §11.2 Query
query 不做跨源归一化语义处理                  §11.2 Query
query 不触发写侧流程                              §11.2 Query
query 不读取 temp                               §11.2 Query
error 不编排重试                                  §9 错误设计
调用方不直接写 data / error / temp                 §11 接口设计
temp 处理完成后删除                               §5.4 temp store
```

## 16. 开发顺序

```text
MVP 批次（transform-only，已实现）
 1. 目录与模块骨架    processing/ 下 transform/audio/env/task/runner/data/error
 2. data / error      文件目录 JSON 存储（复用 fetching 的存储方案）
 3. env               pydantic-settings 配置加载
 4. task              处理任务状态模型 + 枚举
 5. transform 骨架    TransformHandler 接口 + video_metadata handler
 6. runner 骨架       Phase 0 扫描 + 队列分发 + worker pool + 状态汇总
 7. command           process_uid(uid, mode) 入口
 8. query             get_task / get_item / list_items
 9. 测试              transform handler 纯函数测试 + runner 集成测试（无 audio）
10. CLI               python -m bili_unit.processing
11. dynamics handler  动态内容 transform（5 种类型；DRAW/AV/ARTICLE/FORWARD/OPUS 实测覆盖）
12. articles handler  文章内容 transform
13. ASR Protocol 占位 ASRBackend Protocol + MockASRBackend
14. ffmpeg 发现        resolve_ffmpeg(setting) — system / imageio-ffmpeg / 显式路径
15. ruff              lint 通过

audio 实现批次（已 unblock；可立即推进）
16. MimoASRBackend    aiohttp + tp-key + token-plan-cn 域名 + chat completion ASR
17. CDN downloader    bilibili-api Video.get_download_url + AudioStreamDownloadURL
                      （注意 bilibili-api 17.x 在 detect_best_streams 上有 NoneType
                      bug；使用 detecter.detect(audio_max_quality=...) 并按
                      type(stream).__name__ == "AudioStreamDownloadURL" 筛选。）
18. ffmpeg 转码        m4s → mp3 (-ar 16000 -ac 1)，长视频用 -segment_time 480 切段
19. audio runner      Phase 2 worker pool；single bvid → 多 page → ASR → 合并
20. audio 测试        mock backend 单元 + 短视频集成（小文件 fixture）
21. 自动重试调度      可配置 max_retries + 指数退避（参考 fetching 设计）
```

## 17. 完成标准

```text
MVP 完成标准（transform-only）
  - transform 流水线可用（video_metadata + dynamics + articles）
  - 队列解耦的 worker pool 可用
  - incremental / full 两种处理模式可用
  - incremental 增量处理可用（已 SUCCESS 跳过、已 FAILED 重试一次、新增自动入队）
  - ASRBackend Protocol + MockASRBackend 已落地（audio 实现批次的接口稳定）
  - uv run pytest 全部通过
  - uv run ruff check 全部通过
  - CLI 入口可用
  - 默认测试无真实网络依赖（无 ffmpeg / MiMo 调用）

MVP 后批次完成标准
  - audio 流水线可用（CDN 下载 + ffmpeg + MiMo ASR）
  - MiMo ASR 实测通过
  - 音频长视频分段处理可用
  - 自动重试调度可用
```

## 18. 待定

```text
MiMo ASR 限流策略       并发限制、QPS 限制需要更长时段实测确认
MiMo ASR 长音频处理      10MB 限制下的最优分段策略（单段 5-8 分钟为保守初值）
video_metadata 字段范围   最终输出字段列表需要根据 index 需求确定
dynamics 类型覆盖        转发 / COMMON_SQUARE 等长尾类型的字段裁剪
articles 全文            是否需要 item-level fan-out 抓取全文
```

## 19. 已决

| 议题 | 决定 | 理由 |
|------|------|------|
| ASR 引擎 | MiMo-V2.5-ASR 云端 API | 支持中文方言 + 中英混说，与 Dialectica 项目定位匹配；云端部署无需 GPU |
| 处理结果存储 | 独立文件目录 JSON 存储 | 与 fetching 保持一致的技术方案；物理隔离避免耦合 |
| 并发模型 | 队列解耦 worker pool | transform 和 audio 特性不同（CPU vs I/O），独立 worker pool 可分别调优 |
| transform 与 audio 关系 | 完全独立，互不依赖 | 结构约束：两条流水线共享工作项源但处理逻辑解耦 |
| 音频质量 | 64K（最低） | ASR 不需要高保真；减少下载体积和 temp 占用 |
| 音频格式转换 | ffmpeg | 行业标准工具；m4s → mp3 转换可靠 |
| ASR 抽象 | Protocol 接口 + 多后端 | 预留本地 Whisper 扩展能力；测试可用 mock |
| processing 与 fetching task 的关系 | 完全独立的两个 task | fetching task 在 fetching data store，processing task 在 processing data store；processing 通过 fetching.query 只读检查 fetching 状态，不写回；避免"半成功"模糊状态、保持单向依赖、便于独立重跑 |
| MVP 重试入口 | Command 不暴露 retry_failed()；FAILED item 在 incremental 模式下重试一次 | 避免与"已存在结果跳过"语义打架；用户用同一入口（process_uid mode=incremental）即可触发重处理；自动重试调度留到 MVP 后批次 |
| dynamics item_id 字段 | 统一使用 raw_payload.items[*].id_str；store key 占位符 {dynamic_id} 含义 == id_str | id_str 是 B 站动态的稳定字符串 ID，避免精度丢失，与 fetching 端 item_id_path 一致 |
| BILI_PROCESSING_QUEUE_MAXSIZE | 默认 16，由 env 显式给定，不再用 worker_count*4 公式动态计算 | 单一来源、便于运维；当 transform 与 audio worker 数量差异较大时也无须二次推导 |
| audio 实现节奏 | MVP 批次仅落 ASRBackend Protocol + MockASRBackend；CDN 下载 + ffmpeg + MimoASRBackend 推迟到 MVP 后批次 | §4.2 已声明 audio 实现推迟；§16 dev order 与 §17 完成标准对齐到该决定，避免 dev order 与 §4.2 冲突 |
| fetching 状态消费策略 | endpoint 粒度逐项判断；PARTIAL_ITEM 时仅处理已成功的 item；不阻塞、不写回 | 严格单向；processing 不应被 fetching 单端点失败拖死，且 fetching 后续重抓后下次 process_uid 可补处理 |
| processing mode | MVP 支持 incremental / full 两档；不支持 refresh | full 用于 transform 逻辑变更后的全量回填；refresh 语义当前没有消费方（stat 字段未使用），引入只会提升复杂度 |
| processing 是否感知 fetching refresh | MVP 不感知 | fetching refresh 仅刷新 raw_payload 中时效性字段；当前处理结果不消费这些字段；待消费方出现再引入 fetched_at vs processed_at 比对 |
| ffmpeg 依赖策略 | env 默认 `auto`：先试系统 ffmpeg，再回退到 imageio-ffmpeg 捆绑二进制（`pip install dialectica[audio]`） | 系统 ffmpeg 灵活；imageio-ffmpeg 提供 zero-config 部署兜底（~60MiB BSD-licensed wheel）；env 可显式覆盖为 system / imageio / 任意路径 |
| MiMo ASR base URL | tp-* key 默认 `https://token-plan-cn.xiaomimimo.com/v1`；sk-* key 用 `https://api.xiaomimimo.com/v1` | 实测确认 tp-key 必须用 token-plan-* 域名（cn/sgp/ams 三选一），用 sk-* 域名返回 401 |
| MiMo ASR 响应字段 | 仅 `choices[0].message.content`（完整文本）+ `usage.seconds`（音频秒数）+ `usage.prompt_tokens_details.audio_tokens`；不返回 segments / 时间戳 / 检测语言 | 实测样本（134 秒英文歌曲）确认；ASRResult.segments 永远为空，ASRResult.language 由调用时传入参数回填 |
| 长音频分段策略 | token 预算优先 + size 兜底。runner 把 page metadata 的整数秒 duration 传给 `convert_single`；后者按 `ceil(duration * tokens_per_second)` 估算，超过 `asr_max_input_tokens` 则按 `max_input_tokens // tokens_per_second` 切段（不低于 60 秒）。size 阈值仅在 caller 未提供 duration 时作 fallback。同时把 `max_tokens` 写入 MiMo payload（默认 1024）压缩 completion 预算，给输入腾位置 | size-only 实测对长视频 100% 失败：16 kHz mono q:a 9 编码下 17 分钟视频仅 ~3 MB（永远命中不到 size 阈值），但折算 ~6500 input tokens + 默认 2048 completion = 8550 → MiMo 8192 上下文 400 BadRequest。token 预算路径才是根治。新增三个 env：`asr_max_input_tokens=5400` / `asr_tokens_per_second=6.5` / `asr_max_completion_tokens=1024`（经验值来自 fixture 134 s → 837 audio_tokens 与失败案例 1033 s → 6502 audio_tokens 反推） |
| 多段 ASR 后 page.duration 字段 | 优先用 page metadata 已知的整数秒 duration；其次累加每段 ASR 返回的 `usage.seconds`；最后兜底 CDN audio 元数据（注：单位不是秒） | 早期实现把 ASR 单段 duration **覆盖**写入 page_duration，导致多段视频只保留最后一段时长（1033 s 视频被切两段后落盘 `duration=204`）。文本拼接是对的但 duration 字段失真，影响下游统计（total_duration、video_full 视图）。已加 2 个回归测试 |
