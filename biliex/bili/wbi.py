"""bilibili wbi 签名（web 接口风控所需）。

首页推荐等 web 接口需要 wbi 签名（``w_rid`` + ``wts``）。本模块自包含实现该算法，
不依赖 ``bilibili_api`` 的内部接口，便于维护。算法源自 bilibili-API-collect。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse

# wbi mixin_key 打乱表
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 11, 37, 14, 44, 21, 4, 52,
    26, 39, 17, 40, 34, 16, 41, 12, 13, 51, 30, 38, 20, 7, 0, 6,
    36, 22, 24, 1, 25, 55, 57, 48, 28, 59, 56, 54, 60, 61, 62, 63,
]


def get_mixin_key(orig: str) -> str:
    """由 img_key+sub_key 生成 32 位 mixin_key。"""
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """对请求参数做 wbi 签名，返回带 ``wts`` 与 ``w_rid`` 的新 dict。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    wts = int(time.time())
    signed = dict(params)
    signed["wts"] = wts
    # 排序后编码，对部分字符做转义（与官方一致）
    query = urllib.parse.urlencode(
        {
            k: str(v)
            .replace("!", "%21")
            .replace("'", "%27")
            .replace("(", "%28")
            .replace(")", "%29")
            for k, v in sorted(signed.items())
        }
    )
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    signed["w_rid"] = w_rid
    return signed


def extract_key(url: str) -> str:
    """从 ``https://i0.hdslb.com/bfs/wbi/xxx.png`` 提取 key ``xxx``。"""
    if not url:
        return ""
    last = url.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[0]
