## fetching 数据结构

> 本文描述 fetching 层抓取到的数据及其存储结构。
> 下游（processing 层）通过 `fetching.query` 只读消费这些数据。

---

### 1. 存储信封

每条抓取结果存储在文件目录 JSON KV 中，外层信封统一。

**uid-level 端点**（key: `uid:{uid}:fetch:{endpoint}`，路径: `{uid}/fetch/{endpoint}.json`）：

```
{
  "uid": int,
  "endpoint": str,
  "status": str,              // "SUCCESS" 或失败状态
  "raw_payload": dict | list | str | null,  // 见下方各端点定义
  "fetched_at": int,          // epoch 毫秒
  "updated_at": int           // epoch 毫秒（store 层注入）
}
```

**item-level 端点单条**（key: `uid:{uid}:fetch:{endpoint}:{item_id}`，路径: `{uid}/fetch/{endpoint}/{item_id}.json`）：

```
{
  "uid": int,
  "endpoint": str,
  "item_id": str,
  "status": str,
  "raw_payload": dict | null,
  "fetched_at": int | null,
  "updated_at": int
}
```

**item-level 端点聚合**（key: `uid:{uid}:fetch:{endpoint}`，无 item_id 后缀）：

```
{
  "uid": int,
  "endpoint": str,
  "status": str,
  "raw_payload": null,
  "item_counts": {
    "total": int,
    "completed": int,
    "failed": int
  },
  "fetched_at": int,
  "updated_at": int
}
```

---

### 2. raw_payload 两种存储形态

- **非分页端点**（pagination=none）：`raw_payload` 直接存放 bilibili-api-python 返回的原始响应。fetching 不做字段筛选，原样存储。
- **分页端点**（pagination=page/cursor/anchor）：`raw_payload` 包装为 `{"pages": [page1, page2, ...]}`，数组中每个元素是一次 API 响应的原始 dict。

---

### 3. uid-level 端点数据（22 个）

以下每个端点的 `raw_payload` 字段均来源于 bilibili-api-python 的 API 返回值。fetching 对非分页端点原样存储全部字段，对分页端点提取 item ID 和分页总数用于增量检测。

#### 3.1 user_info（非分页）

API: `User.get_user_info()` → `https://api.bilibili.com/x/space/wbi/acc/info`

raw_payload 是 dict，B 站用户空间基本信息：

```
{
  "mid": int,                  // 用户 uid
  "name": str,                 // 昵称
  "sex": str,                  // "男" | "女" | "保密"
  "face": str,                 // 头像 URL
  "sign": str,                 // 签名 / 简介
  "birthday": str,             // "MM-DD" 格式
  "level": int,                // 等级 0-6
  "jointime": int,             // 注册时间 epoch 秒；0 = B 站未公开
  "vip": {
    "type": int,               // 0=无, 1=月度, 2=年度
    "status": int,             // 会员状态
    "label": dict | str        // dict: {"text": str, ...} 或老版纯 str
  },
  "official": dict,            // 认证信息
  "rank": int,
  "DisplayRank": int,
  "regtime": int,
  "spacesta": int,
  "silence": int,
  "end_time": int,
  "nameplate": dict,           // 铭牌
  "pendant": dict,             // 头像框
  "official_verify": dict,     // 认证验证
  "is_senior_member": int,
  "live_room": dict,           // 直播间信息
  "school": dict | null,
  "profession": dict,
  "is_risk": bool,
  "elec": dict,                // 充电信息
  "sys_notice": dict | null    // 系统通知
}
```

processing 消费字段：`mid`, `name`, `sex`, `face`, `sign`, `birthday`, `level`, `vip`, `jointime`

#### 3.2 relation_info（非分页）

API: `User.get_relation_info()` → `https://api.bilibili.com/x/relation/stat`

raw_payload 是 dict：

```
{
  "mid": int,                  // 用户 uid
  "following": int,            // 关注数
  "follower": int,             // 粉丝数
  "whisper": int,              // 悄悄关注数
  "black": int                 // 黑名单数
}
```

processing 消费字段：`following`, `follower`, `whisper`, `black`

#### 3.3 up_stat（非分页）

