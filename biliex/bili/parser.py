"""原始 bilibili 接口返回 → 归一化模型的防御式解析。

全部使用 ``.get()`` + 默认值，B 站字段缺失或更名时不会抛异常，
最坏情况是某字段为空，业务层仍可运行。若字段大规模变更，只需改本文件。
"""

from __future__ import annotations

from typing import Any

from ..models import UserInfo, VideoInfo


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def parse_user_info(data: dict[str, Any] | None) -> UserInfo:
    """解析 ``user.get_self_info`` / ``User.get_user_info`` 的返回。

    两接口返回结构略有差异（前者顶层即用户信息；后者亦然），统一用防御式取值兼容。
    """
    data = data or {}
    # get_self_info 的返回里 uid 字段可能叫 mid；get_user_info 也叫 mid
    uid = _as_str(data.get("mid") or data.get("uid") or data.get("id"))
    name = _as_str(data.get("name") or data.get("uname") or data.get("nickname"))
    face = _as_str(data.get("face") or data.get("avatar"))
    sign = _as_str(data.get("sign") or data.get("signature"))
    return UserInfo(uid=uid, name=name, face=face, sign=sign)


def parse_video_list(data: dict[str, Any] | None, owner_uid: str = "") -> list[VideoInfo]:
    """解析 ``User.get_videos`` 的返回。

    典型结构：``{"list": {"vlist": [ {title,bvid,aid,pic,description,created,length,play,comment}, ... ], "tlist": {...}}, "page": {...}}``
    """
    data = data or {}
    root = data.get("list") if isinstance(data.get("list"), dict) else data
    vlist = root.get("vlist") if isinstance(root, dict) else None
    if not isinstance(vlist, list):
        return []

    videos: list[VideoInfo] = []
    for item in vlist:
        if not isinstance(item, dict):
            continue
        videos.append(
            VideoInfo(
                bvid=_as_str(item.get("bvid")),
                aid=_as_str(item.get("aid")),
                title=_as_str(item.get("title")),
                cover=_as_str(item.get("pic")),
                desc=_as_str(item.get("description") or item.get("desc")),
                pubdate=_as_int(item.get("created") or item.get("pubdate")),
                length=_as_str(item.get("length")),
                play=_as_int(item.get("play")),
                comment=_as_int(item.get("comment")),
                owner_uid=owner_uid,
            )
        )
    return videos


def _format_duration(seconds: Any) -> str:
    s = _as_int(seconds)
    if s <= 0:
        return ""
    return f"{s // 60}:{s % 60:02d}"


def parse_recommendation_list(data: dict[str, Any] | None) -> list[VideoInfo]:
    """解析首页推荐流 ``/x/web-interface/wbi/index/top/feed/rcmd`` 的返回。

    典型结构：``{"data": {"item": [ {id,bvid,title,pic,duration,pubdate, owner:{mid,name,face}, stat:{view,reply,...}, rcmd_reason:{content} } ]}}``
    """
    data = data or {}
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    items = root.get("item") if isinstance(root, dict) else None
    if not isinstance(items, list):
        return []

    videos: list[VideoInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bvid = _as_str(item.get("bvid"))
        if not bvid:
            continue  # 推荐流里可能混入非视频项（广告/番剧），无 bvid 的跳过
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
        rcmd = item.get("rcmd_reason")
        rcmd_text = ""
        if isinstance(rcmd, dict):
            rcmd_text = _as_str(rcmd.get("content"))
        videos.append(
            VideoInfo(
                bvid=bvid,
                aid=_as_str(item.get("id") or item.get("aid")),
                title=_as_str(item.get("title")),
                cover=_as_str(item.get("pic")),
                desc=rcmd_text,
                pubdate=_as_int(item.get("pubdate")),
                length=_format_duration(item.get("duration")),
                play=_as_int(stat.get("view")),
                comment=_as_int(stat.get("reply")),
                owner_uid=_as_str(owner.get("mid")),
            )
        )
    return videos
