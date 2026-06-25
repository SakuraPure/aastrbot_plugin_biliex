"""视频服务：拉取首页推荐、检测未推送项、随机抽取、标记已推送。"""

from __future__ import annotations

import secrets

from ..bili.client import BilibiliClient
from ..config import PluginConfig
from ..models import Binding, VideoInfo
from ..storage import Storage


class VideoService:
    def __init__(self, client: BilibiliClient, storage: Storage, config: PluginConfig) -> None:
        self._client = client
        self._storage = storage
        self._config = config

    async def fetch_latest(self, binding: Binding, count: int | None = None) -> list[VideoInfo]:
        """拉取绑定账号的首页推荐流视频（打开 App/网页首页被推荐的视频）。"""
        n = count or self._config.fetch_count
        return await self._client.get_recommendations(binding.credential, n)

    async def detect_new(self, binding: Binding, videos: list[VideoInfo]) -> list[VideoInfo]:
        """从视频列表中筛出未推送过的新视频。

        按发布时间正序返回（旧的先推，避免乱序推送）。
        """
        pushed = set(binding.pushed_bvids)
        new_videos = [v for v in videos if v.bvid and v.bvid not in pushed]
        # pubdate 升序：早发布先推；pubdate 相同时保持接口顺序
        new_videos.sort(key=lambda v: (v.pubdate or 0))
        return new_videos

    def pick_random(self, videos: list[VideoInfo]) -> VideoInfo | None:
        """从给定视频列表中随机抽一条。"""
        if not videos:
            return None
        idx = secrets.randbelow(len(videos))
        return videos[idx]

    async def mark_pushed(self, binding: Binding, bvids: list[str]) -> None:
        """把 bvids 记入已推送集合，按上限淘汰旧记录。"""
        cap = self._config.pushed_history_size
        pushed = list(binding.pushed_bvids)
        for bvid in bvids:
            if not bvid:
                continue
            if bvid in pushed:
                pushed.remove(bvid)
            pushed.append(bvid)
        # 超出上限：淘汰最早记入的
        if len(pushed) > cap:
            pushed = pushed[-cap:]
        binding.pushed_bvids = pushed
        if bvids:
            binding.last_bvid = bvids[-1]
        await self._storage.upsert_binding(binding)