API: `User.get_up_stat()` → `https://api.bilibili.com/x/space/upstat`

raw_payload 是 dict：

```
{
  "archive": {
    "view": int                // 视频总播放量
  },
  "article": {
    "view": int                // 专栏总阅读量
  },
  "likes": int                 // 获赞数
}
```

processing 消费字段：`archive.view`（兼容 `archive` 为 int 的情况）, `article.view`（同上）, `likes`

#### 3.4 overview_stat（非分页）

API: `User.get_overview_stat()` → `https://api.bilibili.com/x/space/navnum`

raw_payload 是 dict：

```
{
  "video": int,                // 视频数（也可能为 {"video_count": int}）
  "article": int,              // 专栏数（也可能为 {"article_count": int}）
  "opus": int,                 // 图文数（也可能为 {"opus_count": int}）
  "album": int,                // 相册数
  "fav": int,                  // 收藏夹数
  "bangumi": int,              // 追番数
  "season": int,               // 合集数
  "series": int,               // 列表数
  "follow_material": int,      // 追更数
  "subscription": int          // 订阅数
}
```

processing 消费字段：`video` / `video_count`, `article` / `article_count`, `opus` / `opus_count`（代码做版本兼容）

#### 3.5 videos（分页 page, pn/ps=30）

API: `User.get_videos(pn, ps, tid, keyword, order)` → `https://api.bilibili.com/x/space/wbi/arc/search`

raw_payload 结构：

```
{
  "pages": [
    {
      "list": {
        "tlist": dict,           // 分区视频数统计
        "vlist": [
          {
            "aid": int,
            "bvid": str,
            "title": str,
            "description": str,  // 简介，截断至约 250 字符
            "pic": str,          // 封面 URL
            "author": str,       // UP 主昵称
            "mid": int,          // UP 主 uid
            "created": int,      // 创建时间 epoch 秒
            "length": str,       // 时长 "MM:SS"
            "play": int,         // 播放数
            "video_review": int, // 弹幕数
            "comment": int,      // 评论数
            "favorites": int,    // 收藏数
            "typeid": int,       // 分区 ID
            "is_union_video": int, // 是否合作视频
            "is_pay": int,       // 是否付费
            "is_steins_gate": int, // 是否互动视频
            "is_live_playback": int, // 是否直播回放
            "is_lesson_video": int,  // 是否课堂视频
            "is_lesson_finish": int,
            "is_charging_arc": int,
            "is_free": int,
            "is_cooperation": int,
            "is_pugv": int,
            "is_season": int,
            "is_ugc_pay": int,
            "is_ugc_pay_preview": int,
            "is_avoided": int,
            "attribute": int,
            "hide_click": bool,
            "is_charging_arc": int,
            "meta": dict | null, // 合集归属信息
            "is_avoided": int,
            "avoid_reason": str,
            "avoid_show": int
          }
        ]
      },
      "page": {
        "pn": int,               // 当前页码
        "ps": int,               // 每页条数
        "count": int             // 视频总数
      },
      "episodic_button": dict | null,
      "is_risk": bool,
      "gaia_res_type": int,
      "gaia_data": dict | null
    }
  ]
}
```

fetching 提取：`list.vlist[*].bvid`（增量 item ID）, `page.count`（分页总数）

#### 3.6 articles（分页 page, pn/ps=30）

API: `User.get_articles(pn, ps, order)` → `https://api.bilibili.com/x/space/article`

raw_payload 结构：

```
{
  "pages": [
    {
      "articles": [
        {
          "id": int,                // cvid
          "category": dict,         // 分类信息
          "categories": list,       // 分类列表
          "title": str,
          "summary": str,
          "banner_url": str,        // banner 图 URL
          "template_id": int,
          "state": int,
          "author": dict,           // 作者信息 {mid, name, face}
          "reprint": int,           // 转载状态
          "image_urls": list[str],  // 封面图列表
          "origin_image_urls": list[str], // 原始图片列表
          "publish_time": int,      // 发布时间 epoch 秒
          "ctime": int,             // 创建时间 epoch 秒
          "stats": {
            "view": int,
            "favorite": int,
            "like": int,
            "reply": int,
            "share": int,
            "coin": int
          },
          "like_state": int,
          "version_id": int,
          "is_like": bool,
          "media": dict | null,
          "apply_time": str,
          "check_time": str,
          "original": int,
          "act_id": int,
          "dispute": dict | null,
          "authenMark": dict | null,
          "cover": dict | null,
          "type": int,
          "dyn_id": str | null,
          "dyn_type": int,
          "is_top": int,
          "activity": dict | null
        }
      ],
      "pn": int,
      "ps": int,
      "count": int                  // 文章总数
    }
  ]
}
```

