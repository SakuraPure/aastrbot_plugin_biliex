"""推送服务：把视频渲染为消息并推送到目标会话。

通过注入的 ``sender``（``async (umo, MessageChain) -> None``）发送，
与 AstrBot ``context.send_message`` 解耦，便于测试与替换。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..config import PluginConfig
from ..messaging import build_video_chain, videos_list_text
from ..models import Binding, VideoInfo
from .video_service import VideoService

Sender = Callable[[str, object], Awaitable[None]]


class PushService:
    def __init__(self, video_service: VideoService, config: PluginConfig, sender: Sender) -> None:
        self._video = video_service
        self._config = config
        self._send = sender

    async def push_videos(self, umo: str, videos: list[VideoInfo]) -> int:
        """逐条推送视频（标题+链接+封面）到指定会话。返回推送条数。"""
        count = 0
        for v in videos:
            await self._send(umo, build_video_chain(v, include_cover=self._config.include_cover))
            count += 1
        return count

    async def push_new_for_binding(self, binding: Binding) -> int:
        """检测并推送某绑定账号首页推荐中未推送过的视频，自动标记已推送。

        推送到该绑定所有已订阅的会话（``push_targets``）。推荐流是动态的，为避免单次
        刷屏，每周期每会话最多推送 ``push_max_per_cycle`` 条。返回每个会话推送的条数
        （取第一次会话的计数；无订阅会话返回 0）。
        """
        if not binding.push_enabled or not binding.push_targets:
            return 0
        videos = await self._video.fetch_latest(binding)
        new_videos = await self._video.detect_new(binding, videos)
        if not new_videos:
            return 0
        cap = max(1, self._config.push_max_per_cycle)
        to_push = new_videos[:cap]
        pushed = 0
        for umo in binding.push_targets:
            try:
                pushed = await self.push_videos(umo, to_push)
            except Exception as e:  # noqa: BLE001 - 单个会话失败不影响其它
                from astrbot.api import logger

                logger.warning(f"biliex: 推送到会话 {umo} 失败：{e}")
        # 仅标记实际推送的，未推送的留待下次（若再次被推荐则补推）
        await self._video.mark_pushed(binding, [v.bvid for v in to_push])
        return pushed

    async def show_videos(self, binding: Binding, n: int) -> str:
        """拉取当前账号首页推荐 n 条，返回可回复的纯文本（标题+链接）。不标记已推送。"""
        videos = await self._video.fetch_latest(binding, count=n)
        header = f"📦 {binding.uname} 的首页推荐（{len(videos)} 条）：\n"
        return videos_list_text(videos, header=header)
