"""推送服务：把视频渲染为消息并推送到目标会话。

通过注入的 ``sender``（``async (umo, MessageChain) -> None``）发送，
与 AstrBot ``context.send_message`` 解耦，便于测试与替换。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..config import PluginConfig
from ..messaging import build_text_chain, build_video_chain, videos_list_text
from ..models import Binding, VideoInfo
from .video_service import VideoService

Sender = Callable[[str, object], Awaitable[None]]


class PushService:
    def __init__(self, video_service: VideoService, config: PluginConfig, sender: Sender) -> None:
        self._video = video_service
        self._config = config
        self._send = sender

    async def send_text(self, umo: str, text: str) -> None:
        await self._send(umo, build_text_chain(text))

    async def push_videos(self, umo: str, videos: list[VideoInfo]) -> int:
        """逐条推送视频（标题+链接+封面）。返回推送条数。"""
        count = 0
        for v in videos:
            await self._send(umo, build_video_chain(v, include_cover=self._config.include_cover))
            count += 1
        return count

    async def push_new_for_binding(self, binding: Binding) -> int:
        """检测并推送某绑定账号首页推荐中未推送过的视频，自动标记已推送。

        推荐流是动态的，为避免单次刷屏，每周期最多推送 ``push_max_per_cycle`` 条。
        返回实际推送条数。
        """
        if not binding.push_enabled:
            return 0
        videos = await self._video.fetch_latest(binding)
        new_videos = await self._video.detect_new(binding, videos)
        if not new_videos:
            return 0
        cap = max(1, self._config.push_max_per_cycle)
        to_push = new_videos[:cap]
        await self.push_videos(binding.umo, to_push)
        # 仅标记实际推送的，未推送的留待下次（若再次被推荐则补推）
        await self._video.mark_pushed(binding, [v.bvid for v in to_push])
        return len(to_push)

    async def push_random(self, binding: Binding) -> VideoInfo | None:
        """随机推送一条当前账号首页推荐视频。返回被推送的视频，无视频返回 None。"""
        videos = await self._video.fetch_latest(binding)
        picked = self._video.pick_random(videos)
        if picked is None:
            await self.send_text(binding.umo, f"账号 {binding.uname} 的首页推荐暂无可推送的视频。")
            return None
        await self._send(binding.umo, build_video_chain(picked, include_cover=self._config.include_cover))
        return picked

    async def show_videos(self, binding: Binding, n: int) -> str:
        """拉取当前账号首页推荐 n 条，返回可回复的纯文本（标题+链接）。不标记已推送。"""
        videos = await self._video.fetch_latest(binding, count=n)
        header = f"📦 {binding.uname} 的首页推荐（{len(videos)} 条）：\n"
        return videos_list_text(videos, header=header)