fetching 提取：`articles[*].id`（增量 item ID）, `count`（分页总数）

#### 3.7 opus（分页 cursor, offset）

API: `User.get_opus(type, offset)` → `https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space`

raw_payload 结构：

```
{
  "pages": [
    {
      "items": [
        {
          "opus_id": int | str,
          "title": str,
          "summary": str,
          "cover": str,               // 封面 URL
          "jump_url": str,
          "pub_time": int,            // 发布时间 epoch 秒
          "ctime": int,
          "stats": {
            "view": int,
            "favorite": int,
            "like": int,
            "reply": int,
            "share": int,
            "coin": int
          },
          "modules": dict | list,     // 内容模块 (dict 或 list-of-dicts)
          "type": str,                // 动态类型
          "visible": bool,
          "is_top": bool,
          "is_reserve": bool,
          "rid": int | null,
          "id_str": str,
          "orig": dict | null         // 转发时存原动态
        }
      ],
      "offset": str,                  // 下一页游标
      "has_more": int                 // 0=无更多, 1=有更多
    }
  ]
}
```

fetching 提取：`items[*].opus_id`（增量 item ID）, `has_more` + `offset`（翻页）

#### 3.8 dynamics（分页 cursor, offset）

API: `User.get_dynamics_new(offset)` → `https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space`

raw_payload 结构：

```
{
  "pages": [
    {
      "items": [
        {
          "id_str": str,              // 动态稳定 ID
          "type": str,                // "DYNAMIC_TYPE_FORWARD" | "DYNAMIC_TYPE_AV" |
                                      // "DYNAMIC_TYPE_DRAW" | "DYNAMIC_TYPE_WORD" |
                                      // "DYNAMIC_TYPE_ARTICLE" | "DYNAMIC_TYPE_COMMON_SQUARE" | ...
          "rid": int,
          "visible": bool,
          "orig": dict | null,        // FORWARD 类型时存原动态完整副本（结构同本层级）
          "modules": dict | list,     // 内容模块：
            // dict 形态：
            //   "module_author": {
            //     "pub_ts": str,          // epoch 秒（字符串）
            //     "name": str,
            //     "mid": int,
            //     "face": str,
            //     "pub_action": str,
            //     "pub_location": str,
            //     "decorate": dict | null,
            //     "following": bool
            //   },
            //   "module_dynamic": {
            //     "desc": {
            //       "text": str,          // 动态文本
            //       "rich_text_nodes": list
            //     } | null,
            //     "major": {
            //       "type": str,          // "MAJOR_TYPE_ARCHIVE" | "MAJOR_TYPE_ARTICLE" |
            //                              // "MAJOR_TYPE_DRAW" | "MAJOR_TYPE_OPUS" | ...
            //       "archive": {           // MAJOR_TYPE_ARCHIVE
            //         "bvid": str,
            //         "aid": str | int,
            //         "title": str,
            //         "desc": str,
            //         "duration_text": str,
            //         "jump_url": str,
            //         "cover": str,
            //         "type": int,
            //         "badge": dict | null,
            //         "stat": dict | null
            //       } | null,
            //       "article": {           // MAJOR_TYPE_ARTICLE
            //         "id": int,
            //         "title": str,
            //         "desc": str,
            //         "jump_url": str,
            //         "covers": list[str],
            //         "label": str
            //       } | null,
            //       "draw": {              // MAJOR_TYPE_DRAW
            //         "items": [
            //           { "src": str, "width": int, "height": int, "size": float, "tags": list }
            //         ]
            //       } | null,
            //       "opus": {              // MAJOR_TYPE_OPUS
            //         "summary": { "text": str, "rich_text_nodes": list },
            //         "pics": [ { "url": str, "width": int, "height": int } ],
            //         "jump_url": str
            //       } | null,
            //       "common": dict | null, // MAJOR_TYPE_COMMON
            //       "live_rcmd": dict | null, // MAJOR_TYPE_LIVE_RCMD
            //       "live": dict | null,   // MAJOR_TYPE_LIVE
            //       "music": dict | null,  // MAJOR_TYPE_MUSIC
            //       "pgc": dict | null,    // MAJOR_TYPE_PGC
            //       "courses": dict | null, // MAJOR_TYPE_COURSES
            //       "none": dict | null    // MAJOR_TYPE_NONE
            //     } | null,
            //     "additional": dict | null,
            //     "topic": dict | null
            //   },
            //   "module_stat": {
            //     "forward": { "count": int, "forbidden": bool },
            //     "comment": { "count": int, "forbidden": bool },
            //     "like": { "count": int, "forbidden": bool, "status": bool },
            //     "coin": { "count": int, "forbidden": bool },
            //     "favorite": { "count": int, "forbidden": bool }
            //   },
            //   "module_interaction": dict | null,
            //   "module_more": dict | null,
            //   "module_tag": dict | null
        }
      ],
      "offset": str,
      "has_more": int,               // 0 | 1
      "update_num": int,
      "update_baseline": str
    }
  ]
}
```

