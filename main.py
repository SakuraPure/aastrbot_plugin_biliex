"""astrbot_plugin_biliex 插件入口。

Star 插件类装配各服务并注册 ``/bili`` 指令组。handler 保持薄层，
只做「事件上下文 → owner_key / umo」转换与异常 → 友好提示的包装，
业务逻辑全部委托给 :mod:`biliex.services`。
"""

from __future__ import annotations

import asyncio
import os
import sys

# 把插件自身目录加入 sys.path，使 biliex 子包可被正常导入。
# AstrBot 加载插件时不会自动将插件目录加入 sys.path。
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from biliex.bili.client import BilibiliClient
from biliex.config import PluginConfig
from biliex.errors import BiliExError
from biliex.messaging import video_components
from biliex.models import Binding
from biliex.scheduler import PushScheduler
from biliex.security import recall_message
from biliex.services.push_service import PushService
from biliex.services.subscription_service import SubscriptionService, make_owner_key
from biliex.services.summary_service import LLMSummarizer
from biliex.services.video_service import VideoService
from biliex.storage import KvBackend, Storage


class _KvAdapter(KvBackend):
    """把 Star 实例的 KV 方法适配为 Storage 需要的 KvBackend 接口。"""

    def __init__(self, star: "BiliExPlugin") -> None:
        self._star = star

    async def get(self, key: str, default: Any = None) -> Any:
        return await self._star.get_kv_data(key, default)

    async def put(self, key: str, value: Any) -> None:
        await self._star.put_kv_data(key, value)

    async def delete(self, key: str) -> None:
        await self._star.delete_kv_data(key)


class BiliExPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = PluginConfig(config)

        kv = _KvAdapter(self)
        self._storage = Storage(kv)
        self._client = BilibiliClient(timeout=self._config.request_timeout, proxy=self._config.proxy)
        self._video_service = VideoService(self._client, self._storage, self._config)
        self._push = PushService(self._video_service, self._config, self._send_message)
        self._sub = SubscriptionService(self._storage, self._client)
        self._summarizer = LLMSummarizer(self._config, self._resolve_provider)
        self._scheduler = PushScheduler(self._storage, self._push, self._config)

        # 启动后台推送（on_astrbot_loaded 会再保险一次，start 自带幂等）
        if self._config.push_enabled:
            self._scheduler.start()

    # --- AstrBot 生命周期 ---
    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """AstrBot 初始化完成后确保调度器运行（平台就绪后再启动更稳妥）。"""
        if self._config.push_enabled:
            self._scheduler.start()

    async def terminate(self) -> None:
        """插件卸载/停用时停止后台任务。"""
        await self._scheduler.stop()

    # --- 注入到服务的适配器 ---
    async def _send_message(self, umo: str, chain: Any) -> None:
        """主动推送适配器：供 PushService 发送到目标会话。"""
        await self.context.send_message(umo, chain)

    async def _resolve_provider(self, umo: str) -> Any:
        """LLM Provider 解析器：供 LLMSummarizer 取当前会话的 Provider。"""
        try:
            return self.context.get_using_provider(umo=umo)
        except Exception:
            return None

    # --- 事件上下文工具 ---
    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            sid = event.get_sender_id()
            if sid:
                return str(sid)
        except Exception:
            pass
        try:
            return str(event.message_obj.sender.user_id)
        except Exception:
            return ""

    def _owner_key(self, event: AstrMessageEvent) -> str:
        # 按用户全局：platform:sender_id。私聊绑定一次，所有群/私聊通用。
        return make_owner_key(event.get_platform_name(), self._sender_id(event))

    async def _active_binding(self, event: AstrMessageEvent) -> Binding:
        return await self._sub.get_active(self._owner_key(event))

    # ==================== /bili 指令组 ====================
    @filter.command_group("bili", alias={"B站", "bilibili"})
    def bili(self) -> None:
        """哔哩哔哩账户绑定与视频推送。子指令见 /bili help。"""
        pass

    @bili.command("help", alias={"帮助"})
    async def bili_help(self, event: AstrMessageEvent) -> Any:
        """查看帮助"""
        yield event.plain_result(
            "哔哩哔哩推送 指令：\n"
            "/bili bind  交互式绑定账号（建议私聊执行，Cookie 不暴露）\n"
            "/bili unbind [标识]  解绑（无参解绑当前）\n"
            "/bili list  列出已绑定账号及其推送目标\n"
            "/bili switch [标识]  切换当前账号\n"
            "/bili sub  把当前会话加入推送目标（群/私聊皆可）\n"
            "/bili unsub  把当前会话移出推送目标\n"
            "/bili videos [n]  查看首页推荐 n 条（默认5）\n"
            "/bili random  随机推送一条首页推荐\n"
            "/bili summary [n]  AI 总结首页推荐标题（默认20）\n"
            "/bili push  手动触发首页推荐检测推送\n"
            "/bili toggle  开关当前账号自动推送\n"
            "绑定按用户全局，私聊绑定后在任意群 /bili sub 即可推送。标识可为 uid / 名称 / 绑定 id。"
        )

    @bili.command("bind", alias={"绑定"})
    async def bili_bind(self, event: AstrMessageEvent) -> Any:
        """交互式绑定 B 站账号（按用户全局，建议私聊执行）"""
        if event.get_group_id():
            warn = "⚠️ 你在群聊中绑定，Cookie 会被群成员看到（bot 会尝试撤回，仍建议私聊执行 /bili bind）。\n"
        else:
            warn = ""
        yield event.plain_result(
            warn + "请发送你的哔哩哔哩登录 Cookie（含 SESSDATA 等），120 秒内有效。发送「取消」放弃。"
        )

        owner_key = self._owner_key(event)
        umo = event.unified_msg_origin
        sender_id = self._sender_id(event)
        platform = event.get_platform_name()

        @session_waiter(timeout=120, record_history_chains=False)
        async def bind_waiter(controller: SessionController, ev: AstrMessageEvent) -> None:
            msg = (ev.message_str or "").strip()
            if msg in ("取消", "cancel", "退出"):
                await ev.send(ev.plain_result("已取消绑定。"))
                controller.stop()
                return
            # 只接受含 Cookie 特征的消息（含 = 且含已知字段名）。
            # 这样可忽略抢先进入等待器的无关消息（如其它插件的主动消息），避免误判。
            msg_lower = msg.lower()
            looks_like_cookie = "=" in msg and any(
                k in msg_lower for k in ("sessdata", "bili_jct", "buvid3", "dedeuserid")
            )
            if not looks_like_cookie:
                await ev.send(
                    ev.plain_result(
                        "未识别到 Cookie，请发送包含 SESSDATA/bili_jct 等字段的完整 Cookie，或发送「取消」。"
                    )
                )
                controller.keep(timeout=120, reset_timeout=True)
                return
            # 撤回含 Cookie 的消息，防泄露
            if self._config.delete_credential_msg:
                await recall_message(ev, ev.message_obj.message_id)
            try:
                binding = await self._sub.bind(owner_key, sender_id, platform, msg, umo)
                await ev.send(
                    ev.plain_result(
                        f"✅ 绑定成功：{binding.uname}（uid: {binding.uid}）\n"
                        f"已自动把当前会话加入推送目标。在其它群/私聊可用 /bili sub 订阅推送。"
                    )
                )
            except BiliExError as e:
                await ev.send(
                    ev.plain_result(f"❌ 绑定失败：{e}（收到内容长度 {len(msg)}）")
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"biliex bind: {e}")
                await ev.send(ev.plain_result(f"❌ 发生错误：{e}"))
            finally:
                controller.stop()

        try:
            await bind_waiter(event)
        except TimeoutError:
            yield event.plain_result("⏱ 绑定超时，请重新执行 /bili bind。")
        finally:
            event.stop_event()

    @bili.command("unbind", alias={"解绑"})
    async def bili_unbind(self, event: AstrMessageEvent, token: str = "") -> Any:
        """解绑账号"""
        try:
            b = await self._sub.unbind(self._owner_key(event), token or None)
            yield event.plain_result(f"✅ 已解绑：{b.uname}（uid: {b.uid}）")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex unbind: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("list", alias={"列表"})
    async def bili_list(self, event: AstrMessageEvent) -> Any:
        """列出已绑定账号及其推送目标"""
        try:
            bindings = await self._sub.list_bindings(self._owner_key(event))
            if not bindings:
                yield event.plain_result("尚未绑定任何账号。使用 /bili bind 绑定。")
                return
            active = await self._sub.get_active(self._owner_key(event))
            cur_umo = event.unified_msg_origin
            lines = ["📋 已绑定账号："]
            for b in bindings:
                mark = "（当前）" if b.binding_id == active.binding_id else ""
                push = "推送开" if b.push_enabled else "推送关"
                tgt = len(b.push_targets)
                here = "，本会话已订阅" if cur_umo in b.push_targets else ""
                lines.append(f"- {b.uname}（uid: {b.uid}）{mark} [{push}] 推送目标 {tgt} 个{here}")
            yield event.plain_result("\n".join(lines))
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex list: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("switch", alias={"切换"})
    async def bili_switch(self, event: AstrMessageEvent, token: str = "") -> Any:
        """切换当前账号"""
        try:
            b = await self._sub.switch(self._owner_key(event), token or None)
            yield event.plain_result(f"✅ 已切换到：{b.uname}（uid: {b.uid}）")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex switch: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("sub", alias={"订阅"})
    async def bili_sub(self, event: AstrMessageEvent) -> Any:
        """把当前会话加入当前账号的推送目标"""
        try:
            b, added = await self._sub.subscribe(self._owner_key(event), event.unified_msg_origin)
            if added:
                yield event.plain_result(f"✅ 已把本会话加入 {b.uname} 的推送目标（共 {len(b.push_targets)} 个）。")
            else:
                yield event.plain_result(f"本会话已在 {b.uname} 的推送目标中，无需重复订阅。")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex sub: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("unsub", alias={"退订"})
    async def bili_unsub(self, event: AstrMessageEvent) -> Any:
        """把当前会话移出当前账号的推送目标"""
        try:
            b, removed = await self._sub.unsubscribe(self._owner_key(event), event.unified_msg_origin)
            if removed:
                yield event.plain_result(f"✅ 已把本会话移出 {b.uname} 的推送目标（剩余 {len(b.push_targets)} 个）。")
            else:
                yield event.plain_result(f"本会话不在 {b.uname} 的推送目标中。")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex unsub: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("videos", alias={"视频"})
    async def bili_videos(self, event: AstrMessageEvent, n: int = 5) -> Any:
        """查看当前账号首页推荐"""
        try:
            b = await self._active_binding(event)
            text = await self._push.show_videos(b, n)
            yield event.plain_result(text)
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex videos: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("random", alias={"随机"})
    async def bili_random(self, event: AstrMessageEvent) -> Any:
        """随机推送一条当前账号首页推荐"""
        try:
            b = await self._active_binding(event)
            videos = await self._video_service.fetch_latest(b)
            picked = self._video_service.pick_random(videos)
            if picked is None:
                yield event.plain_result(f"账号 {b.uname} 的主页暂无视频。")
                return
            result = event.make_result()
            result.chain = video_components(picked, self._config.include_cover)
            yield result
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex random: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("summary", alias={"总结"})
    async def bili_summary(self, event: AstrMessageEvent, n: int = 20) -> Any:
        """AI 总结当前账号首页推荐标题"""
        try:
            b = await self._active_binding(event)
            videos = await self._video_service.fetch_latest(b, count=n)
            text = await self._summarizer.summarize(videos, event.unified_msg_origin)
            yield event.plain_result(f"📝 {b.uname} 首页推荐总结：\n\n{text}")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex summary: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("push", alias={"推送"})
    async def bili_push(self, event: AstrMessageEvent) -> Any:
        """手动触发首页推荐检测推送（当前账号）"""
        try:
            b = await self._active_binding(event)
            n = await self._push.push_new_for_binding(b)
            if n > 0:
                yield event.plain_result(f"✅ 已推送 {n} 条首页推荐（{b.uname}）到本会话。")
            else:
                yield event.plain_result(f"暂无新的首页推荐（{b.uname}）。")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex push: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    @bili.command("toggle", alias={"开关"})
    async def bili_toggle(self, event: AstrMessageEvent) -> Any:
        """开关当前账号的自动推送"""
        try:
            b, enabled = await self._sub.toggle_push(self._owner_key(event))
            state = "开启" if enabled else "关闭"
            yield event.plain_result(f"✅ 已{state} {b.uname} 的自动推送。")
        except BiliExError as e:
            yield event.plain_result(str(e))
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex toggle: {e}")
            yield event.plain_result(f"❌ 发生错误：{e}")

    # ==================== LLM 工具（AI 对话可直接调用） ====================
    # 通过 @filter.llm_tool 暴露给 AstrBot 的 LLM，用户用自然语言即可触发。
    #
    # 重要：工具内 yield 的是【纯字符串】而非 event.plain_result(...)。
    # 在 AstrBot 中，llm_tool yield 字符串 → 作为工具结果回传给模型（不发给用户）；
    # yield MessageEventResult → 直接发给用户。这里要让模型用自己的话组织回复，
    # 所以一律 yield 纯字符串，由模型完成对话输出，工具不主动发送任何消息。

    @filter.llm_tool(name="bili_push_random_video")
    async def tool_push_random_video(self, event: AstrMessageEvent) -> Any:
        '''从当前绑定账号的哔哩哔哩首页推荐流中随机抽取一条视频，返回其标题与链接。由你（AI）用自然语言把这个视频推荐给用户，在回复中包含标题和链接。

        当用户想要看一个B站视频、随机推送B站视频、推送首页推荐视频、推一个视频时调用此工具。
        '''
        try:
            b = await self._active_binding(event)
            videos = await self._video_service.fetch_latest(b)
            picked = self._video_service.pick_random(videos)
            if picked is None:
                yield f"账号 {b.uname} 的首页推荐暂无视频。请把这一情况告诉用户。"
                return
            yield (
                f"已从账号 {b.uname} 的首页推荐中随机挑选一条视频：\n"
                f"标题：{picked.title}\n"
                f"链接：{picked.url}\n"
                f"请用自然语言向用户推荐这个视频，回复中包含标题和链接。"
            )
        except BiliExError as e:
            yield f"操作失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_push_random_video: {e}")
            yield f"操作失败：{e}。请把失败原因告诉用户。"

    @filter.llm_tool(name="bili_get_home_recommendations")
    async def tool_get_home_recommendations(self, event: AstrMessageEvent) -> Any:
        '''获取当前绑定账号的哔哩哔哩首页推荐视频列表（标题+链接），返回给由你（AI）。你可以据此回答用户「首页推荐了什么」，或对其进行归纳总结。

        当用户问「我首页推荐了什么」「B站给我推荐了什么视频」「总结一下我的首页推荐」「我首页有哪些视频」时调用此工具。
        '''
        try:
            b = await self._active_binding(event)
            text = await self._push.show_videos(b, self._config.fetch_count)
            yield text
        except BiliExError as e:
            yield f"获取失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_get_home_recommendations: {e}")
            yield f"获取失败：{e}。请把失败原因告诉用户。"

    @filter.llm_tool(name="bili_list_bound_accounts")
    async def tool_list_bound_accounts(self, event: AstrMessageEvent) -> Any:
        '''列出当前用户已绑定的所有哔哩哔哩账号（含 uid、名称、是否为当前账号、推送开关、推送目标数），返回给你（AI）。

        当用户问「我绑定了哪些B站账号」「有几个账号」「当前是哪个账号」「推送到哪」时调用此工具。
        '''
        try:
            bindings = await self._sub.list_bindings(self._owner_key(event))
            if not bindings:
                yield "当前用户尚未绑定任何哔哩哔哩账号。请引导用户使用 /bili bind 绑定账号。"
                return
            active = await self._sub.get_active(self._owner_key(event))
            cur_umo = event.unified_msg_origin
            lines = ["已绑定账号："]
            for b in bindings:
                mark = "（当前）" if b.binding_id == active.binding_id else ""
                push = "推送开" if b.push_enabled else "推送关"
                here = "，本会话已订阅" if cur_umo in b.push_targets else ""
                lines.append(f"- {b.uname}（uid: {b.uid}）{mark} [{push}] 推送目标 {len(b.push_targets)} 个{here}")
            yield "\n".join(lines)
        except BiliExError as e:
            yield f"查询失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_list_bound_accounts: {e}")
            yield f"查询失败：{e}。请把失败原因告诉用户。"

    @filter.llm_tool(name="bili_switch_account")
    async def tool_switch_account(self, event: AstrMessageEvent, token: str) -> Any:
        '''切换当前激活的哔哩哔哩账号。后续的推送/查询操作都会作用于切换后的账号。

        Args:
            token(string): 目标账号的标识，可为 uid、账号名称或绑定 id
        '''
        try:
            b = await self._sub.switch(self._owner_key(event), token)
            yield f"已切换到账号：{b.uname}（uid: {b.uid}）。请把切换结果告诉用户。"
        except BiliExError as e:
            yield f"切换失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_switch_account: {e}")
            yield f"切换失败：{e}。请把失败原因告诉用户。"

    @filter.llm_tool(name="bili_subscribe_push")
    async def tool_subscribe_push(self, event: AstrMessageEvent) -> Any:
        '''把当前会话（群或私聊）加入当前激活账号的推送目标。之后该账号的首页推荐新视频会推送到本会话。无需 Cookie。

        当用户说「把B站推送发到这个群」「这个群也要接收推送」「在这里订阅推送」时调用此工具。
        '''
        try:
            b, added = await self._sub.subscribe(self._owner_key(event), event.unified_msg_origin)
            if added:
                yield f"已把当前会话加入 {b.uname} 的推送目标（共 {len(b.push_targets)} 个）。请把结果告诉用户。"
            else:
                yield f"当前会话已在 {b.uname} 的推送目标中。请把结果告诉用户。"
        except BiliExError as e:
            yield f"订阅失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_subscribe_push: {e}")
            yield f"订阅失败：{e}。请把失败原因告诉用户。"

    @filter.llm_tool(name="bili_unsubscribe_push")
    async def tool_unsubscribe_push(self, event: AstrMessageEvent) -> Any:
        '''把当前会话从当前激活账号的推送目标中移除。之后该账号的新视频不再推送到本会话。

        当用户说「这个群别推了」「取消这个群的推送」「这里别再发B站推送」时调用此工具。
        '''
        try:
            b, removed = await self._sub.unsubscribe(self._owner_key(event), event.unified_msg_origin)
            if removed:
                yield f"已把当前会话移出 {b.uname} 的推送目标（剩余 {len(b.push_targets)} 个）。请把结果告诉用户。"
            else:
                yield f"当前会话不在 {b.uname} 的推送目标中。请把结果告诉用户。"
        except BiliExError as e:
            yield f"退订失败：{e}。请把失败原因告诉用户。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"biliex tool_unsubscribe_push: {e}")
            yield f"退订失败：{e}。请把失败原因告诉用户。"
