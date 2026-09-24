"""AstrBot MC 白名单管理系统（多服安全版）——插件入口。

实现依据：
    文档：docs/开发文档_v0.3.md
    契约：docs/接口契约冻结_v0.3.md（协议部分与之冲突时以契约文件为准）

指令一览（文档 §十三）：
    /注册 [正|皮] [游戏名]   绑定（默认用群昵称）
    /注销                    解绑
    /迁移 皮转正|正转皮       迁移绑定
    /更新昵称                重新读取群昵称
    /info [服务器名|编号]     统计图片（纯图片回复）
    /black add|remove|list   黑名单管理
    /sync [服务器名|编号]     手动同步
    /status                  群服互联状态
    /mc <服务器名|编号> <内容> 定向发送到某个服务器
    /mcwl                    帮助
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot >= 4.5
    from astrbot.api.event.filter import EventMessageType
except Exception:  # pragma: no cover
    from astrbot.core.star.filter.event_message_type import EventMessageType

try:
    from astrbot.api.message_components import Image
except Exception:  # pragma: no cover - 老版本路径
    from astrbot.core.message.components import Image  # type: ignore

try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 测试/老内核环境

    class GreedyStr(str):  # type: ignore[no-redef]
        """占位：老内核没有 GreedyStr，退化为普通字符串参数。"""


from .core.protocol import PROTO_VERSION, now_iso
from .core.version import PLUGIN_VERSION
from .data_manager import DataManager
from .interop.client import InteropHooks, ServerLink, ServerManager
from .interop.convert import message_to_text
from .services.diagnose import WsConnectivityTester
from .services.perm import (
    NODE_BLACKLIST,
    NODE_INTEROP,
    NODE_REGISTER,
    NODE_STATS,
    NODE_SYNC,
    PermissionService,
)
from .services.stats_image import (
    list_backgrounds,
    plugin_data_root,
    render_stats_image,
    resolve_background_files,
)
from .services.uuid import (
    TYPE_LITTLESKIN,
    TYPE_MOJANG,
    UUIDService,
    parse_type,
    type_label,
    validate_username,
)

PLUGIN_NAME = "astrbot_plugin_mc_whitelist"
PLUGIN_DIR = Path(__file__).resolve().parent

HELP_TEXT = """📖 MC白名单管理系统 v{version}

【绑定】
/注册 正        绑定正版账号（用当前群昵称）
/注册 皮        绑定皮肤站角色（用当前群昵称）
/注册 正 <名字>  指定游戏名绑定
/注销           解绑
/迁移 皮转正     皮 → 正
/迁移 正转皮     正 → 皮
/更新昵称        群昵称改了之后重新同步

【查询】
/info [服务器]   查看本人在各服的统计（图片）
/status         群服互联状态（管理员）
/mc <服务器> <内容>  定向发送到某个服务器

【管理】（管理员）
/black add|remove <QQ或@>
/black list
/sync [服务器]   手动同步白名单