fetching 提取：`items[*].id_str`（增量 item ID）, `has_more` + `offset`（翻页）

#### 3.9 audios（分页 page, pn/ps=30）

API: `User.get_audios(pn, ps, order)` → `https://www.bilibili.com/audio/music-service-c/web/song/upper`

raw_payload 结构：

```
{
  "pages": [
    {
      "data": [
        {
          "id": int,                  // 音频 ID
          "uid": int,
          "uname": str,
          "author": str,
          "title": str,
          "intro": str,
          "intro_copy": str,
          "mbnames": str,
          "cover": str,               // 封面 URL
          "lyric": str,
          "crtitle": str,
          "duration": int,            // 时长秒数
          "passtime": int,            // epoch 秒
          "ctime": int,               // 创建时间 epoch 秒
          "statistic": {
            "sid": int,
            "play": int,
            "collect": int,
            "comment": int,
            "share": int
          },
          "is_cooper": int,
          "pgc_info": dict | null
        }
      ],
      "curPage": int,
      "pageCount": int,
      "totalSize": int,               // 音频总数
      "pageSize": int
    }
  ]
}
```

fetching 提取：`data[*].id`（增量 item ID）, `totalSize`（分页总数）

#### 3.10 channel_list（分页 page, pn/ps=20）

API: `User.get_channel_list(pn, ps)` → `https://api.bilibili.com/x/polymer/web-space/seasons_series_list`

raw_payload 结构：

```
{
  "pages": [
    {
      "items_lists": {
        "page": {
          "page_num": int,
          "page_size": int,
          "total": int               // 合集+列表总数
        },
        "seasons_list": [
          {
            "meta": {
              "season_id": int,
              "mid": int,
              "name": str,
              "cover": str,
              "total": int,           // 合集内视频数
              "ptime": int,
              "category": int
            },
            "recent_aids": list[int],
            "archive_count": int
          }
        ],
        "series_list": [
          {
            "meta": {
              "series_id": int,
              "mid": int,
              "name": str,
              "cover": str,
              "total": int,
              "ctime": int,
              "mtime": int,
              "description": str,
              "keywords": list[str],
              "raw_keywords": str,
              "category": int
            },
            "recent_aids": list[int],
            "archive_count": int
          }
        ]
      }
    }
  ]
}
```

fetching 提取：`items_lists.seasons_list[*].meta.season_id` + `items_lists.series_list[*].meta.series_id`（增量 item ID）, `items_lists.page.total`（分页总数）

#### 3.11 subscribed_bangumi（分页 page, pn/ps=15）

API: `User.get_subscribed_bangumi(pn, ps, type_, follow_status)` → `https://api.bilibili.com/x/space/bangumi/follow/list`

raw_payload 结构：

