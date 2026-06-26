# astrbot_plugin_biliex

适用于 [AstrBot](https://docs.astrbot.app/)（消息平台为 OneBot / aiocqhttp）的哔哩哔哩账户插件。

## 功能

- **绑定 B 站账号**：通过登录 Cookie 绑定哔哩哔哩账号，bot 以该账号身份读取其**首页推荐流**（即打开 App/网页首页时被推荐给该账号的视频）。
  - **按用户全局绑定**：私聊 `/bili bind` 绑定一次，Cookie 不暴露在群里；该绑定在你所有的群和私聊里通用。
  - 一个用户可绑定多个 B 站账号，并支持切换「当前账号」。
- **按会话订阅推送**：`/bili sub` 把当前会话（群或私聊）加入当前账号的推送目标，`/bili unsub` 移除。新视频只推到已订阅的会话。群成员各自私聊绑定 + 在群里 `/bili sub`，互不干扰。
- **自动推送首页推荐**：后台定时拉取已绑定账号的首页推荐流，把未推送过的视频自动推送到该账号所有已订阅会话（标题 + 链接 + 封面图）。推荐流是动态的，已推送过的视频不会重复推送，且每周期每会话最多推送 `push_max_per_cycle` 条以防刷屏。
- **随机推送**：`/bili random` 从当前账号首页推荐中随机推送一条。
- **AI 标题总结**：`/bili summary` 依据视频标题对当前首页推荐进行 AI 总结（走 AstrBot 配置的 LLM Provider）。

## 安装

将本插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_biliex/`，重启或在 WebUI 插件管理处重载。
依赖 `bilibili-api-python` 会随插件自动安装；异步 http 后端由 AstrBot 自带的 `httpx`/`aiohttp` 提供。

## 获取 Cookie

1. 浏览器登录 [bilibili.com](https://www.bilibili.com)。
2. 打开开发者工具（F12）→ Network → 任选一个请求 → Headers → 复制 `Cookie` 字段。
3. Cookie 形如：`SESSDATA=xxx; bili_jct=yyy; buVID3=zzz; DedeUserID=123456; ...`

> ⚠️ **隐私安全**：Cookie 即账号登录凭据。**请在私聊中**执行绑定；若在群内绑定，bot 会尽力撤回含 Cookie 的消息（仅 aiocqhttp 生效），但仍建议私聊操作。

## 指令

所有指令挂载在 `/bili` 指令组下：

| 指令 | 说明 |
|---|---|
| `/bili bind` | 交互式绑定向导（建议私聊执行，Cookie 不暴露）。 |
| `/bili unbind [标识]` | 解绑。无参数解绑当前账号；标识可为 uid / 名称 / 绑定 id。 |
| `/bili list` | 列出本人已绑定的账号及其推送目标数。 |
| `/bili switch [标识]` | 切换「当前账号」。 |
| `/bili sub` | 把当前会话加入当前账号的推送目标（群/私聊皆可）。 |
| `/bili unsub` | 把当前会话移出当前账号的推送目标。 |
| `/bili videos [n]` | 查看当前账号首页推荐 n 条（默认 5）。 |
| `/bili random` | 从当前账号首页推荐中随机推送一条。 |
| `/bili summary [n]` | 对当前账号首页推荐 n 条按标题做 AI 总结（默认 20）。 |
| `/bili push` | 手动触发当前账号的首页推荐检测与推送。 |
| `/bili toggle` | 开关当前账号的自动推送。 |
| `/bili help` | 查看帮助。 |

「标识」用于 unbind / switch，支持 uid、账号名称、绑定 id 的模糊匹配。

> 群成员安全绑定流程：① 私聊 bot 执行 `/bili bind`（Cookie 不暴露）；② 在群里执行 `/bili sub` 订阅推送。每位成员各自独立。

## AI 对话触发（LLM 工具）

本插件已把以下能力注册为 AstrBot 的 LLM 工具，**无需指令**，直接用自然语言对话即可触发（需在 AstrBot 中配置好 LLM Provider 并启用函数调用）：

- 「给我推送一个b站首页视频」→ 工具返回一条随机首页推荐的标题与链接，由模型用自然语言推荐给你
- 「我首页推荐了什么 / 总结一下我的首页推荐」→ 工具返回首页推荐列表，模型据此归纳
- 「我绑定了哪些B站账号 / 当前是哪个账号」→ 列出绑定账号
- 「切换到 xxx 账号」→ 切换当前激活账号
- 「把B站推送发到这个群 / 这个群别推了」→ 订阅 /退订当前会话的推送

对话场景下工具只把结果交给模型、由模型组织回复，**不会**额外推送卡片或生硬的提示文字（避免重复）。如果你想要带封面的视频卡片，用 `/bili random` 指令。

## 配置

在 WebUI 插件配置页可调整：推送开关、检测间隔、获取视频数、已推送记录上限、单次推送上限、总结视频数上限、总结语言、是否含封面图、是否撤回 Cookie 消息、接口超时、HTTP 代理等。

## 架构

分层设计，`bilibili-api` 的调用被隔离在 `biliex/bili/` 子包内（仅 `client.py` 引用 `bilibili_api`，`parser.py` 用防御式解析把原始返回映射为稳定模型）。若 B 站接口字段或 `bilibili-api` 库发生变更，通常只需改动这两个文件，其余业务代码不受影响。详见 `biliex/` 目录。
