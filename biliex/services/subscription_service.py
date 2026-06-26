"""订阅（绑定）服务：bind / unbind / list / switch / sub / unsub / 取激活绑定。

绑定维度 = ``owner_key``（形如 ``"{platform}:{sender_id}"``）：**按用户全局**，
私聊绑定一次即可在所有群/私聊通用，Cookie 无需在群里暴露。一个 owner 可绑定
多个 B 站账号，其中一个为「激活」。推送目标由 ``push_targets``（已订阅的会话 umo
列表）决定，通过 sub / unsub 在各会话里按需订阅。
"""

from __future__ import annotations

import time
import uuid

from ..bili.client import BilibiliClient
from ..bili.credential import parse_cookie
from ..errors import BindError, BindingNotFoundError, CredentialError, NoActiveBindingError
from ..models import Binding, CredentialInfo, Owner
from ..storage import Storage


def make_owner_key(platform: str, sender_id: str) -> str:
    """构造 owner_key：``{platform}:{sender_id}``（按用户全局）。"""
    return f"{platform}:{sender_id}"


class SubscriptionService:
    def __init__(self, storage: Storage, client: BilibiliClient) -> None:
        self._storage = storage
        self._client = client

    async def _get_or_create_owner(self, owner_key: str, sender_id: str, platform: str) -> Owner:
        owner = await self._storage.get_owner(owner_key)
        if owner is None:
            owner = Owner(owner_key=owner_key, sender_id=sender_id, platform=platform)
            await self._storage.upsert_owner(owner)
        return owner

    async def bind(
        self, owner_key: str, sender_id: str, platform: str, cookie: str, current_umo: str
    ) -> Binding:
        """解析 Cookie、验证账号、保存绑定，并自动把当前会话加入推送目标。返回该 Binding。"""
        try:
            cred = parse_cookie(cookie)
        except ValueError as e:
            raise CredentialError(str(e)) from e

        info = await self._client.get_self_account(cred)
        if not info.uid:
            raise BindError("无法从凭据获取账号 uid，请检查 Cookie 是否完整。")

        owner = await self._get_or_create_owner(owner_key, sender_id, platform)
        # 去重：同 owner 下同一 uid 不重复绑定
        for bid in owner.binding_ids:
            existing = await self._storage.get_binding(bid)
            if existing and existing.uid == info.uid:
                # 刷新凭据（用户重新粘贴 Cookie 通常是更新凭据）
                existing.credential = cred
                existing.uname = info.name or existing.uname
                if current_umo and current_umo not in existing.push_targets:
                    existing.push_targets.append(current_umo)
                await self._storage.upsert_binding(existing)
                return existing

        binding = Binding(
            binding_id=uuid.uuid4().hex[:8],
            owner_key=owner_key,
            uid=info.uid,
            uname=info.name or info.uid,
            credential=cred,
            push_enabled=True,
            push_targets=[current_umo] if current_umo else [],
            created_at=int(time.time()),
        )
        await self._storage.save_new_binding(owner, binding)
        return binding

    async def list_bindings(self, owner_key: str) -> list[Binding]:
        return await self._storage.list_bindings_for_owner(owner_key)

    async def get_active(self, owner_key: str) -> Binding:
        """取当前激活绑定；无则报 :class:`NoActiveBindingError`。"""
        owner = await self._storage.get_owner(owner_key)
        if not owner or not owner.binding_ids:
            raise NoActiveBindingError("尚未绑定任何哔哩哔哩账号，请先使用 /bili bind 绑定。")
        active_id = owner.active_binding_id or owner.binding_ids[0]
        binding = await self._storage.get_binding(active_id)
        if binding is None:
            # 激活态脏数据：回退到第一个
            binding = await self._storage.get_binding(owner.binding_ids[0])
            if binding is None:
                raise NoActiveBindingError("绑定数据异常，请重新绑定。")
            owner.active_binding_id = binding.binding_id
            await self._storage.upsert_owner(owner)
        return binding

    async def switch(self, owner_key: str, token: str | None) -> Binding:
        """按标识切换激活绑定。token 为空时切换到下一个。"""
        owner = await self._storage.get_owner(owner_key)
        if not owner or not owner.binding_ids:
            raise NoActiveBindingError("尚未绑定任何哔哩哔哩账号。")
        bindings = await self._storage.list_bindings_for_owner(owner_key)
        if not bindings:
            raise NoActiveBindingError("无可切换的账号。")

        target: Binding | None = None
        if token:
            target = _match_binding(bindings, token)
            if target is None:
                raise BindingNotFoundError(f"未找到匹配「{token}」的账号。")
        else:
            # 无参：切到当前激活的下一个
            cur = owner.active_binding_id
            idx = owner.binding_ids.index(cur) if cur in owner.binding_ids else -1
            nxt = owner.binding_ids[(idx + 1) % len(owner.binding_ids)]
            target = await self._storage.get_binding(nxt)

        owner.active_binding_id = target.binding_id
        await self._storage.upsert_owner(owner)
        return target

    async def unbind(self, owner_key: str, token: str | None) -> Binding:
        """按标识解绑；无参解绑当前激活。"""
        owner = await self._storage.get_owner(owner_key)
        if not owner or not owner.binding_ids:
            raise NoActiveBindingError("尚未绑定任何哔哩哔哩账号。")
        bindings = await self._storage.list_bindings_for_owner(owner_key)

        target: Binding | None = None
        if token:
            target = _match_binding(bindings, token)
            if target is None:
                raise BindingNotFoundError(f"未找到匹配「{token}」的账号。")
        else:
            target = await self._storage.get_binding(owner.active_binding_id or owner.binding_ids[0])
            if target is None:
                target = bindings[0]

        await self._storage.remove_binding(owner, target.binding_id)
        return target

    async def toggle_push(self, owner_key: str) -> tuple[Binding, bool]:
        """切换当前激活绑定的推送开关，返回 (binding, 新状态)。"""
        binding = await self.get_active(owner_key)
        binding.push_enabled = not binding.push_enabled
        await self._storage.upsert_binding(binding)
        return binding, binding.push_enabled

    async def subscribe(self, owner_key: str, umo: str) -> tuple[Binding, bool]:
        """把当前会话加入激活绑定的推送目标。返回 (binding, 是否新增)。"""
        binding = await self.get_active(owner_key)
        if umo in binding.push_targets:
            return binding, False
        binding.push_targets.append(umo)
        await self._storage.upsert_binding(binding)
        return binding, True

    async def unsubscribe(self, owner_key: str, umo: str) -> tuple[Binding, bool]:
        """把当前会话从激活绑定的推送目标移除。返回 (binding, 是否移除)。"""
        binding = await self.get_active(owner_key)
        if umo not in binding.push_targets:
            return binding, False
        binding.push_targets.remove(umo)
        await self._storage.upsert_binding(binding)
        return binding, True


def _match_binding(bindings: list[Binding], token: str) -> Binding | None:
    """按 binding_id / uid / uname 模糊匹配。"""
    token = token.strip()
    if not token:
        return None
    # 1) 精确 binding_id
    for b in bindings:
        if b.binding_id == token:
            return b
    # 2) 精确 uid
    for b in bindings:
        if b.uid == token:
            return b
    # 3) 名称包含
    for b in bindings:
        if token in b.uname:
            return b
    return None
