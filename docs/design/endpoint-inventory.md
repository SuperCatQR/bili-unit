# 接口清单 — B站用户数据抓取完整端点设计

> ⚠️ **状态：废弃（DEPRECATED）**
>
> 本文为早期端点选型记录，**不再维护**。已实现的端点清单与状态以 [docs/feature/fetching.md](../feature/fetching.md) §端点注册表
> 为唯一真相源。本文与现状不一致时按 feature 为准。新增端点请直接更新 feature 文档。

> 性质：设计决策。本文从 `docs/bili-api-info` 中提取所有 `User(uid)` / `Video(bvid)` 只读接口，
> 划定 fetching 层的完整抓取范围，并标注实现优先级。
> 关联文档：`docs/design/fetching.md` §10（endpoint registry 设计）、`docs/feature/fetching.md` §端点注册表（已实现状态）

## 1. 设计原则

```text
来源        docs/bili-api-info/modules/user.md（User 类 40 个方法）
            docs/bili-api-info/modules/video.md（Video 类 ~40 个方法）
            docs/bili-api-info/modules/ 下关联模块（dynamic, channel_series, article, comment）
范围        User(uid) 只读接口 + Video(bvid) 只读接口（item-level fan-out）
排除        所有写接口；模块级函数（get_self_*、name2uid 等）；内部令牌/同步方法；
            以及下方 §3 明确排除的端点
粒度        每个端点对应一个独立 API 调用或一组紧密关联的 API 调用
```

## 2. 完整清单

### 2.1 用户身份（Identity）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
user_info           get_user_info()           none       昵称、性别、生日、签名、头像、空间横幅         T0 ✅
overview_stat       get_overview_stat()       none       订阅/投稿简易统计                             T1
user_medal          get_user_medal()          none       粉丝勋章列表（隐私受限）                       T2
space_notice        get_space_notice()        none       空间公告文本（临时性）                         T2
```

### 2.2 社交关系（Social）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
relation_info       get_relation_info()       none       关注数、粉丝数、悄悄关注数、黑名单数           T0 ✅
all_followings      get_all_followings()      none(全量) 完整关注列表（需 Credential，隐私受限）         T2
user_fav_tag        get_user_fav_tag()        page       用户关注的标签列表（隐私受限）                  T2
```

### 2.3 内容 — 视频（Video）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
videos              get_videos()              page       投稿视频列表（摘要：bvid/title/play/...）      T0 ✅
top_videos          get_top_videos()          none       置顶/代表作                                   T2
masterpiece         get_masterpiece()         none       代表作列表（与 top_videos 重叠）               T2
```

### 2.4 内容 — 视频详情（item-level）

```text
端点                源端点     API 方法                              产出                               优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
video_detail        videos     Video.get_info() + get_tags()         完整信息 + 标签                     T0 ✅
```

### 2.5 内容 — 动态（Dynamic）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
dynamics            get_dynamics_new()        cursor     动态时间线（文本、转发、视频投稿、图文）        T0 ✅
```

注：当前代码注册名为 `dynamics`，实现使用 `get_dynamics_new()`（推荐新接口）。

### 2.6 内容 — 图文/专栏（Article / Opus）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
articles            get_articles()            page       投稿专栏列表                                   T1
article_list        get_article_list()        none       专栏文集                                       T2
opus                get_opus()                cursor     图文帖子（专栏 + 动态图文混合）                 T1
album               get_album()               page       相簿（绘画/摄影/日常）                          T2
```

### 2.7 内容 — 音频（Audio）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
audios              get_audios()              page       音频投稿列表                                    T0 ✅
```

### 2.8 内容 — 频道/合集（Channel）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
channel_list        get_channel_list()        page       合集(season) + 列表(series) 元数据（原始 dict） T0 ✅
channels            get_channels()            none       合集列表（返回 List[ChannelSeries] 对象）       T2
```

注：`channel_list` 返回原始 dict 适合 fetching 层的 raw_payload 存储；
`channels` 返回结构化 `ChannelSeries` 对象，序列化后字段更清晰。两者数据源相同，
按需选用或互为校验。

item-level 扩展（以 channel_list 或 channels 为上游）：

```text
端点                      源端点         API 方法                                      产出                     优先级
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
channel_videos_season     channel_list   get_channel_videos_season(sid, sort, pn, ps)  season 合集内视频列表     T2
channel_videos_series     channel_list   get_channel_videos_series(sid, pn, ps)        series 列表内视频列表     T2
```

注：season（合集）和 series（视频列表）的 API 签名不同——season 支持 sort 参数，
series 不支持。拆分为两个端点比合并为一个更忠实于 API 实际结构。

### 2.9 内容 — 订阅（Subscription）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
subscribed_bangumi  get_subscribed_bangumi()  page       追番/追剧列表                                   T1
cheese              get_cheese()              none       课程列表                                        T2
```

