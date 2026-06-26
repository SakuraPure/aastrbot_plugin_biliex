"""归一化领域模型。

业务层只依赖这些数据类，不接触 bilibili-api 的原始返回结构。
原始 dict → model 的映射集中在 :mod:`biliex.bili.parser`，且全部采用防御式 ``.get()``，
即便 B 站接口字段变动也只影响该解析模块，业务代码不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CredentialInfo:
    """B 站登录凭据（由 Cookie 解析得到）。各字段均可缺省，按需使用。"""

    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    dedeuserid: str = ""

    def is_empty(self) -> bool:
        return not (self.sessdata or self.bili_jct or self.buvid3 or self.dedeuserid)

    def to_dict(self) -> dict[str, str]:
        return {
            "sessdata": self.sessdata,
            "bili_jct": self.bili_jct,
            "buvid3": self.buvid3,
            "dedeuserid": self.dedeuserid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CredentialInfo":
        data = data or {}
        return cls(
            sessdata=str(data.get("sessdata", "") or ""),
            bili_jct=str(data.get("bili_jct", "") or ""),
            buvid3=str(data.get("buvid3", "") or ""),
            dedeuserid=str(data.get("dedeuserid", "") or ""),
        )


@dataclass
class UserInfo:
    """B 站用户信息（绑定验证时取到）。"""

    uid: str = ""
    name: str = ""
    face: str = ""
    sign: str = ""


@dataclass
class VideoInfo:
    """单条投稿视频的归一化信息。

    字段命名与 B 站原始接口解耦：原始 ``pic`` → ``cover``，``created`` → ``pubdate`` 等。
    """

    bvid: str = ""
    aid: str = ""
    title: str = ""
    cover: str = ""
    desc: str = ""
    pubdate: int = 0  # 发布时间，秒级时间戳
    length: str = ""  # 时长，原始为 "mm:ss" 字符串
    play: int = 0
    comment: int = 0
    owner_uid: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""


@dataclass
class Binding:
    """一条「绑定」：某用户（owner_key 全局）绑定了一个 B 站账号。

    ``owner_key`` 形如 ``"{platform}:{sender_id}"``，全局定位某用户（跨会话）；
    ``push_targets`` 为已订阅推送的会话（umo）列表，新视频会推送到这些会话。
    """

    binding_id: str
    owner_key: str
    uid: str
    uname: str
    credential: CredentialInfo = field(default_factory=CredentialInfo)
    push_enabled: bool = True
    push_targets: list[str] = field(default_factory=list)
    last_bvid: str = ""
    pushed_bvids: list[str] = field(default_factory=list)
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "owner_key": self.owner_key,
            "uid": self.uid,
            "uname": self.uname,
            "credential": self.credential.to_dict(),
            "push_enabled": self.push_enabled,
            "push_targets": list(self.push_targets),
            "last_bvid": self.last_bvid,
            "pushed_bvids": list(self.pushed_bvids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Binding":
        return cls(
            binding_id=str(data.get("binding_id", "") or ""),
            owner_key=str(data.get("owner_key", "") or ""),
            uid=str(data.get("uid", "") or ""),
            uname=str(data.get("uname", "") or ""),
            credential=CredentialInfo.from_dict(data.get("credential")),
            push_enabled=bool(data.get("push_enabled", True)),
            push_targets=list(data.get("push_targets", []) or []),
            last_bvid=str(data.get("last_bvid", "") or ""),
            pushed_bvids=list(data.get("pushed_bvids", []) or []),
            created_at=int(data.get("created_at", 0) or 0),
        )


@dataclass
class Owner:
    """绑定归属者：某用户（全局，跨会话），可拥有多个绑定，其中一个为「激活」。"""

    owner_key: str
    sender_id: str
    platform: str
    active_binding_id: str = ""
    binding_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_key": self.owner_key,
            "sender_id": self.sender_id,
            "platform": self.platform,
            "active_binding_id": self.active_binding_id,
            "binding_ids": list(self.binding_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Owner":
        return cls(
            owner_key=str(data.get("owner_key", "") or ""),
            sender_id=str(data.get("sender_id", "") or ""),
            platform=str(data.get("platform", "") or ""),
            active_binding_id=str(data.get("active_binding_id", "") or ""),
            binding_ids=list(data.get("binding_ids", []) or []),
        )
