"""Cookie 字符串解析 → :class:`CredentialInfo`。

兼容多种输入：完整的 ``Set-Cookie`` 串、``key=value; key=value`` 串、
以及用户从浏览器复制的整段 Cookie。字段名大小写不敏感，缺字段也能用。
自动把中文输入法「智能标点」产生的全角标点（；＝ 等）归一化为 ASCII。
"""

from __future__ import annotations

from ..models import CredentialInfo

# Cookie 中各凭据字段的常见别名（大小写不敏感匹配）
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "sessdata": ("SESSDATA", "sessdata", "Session"),
    "bili_jct": ("bili_jct", "BILI_JCT", "bilibili_jct"),
    "buvid3": ("buvid3", "BUVID3", "buVID3"),
    "dedeuserid": ("DedeUserID", "dedeuserid", "DEDEUSERID", "DedeUserId"),
}

# 全角标点 → ASCII（中文输入法智能标点会把 ;=,: 转成全角，导致解析失败）
_FULLWIDTH_TABLE = str.maketrans({"；": ";", "＝": "=", "，": ",", "：": ":", "　": " "})


def normalize_cookie_text(text: str) -> str:
    """归一化全角标点为 ASCII。"""
    return text.translate(_FULLWIDTH_TABLE)


def parse_cookie(raw: str) -> CredentialInfo:
    """解析 Cookie 字符串为 :class:`CredentialInfo`。

    :raises ValueError: 输入为空或不含任何已知凭据字段。
    """
    if not raw or not raw.strip():
        raise ValueError("Cookie 为空")

    # 归一化全角标点（；＝ 等 → ;=）
    text = normalize_cookie_text(raw).strip()
    # 去掉可能的 "Cookie:" 前缀
    if text.lower().startswith("cookie:"):
        text = text[text.index(":") + 1:].strip()

    # 解析成大小写不敏感的键值表
    pairs: dict[str, str] = {}
    # 同时支持分号分隔与换行分隔
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            pairs[key.lower()] = value

    cred = CredentialInfo()
    for attr, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            value = pairs.get(alias.lower())
            if value:
                setattr(cred, attr, value)
                break

    if cred.is_empty():
        raise ValueError("Cookie 中未识别到任何凭据字段（SESSDATA / bili_jct / buvid3 / DedeUserID）")
    return cred
