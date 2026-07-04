"""后台定时推送调度器。

一个 asyncio 后台循环：按配置间隔枚举所有绑定，检测并推送新视频，逐项容错。
未来新增定时任务可在此注册更多「job」，调度框架本身无需改动。
"""

from __future__ import annotations

import asyncio
import random

from astrbot.api import logger  # AstrBot 规定的 logger 接口

from .config import PluginConfig
from .services.push_service import PushService
from .storage import Storage


class PushScheduler:
    def __init__(self, storage: Storage, push_service: PushService, config: PluginConfig) -> None:
        self._storage = storage
        self._push = push_service
        self._config = config
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="biliex-push-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover
            logger.warning(f"biliex: 调度器停止时出现异常：{e}")

    def _next_interval(self) -> int:
        """计算下一轮等待时长：不定时模式下在 [min, max] 内随机，否则用固定间隔。"""
        if self._config.push_random_enabled:
            return random.randint(self._config.push_interval_min, self._config.push_interval_max)
        return self._config.push_interval

    async def _run(self) -> None:
        logger.info("biliex: 推送调度器已启动。")
        # 启动后先等待一个间隔，避免开机即打接口
        while not self._stopped.is_set():
            interval = self._next_interval()
            if self._config.push_random_enabled:
                logger.debug(f"biliex: 不定时推送，本轮 {interval} 秒后检测。")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stopped.is_set():
                break
            if not self._config.push_enabled:
                continue
            await self._tick()
        logger.info("biliex: 推送调度器已停止。")

    async def _tick(self) -> None:
        try:
            bindings = await self._storage.iter_all_bindings()
        except Exception as e:
            logger.warning(f"biliex: 枚举绑定失败：{e}")
            return
        if not bindings:
            return
        for binding in bindings:
            if self._stopped.is_set():
                break
            try:
                await self._push.push_new_for_binding(binding)
            except Exception as e:
                # 单个绑定出错不阻断整体循环
                logger.warning(f"biliex: 推送绑定 {binding.uname}({binding.uid}) 时出错：{e}")

    async def run_once(self) -> int:
        """手动触发一次全量检测推送（/bili push 全局态）。返回处理的绑定数。"""
        try:
            bindings = await self._storage.iter_all_bindings()
        except Exception as e:
            logger.warning(f"biliex: 枚举绑定失败：{e}")
            return 0
        n = 0
        for binding in bindings:
            try:
                await self._push.push_new_for_binding(binding)
                n += 1
            except Exception as e:
                logger.warning(f"biliex: 推送绑定 {binding.uname}({binding.uid}) 时出错：{e}")
        return n