```
{
  "pages": [
    {
      "list": [
        {
          "season_id": int,
          "media_id": int,
          "season_type": int,
          "season_type_name": str,
          "title": str,
          "cover": str,
          "total_count": int,
          "is_finish": int,
          "is_started": int,
          "is_play": int,
          "badge": str,
          "badge_type": int,
          "rights": dict,
          "stat": dict,
          "new_ep": dict,
          "rating": dict | null,
          "square_cover": str,
          "season_status": int,
          "areas": list,
          "subtitle": str,
          "progress": str,
          "publish": dict,
          "mode": int,
          "url": str,
          "badge_ep": str,
          "media_badge": str,
          "follow_status": int,
          "followOfficial": int,
          "series_title": str,
          "series_ord": int
        }
      ],
      "pn": int,
      "ps": int,
      "total": int
    }
  ]
}
```

fetching 提取：`list[*].season_id`（增量 item ID）, 分页总数通过 `list` 长度和 `total` 检测

#### 3.12 article_list（非分页）

API: `User.get_article_list(order)` → `https://api.bilibili.com/x/article/up/lists`

raw_payload 是 dict：

```
{
  "lists": [
    {
      "id": int,                    // rlid（文集 ID）
      "mid": int,                   // 用户 uid
      "name": str,                  // 文集名称
      "image_url": str,
      "update_time": int,
      "ctime": int,
      "publish_time": int,
      "summary": str,
      "article_count": int,
      "read_count": int,
      "words": int,
      "state": int,
      "reason": str,
      "apply_time": str,
      "check_time": str
    }
  ],
  "total": int
}
```

#### 3.13 album（分页 page, pn/ps=30, 参数名 page_num/page_size）

API: `User.get_album(biz, page_num, page_size)` → `https://api.vc.bilibili.com/link_draw/v1/doc/doc_list`

raw_payload 结构：

```
{
  "pages": [
    {
      "biz_list": [
        {
          "doc_id": int,
          "poster_uid": int,
          "pictures": list[dict],     // [{img_src, img_width, img_height, img_size, img_tags}]
          "title": str,
          "description": str,
          "category": str,
          "upload_time": int,
          "like_num": int,
          "comment_num": int,
          "view_num": int
        }
      ],
      "total_count": int
    }
  ]
}
```

fetching 提取：`biz_list[*].doc_id`（增量 item ID）, `total_count`（分页总数）

#### 3.14 user_fav_tag（分页 page, pn/ps=20；实际不翻页）

API: `User.get_user_fav_tag(pn, ps)` → `https://api.bilibili.com/x/space/fav/tag`

raw_payload 结构：

```
{
  "pages": [
    {
      "list": [
        {
          "tag_id": int,
          "name": str,
          "cover": str,
          "type": int,
          "count": int,
          "ctime": int,
          "mtime": int,
          "is_activity": bool,
          "is_atten": int
        }
      ],
      "has_more": bool
    }
  ]
}
```

已知限制：bilibili-api-python 内部 pn/ps 参数被注释，实际只返回第一页。

#### 3.15 upower_qa（分页 anchor）

API: `User.get_upower_qa_list(anchor)` → `https://api.bilibili.com/x/upower/up/question`

raw_payload 结构：

```
{
  "pages": [
    {
      "list": [
        {
          "qa_id": int | str,
          "question": str,
          "answer": str,
          "answer_pics": list[str],
          "ctime": int,
          "mtime": int,
          "state": int,
          "order_num": int
        }
      ],
      "anchor": int                  // 下一页锚点; 0 = 最后一页
    }
  ]
}
```

fetching 提取：`list[*].qa_id`（增量 item ID）, `anchor`（翻页; 0 终止）

#### 3.16 需凭据的端点

**user_medal**（非分页，需凭据）

API: `User.get_user_medal()` → `https://api.bilibili.com/x/space/fans/medal/panel`

raw_payload 是 dict，包含用户粉丝勋章列表：

```
{
  "list": list[dict],               // 勋章列表
  "special_list": list[dict],       // 特殊勋章列表
  "count": int,
  "total_num": int
}
```

**all_followings**（非分页，需凭据）

API: `User.get_all_followings()` → 多次调用 `https://api.bilibili.com/x/relation/followings` 合并

