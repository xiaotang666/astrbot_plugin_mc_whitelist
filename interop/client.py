"""多服务器连接管理：每服一条 WebSocket 长连接 + 独立 HTTP 客户端。

契约依据：docs/接口契约冻结_v10.3.md
    §1.1 模组 = 服务端（WS Server + HTTP Server），插件 = 客户端
    §1.2 外层信封统一带 type/seq/msg_id/proto_version；前缀必填；msg_id 去重
    §1.3 HTTP 用 Authorization: Bearer <token>，统一响应 {code,message,data,timestamp}
    §2   消息类型表（auth / auth_result / whitelist_update / whitelist_ack /
         whitelist_sync_request / chat / player_event / server_status）
    B3  收到 whitelist_ack 才清 pending_sync
    B4  模组发 whitelist_sync_request → 插件回一条全量 whitelist_update
    B5  心跳每 heartbeat_seconds(默认30)s 推 server_status；插件 3× 未收到即判离线
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from ..core.crypto import AESCrypto, CryptoError
from ..core.protocol import (
    PROTO_VERSION,
    Envelope,
    MsgIdCache,
    MsgType,
    ProtocolError,
)
from ..core.version import PLUGIN_VERSION

logger = logging.getLogger("astrbot")

DEFAULT_HEARTBEAT = 30
DEFAULT_TIMEOUT = 90
CLIENT_NAME = "astrbot_plugin_mc_whitelist"
BACKOFF_STEPS = (1, 2, 4, 8, 16, 30, 60)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


@dataclass
class InteropHooks:
    """插件侧注入的回调（全部可选）。"""

    on_chat: Callable[[str, dict], Awaitable[None]] | None = None
    on_player_event: Callable[[str, dict], Awaitable[None]] | None = None
    on_server_status: Callable[[str, dict], Awaitable[None]] | None = None
    on_state_change: Callable[[str, dict], Awaitable[None]] | None = None
    # 契约 B4：模组请求同步时，插件需要拿到当前全量白名单
    get_whitelist: Callable[[], Awaitable[tuple[int, list[dict]]]] | None = None


@dataclass
class ServerConfig:
    name: str
    ws_url: str = ""
    http_url: str = ""
    token: str = ""
    enabled: bool = True
    sync_whitelist: bool = True
    chat_sync: bool = True
    event_broadcast: bool = True
    index: int = 0

    @classmethod
    def from_dict(cls, raw: dict, index: int = 0) -> "ServerConfig":
        return cls(
            name=str(raw.get("name") or f"服务器{index + 1}").strip(),
            ws_url=str(raw.get("ws_url") or "").strip(),
            http_url=str(raw.get("http_url") or "").strip(),
            token=str(raw.get("token") or "").strip(),
            enabled=_as_bool(raw.get("enabled"), True),
            sync_whitelist=_as_bool(raw.get("sync_whitelist"), True),
            chat_sync=_as_bool(raw.get("chat_sync"), True),
            event_broadcast=_as_bool(raw.get("event_broadcast"), True),
            index=index,
        )


class ServerLink:
    """单个 MC 服务器（模组）的连接。"""

    def __init__(self, manager: "ServerManager", cfg: ServerConfig) -> None:
        self.manager = manager
        self.cfg = cfg
        self.envelope = manager.build_envelope()
        self.dedup = MsgIdCache(capacity=manager.dedup_capacity)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.connected = False
        self.authenticated = False
        self.last_seen = 0.0
        self.last_error: str | None = None
        self.pending_sync = True
        self.retry_delay = 0
        self.online_players = 0
        self.max_players = 0
        self.tps: float | None = None
        self.whitelist_count: int | None = None
        self.server_reported_name: str | None = None
        self.auth_reason: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------- 生命周期
    @property
    def name(self) -> str:
        return self.cfg.name

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None

    # ------------------------------------------------------------- 主循环
    async def _run(self) -> None:
        step = 0
        while not self._stop.is_set():
            if not self.cfg.ws_url:
                await self._set_state(last_error="未配置 ws_url", connected=False)
                return
            try:
                await self._connect_once()
                step = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"[MCWL] {self.name} 连接异常：{self.last_error}")
            finally:
                await self._mark_offline(self.last_error)

            if self._stop.is_set() or not self.manager.auto_reconnect:
                return
            delay = BACKOFF_STEPS[min(step, len(BACKOFF_STEPS) - 1)]
            step += 1
            self.retry_delay = delay
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                continue

    async def _connect_once(self) -> None:
        assert self.manager.session is not None
        try:
            ws = await self.manager.session.ws_connect(
                self.cfg.ws_url,
                heartbeat=self.manager.heartbeat_seconds,
                max_msg_size=2**22,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"连接失败：{exc}"
            raise

        self.ws = ws
        self.connected = True
        self.retry_delay = 0
        self.last_seen = time.time()
        await self._set_state(connected=True, last_error=None)
        logger.info(f"[MCWL] {self.name} WS 已连接：{self.cfg.ws_url}")
        await self._send_auth()

        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._on_text(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await self._on_text(message.data.decode("utf-8", "replace"))
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    self.last_error = f"WS 关闭：{ws.close_code}"
                    break
        finally:
            self.connected = False
            self.authenticated = False
            self.ws = None
            await self._mark_offline(self.last_error)

    async def _send_auth(self) -> None:
        await self.send(
            MsgType.AUTH,
            {
                "token": self.token(),
                "client": CLIENT_NAME,
                "version": self.manager.plugin_version,
                "proto_version": PROTO_VERSION,
            },
        )

    # ------------------------------------------------------------- 收发
    def token(self) -> str:
        return self.cfg.token or self.manager.default_token or ""

    async def send(self, msg_type: str, data: dict | None = None) -> bool:
        """发送一条消息；未连接时返回 False（调用方负责标记 pending）。"""
        if not self.ws or self.ws.closed:
            self.pending_sync = True
            return False
        payload = self.envelope.encode(msg_type, data or {})
        async with self._send_lock:
            try:
                await self.ws.send_str(payload)
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"发送失败：{exc}"
                logger.warning(f"[MCWL] {self.name} 发送 {msg_type} 失败：{exc}")
                return False

    async def _on_text(self, raw: str) -> None:
        try:
            message = self.envelope.decode(raw)
        except (ProtocolError, CryptoError) as exc:
            # 前缀不匹配 / 版本不符 / 解密失败 / 重复 msg_id —— 按契约静默处理，只记日志
            self.last_error = f"消息被拒收：{exc}"
            logger.debug(f"[MCWL] {self.name} 消息被拒收：{exc}")
            return

        self.last_seen = time.time()
        if self.dedup.is_duplicate(message.msg_id):
            logger.debug(f"[MCWL] {self.name} 重复 msg_id，已丢弃：{message.msg_id}")
            return
        # 契约 §1.2 规则 3：模组在内层 payload 里也带 msg_id，两者都参与去重
        payload_msg_id = (message.data or {}).get("msg_id")
        if payload_msg_id and payload_msg_id != message.msg_id and self.dedup.is_duplicate(payload_msg_id):
            logger.debug(f"[MCWL] {self.name} 重复 payload msg_id，已丢弃：{payload_msg_id}")
            return

        data = dict(message.data or {})
        data.setdefault("server", self.name)
        data.setdefault("msg_id", message.msg_id)
        handlers = {
            MsgType.AUTH_RESULT: self._handle_auth_result,
            MsgType.WHITELIST_ACK: self._handle_whitelist_ack,
            MsgType.WHITELIST_SYNC_REQUEST: self._handle_sync_request,
            MsgType.CHAT: self._handle_chat,
            MsgType.PLAYER_EVENT: self._handle_player_event,
            MsgType.SERVER_STATUS: self._handle_server_status,
        }
        handler = handlers.get(message.type)
        if handler is None:
            logger.debug(f"[MCWL] {self.name} 未处理的消息类型：{message.type}")
            return
        await handler(data)

    async def _handle_auth_result(self, data: dict) -> None:
        success = bool(data.get("success"))
        self.authenticated = success
        self.server_reported_name = str(data.get("server_name") or "") or None
        self.auth_reason = data.get("reason")
        self.online_players = int(data.get("online_players") or 0)
        if success:
            logger.info(f"[MCWL] {self.name} 认证成功（模组自称：{self.server_reported_name}）")
            if self.cfg.sync_whitelist:
                await self.manager.request_push(self)
        else:
            reason = self.auth_reason or "unknown"
            logger.warning(f"[MCWL] {self.name} 认证失败：{reason}")
            if reason == "protocol_mismatch":
                # 契约 §1.2 规则 4：协议不一致 → 停止重连，提示升级
                self.last_error = "协议版本不一致，请同步升级插件与模组（proto_version）"
                self._stop.set()
                await self.manager.notify_protocol_mismatch(self)
        await self._publish_state()

    async def _handle_whitelist_ack(self, data: dict) -> None:
        version = data.get("version")
        count = int(data.get("count") or 0)
        success = bool(data.get("success", True))
        if success:
            self.pending_sync = False
            self.whitelist_count = count
            self.last_error = None
            await self.manager.data_manager.mark_synced(self.name, count)
        else:
            self.pending_sync = True
            await self.manager.data_manager.update_server_state(
                self.name, pending_sync=True, last_error=str(data.get("reason") or "ack failed")
            )
        logger.info(f"[MCWL] {self.name} whitelist_ack v{version} count={count} ok={success}")
        await self._publish_state()

    async def _handle_sync_request(self, data: dict) -> None:
        # 契约 B4：模组重启后主动要求全量
        logger.info(f"[MCWL] {self.name} 请求全量白名单，立即补推")
        await self.manager.request_push(self, force=True)

    async def _handle_chat(self, data: dict) -> None:
        if self.manager.hooks.on_chat:
            await self.manager.hooks.on_chat(self.name, data)

    async def _handle_player_event(self, data: dict) -> None:
        if self.manager.hooks.on_player_event:
            await self.manager.hooks.on_player_event(self.name, data)

    async def _handle_server_status(self, data: dict) -> None:
        self.online_players = int(data.get("online_players") or 0)
        self.max_players = int(data.get("max_players") or 0)
        try:
            self.tps = float(data["tps"]) if data.get("tps") is not None else None
        except (TypeError, ValueError):
            self.tps = None
        if self.manager.hooks.on_server_status:
            await self.manager.hooks.on_server_status(self.name, data)
        await self._publish_state()

    # ------------------------------------------------------------- 状态
    async def _set_state(self, **fields: Any) -> None:
        for key, value in fields.items():
            if key == "connected":
                self.connected = bool(value)
            elif key == "last_error":
                self.last_error = value
        await self._publish_state()

    async def _mark_offline(self, error: str | None) -> None:
        self.connected = False
        self.authenticated = False
        # 契约 B5：离线即标 pending_sync，重连后补推
        self.pending_sync = True
        await self._publish_state()

    async def _publish_state(self) -> None:
        await self.manager.data_manager.update_server_state(
            self.name,
            ws_connected=self.connected and self.authenticated,
            pending_sync=self.pending_sync,
            last_error=self.last_error,
            online_players=self.online_players,
            max_players=self.max_players,
            tps=self.tps,
            whitelist_count=self.whitelist_count,
        )
        if self.manager.hooks.on_state_change:
            await self.manager.hooks.on_state_change(self.name, self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        state = self.manager.data_manager.server_state(self.name)
        return {
            "name": self.name,
            "index": self.cfg.index,
            "ws_connected": self.connected and self.authenticated,
            "pending_sync": self.pending_sync,
            "last_sync_time": state.get("last_sync_time"),
            "last_sync_count": state.get("last_sync_count", 0),
            "last_error": self.last_error,
            "online_players": self.online_players,
            "max_players": self.max_players,
            "tps": self.tps,
            "whitelist_count": self.whitelist_count,
            "enabled": self.cfg.enabled,
            "retry_delay": self.retry_delay,
            "server_reported_name": self.server_reported_name,
        }

    # ------------------------------------------------------------- HTTP
    async def http_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> tuple[bool, Any, str]:
        """契约 §1.3。返回 (ok, data, message)。"""
        session = self.manager.session
        if session is None:
            return False, None, "会话未初始化"
        if not self.cfg.http_url:
            return False, None, "未配置 http_url"
        url = f"{self.cfg.http_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        token = self.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.manager.http_timeout,
            ) as resp:
                text = await resp.text()
                if resp.status == 401 or resp.status == 403:
                    return False, None, "鉴权失败（token 不匹配）"
                if resp.status >= 500:
                    return False, None, f"模组返回 {resp.status}"
                try:
                    body = json.loads(text)
                except Exception:  # noqa: BLE001
                    return False, None, f"响应不是合法 JSON（{resp.status}）"
                code = body.get("code")
                if code in (0, None):
                    return True, body.get("data"), str(body.get("message") or "success")
                if code in (401, 403):
                    # 契约 §1.3：错误也走 HTTP 200 + code，鉴权失败单独给出人话提示
                    return False, None, "鉴权失败（token 不匹配）"
                return False, body.get("data"), str(body.get("message") or f"code={code}")
        except asyncio.TimeoutError:
            return False, None, "请求超时"
        except aiohttp.ClientError as exc:
            return False, None, f"网络错误：{exc}"


class ServerManager:
    """所有服务器的连接池 + HTTP 网关 + 对外统一 API。"""

    def __init__(
        self,
        *,
        data_manager: Any,
        hooks: InteropHooks | None = None,
        plugin_version: str | None = None,
    ) -> None:
        self.data_manager = data_manager
        self.hooks = hooks or InteropHooks()
        self.plugin_version = plugin_version or PLUGIN_VERSION
        self.session: aiohttp.ClientSession | None = None
        self.links: list[ServerLink] = []
        # 运行时参数（由 configure 覆盖）
        self.security_mode = "encrypted"
        self.aes_key = ""
        self.prefix = "[MC]"
        self.default_token = ""
        self.auto_reconnect = True
        self.heartbeat_seconds = DEFAULT_HEARTBEAT
        self.heartbeat_timeout = DEFAULT_TIMEOUT
        self.http_timeout = aiohttp.ClientTimeout(total=10)
        self.dedup_capacity = 512
        self._watchdog: asyncio.Task | None = None
        self._started = False
        self.last_push_at: dict[str, float] = {}

    # ------------------------------------------------------------- 配置
    def configure(self, config: dict[str, Any]) -> None:
        cfg = config or {}
        self.security_mode = str(cfg.get("security_mode") or "encrypted").lower()
        self.aes_key = str(cfg.get("aes_key") or "")
        self.prefix = str(cfg.get("message_prefix") or "[MC]")
        self.default_token = str(cfg.get("default_api_token") or "")
        self.auto_reconnect = _as_bool(cfg.get("auto_reconnect"), True)
        self.heartbeat_seconds = max(
            1, int(cfg.get("heartbeat_seconds", DEFAULT_HEARTBEAT) or DEFAULT_HEARTBEAT)
        )
        self.heartbeat_timeout = max(
            2,
            int(
                cfg.get("heartbeat_timeout_seconds")
                or self.heartbeat_seconds * 3
            ),
        )
        self.http_timeout = aiohttp.ClientTimeout(
            total=float(cfg.get("http_timeout_seconds", 10) or 10)
        )
        self.dedup_capacity = int(cfg.get("dedup_cache_size", 512) or 512)

        raw_servers = _normalize_servers(cfg.get("mc_servers"))
        existing = {link.name: link for link in self.links}
        links: list[ServerLink] = []
        for index, raw in enumerate(raw_servers):
            server_cfg = ServerConfig.from_dict(raw, index=index)
            link = existing.get(server_cfg.name)
            if link is not None:
                link.cfg = server_cfg
                link.envelope = self.build_envelope()
            else:
                link = ServerLink(self, server_cfg)
            links.append(link)
        self.links = links

    def build_envelope(self) -> Envelope:
        """契约 §1.2 规则 5：none/prefix 明文，encrypted 才有 crypto。"""
        crypto = None
        if self.security_mode == "encrypted":
            if not self.aes_key:
                logger.warning("[MCWL] security_mode=encrypted 但未配置 aes_key，按明文处理")
            else:
                crypto = AESCrypto(self.aes_key)
        return Envelope(crypto=crypto, prefix=self.prefix, proto_version=PROTO_VERSION)

    # ------------------------------------------------------------- 生命周期
    def enabled_links(self) -> list[ServerLink]:
        """契约 B8：对外编号 = mc_servers 配置顺序，跳过 enabled=false。"""
        return [link for link in self.links if link.cfg.enabled]

    def resolve(self, token: str | None) -> ServerLink | None:
        """按编号（从 1 开始，跳过禁用项）或名字定位服务器。"""
        links = self.enabled_links()
        if not token:
            return None
        text = str(token).strip()
        if text.isdigit():
            idx = int(text) - 1
            return links[idx] if 0 <= idx < len(links) else None
        for link in links:
            if link.name == text:
                return link
        for link in links:
            if text and text in link.name:
                return link
        return None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": f"{CLIENT_NAME}/{self.plugin_version}"},
            )
        for link in self.enabled_links():
            link.start()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.ensure_future(self._watchdog_loop())
        logger.info(
            f"[MCWL] 群服互联已启动：{len(self.enabled_links())} 个服务器，"
            f"安全模式={self.security_mode}"
        )

    async def stop(self) -> None:
        self._started = False
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await asyncio.wait_for(self._watchdog, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._watchdog = None
        for link in self.links:
            await link.stop()
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    # ------------------------------------------------------------- 心跳看门狗
    async def _watchdog_loop(self) -> None:
        """契约 B5：3× 心跳未收到 server_status → 判离线 + 标 pending_sync。"""
        while True:
            try:
                await asyncio.sleep(max(1, self.heartbeat_seconds / 3))
                now = time.time()
                for link in self.enabled_links():
                    if not link.connected:
                        continue
                    if link.last_seen and now - link.last_seen > self.heartbeat_timeout:
                        link.last_error = (
                            f"{int(now - link.last_seen)}s 未收到 server_status（心跳超时）"
                        )
                        link.pending_sync = True
                        logger.warning(f"[MCWL] {link.name} 心跳超时，判定离线并标记待同步")
                        if link.ws and not link.ws.closed:
                            await link.ws.close()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[MCWL] 心跳看门狗异常：{exc}")

    # ------------------------------------------------------------- 白名单推送
    async def request_push(self, link: ServerLink, *, force: bool = False) -> bool:
        if not self.hooks.get_whitelist:
            return False
        if not link.cfg.sync_whitelist and not force:
            return False
        version, entries = await self.hooks.get_whitelist()
        return await self.push_whitelist(link, version, entries)

    async def push_whitelist(
        self, link: ServerLink, version: int, entries: list[dict]
    ) -> bool:
        """契约 §2：只发全量（action 恒为 "full"）。"""
        payload = {"version": int(version), "action": "full", "entries": entries}
        ok = await link.send(MsgType.WHITELIST_UPDATE, payload)
        if not ok:
            await self.data_manager.update_server_state(link.name, pending_sync=True)
            link.pending_sync = True
        else:
            self.last_push_at[link.name] = time.time()
        return ok

    async def push_whitelist_all(
        self,
        version: int,
        entries: list[dict],
        targets: list[ServerLink] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """并发推送到所有（或指定）服务器；返回 {服务器名: {ok, detail}}。"""
        links = [
            link
            for link in (targets if targets is not None else self.enabled_links())
            if link.cfg.sync_whitelist
        ]
        if not links:
            return {}

        async def _push(link: ServerLink) -> tuple[str, dict[str, Any]]:
            ok = await self.push_whitelist(link, version, entries)
            return link.name, {
                "ok": ok,
                "detail": "" if ok else (link.last_error or "未连接"),
            }

        results = await asyncio.gather(
            *(_push(link) for link in links), return_exceptions=True
        )
        out: dict[str, dict[str, Any]] = {}
        for link, result in zip(links, results):
            if isinstance(result, Exception):
                out[link.name] = {"ok": False, "detail": f"{type(result).__name__}: {result}"}
            else:
                out[result[0]] = result[1]
        return out

    async def notify_protocol_mismatch(self, link: ServerLink) -> None:
        await self.data_manager.update_server_state(
            link.name,
            pending_sync=True,
            last_error="协议版本不一致，请同步升级插件与模组",
        )

    # ------------------------------------------------------------- HTTP 封装
    async def fetch_stats(self, link: ServerLink, player: str) -> tuple[bool, dict | None, str]:
        """契约 §3.3：玩家在该服无记录 → code:0, data:null（不是错误）。"""
        return await link.http_request(
            "GET", "/v2/mcwhitelist/stats/player", params={"name": player}
        )

    async def fetch_health(self, link: ServerLink) -> tuple[bool, dict | None, str]:
        return await link.http_request("GET", "/v2/mcwhitelist/health")

    async def fetch_whitelist(self, link: ServerLink) -> tuple[bool, dict | None, str]:
        return await link.http_request("GET", "/v2/mcwhitelist/whitelist")

    async def sync_via_http(
        self, link: ServerLink, version: int, entries: list[dict]
    ) -> tuple[bool, dict | None, str]:
        """契约 §1.3：POST /whitelist/sync 的 body 与 WS whitelist_update.data 完全一致。"""
        body = {"version": int(version), "action": "full", "entries": entries}
        return await link.http_request("POST", "/v2/mcwhitelist/whitelist/sync", json_body=body)

    # ------------------------------------------------------------- 消息推送
    async def broadcast_chat(
        self, sender: str, content: str, *, targets: list[ServerLink] | None = None
    ) -> dict[str, bool]:
        links = [
            link
            for link in (targets if targets is not None else self.enabled_links())
            if link.cfg.chat_sync
        ]
        if not content.strip() or not links:
            return {}

        async def _send(link: ServerLink) -> tuple[str, bool]:
            ok = await link.send(
                MsgType.CHAT,
                {"source": "qq", "sender": sender, "content": content, "server": link.name},
            )
            return link.name, ok

        results = await asyncio.gather(
            *(_send(link) for link in links), return_exceptions=True
        )
        out: dict[str, bool] = {}
        for link, result in zip(links, results):
            if isinstance(result, Exception):
                out[link.name] = False
            else:
                out[result[0]] = result[1]
        return out

    # ------------------------------------------------------------- 状态汇总
    def snapshot(self) -> list[dict[str, Any]]:
        return [link.snapshot() for link in self.enabled_links()]

    def connected_count(self) -> int:
        return sum(1 for link in self.enabled_links() if link.connected and link.authenticated)

    def pending_names(self) -> list[str]:
        return [link.name for link in self.enabled_links() if link.pending_sync]


def _normalize_servers(raw: Any) -> list[dict]:
    """mc_servers 可能是 template_list、dict 列表，也可能被存成 JSON 字符串。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except Exception:  # noqa: BLE001
            logger.warning("[MCWL] mc_servers 不是合法 JSON，已忽略")
            return []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return []
    servers: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            servers.append(item)
    return servers
