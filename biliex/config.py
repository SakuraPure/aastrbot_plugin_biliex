"""类型化配置访问。

``_conf_schema.json`` 的消费集中在此一处：业务代码只通过 :class:`PluginConfig` 取值，
新增配置项时只需改 schema 与本类，调用方无感。
"""

from __future__ import annotations

from typing import Any

from .errors import ConfigError


class PluginConfig:
    """对 AstrBotConfig（dict-like）的类型化封装，带默认值兜底。"""

    def __init__(self, config: dict[str, Any] | None) -> None:
        self._raw: dict[str, Any] = dict(config or {})

    # --- 内部取值工具 ---
    def _get(self, key: str, default: Any) -> Any:
        val = self._raw.get(key, default)
        return default if val is None else val

    def _get_bool(self, key: str, default: bool) -> bool:
        val = self._get(key, default)
        try:
            return bool(val)
        except Exception:
            return default

    def _get_int(self, key: str, default: int) -> int:
        val = self._get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            raise ConfigError(f"配置项 {key} 必须是整数，当前值：{val!r}")

    def _get_str(self, key: str, default: str) -> str:
        val = self._get(key, default)
        return str(val) if val is not None else default

    # --- 业务配置 ---
    @property
    def push_enabled(self) -> bool:
        return self._get_bool("push_enabled", True)

    @property
    def push_interval(self) -> int:
        return max(60, self._get_int("push_interval", 1800))

    @property
    def push_random_enabled(self) -> bool:
        """不定时推送：每轮检测间隔在 [min, max] 内随机取值。"""
        return self._get_bool("push_random_enabled", False)

    @property
    def push_interval_min(self) -> int:
        return max(60, self._get_int("push_interval_min", 1200))

    @property
    def push_interval_max(self) -> int:
        return max(self.push_interval_min, self._get_int("push_interval_max", 3600))

    @property
    def fetch_count(self) -> int:
        return max(1, min(50, self._get_int("fetch_count", 10)))

    @property
    def pushed_history_size(self) -> int:
        return max(10, self._get_int("pushed_history_size", 50))

    @property
    def push_max_per_cycle(self) -> int:
        """每次定时检测最多推送多少条推荐视频，防止刷屏。"""
        return max(1, min(20, self._get_int("push_max_per_cycle", 5)))

    @property
    def summary_max_videos(self) -> int:
        return max(1, min(100, self._get_int("summary_max_videos", 20)))

    @property
    def summary_language(self) -> str:
        return self._get_str("summary_language", "中文")

    @property
    def include_cover(self) -> bool:
        return self._get_bool("include_cover", True)

    @property
    def delete_credential_msg(self) -> bool:
        return self._get_bool("delete_credential_msg", True)

    @property
    def request_timeout(self) -> int:
        return max(3, self._get_int("request_timeout", 15))

    @property
    def proxy(self) -> str:
        return self._get_str("proxy", "").strip()

    def reload(self, config: dict[str, Any] | None) -> None:
        """配置变更后重新装载（如 WebUI 修改配置后插件重载）。"""
        self._raw = dict(config or {})