raw_payload 是 list（非 dict）：

```
[
  {
    "mid": int,
    "attribute": int,
    "tag": list[int] | null,
    "special": int,
    "uname": str,
    "face": str,
    "sign": str,
    "official_verify": dict,
    "vip": dict,
    "live": dict | null
  }
]
```

**elec_monthly**（非分页，需凭据）

API: `User.get_elec_user_monthly()` → `https://api.bilibili.com/x/ugcpay/web/v2/user/month/elec/user`

raw_payload 是 dict：

```
{
  "show_info": int,
  "total": int,
  "count": int,
  "list": list[dict]                // 充电用户列表
}
```

#### 3.17 其他非分页端点

**space_notice**（非分页）

API: `User.get_space_notice()` → `https://api.bilibili.com/x/space/notice`

raw_payload 是 str（纯字符串，非 dict）：

```
"公告文本内容"
```

**top_videos**（非分页）

API: `User.get_top_videos()` → `https://api.bilibili.com/x/space/top/arc`

raw_payload 是 dict：

```
{
  "list": list[dict] | null,        // 置顶视频列表（结构同 videos 的 vlist 条目）
  "count": int
}
```

**masterpiece**（非分页）

API: `User.get_masterpiece()` → `https://api.bilibili.com/x/space/masterpiece`

原始返回 list，经 `_wrap_list_result` 包装为 dict：

```
{
  "list": [
    {
      "aid": int,
      "bvid": str,
      "title": str,
      "pic": str,
      "description": str,
      "play": int,
      "video_review": int,
      "comment": int,
      "reason": str,
      "reason_type": int
    }
  ]
}
```

**cheese**（非分页）

API: `User.get_cheese()` → `https://api.bilibili.com/pugv/app/web/season/page`

raw_payload 是 dict：

```
{
  "items": list[dict],              // 课堂列表
  "page": dict
}
```

---

### 4. item-level 端点数据（6 个）

item-level 端点从父端点的 raw_payload 中派生 item ID 列表，然后逐个抓取。每个 item 独立存储。

#### 4.1 video_detail

父端点：`videos`。item_id：bvid。
API: `Video(bvid).get_info()` + `Video(bvid).get_tags()`

raw_payload 是由 fetching 代码构造的 dict（非 B 站原始响应）：

```
{
  "info": {                           // get_info() 返回值
    "bvid": str,
    "aid": int,
    "videos": int,                    // 分 P 数
    "tid": int,                       // 分区 ID
    "tname": str,                     // 分区名称
    "copyright": int,                 // 1=原创, 2=转载
    "pic": str,                       // 封面 URL
    "title": str,
    "pubdate": int,                   // 发布时间 epoch 秒
    "ctime": int,                     // 创建时间 epoch 秒
    "desc": str,                      // 完整简介（不截断）
    "desc_v2": list[dict] | null,     // 富文本简介
    "state": int,
    "duration": int,                  // 总时长秒数
    "rights": {
      "bp": int,
      "elec": int,
      "download": int,
      "movie": int,
      "pay": int,
      "hd5": int,
      "no_reprint": int,
      "autoplay": int,
      "ugc_pay": int,
      "is_cooperation": int,
      "ugc_pay_preview": int,
      "no_background": int,
      "clean_mode": int,
      "is_stein_gate": int,
      "is_360": int,
      "no_share": int,
      "arc_pay": int,
      "free_watch": int
    },
    "owner": {
      "mid": int,
      "name": str,
      "face": str
    },
    "stat": {
      "aid": int,
      "view": int,
      "danmaku": int,
      "reply": int,
      "favorite": int,
      "coin": int,
      "share": int,
      "now_rank": int,
      "his_rank": int,
      "like": int,
      "dislike": int,
      "evaluation": str,
      "argue_msg": str,
      "vt": int
    },
    "dynamic": str,
    "cid": int,                       // 第一分 P 的 cid
    "dimension": {
      "width": int,
      "height": int,
      "rotate": int
    },
    "season_id": int | null,
    "no_cache": bool,
    "pages": [
      {
        "cid": int,
        "page": int,                  // 分 P 序号（从 1 开始）
        "from": str,                  // "vupload"
        "part": str,                  // 分 P 标题
        "duration": int,              // 分 P 时长秒数
        "vid": str,
        "weblink": str,
        "dimension": {
          "width": int,
          "height": int,
          "rotate": int
        },
        "first_frame": str | null
      }
    ],
    "subtitle": {
      "allow_submit": bool,
      "list": [
        {
          "id": int,
          "lan": str,
          "lan_doc": str,
          "is_lock": bool,
          "subtitle_url": str
        }
      ]
    },
    "is_season_display": bool,
    "user_garb": dict | null,
    "honor_reply": dict | null,
    "like_icon": str | null,
    "need_jump_bv": bool,
    "disable_show_up_info": bool,
    "label": dict                     // 透传 B 站原始结构
  },
  "tags": [                           // get_tags() 返回值
    {
      "tag_id": int,
      "tag_name": str,
      "cover": str,
      "likes": int,
      "hates": int,
      "attribute": int,
      "liked": int,
      "hated": int,
      "extra_attr": int,
      "music_id": str,
      "tag_type": str,
      "is_activity": bool,
      "color": str | null,
      "alpha": int,
      "content": str,
      "is_sub": int,
      "subscribed_count": int,
      "archive_count": str,
      "featured_count": int,
      "jump_url": str
    }
  ]
}
```