### 2.10 统计（Stats）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
up_stat             get_up_stat()             none       总播放、总阅读、总点赞                           T0 ✅
```

### 2.11 付费/充电（Monetization）

```text
端点                API 方法                  分页       产出                                          优先级
─────────────────────────────────────────────────────────────────────────────────────────────────────────
elec_monthly        get_elec_user_monthly()   none       充电公示信息                                    T2
upower_qa           get_upower_qa_list()      cursor     充电问答列表                                    T2
```

## 3. 排除项

以下端点不纳入 fetching 层：

```text
接口                          排除理由
────────────────────────────────────────────────────────────────────
get_access_id()               内部令牌（w_webid），非用户数据
get_uid()                     同步本地方法，无网络请求
get_reservation()             临时性预约数据，无长期 KG 价值
get_uplikeimg()               视频三联特效配置，非元数据
get_relation()                描述"我"与目标的关系，非目标用户自身属性
get_self_same_followers()     需要自身 Credential，描述共同关注
get_dynamics()(legacy)        已被 get_dynamics_new() 取代
live_info                     主播身份标志，非核心 KG 维度
followers                     粉丝列表，5 页限制且社交图构建属 processing 层职责
media_list                    与 videos 高度重叠，cursor(oid) 分页策略需额外开发
channels（get_channels）    与 channel_list 同 API 数据源，返回 ChannelSeries 对象属 processing 层视图，fetching 层已有 channel_list 覆盖
弹幕/评论/视频流/下载         体量独立、非用户属性维度，属于独立数据域
收藏夹（favorite_lists/content）  体量独立、内容多为他人视频，属于独立数据域
所有写接口                    fetching 层只读
模块级函数                    get_self_* / name2uid 等非 User(uid) 读取接口
```

## 4. 优先级分层

### T0 — 已实现（8 个）

```text
uid-level (7)     user_info, relation_info, up_stat, videos, dynamics, audios, channel_list
item-level (1)    video_detail
```

当前 105 个测试全部通过，覆盖 3 种分页策略（none / page / cursor）和 item-level fan-out。

### T1 — 下一批（4 个）

新增端点实现成本低：均为 uid-level，不需要新分页策略（none / page / cursor 已有），只需追加 EndpointSpec。

```text
端点                分页       新增复杂度    KG 价值
──────────────────────────────────────────────────────────────
articles            page       低            文字内容节点，补全"创作者"画像
opus                cursor     低            跨类型内容（专栏 + 动态图文）
overview_stat       none       极低          与 up_stat 互补的统计维度
subscribed_bangumi  page       低            兴趣画像（追番偏好）
```

### T2 — 未来（13 个）

```text
端点                      额外基础设施需求
──────────────────────────────────────────────────────────────
user_medal                隐私受限，部分用户不可用
space_notice              临时性文本，KG 价值有限
all_followings            需要自身 Credential 注入机制
user_fav_tag              隐私受限，部分用户不可用
top_videos                与 videos 重叠度高
masterpiece               与 top_videos 重叠
channel_videos_season     item-level fan-out，需新 source_endpoint + sort 参数
channel_videos_series     item-level fan-out，需新 source_endpoint
article_list              依赖 articles 先实现
album                     低优先级视觉内容
cheese                    小众内容
elec_monthly              商业化指标
upower_qa                 小众功能，cursor(anchor) 分页需新增
```

## 5. 分页策略覆盖

```text
策略            T0 已覆盖                                         T1 新增                    T2 新增
────────────────────────────────────────────────────────────────────────────────────────────────────────────
none            user_info, relation_info, up_stat                 overview_stat              user_medal, space_notice,
                                                                                           cheese, elec_monthly,
                                                                                           all_followings
page(pn/ps)     videos, audios, channel_list                      articles,                  album, user_fav_tag,
                                                                 subscribed_bangumi          channel_videos_season,
                                                                                             channel_videos_series
cursor(offset)  dynamics                                          opus                       —
cursor(anchor)  —                                                 —                          upower_qa
item-level      video_detail                                      —                          channel_videos_season,
                                                                                             channel_videos_series
```

## 6. 与 fetching.md 待定项的关系

`docs/design/fetching.md` §19 待定中：

- **接口清单** → 本文给出完整清单和优先级划分
- **endpoint registry** → 本文 §2 列出全部候选，T1 的 4 个可直接追加到 `_build_endpoints()`

后续 T1 实现时，更新 `docs/feature/fetching.md` 端点注册表并标注实测状态。
