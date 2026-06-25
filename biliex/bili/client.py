"""bilibili 接口封装层（插件内**唯一**触碰 ``bilibili_api`` / B 站网络请求的地方）。

- 绑定验证（反查自身账号）走 ``bilibili_api`` 库；
- 首页推荐流走自包含的 httpx + wbi 签名请求，不依赖库的内部接口，便于维护。

对外只返回归一化模型 :class:`UserInfo` / :class:`VideoInfo`，并统一把底层异常
转换为 :class:`ApiError` / :class:`CredentialError`，使业务层无需感知细节。

若 B 站接口字段或 ``bilibili-api`` 库签名变更，通常仅需调整本文件。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..errors import ApiError, CredentialError
from ..models import CredentialInfo, UserInfo, VideoInfo
from . import parser, wbi

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_RCMD_URL = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _import_bili():
    """延迟导入 bilibili_api（仅绑定验证用到）。库未安装时给出清晰报错。"""
    try:
        from bilibili_api import user as bili_user  # noqa: F401
        from bilibili_api import Credential  # noqa: F401
        return bili_user, Credential
    except ImportError as e:  # pragma: no cover - 依赖缺失场景
        raise ApiError(
            "未安装 bilibili-api 依赖，请在插件目录执行 pip install bilibili-api-python，或在 AstrBot 中重装本插件以自动安装依赖。"
        ) from e


class BilibiliClient:
    """对 B 站接口的薄封装，返回归一化模型。"""

    def __init__(self, timeout: int = 15, proxy: str = "") -> None:
        self._timeout = max(3, timeout)
        self._proxy = proxy.strip()
        # wbi 密钥缓存（img_key, sub_key），带 TTL
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_ts: float = 0.0

    # ==================== 通用工具 ====================
    def _cookies(self, cred: CredentialInfo) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for k, v in (
            ("SESSDATA", cred.sessdata),
            ("bili_jct", cred.bili_jct),
            ("buvid3", cred.buvid3),
            ("DedeUserID", cred.dedeuserid),
        ):
            if v:
                cookies[k] = v
        return cookies

    def _build_credential(self, cred: CredentialInfo) -> Any:
        """构建 bilibili_api.Credential（绑定验证用）。"""
        _, Credential = _import_bili()
        try:
            return Credential(
                sessdata=cred.sessdata or None,
                bili_jct=cred.bili_jct or None,
                buvid3=cred.buvid3 or None,
                dedeuserid=cred.dedeuserid or None,
            )
        except Exception as e:
            raise CredentialError(f"凭据构建失败：{e}") from e

    async def _call(self, coro):
        """带超时与异常归一化的协程执行器（用于 bilibili_api 调用）。"""
        try:
            return await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.TimeoutError:
            raise ApiError(f"调用 B 站接口超时（{self._timeout}s）")
        except Exception as e:
            msg = str(e)
            if any(k in msg for k in ("登录", "未登录", "-101", "credential", "Credential", "SESSDATA")):
                raise CredentialError(f"凭据无效或已失效：{msg}") from e
            raise ApiError(f"调用 B 站接口失败：{msg}") from e

    def _http_client(self):
        """构造 httpx AsyncClient。httpx 由 AstrBot 提供。"""
        import httpx

        kwargs: dict[str, Any] = {"timeout": self._timeout, "headers": {"User-Agent": _UA}}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return httpx.AsyncClient(**kwargs)

    async def _get_json(self, url: str, params: dict, cookies: dict[str, str]) -> dict:
        """发起 GET 请求并返回 JSON dict。统一异常归一化。"""
        try:
            async with self._http_client() as client:
                resp = await client.get(url, params=params, cookies=cookies)
                resp.raise_for_status()
                import json

                try:
                    return resp.json()
                except Exception as e:
                    raise ApiError(f"B 站接口返回非 JSON：{e}") from e
        except (ApiError, CredentialError):
            raise
        except Exception as e:
            raise ApiError(f"请求 B 站接口失败：{e}") from e

    # ==================== wbi 签名 ====================
    async def _ensure_wbi_keys(self, cookies: dict[str, str]) -> tuple[str, str]:
        """获取并缓存 wbi img_key/sub_key（TTL 1 小时）。"""
        if self._wbi_keys and (time.time() - self._wbi_keys_ts < 3600):
            return self._wbi_keys
        data = await self._get_json(_NAV_URL, {}, cookies)
        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_key = wbi.extract_key(wbi_img.get("img_url", ""))
        sub_key = wbi.extract_key(wbi_img.get("sub_url", ""))
        if not img_key or not sub_key:
            raise ApiError("获取 wbi 密钥失败，可能是接口变更或网络异常。")
        self._wbi_keys = (img_key, sub_key)
        self._wbi_keys_ts = time.time()
        return self._wbi_keys

    # ==================== 对外能力 ====================
    async def get_self_account(self, cred: CredentialInfo) -> UserInfo:
        """用凭据反查自身账号信息（uid + 名称），用于绑定验证。

        优先 ``user.get_self_info``；失败回退「DedeUserID → User.get_user_info」。
        """
        bili_user, _ = _import_bili()
        credential = self._build_credential(cred)

        # 路径 1：get_self_info
        try:
            data = await self._call(self._safe_call(bili_user.get_self_info, credential=credential))
            info = parser.parse_user_info(data)
            if info.uid:
                return info
        except CredentialError:
            raise
        except Exception:
            pass  # 回退到路径 2

        # 路径 2：DedeUserID + User.get_user_info
        if cred.dedeuserid:
            try:
                u = bili_user.User(uid=int(cred.dedeuserid), credential=credential)
                data = await self._call(u.get_user_info())
                info = parser.parse_user_info(data)
                if info.uid:
                    return info
            except CredentialError:
                raise
            except Exception:
                pass

        raise CredentialError("凭据无效或无法获取账号信息，请检查 Cookie 是否完整且未失效。")

    async def get_recommendations(self, cred: CredentialInfo, count: int = 10) -> list[VideoInfo]:
        """获取绑定账号的**首页推荐流**视频（即打开 App/网页首页被推荐的视频）。

        走 web 推荐接口 ``/x/web-interface/wbi/index/top/feed/rcmd``，带 wbi 签名与登录 Cookie。
        返回归一化的 :class:`VideoInfo` 列表。
        """
        if cred.is_empty():
            raise CredentialError("凭据为空，无法获取首页推荐。")
        cookies = self._cookies(cred)
        ps = max(1, min(30, count))
        params: dict[str, Any] = {
            "fresh_type": 4,
            "ps": ps,
            "fresh_idx": int(time.time()),
            "brush": 0,
            "feed_style": 1,
        }
        try:
            img_key, sub_key = await self._ensure_wbi_keys(cookies)
        except Exception:
            # wbi 密钥获取失败时，清除缓存重试一次
            self._wbi_keys = None
            img_key, sub_key = await self._ensure_wbi_keys(cookies)
        signed = wbi.enc_wbi(params, img_key, sub_key)

        data = await self._get_json(_RCMD_URL, signed, cookies)
        code = data.get("code")
        if code in (-101, -352) or "登录" in str(data.get("message", "")):
            # -101 未登录 / -352 风控：凭据问题
            raise CredentialError(f"获取首页推荐失败（code={code}）：{data.get('message', '')}，请检查 Cookie 是否有效。")
        if code != 0:
            raise ApiError(f"获取首页推荐失败（code={code}）：{data.get('message', '')}")
        return parser.parse_recommendation_list(data)

    async def _safe_call(self, fn, **kwargs):
        """兼容 get_self_info 可能的两种签名：``()`` 或 ``(credential=...)``。"""
        import inspect

        sig = inspect.signature(fn)
        if "credential" in sig.parameters:
            return await fn(credential=kwargs.get("credential"))
        try:
            return await fn()
        except TypeError:
            return await fn(kwargs.get("credential"))