#### 4.2 article_detail

父端点：`articles`。item_id：str(cvid)。
API: `Article(cvid).get_info()` + `Article(cvid).fetch_content()` + `.markdown()` + `.json()`

raw_payload 是由 fetching 代码构造的 dict：

```
{
  "info": {                           // get_info() 返回值
    "id": int,                        // cvid
    "title": str,
    "state": int,
    "publish_time": int,
    "words": int,
    "image_urls": list[str],
    "category": dict,
    "categories": list[dict],
    "summary": str,
    "author": dict,
    "stats": dict,
    "like_state": int,
    "type": int,
    "reprint": int,
    "banner_url": str,
    "media": dict | null,
    "apply_time": str,
    "check_time": str,
    "original": int,
    "act_id": int,
    "dispute": dict | null,
    "authenMark": dict | null,
    "cover": dict | null,
    "top_video_info": dict | null,
    "is_author": bool,
    "pre": int,
    "last": int,
    "template_id": int,
    "dyn_id": str | null,
    "dyn_type": int,
    "is_top": int,
    "is_like": bool,
    "is_fav": bool,
    "is_coin": bool,
    "coin_num": int,
    "favorite": bool,
    "attention": bool
  },
  "markdown": str,                    // markdown() 返回值 — 渲染后的正文
  "content_json": list[dict]          // json() 返回值 — 编辑器节点树
}
```

#### 4.3 opus_detail

父端点：`opus`。item_id：str(opus_id)。
API: `Opus(opus_id).get_info()` + `.markdown()` + `.get_images_raw_info()`

raw_payload 是由 fetching 代码构造的 dict：

```
{
  "info": {                           // get_info() 返回值
    "item": {
      "basic": {
        "comment_id_str": str,
        "comment_type": int,
        "like_icon": str,
        "rid_str": str
      },
      "modules": [
        {
          "module_type": int,
          "module_content": dict
        }
      ]
    }
  },
  "markdown": str,                    // markdown() 返回值 — 渲染后的正文
  "images": [                         // get_images_raw_info() 返回值
    {
      "url": str,
      "width": int,
      "height": int,
      "size": float,
      "comment": str
    }
  ]
}
```

#### 4.4 article_list_detail

父端点：`article_list`。item_id：str(rlid)。
API: `ArticleList(rlid).get_content()` → `https://api.bilibili.com/x/article/list/web/articles`

raw_payload 是 B 站 API 原始响应：