服务器可用名字或编号（编号 = 配置顺序，跳过已禁用项）"""


@register(
    PLUGIN_NAME,
    "MC白名单管理系统（多服安全版）",
    "昵称直连、多服汇聚、AES加密、前缀验证、群服互联",
    PLUGIN_VERSION,
)
class MCWhitelistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 保持对内核配置对象的**引用**，不拷贝：AstrBotConfig 是 dict 子类，WebUI 保存配置
        # 走的是原地 update()，引用保持不变就能立刻读到新值（上传背景图后不用重载插件）。
        if isinstance(config, dict):
            self._config: dict[str, Any] = config
        elif config is not None:
            try:
                self._config = dict(config)
            except Exception:  # noqa: BLE001 - 非常规配置对象，退化为空配置
                self._config = {}
        else:
            self._config = {}
        self.data_manager = DataManager(self)
        self.perm = PermissionService(self._config)
        self.uuid_service = UUIDService(
            mojang_api=str(self._config.get("mojang_api") or ""),
            littleskin_api=str(self._config.get("littleskin_api") or ""),
        )
        self.manager = ServerManager(
            data_manager=self.data_manager,
            hooks=InteropHooks(
                on_chat=self._on_mc_chat,
                on_player_event=self._on_player_event,
                on_server_status=self._on_server_status,
                on_state_change=self._on_server_state,
                get_whitelist=self._current_whitelist,
            ),
            plugin_version=PLUGIN_VERSION,
        )
        self._uuid_session = None
        self._sessions: dict[str, str] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._last_seen_umo: str | None = None
        # 同一条「未转发原因」最多每 60 秒提示一次：既不刷屏，又不让失败消失得无声无息
        self._throttle_at: dict[str, float] = {}
        # WS 连通性自检（插件页面上的「测试」按钮用）
        self.diagnose = WsConnectivityTester(self.manager)

    # ------------------------------------------------------------- 生命周期
    async def initialize(self) -> None:
        await self.data_manager.load()
        initial = _safe_list(self._config.get("initial_blacklist"))
        if initial:
            await self.data_manager.merge_initial_blacklist(initial)
        self.manager.configure(self._config)
        self.perm.reload(self._config)
        self._register_web_apis()
        if _as_bool(self._config.get("interop_enabled"), False):
            await self.manager.start()
            logger.info(
                f"[MCWL] v{PLUGIN_VERSION} 已启动，服务器："
                f"{', '.join(link.name for link in self.manager.enabled_links()) or '（未配置）'}"
            )
        else:
            logger.info(f"[MCWL] v{PLUGIN_VERSION} 已启动（群服互联未启用）")
        if _as_bool(self._config.get("auto_cleanup_enabled"), False):
            self._cleanup_task = asyncio.ensure_future(self._cleanup_loop())

    async def terminate(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._cleanup_task = None
        try:
            await self.manager.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MCWL] 关闭群服互联失败：{exc}")
        if self._uuid_session and not self._uuid_session.closed:
            await self._uuid_session.close()
            self._uuid_session = None
        logger.info("[MCWL] 插件已停止")

    # ------------------------------------------------------------- 内部工具
    def cfg(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def live_config(self) -> dict[str, Any]:
        """取当前生效的插件配置。

        优先问内核要（`star_map[<本模块>].config`）—— 内核就是把它传进 `__init__` 的那一个对象，
        WebUI 保存时原地更新，所以能立刻读到新值；万一内核换了对象实例，退回构造时的引用。
        """
        try:
            from astrbot.core.star.star import star_map

            metadata = star_map.get(type(self).__module__)
            candidate = getattr(metadata, "config", None) if metadata else None
            if isinstance(candidate, dict):
                return candidate
        except Exception:  # noqa: BLE001 - 测试/旧内核环境
            pass
        return self._config

    # ------------------------------------------------------------- 插件页面接口
    # 内核分发规则：插件页面里的 bridge 把接口地址拼成 /api/plug/<插件名>/<路由>，
    # 而 dashboard 的服务端是拿「整个 subpath」（含插件名）去比对注册表的，
    # 所以这里注册的路由**必须带自己的插件名前缀**，否则永远匹配不上。
    def plugin_api_prefix(self) -> str:
        """取 dashboard 眼里的插件名（= metadata.yaml 的 name）。"""
        try:
            from astrbot.core.star.star import star_map

            metadata = star_map.get(type(self).__module__)
            name = str(getattr(metadata, "name", "") or "").strip()
            if name:
                return name
        except Exception:  # noqa: BLE001 - 测试/旧内核环境
            pass
        return str(type(self).__module__).split(".")[0] or "astrbot_plugin_mc_whitelist"

    def web_api_specs(self) -> list[tuple[str, str, list[str], str]]:
        """(路由, 处理方法名, HTTP 方法, 描述)。"""
        prefix = self.plugin_api_prefix()
        return [
            (f"/{prefix}/servers", "api_servers", ["GET"], "读取 mc_servers 列表"),
            (f"/{prefix}/test/all", "api_test_all", ["POST"], "测试全部服务器的 WS 连通性"),
            (
                f"/{prefix}/test/<path:target>",
                "api_test_one",
                ["POST"],
                "测试单个服务器的 WS 连通性",
            ),
        ]

    def _register_web_apis(self) -> None:
        context = getattr(self, "context", None)
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.debug("[MCWL] 内核不支持 register_web_api，跳过插件页面接口")
            return
        for route, handler_name, methods, desc in self.web_api_specs():
            register(route, getattr(self, handler_name), methods, desc)

    @staticmethod
    def _api_ok(data: Any) -> dict[str, Any]:
        # 与内核 dashboard 的 Response 同构：页面 bridge 只认 status/data
        return {"status": "ok", "message": None, "data": data}

    @staticmethod
    def _api_error(message: str) -> dict[str, Any]:
        return {"status": "error", "message": message, "data": None}

    def _refresh_links(self) -> None:
        """按当前配置刷新连接对象：改完 mc_servers 不重载插件也能测到新地址。"""
        self.manager.configure(self.live_config())

    async def api_servers(self) -> dict[str, Any]:
        """插件页面加载时拉服务器清单（含常驻连接当前状态，便于对照）。"""
        self._refresh_links()
        config = self.live_config()
        servers = self.diagnose.server_rows()
        return self._api_ok(
            {
                "plugin_version": PLUGIN_VERSION,
                "interop_enabled": _as_bool(config.get("interop_enabled"), False),
                "security_mode": str(config.get("security_mode") or "encrypted"),
                "generated_at": now_iso(),
                "servers": servers,
                "total": len(servers),
                "connected": sum(1 for row in servers if row["connected"]),
            }
        )

    async def api_test_all(self) -> dict[str, Any]:
        self._refresh_links()
        if not self.manager.links:
            return self._api_error("mc_servers 里没有任何服务器，先在插件配置里添加")
        checks = await self.diagnose.test_all()
        results = [check.to_dict() for check in checks]
        return self._api_ok(
            {
                "results": results,
                "total": len(results),
                "passed": sum(1 for item in results if item["ok"]),
                "generated_at": now_iso(),
            }
        )

    async def api_test_one(self, target: str = "", **kwargs: Any) -> dict[str, Any]:
        # 内核的 <path:...> 转换器不会解百分号编码（实测 werkzeug），页面对中文名做了
        # encodeURIComponent，所以这里容错再解一次；解不动就按原样走，不会更糟。
        raw = str(target or kwargs.get("target") or "").strip()
        target = unquote(raw).strip() or raw
        if not target:
            return self._api_error("缺少要测试的服务器名")
        self._refresh_links()
        check = await self.diagnose.test_target(target)
        return self._api_ok(
            {
                "results": [check.to_dict()],
                "total": 1,
                "passed": 1 if check.ok else 0,
                "generated_at": now_iso(),
            }
        )

    async def _get_uuid_session(self):
        import aiohttp

        if self._uuid_session is None or self._uuid_session.closed:
            self._uuid_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"},
            )
        return self._uuid_session

    def _group_allowed(self, group_id: str | None, qq: str) -> bool:
        """群访问控制（文档 §14 group_mode / group_list）。"""
        mode = str(self.cfg("group_mode", "whitelist") or "whitelist").lower()
        values = _safe_list(self.cfg("group_list"))
        if not values:
            return True
        gid = str(group_id or "")
        if not gid:
            return mode != "whitelist"
        if mode == "blacklist":
            return gid not in values
        return gid in values

    async def _resolve_username(
        self, event: AstrMessageEvent, explicit_name: str | None = None
    ) -> tuple[bool, str]:
        """文档 §7.3：优先显式名字 → 群昵称 → 已有绑定。"""
        if explicit_name and _as_bool(self.cfg("allow_explicit_name"), True):
            return validate_username(explicit_name)

        nickname_mode = _as_bool(self.cfg("nickname_mode"), True)
        group_id = _safe_group_id(event)
        if nickname_mode and group_id:
            name = _safe_sender_name(event)
            if name:
                return validate_username(name)

        binding = self.data_manager.get_binding(_safe_sender_id(event))
        if binding:
            return True, str(binding.get("username"))
        return False, "请手动指定游戏名，格式：/注册 皮 <游戏名>"

    async def _lookup_uuid(
        self, username: str, type_: str
    ) -> tuple[bool, str, str]:
        session = await self._get_uuid_session()
        result = await self.uuid_service.lookup(session, username, type_)
        if not result.ok:
            return False, result.error, ""
        return True, result.uuid or "", result.username or username

    # ------------------------------------------------------------- 白名单同步
    async def _current_whitelist(self) -> tuple[int, list[dict]]:
        version = int(self.data_manager.sync_version or 0)
        return version, self.data_manager.entries()

    async def sync_whitelist(
        self, targets: list[ServerLink] | None = None
    ) -> dict[str, Any]:
        """全量同步（契约 §2：只发 full）。返回统计结果。"""
        links = targets if targets is not None else self.manager.enabled_links()
        links = [link for link in links if link.cfg.sync_whitelist or targets]
        if not links:
            return {"success": 0, "failed": 0, "total": 0, "detail": {}}
        if self.cfg("sync_version_enabled", True):
            version = await self.data_manager.next_sync_version()
        else:
            version = int(self.data_manager.sync_version or 0)
        entries = self.data_manager.entries()
        detail = await self.manager.push_whitelist_all(version, entries, targets=links)
        success = sum(1 for item in detail.values() if item.get("ok"))
        return {
            "success": success,
            "failed": len(detail) - success,
            "total": len(detail),
            "version": version,
            "entries": len(entries),
            "detail": detail,
        }

    # ------------------------------------------------------------- 回调（MC → QQ）
    async def _on_mc_chat(self, server: str, data: dict) -> None:
        groups = _int_list(data.get("target_groups"))
        if not groups:
            return
        sender = str(data.get("sender") or "未知")
        content = str(data.get("content") or "")
        if not content:
            return
        with_prefix = _as_bool(self.cfg("event_server_prefix"), True)
        text = f"[{server}] {sender}: {content}" if with_prefix else f"{sender}: {content}"
        await self._send_to_groups(groups, text)

    async def _on_player_event(self, server: str, data: dict) -> None:
        # 契约：target_groups 为空则不推送（模组端负责带群白名单）
        groups = _int_list(data.get("target_groups"))
        if not groups:
            return
        event = str(data.get("event") or "")
        player = str(data.get("player") or "未知")
        message = str(data.get("message") or "")
        icons = {
            "join": "🟢",
            "leave": "🔴",
            "death": "💀",
            "achievement": "🏆",
        }
        labels = {
            "join": "加入服务器",
            "leave": "离开服务器",
            "death": "死亡",
            "achievement": "达成成就",
        }
        icon = icons.get(event, "ℹ️")
        prefix = f"[{server}] " if _as_bool(self.cfg("event_server_prefix"), True) else ""
        body = labels.get(event, event)
        text = f"{prefix}{icon} {player} {body}"
        if message:
            text += f"：{message}"
        await self._send_to_groups(groups, text)

    async def _on_server_status(self, server: str, data: dict) -> None:
        logger.debug(f"[MCWL] {server} 心跳：{data.get('online_players')} 人在线")

    async def _on_server_state(self, server: str, state: dict) -> None:
        logger.debug(f"[MCWL] {server} 状态：{state}")

    async def _send_to_groups(self, groups: list[int], text: str) -> int:
        """主动推送到 QQ 群（优先用真实 umo，缺失时按模板拼）。"""
        template = str(self.cfg("group_session_template") or "{platform}:GroupMessage:{group_id}")
        platform = str(self.cfg("platform_id") or "aiocqhttp")
        sent = 0
        for group_id in groups:
            gid = str(group_id)
            umo = self._sessions.get(gid) or template.format(
                platform=platform, group_id=gid
            )
            try:
                await self.context.send_message(umo, _chain(text))
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[MCWL] 推送到群 {gid} 失败：{exc}")
        return sent

    def _remember_session(self, event: AstrMessageEvent) -> None:
        gid = _safe_group_id(event)
        umo = getattr(event, "unified_msg_origin", None)
        if gid and umo:
            self._sessions[str(gid)] = str(umo)
        if umo:
            self._last_seen_umo = str(umo)

    async def _cleanup_loop(self) -> None:
        """auto_cleanup：定期把「已不在群里」的会话缓存清掉（不删绑定）。

        文档 §14 auto_cleanup_enabled 语义模糊（分析报告 B8），本实现只做安全的
        会话缓存清理 + 日志统计，不动绑定数据，需要真正解绑请用 /black 或 /注销。
        """
        while True:
            try:
                await asyncio.sleep(3600)
                logger.info(
                    f"[MCWL] 定时检查：绑定 {self.data_manager.binding_count()} 条，"
                    f"待同步服务器 {self.manager.pending_names() or '无'}"
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[MCWL] 定时检查异常：{exc}")

    # ------------------------------------------------------------- 指令：绑定
    @filter.command("注册", alias=["zc", "绑定"])
    async def cmd_register(
        self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""
    ):
        """绑定：/注册 正|皮 [游戏名]"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        group_id = _safe_group_id(event)
        allowed, reason = await self.perm.check(event, NODE_REGISTER, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not self._group_allowed(group_id, qq):
            return
        if self.data_manager.is_blacklisted(qq):
            yield event.plain_result("❌ 您在黑名单中，无法注册")
            return

        type_ = parse_type(arg1) or parse_type(self.cfg("default_bind_type", "mojang"))
        explicit = arg2 or None
        if arg1 and not parse_type(arg1) and not arg2:
            # /注册 <游戏名>：名字当显式名，类型取默认
            explicit = arg1
        if not type_:
            yield event.plain_result("❌ 绑定类型错误，请使用：/注册 正 或 /注册 皮")
            return

        valid, result = await self._resolve_username(event, explicit)
        if not valid:
            yield event.plain_result(f"❌ {result}")
            return
        username = result

        existing = self.data_manager.get_binding(qq)
        if existing:
            yield event.plain_result(
                f"❌ 您已绑定 {existing.get('username')}（{type_label(str(existing.get('type')))}）\n"
                f"如需更换请先 /注销，或使用 /迁移"
            )
            return
        if self.data_manager.username_exists(username):
            yield event.plain_result(f"❌ 游戏名 {username} 已被其它 QQ 绑定")
            return

        ok, uuid_or_err, real_name = await self._lookup_uuid(username, type_)
        if not ok:
            yield event.plain_result(f"❌ 无法获取 {username} 的 UUID：{uuid_or_err}")
            return

        ok, msg = await self.data_manager.register(
            qq, real_name, uuid_or_err, type_, group_id
        )
        if not ok:
            yield event.plain_result(f"❌ {msg}")
            return
        sync = await self.sync_whitelist()
        yield event.plain_result(
            "✅ 绑定成功！\n"
            f"🎮 游戏名：{real_name}\n"
            f"🔑 类型：{type_label(type_)}\n"
            f"🆔 UUID：{uuid_or_err}\n"
            f"🔄 已同步：{sync['success']}/{sync['total']} 个服务器"
            + (f"（{sync['failed']} 个失败，稍后自动补推）" if sync["failed"] else "")
        )

    @filter.command("注销", alias=["zx", "解绑"])
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解绑：/注销"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_REGISTER, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        ok, old = await self.data_manager.unbind(qq)
        if not ok:
            yield event.plain_result("❌ 您尚未绑定")
            return
        sync = await self.sync_whitelist()
        yield event.plain_result(
            f"✅ 已注销 {old.get('username') if old else ''}\n"
            f"🔄 已同步：{sync['success']}/{sync['total']} 个服务器"
        )

    @filter.command("迁移", alias=["qy"])
    async def cmd_migrate(self, event: AstrMessageEvent, direction: str = ""):
        """迁移：/迁移 皮转正|正转皮"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_REGISTER, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        direction_map = {
            "皮转正": (TYPE_LITTLESKIN, TYPE_MOJANG),
            "正转皮": (TYPE_MOJANG, TYPE_LITTLESKIN),
        }
        key = direction.strip()
        if key not in direction_map:
            yield event.plain_result("❌ 方向错误，请使用：/迁移 皮转正 或 /迁移 正转皮")
            return
        from_type, to_type = direction_map[key]
        binding = self.data_manager.get_binding(qq)
        if not binding:
            yield event.plain_result("❌ 您尚未绑定")
            return
        if str(binding.get("type")) != from_type:
            yield event.plain_result(
                f"❌ 您的绑定类型是 {type_label(str(binding.get('type')))}，无法执行 {key}"
            )
            return

        valid, result = await self._resolve_username(event, None)
        if not valid:
            yield event.plain_result(f"❌ {result}")
            return
        new_name = result
        if new_name.lower() != str(binding.get("username", "")).lower() and (
            self.data_manager.username_exists(new_name)
        ):
            yield event.plain_result(f"❌ {new_name} 已被占用")
            return
        ok, uuid_or_err, real_name = await self._lookup_uuid(new_name, to_type)
        if not ok:
            yield event.plain_result(f"❌ 无法获取 {new_name} 的 UUID：{uuid_or_err}")
            return
        ok, msg = await self.data_manager.update_binding(
            qq, real_name, uuid_or_err, to_type
        )
        if not ok:
            yield event.plain_result(f"❌ {msg}")
            return
        sync = await self.sync_whitelist()
        yield event.plain_result(
            f"✅ 迁移成功！\n🎮 {binding.get('username')} → {real_name}\n"
            f"📦 {type_label(from_type)} → {type_label(to_type)}\n"
            f"🔄 已同步 {sync['success']}/{sync['total']} 个服务器"
        )

    @filter.command("更新昵称", alias=["gxnc"])
    async def cmd_refresh_nickname(self, event: AstrMessageEvent):
        """群昵称改了之后重新同步到白名单：/更新昵称"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_REGISTER, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        binding = self.data_manager.get_binding(qq)
        if not binding:
            yield event.plain_result("❌ 您尚未绑定，请先 /注册 正 或 /注册 皮")
            return
        valid, result = await self._resolve_username(event, None)
        if not valid:
            yield event.plain_result(f"❌ {result}")
            return
        new_name = result
        if new_name.lower() == str(binding.get("username", "")).lower():
            yield event.plain_result(f"ℹ️ 昵称未变化（{new_name}），无需更新")
            return
        if self.data_manager.username_exists(new_name):
            yield event.plain_result(f"❌ {new_name} 已被其它 QQ 绑定")
            return
        type_ = str(binding.get("type") or TYPE_MOJANG)
        ok, uuid_or_err, real_name = await self._lookup_uuid(new_name, type_)
        if not ok:
            yield event.plain_result(f"❌ 无法获取 {new_name} 的 UUID：{uuid_or_err}")
            return
        ok, msg = await self.data_manager.update_binding(
            qq, real_name, uuid_or_err, type_
        )
        if not ok:
            yield event.plain_result(f"❌ {msg}")
            return
        sync = await self.sync_whitelist()
        yield event.plain_result(
            f"✅ 昵称已更新：{binding.get('username')} → {real_name}\n"
            f"🔄 已同步 {sync['success']}/{sync['total']} 个服务器"
        )

    # ------------------------------------------------------------- 指令：统计
    @filter.command("info", alias=["统计", "我的统计"])
    async def cmd_info(self, event: AstrMessageEvent, target: str = ""):
        """统计查询：/info [服务器名|编号]（先 QQ 群，不是游戏内指令）"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_STATS, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        binding = self.data_manager.get_binding(qq)
        if not binding:
            yield event.plain_result("❌ 您尚未绑定，无法查询统计（先 /注册）")
            return
        player = str(binding.get("username") or "")
        servers = self.manager.enabled_links()
        if not servers:
            yield event.plain_result("❌ 未配置任何服务器（mc_servers 为空）")
            return

        if target:
            link = self.manager.resolve(target)
            if link is None:
                yield event.plain_result(f"❌ 未找到服务器：{target}")
                return
            targets = [link]
        else:
            targets = servers

        results = await asyncio.gather(
            *(self.manager.fetch_stats(link, player) for link in targets),
            return_exceptions=True,
        )
        sections: list[dict[str, Any]] = []
        for link, result in zip(targets, results):
            if isinstance(result, Exception):
                sections.append(
                    {"server": link.name, "data": None, "note": f"查询异常：{result}"}
                )
                continue
            ok, data, message = result
            if not ok:
                sections.append({"server": link.name, "data": None, "note": f"获取失败：{message}"})
            elif not data:
                sections.append({"server": link.name, "data": None, "note": "该服无此玩家记录"})
            else:
                sections.append({"server": link.name, "data": dict(data), "note": ""})

        if not any(section["data"] for section in sections):
            notes = "\n".join(f"· {s['server']}：{s['note']}" for s in sections)
            yield event.plain_result(f"❌ 未获取到 {player} 的统计数据\n{notes}")
            return

        background = await self._pick_background()
        image = await asyncio.to_thread(
            render_stats_image,
            player,
            sections,
            background_path=background,
            footer=f"{PLUGIN_NAME} v{PLUGIN_VERSION}",
        )
        yield event.chain_result([Image.fromBytes(image)])

    async def _pick_background(self) -> str | None:
        """背景图：WebUI 上传的优先，回落目录方式，都没有则渲染纯色底。"""
        files = self._uploaded_backgrounds()
        if not files:
            files = list_backgrounds(self._background_dir())
        if not files:
            return None
        idx = await self.data_manager.next_background_index()
        return files[idx % len(files)]

    def _uploaded_backgrounds(self) -> list[str]:
        """WebUI 里上传的背景图（`file` 类型配置项，值形如 files/background_images/x.png）。"""
        rels = self.live_config().get("background_images")
        files = resolve_background_files(rels, plugin_data_root(PLUGIN_NAME))
        if rels and not files:
            logger.warning(
                "[MCWL] 配置里的背景图都不可用（文件被删或路径失效），"
                "请到 WebUI 插件配置里重新上传"
            )
        return files

    def _background_dir(self) -> str:
        """目录方式的背景图（进阶用法，相对路径按插件目录解析）。"""
        directory = str(self.cfg("background_images_dir") or "")
        if directory and not Path(directory).is_absolute():
            directory = str(PLUGIN_DIR / directory)
        return directory

    # ------------------------------------------------------------- 指令：管理
    @filter.command("black", alias=["黑名单"])
    async def cmd_blacklist(
        self, event: AstrMessageEvent, action: str = "", who: str = ""
    ):
        """黑名单：/black add|remove <QQ或@>，/black list"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_BLACKLIST, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        action = action.strip().lower()
        if action in ("list", "列表", ""):
            values = self.data_manager.blacklist()
            if not values:
                yield event.plain_result("ℹ️ 黑名单为空")
                return
            yield event.plain_result(
                f"🚫 黑名单（{len(values)} 个）\n" + "\n".join(f"· {v}" for v in values)
            )
            return

        target = _extract_target_qq(event, who)
        if not target:
            yield event.plain_result("❌ 请指定 QQ 号或 @成员")
            return
        if action in ("add", "添加"):
            added, removed = await self.data_manager.add_blacklist(target)
            if not added and not removed:
                yield event.plain_result(f"ℹ️ {target} 已在黑名单中")
                return
            sync = await self.sync_whitelist()
            extra = f"\n🔗 已自动解绑：{removed.get('username')}" if removed else ""
            yield event.plain_result(
                f"✅ 已加入黑名单：{target}{extra}\n"
                f"🔄 已同步：{sync['success']}/{sync['total']} 个服务器"
            )
            return
        if action in ("remove", "删除", "移除"):
            ok = await self.data_manager.remove_blacklist(target)
            if not ok:
                yield event.plain_result(f"ℹ️ {target} 不在黑名单中")
                return
            yield event.plain_result(f"✅ 已移出黑名单：{target}")
            return
        yield event.plain_result("❌ 用法：/black add|remove <QQ或@> 或 /black list")

    @filter.command("sync", alias=["同步"])
    async def cmd_sync(self, event: AstrMessageEvent, target: str = ""):
        """手动同步白名单：/sync [服务器名|编号]"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_SYNC, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        if target:
            link = self.manager.resolve(target)
            if link is None:
                yield event.plain_result(f"❌ 未找到服务器：{target}")
                return
            targets = [link]
        else:
            targets = None
        sync = await self.sync_whitelist(targets)
        if not sync["total"]:
            yield event.plain_result("❌ 没有可同步的服务器（检查 mc_servers / sync_whitelist）")
            return
        lines = [
            f"{'✅' if item.get('ok') else '❌'} {name}："
            f"{'已推送' if item.get('ok') else item.get('detail')}"
            for name, item in sync["detail"].items()
        ]
        yield event.plain_result(
            f"📦 白名单版本 v{sync['version']}（{sync['entries']} 条）\n"
            + "\n".join(lines)
        )

    @filter.command("status", alias=["状态"])
    async def cmd_status(self, event: AstrMessageEvent):
        """群服互联状态：/status"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_INTEROP, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not self.manager.enabled_links():
            yield event.plain_result("ℹ️ 未配置任何服务器（mc_servers 为空）")
            return
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
        lines = ["📊 群服互联状态", "━━━━━━━━━━━━━━━━━━"]
        for idx, state in enumerate(self.manager.snapshot()):
            mark = emojis[idx] if idx < len(emojis) else f"{idx + 1}."
            flag = "✅" if state["ws_connected"] else "🔴"
            parts = [f"{flag} {state['name']}"]
            if state["ws_connected"]:
                parts.append(f"已连接")
                if state.get("online_players") is not None:
                    parts.append(f"{state['online_players']}人在线")
                if state.get("whitelist_count") is not None:
                    parts.append(f"白名单{state['whitelist_count']}条")
            else:
                parts.append("未连接")
                if state.get("pending_sync"):
                    parts.append("待同步")
                if state.get("retry_delay"):
                    parts.append(f"{state['retry_delay']}s后重连")
            last = state.get("last_sync_time")
            if last:
                parts.append(f"上次同步 {str(last)[11:16]}")
            if not state["ws_connected"] and state.get("last_error"):
                parts.append(f"错误：{state['last_error']}")
            lines.append(f"{mark} " + " | ".join(parts))
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📡 全局绑定：{self.data_manager.binding_count()} 个")
        lines.append(f"🔒 安全模式：{self.manager.security_mode}")
        lines.append(f"🧩 协议版本：{PROTO_VERSION}")
        pending = self.manager.pending_names()
        if pending:
            lines.append(f"⏳ 待同步：{', '.join(pending)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("mc")
    async def cmd_send_to_server(
        self, event: AstrMessageEvent, target: str = "", content: GreedyStr = ""
    ):
        """定向发送：/mc <服务器名|编号> <内容>"""
        self._remember_session(event)
        qq = _safe_sender_id(event)
        allowed, reason = await self.perm.check(event, NODE_REGISTER, qq)
        if not allowed:
            yield event.plain_result(reason)
            return
        link = self.manager.resolve(target)
        if link is None:
            yield event.plain_result(f"❌ 未找到服务器：{target or '（未指定）'}")
            return
        text = str(content).strip()
        if not text:
            yield event.plain_result("❌ 消息内容为空")
            return
        sender = _safe_sender_name(event) or f"QQ{qq}"
        ok = await self.manager.broadcast_chat(sender, text, targets=[link])
        yield event.plain_result(
            f"{'✅ 已发送到' if ok.get(link.name) else '❌ 发送失败（未连接）：'}{link.name}"
        )

    @filter.command("mcwl", alias=["mc白名单", "白名单帮助"])
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助：/mcwl"""
        yield event.plain_result(HELP_TEXT.format(version=PLUGIN_VERSION))

    # ------------------------------------------------------------- QQ → MC 转发
    def _log_throttled(
        self, key: str, message: str, *, level: str = "warning", interval: float = 60.0
    ) -> None:
        """同一原因每 interval 秒最多提示一次。

        转发失败必须可见（以前是静默丢弃，群里没反应、日志里也查不到），
        但群消息是高频事件，不能每条都刷屏。
        """
        now = time.time()
        if now - self._throttle_at.get(key, 0.0) < interval:
            return
        self._throttle_at[key] = now
        getattr(logger, level, logger.warning)(message)

    def _chat_forward_block(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """这条群消息能不能转发。返回 None = 可以；否则 (原因代码, 原因说明)。

        抽成独立方法是为了让「为什么没转发」可单测、可日志——
        原先每个条件都是静默 return，用户只能看到「群里没反应」。
        """
        if not _as_bool(self.cfg("interop_enabled"), False):
            return "interop_off", "群服互联未启用（配置页打开「启用群服互联」并重载插件）"
        if not _as_bool(self.cfg("chat_sync_enabled"), True):
            return "chat_sync_off", "「启用 QQ→MC 广播」是关的（配置页打开后保存即可，无需重载）"
        try:
            if event.is_wake_up():
                return "wake_up", ""
        except Exception:  # noqa: BLE001
            pass
        group_id = _safe_group_id(event)
        qq = _safe_sender_id(event)
        if not self._group_allowed(group_id, qq):
            return "group_denied", f"群 {group_id} 不在允许范围（group_mode / group_list）"
        if self.data_manager.is_blacklisted(qq):
            return "blacklisted", f"QQ {qq} 在黑名单里"
        if not [link for link in self.manager.enabled_links() if link.cfg.chat_sync]:
            return "no_target", (
                "没有可转发的服务器：需要同时满足「启用群服互联」「该服务器已启用」"
                "「该服务器开启「转发群消息」」"
            )
        return None

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息转发到 MC（文档 §10.2 + 契约 P1：chat_forward_trigger 触发词）。

        注意：本处理器**不产生任何回复**，不影响 AstrBot 的 LLM 流程；
        带唤醒前缀（指令）的消息不转发。
        """
        self._remember_session(event)
        block = self._chat_forward_block(event)
        if block is not None:
            code, reason = block
            # 指令消息（唤醒词）属正常跳过，不提示；其余原因要让用户看得见
            if reason:
                self._log_throttled(f"chat-skip:{code}", f"[MCWL] 群消息未转发：{reason}")
            return

        text = message_to_text(
            event.get_messages(),
            enable_at_conversion=_as_bool(self.cfg("enable_at_conversion"), True),
            enable_image_forward=_as_bool(self.cfg("enable_image_forward"), True),
            max_length=int(self.cfg("max_message_length", 256) or 256),
        )
        trigger = str(self.cfg("chat_forward_trigger") or "")
        if trigger:
            if not text.startswith(trigger):
                return  # 触发词不匹配 = 设计如此，不提示
            text = text[len(trigger):].strip()
        if not text:
            return
        sender = _safe_sender_name(event) or f"QQ{_safe_sender_id(event)}"
        results = await self.manager.broadcast_chat(sender, text)
        failed = [name for name, ok in results.items() if not ok]
        if failed:
            self._log_throttled(
                "chat-fail:" + ",".join(sorted(failed)),
                f"[MCWL] 群消息转发失败（{', '.join(failed)} 未连接，这条消息已丢弃）："
                "用 /status 或插件页面「连通测试」查看链路状态",
            )
        elif results:
            logger.info(
                f"[MCWL] 群消息已转发到 {', '.join(results)}（{sender}，{len(text)} 字）"
            )

    @filter.event_message_type(EventMessageType.OTHER_MESSAGE)
    async def on_notice_message(self, event: AstrMessageEvent):
        """退群自动解绑（文档 §13.6）——best effort，依赖平台的群成员减少通知。"""
        if not _as_bool(self.cfg("auto_unbind_on_leave"), False):
            return
        raw = getattr(event, "message_obj", None)
        raw_data = getattr(raw, "raw_message", None) or getattr(event, "raw_message", None)
        if not isinstance(raw_data, dict):
            return
        if raw_data.get("notice_type") != "group_decrease":
            return
        qq = str(raw_data.get("user_id") or "")
        if not qq:
            return
        ok, old = await self.data_manager.unbind(qq)
        if ok:
            sync = await self.sync_whitelist()
            logger.info(
                f"[MCWL] {qq} 退群自动解绑（{old.get('username') if old else ''}），"
                f"已同步 {sync['success']}/{sync['total']}"
            )


# ---------------------------------------------------------------- 模块级工具
def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


def _safe_list(value: Any) -> list[str]:
    """AstrBot 的 list 配置可能是 JSON 字符串 / list / None。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        import json

        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    return []


def _int_list(value: Any) -> list[int]:
    out: list[int] = []
    for item in _safe_list(value):
        try:
            out.append(int(float(item)))
        except (TypeError, ValueError):
            continue
    return out


def _safe_sender_id(event: AstrMessageEvent) -> str:
    for getter in ("get_sender_id", "get_user_id"):
        fn = getattr(event, getter, None)
        if callable(fn):
            try:
                value = fn()
                if value:
                    return str(value).strip()
            except Exception:  # noqa: BLE001
                pass
    for attr in ("sender_id", "user_id"):
        value = getattr(event, attr, None)
        if value:
            return str(value).strip()
    return ""


def _safe_sender_name(event: AstrMessageEvent) -> str:
    fn = getattr(event, "get_sender_name", None)
    if callable(fn):
        try:
            return str(fn() or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def _safe_group_id(event: AstrMessageEvent) -> str:
    fn = getattr(event, "get_group_id", None)
    if callable(fn):
        try:
            return str(fn() or "").strip()
        except Exception:  # noqa: BLE001
            pass
    value = getattr(event, "group_id", None)
    return str(value).strip() if value else ""


def _extract_target_qq(event: AstrMessageEvent, arg: str = "") -> str | None:
    """@成员优先，其次纯数字 QQ 号。"""
    fn = getattr(event, "get_mentions", None)
    if callable(fn):
        try:
            mentions = fn() or []
            if mentions:
                return str(mentions[0])
        except Exception:  # noqa: BLE001
            pass
    text = str(arg or "").strip().lstrip("@")
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits or None


def _chain(text: str):
    """构造主动推送用的消息链（不依赖 astrbot.api.event.MessageChain）。"""
    from astrbot.api.message_components import Plain

    return [Plain(text=text)]
