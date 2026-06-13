# bili unit

> 性质：结构设计。本文描述 `bili` unit。
> 术语：**`A 服务 B` ≡ A 是基础，B 调用 A**（服务方向 A→B，调用方向 B→A）。

## 0. Dialectica 体系定位

bili_unit 是 [Dialectica](https://github.com/ChosenEcho/Dialectica) 项目
`source_data` 层下的一个 unit。Dialectica 的体系级结构约束（`main` /
`source-data` / `unit` / `index`）保留在 Dialectica 主仓库，**本文件不重复**：

- [main.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/main.md) — 四层总结构 + 服务方向不变量
- [source-data.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/source-data.md) — `unit (1..N) → index.ingestion`
- [unit.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/unit.md) — unit 抽象（`抓取 → 处理`）
- [index.md](https://github.com/ChosenEcho/Dialectica/blob/main/docs/structure/index.md) — `ingestion → indexing → storage`

本仓库主管 bili 这个 unit 的内部结构（本文件 §1+）、设计（[docs/design/](../design/)）
与现状（[docs/feature/](../feature/)）。下文出现的 `docs/structure/unit.md`、
`docs/structure/source-data.md` 等引用，均指 Dialectica 主仓库的对应文件。

## 1. 位置

```text
source_data → unit → bili
```

```text
上游文档  unit
下游文档  无
结构位置  Bilibili 外部源 → bili → index.ingestion
```

## 2. 定位

```text
对象   Bilibili 数据源
单位   目标用户 uid
输入   目标用户 uid + 认证信息 + 任务
输出   处理结果与状态
服务   index.ingestion
```

## 3. 管线

```text
抓取 → 解析 → 处理
```

```text
抓取   认证 → 调用 Bilibili API → 原始数据入库
解析   读取原始数据 → 对象化为 typed dataclass → 可选图片下载 → typed object 入库
处理   读取 typed object → ASR 转录 → 处理结果入库
```

## 4. 模块

### 抓取

```text
auth         获取 / 校验 / 提供可用认证；认证异常写入 error
env          保存认证配置
client       抓取脚本；Credential 由 auth 提供；只依据 bili-api-info 调用 bilibili-api-python
rate_limit   控制请求频率与并发；限流状态写入 data，限流异常写入 error
task         定义任务状态形状与枚举；任务状态持久化在 data，由 runner 读写
runner       根据任务状态与错误状态编排抓取执行 / 重试
```

### 解析

```text
models       5 个 legacy typed dataclass（UpProfile / VideoDetail / Article / OpusPost / DynamicPost）+ 1 个 ContentPost（Article / Opus / Dynamic 的统一内容视图）；from_raw() / to_dict() / from_dict() + 图片协议。processing 层通过 ContentPost 统一消费 article / opus / dynamic 类内容，legacy 三类 dataclass 保留作为 ContentPost candidate 来源。
_images      ImageDownloader；aiohttp 并发下载 + skip-existing + 失败隔离
env          保存解析配置；存储目录、图片并发数、超时
keys         解析层 key 生成（uid:{uid}:task / uid:{uid}:parse:{model}:{item_id}）
data         ParsingDataStore；JsonKVStore wrapper + 原子更新 helper
query        ParsingQuery；读取 typed objects 的只读视图
command      ParsingCommand；parse_uid() 编排 6 个 model（5 legacy + content_post）+ 可选图片下载
```

### 处理

```text
audio        音频下载 + ASR 转录逻辑；调用外部 CDN 与 ASR 引擎（处理阶段唯一外部调用模块，依据 unit §3 显式登记）
env          保存处理配置；ASR 引擎配置、下载配置
task         定义任务状态形状与枚举；任务状态持久化在 data，由 runner 读写
runner       根据任务状态与错误状态编排处理执行 / 重试；驱动 audio pipeline
```

```text
跨源归一化 / 清洗不在 bili unit 内部完成。
  bili.processing 仅产出"bili 形态"的结构化数据；
  归一化为 index 文档形态由 index.ingestion 承担。
```

### 存储

```text
raw          原数据存储位置；保存从抓取结果读取的原数据
temp         临时数据存储位置；处理完成后删除
data         数据状态存储位置；保存抓取结果、抓取状态、处理结果、处理状态、任务状态与进度
error        错误状态位置；保存认证异常、请求失败、ASR 失败、格式异常；不编排重试
```

```text
后端选型属于实现层；当前 fetching 已采用文件目录 JSON，
后续 processing 沿用同一方案。各存储的具体后端见 docs/design/*.md。
```

### 入口

```text
command      写侧入口；驱动抓取与处理管线
query        只读视图入口；读取 data / error
```

## 5. 管线对象

```text
来源   docs/bili-api-info/modules/user.md + docs/bili-api-info/modules/video.md
范围   bili-api-info 中和目标用户 uid 有关的读取接口（uid-level），以及从 uid 抓取结果派生的 item-level 读取接口
单位   目标用户 uid
分类   如下
```

```text
User(uid)
用户基础信息           → parsing
用户发布内容（视频）   → parsing + audio
用户发布内容（文章）   → parsing
用户空间内容           → parsing
用户关系内容           → parsing
用户列表内容           → parsing
用户状态 / 统计内容    → parsing
```

## 6. 数据流

```text
command → runner → auth → env
                    ↘ client → data
                    ↘ rate_limit → data / error
                    ↘ error
         runner → fetching.query → raw
                    ↘ audio ← raw → env
                    ↘ temp
                    ↘ error
         parsing.command → fetching.query → raw
                    ↘ parser.from_raw() → typed object
                    ↘ ImageDownloader（可选）→ images/
                    ↘ parsing.data → typed object 入库
         runner → data（write results / task / status / progress）
         runner → parsing.query（read VideoDetail for audio cid lookup）
         runner → temp（处理完成后删除）
query → data（read）
query → error（read）
query → parsing.data（read typed objects for video-full / list-all-videos metadata）
```

```text
bili 主动调用 Bilibili 外部源
bili 通过 fetching.query 读取抓取结果，不直接访问抓取存储内部
audio 主动调用外部源（CDN 下载、ASR 转录）；其余模块不调用外部 API
index.ingestion 通过 query 只读访问 data / error
```

## 7. 状态归属

```text
目标用户 uid
认证状态
认证配置
处理配置
任务状态
抓取状态
抓取进度
解析状态
解析图片下载状态
处理状态
处理进度
请求状态
限流状态
重试状态
失败状态
抓取结果
解析结果（typed objects）
处理结果
raw 存储
temp 存储
parsing 存储（typed objects + images）
data 存储
错误状态
抓取时间
解析时间
处理时间
```

## 8. 边界

```text
不处理抓取结果语义（在处理阶段处理）
不固定用户相关接口清单
不使用 bili-api-info 之外的抓取能力
不跨 unit 聚合
不做跨源归一化 / 清洗（归 index.ingestion）
不推送
不服务 index.ingestion 之外的调用方
不直接服务 index / reasoning / interaction
audio 不直接读取抓取存储内部
env 不写入 data
task 不直接调用 client / audio
runner 编排 client / audio
runner 根据任务状态与错误状态编排重试
command 不直接调用 client / audio
command 不写 raw / temp / data
command 不提供 data / error 读取
query 不暴露 data / error 内部存储结构
query 不做跨源归一化语义处理
query 不触发写侧流程
query 不读取 raw / temp
error 不编排重试
调用方不直接写 raw / temp / data / error
调用方不直接依赖 raw / temp / data / error 内部存储结构
temp 处理完成后删除
```

## 9. 外部依赖

```text
bilibili-api-python [GitHub](https://github.com/nemo2011/bilibili-api)
bili-api-info 作为抓取脚本唯一依据
Credential 认证
Bilibili CDN        视频音频流下载
ASR 引擎             语音转文字
异步调用
关键字参数调用
请求后端优先级  curl_cffi → aiohttp
412 风险         请求过快触发；由限流控制处理
```

## 10. 目录骨架

```text
bili_unit/                # Python 包根（pyproject 里 packages = ["bili_unit"]）
├── __init__.py           # DTO、异常、assemble() 装配
├── __main__.py           # 统一 CLI 入口；python -m bili_unit <subcommand>
├── _retry.py             # 共享 RetryDriver（fetching / processing 通用）
├── _storage/             # 共享存储抽象（JsonKVStore + JsonErrorStore）
├── command/              # 写侧入口；驱动抓取、解析与处理管线
├── query/                # 只读视图入口；读取 data / error
├── fetching/             # 抓取阶段（auth / client / rate_limit / runner/ / task / data / error / env）
├── parsing/              # 解析阶段（models / _images / command / query / data / env / keys）
├── processing/           # 处理阶段（audio / runner/ / task / data / error / env）
└── tests/                # pytest 测试目录
```

运行时数据（raw / temp / data / error）默认落在工作目录下的 `data/bili/...`，
由 `BILI_*_DIR` env 覆盖；不在 Python 包目录内。

```text
代码现状（结构 vs 实现）
  上表为 bili_unit 仓库当前的实际布局，包级形态稳定。
  fetching 与 processing 作为阶段子包存在，彼此通过 fetching.query 单向衔接，
  bili_unit/command/、bili_unit/query/ 作为跨阶段统一入口。
  raw / temp / data / error 的物理目录由 env 控制，结构上不属于代码包。
```