```
{
  "list": {                           // 文集元数据
    "id": int,                        // rlid
    "mid": int,
    "name": str,
    "image_url": str,
    "update_time": int,
    "ctime": int,
    "publish_time": int,
    "summary": str,
    "article_count": int,
    "read_count": int,
    "words": int,
    "state": int,
    "reason": str,
    "apply_time": str,
    "check_time": str
  },
  "articles": [                       // 文集内文章列表
    {
      "id": int,                      // cvid
      "title": str,
      "summary": str,
      "banner_url": str,
      "template_id": int,
      "state": int,
      "author": dict,
      "reprint": int,
      "image_urls": list[str],
      "publish_time": int,
      "ctime": int,
      "stats": {
        "view": int,
        "favorite": int,
        "like": int,
        "reply": int,
        "share": int,
        "coin": int
      },
      "words": int,
      "category": dict,
      "type": int,
      "is_top": int,
      "dyn_id": str | null,
      "dyn_type": int
    }
  ],
  "author": {
    "mid": int,
    "name": str,
    "face": str
  },
  "articles_count": int,
  "attention": bool,
  "next": dict | null,
  "last": dict | null
}
```

#### 4.5 channel_videos_season

父端点：`channel_list`。item_id：season_id。
API: `User.get_channel_videos_season(sid, sort, pn, ps)`（内部分页，所有页合并）

raw_payload 是由 fetching 代码构造的 dict（合并所有分页）：

```
{
  "archives": [
    {
      "aid": int,
      "bvid": str,
      "title": str,
      "description": str,
      "pic": str,
      "author": str,
      "mid": int,
      "created": int,
      "length": str,
      "play": int,
      "video_review": int,
      "comment": int,
      "favorites": int,
      "typeid": int,
      "is_union_video": int,
      "is_pay": int,
      "is_steins_gate": int,
      "is_live_playback": int,
      "meta": dict | null,
      "attribute": int
    }
  ],
  "page": {
    "count": int                      // 视频总数
  }
}
```

#### 4.6 channel_videos_series

父端点：`channel_list`。item_id：series_id。
API: `User.get_channel_videos_series(sid, sort, pn, ps)`（内部分页，所有页合并）

raw_payload 结构与 `channel_videos_season` 完全一致：

```
{
  "archives": [
    {
      "aid": int,
      "bvid": str,
      "title": str,
      "description": str,
      "pic": str,
      "author": str,
      "mid": int,
      "created": int,
      "length": str,
      "play": int,
      "video_review": int,
      "comment": int,
      "favorites": int,
      "typeid": int,
      "is_union_video": int,
      "is_pay": int,
      "is_steins_gate": int,
      "is_live_playback": int,
      "meta": dict | null,
      "attribute": int
    }
  ],
  "page": {
    "count": int
  }
}
```

---

### 5. 端点与 item 的派生关系

```
videos ─────────→ video_detail       （每个 bvid 一条）
articles ───────→ article_detail     （每个 cvid 一条）
opus ───────────→ opus_detail        （每个 opus_id 一条）
article_list ───→ article_list_detail（每个 rlid 一条）
channel_list ───→ channel_videos_season（每个 season_id 一条）
                → channel_videos_series（每个 series_id 一条）
```

processing 层通过这些父子关系，从列表端点获取 item ID 清单，再从 item-level 端点获取详情。

---

### 6. 已知数据特征

- `videos` 端点的 `description` 被 B 站截断至约 250 字符；完整简介在 `video_detail.info.desc`。
- `space_notice` 的 raw_payload 是纯字符串，不是 dict。
- `all_followings` 的 raw_payload 是 list，不是 dict。
- `masterpiece` 原始响应是 list，经 `_wrap_list_result` 包装为 `{"list": [...]}`。
- `opus.modules` 在 B 站 API 不同版本中可能是 dict 也可能是 list-of-dicts，processing 层做兼容。
- `user_fav_tag` 因 bilibili-api-python 内部 bug（pn/ps 参数被注释），实际只返回第一页数据。
- `album` 使用 `page_num`/`page_size` 参数名（非 `pn`/`ps`），client 层已做映射。
- `up_stat.archive` 和 `up_stat.article` 在不同 API 版本中可能是 `{view: int}` 嵌套或直接 `int`。
- `overview_stat` 的字段名在不同 API 版本中有 `video` vs `video_count` 等差异。
- 非分页端点的 raw_payload 是 B 站 API 原样存储，包含本文未列出的额外字段（B 站随时可能新增字段）。fetching 不做字段筛选。
