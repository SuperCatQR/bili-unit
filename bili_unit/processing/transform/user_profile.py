# transform/user_profile — UP 主画像 transform handler.
#
# Per docs/design/processing.md §6.6:
#   输入来源
#       四个 uid-level endpoint 的 raw_payload — user_info / relation_info /
#       up_stat / overview_stat。前 3 个必填，overview_stat 可选。
#   输出
#       每个 uid 产出 1 个 WorkItem（item_id == str(uid)），transform 返回单个
#       结构化 dict；overview_stat 缺失时整段省略 result.overview。
#
# 必填 endpoint 任一缺失或 raw_payload 为空时 extract_items 返回空列表，
# 与现有 handler（video_metadata/dynamics/articles）的处理方式一致。
# 可选 endpoint 由 runner 在 _discover_items 阶段透传（缺失时不进入
# raw_payloads），此处通过键存在性判断是否输出 result.overview。

from __future__ import annotations

from typing import Any

from ._base import TransformHandler, WorkItem

ITEM_TYPE = "user_profile"
SOURCE_ENDPOINTS: tuple[str, ...] = (
    "user_info", "relation_info", "up_stat", "overview_stat",
)
OPTIONAL_ENDPOINTS: tuple[str, ...] = ("overview_stat",)
_REQUIRED: tuple[str, ...] = tuple(
    ep for ep in SOURCE_ENDPOINTS if ep not in OPTIONAL_ENDPOINTS
)


def _label_text(vip_label: Any) -> str:
    """``vip.label`` 在现行 acc/info 接口中是 dict({text, ...})；兼容老版字符串。"""
    if isinstance(vip_label, dict):
        txt = vip_label.get("text")
        return txt if isinstance(txt, str) else ""
    if isinstance(vip_label, str):
        return vip_label
    return ""


def _int_or_zero(d: dict, key: str) -> int:
    val = d.get(key, 0)
    return val if isinstance(val, int) else 0


def _build_vip(user_info: dict) -> dict[str, Any]:
    vip = user_info.get("vip")
    if not isinstance(vip, dict):
        return {"type": 0, "status": 0}
    return {
        "type": vip.get("type", 0),
        "status": vip.get("status", 0),
        "label": _label_text(vip.get("label")),
    }


def _build_social(relation_info: dict) -> dict[str, int]:
    return {
        "following": _int_or_zero(relation_info, "following"),
        "follower": _int_or_zero(relation_info, "follower"),
        "whisper": _int_or_zero(relation_info, "whisper"),
        "black": _int_or_zero(relation_info, "black"),
    }


def _nested_view(d: dict, key: str) -> int:
    """up_stat.archive / up_stat.article 是 {view: int} 嵌套；兼容扁平 int。"""
    sub = d.get(key)
    if isinstance(sub, dict):
        return _int_or_zero(sub, "view")
    if isinstance(sub, int):
        return sub
    return 0


def _build_stats(up_stat: dict) -> dict[str, int]:
    return {
        "archive_view": _nested_view(up_stat, "archive"),
        "article_view": _nested_view(up_stat, "article"),
        "likes": _int_or_zero(up_stat, "likes"),
    }


def _overview_count(d: dict, *names: str) -> int:
    """overview_stat 字段名在不同 B 站 API 版本间略有差异；按候选列表取首个 int。"""
    for name in names:
        val = d.get(name)
        if isinstance(val, int):
            return val
    return 0


def _build_overview(overview_stat: dict) -> dict[str, int]:
    return {
        "video_count": _overview_count(overview_stat, "video", "video_count"),
        "article_count": _overview_count(overview_stat, "article", "article_count"),
        "opus_count": _overview_count(overview_stat, "opus", "opus_count"),
    }


class _UserProfileHandler:
    item_type = ITEM_TYPE
    source_endpoints = SOURCE_ENDPOINTS
    # 可选端点：runner 在发现阶段读到 getattr(handler, "optional_endpoints", ())
    # 时，对其中的 endpoint 做"缺失即跳过"处理（不并入 raw_payloads，不阻塞）。
    optional_endpoints = OPTIONAL_ENDPOINTS

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        # 必填端点任一缺失或为空 → 不入队（runner 这一轮不处理；下次 process_uid
        # 重新评估）。与 §3 / §5 描述一致。
        for ep in _REQUIRED:
            rp = raw_payloads.get(ep)
            if not isinstance(rp, dict) or not rp:
                return []
        ui = raw_payloads["user_info"]
        uid = ui.get("mid")
        if not isinstance(uid, int):
            return []
        return [WorkItem(
            item_type=ITEM_TYPE,
            item_id=str(uid),
            item_data=raw_payloads,
        )]

    def transform(self, item: WorkItem) -> dict[str, Any]:
        rp: dict[str, dict] = item.item_data
        ui = rp.get("user_info") or {}
        ri = rp.get("relation_info") or {}
        up = rp.get("up_stat") or {}

        uid_int = ui.get("mid")
        if not isinstance(uid_int, int):
            try:
                uid_int = int(item.item_id)
            except (TypeError, ValueError):
                uid_int = 0

        result: dict[str, Any] = {
            "uid": uid_int,
            "name": ui.get("name") or "",
            "sex": ui.get("sex") or "",
            "sign": ui.get("sign") or "",
            "avatar": ui.get("face") or "",
            "birthday": ui.get("birthday") or "",
            "level": _int_or_zero(ui, "level"),
            "vip": _build_vip(ui),
            "join_time": _int_or_zero(ui, "jointime"),

            "social": _build_social(ri),
            "stats": _build_stats(up),
        }

        # overview_stat 可选 — 仅在 raw_payload 存在且非空时输出 result.overview，
        # 让 ingestion 通过键存在性判断（§4.2）。
        ov = rp.get("overview_stat")
        if isinstance(ov, dict) and ov:
            result["overview"] = _build_overview(ov)

        return result


HANDLER: TransformHandler = _UserProfileHandler()
