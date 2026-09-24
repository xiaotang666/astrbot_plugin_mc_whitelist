"""Mock 模组（按 docs/接口契约冻结_v10.3.md 实现服务端行为）。

用途：插件侧联调/回归——不依赖真实的 NeoForge 模组即可验证：
    · 握手与认证（auth / auth_result，含 proto_version 校验）
    · 全量白名单推送与 ack（B3）
    · 模组主动请求全量（B4）
    · 心跳 server_status 与超时判定（B5）
    · chat / player_event 推送（target_groups 为空则不应被插件处理）
    · HTTP：health / whitelist / whitelist/sync / stats/player（Bearer 鉴权）
    · 前缀、AES 密钥、proto_version 不匹配时的拒收行为（契约 §1.2 规则 1/4/5）
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from aiohttp import WSMsgType, web

import _loader

_loader.bootstrap()

from astrbot_plugin_mc_whitelist.core.crypto import AESCrypto  # noqa: E402
from astrbot_plugin_mc_whitelist.core.protocol import (  # noqa: E402
    PROTO_VERSION,
    Envelope,
    MsgType,
)


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class MockModServer:
    """一个可配置的假模组实例。"""

    def __init__(
        self,
        *,
        server_name: str = "生存服",
        aes_key: str = "test_key_16bytes",
        prefix: str = "[MC]",
        security_mode: str = "encrypted",
        token: str = "",
        heartbeat: float = 1.0,
        push_status: bool = True,
        protocol_mismatch: bool = False,
        auth_deny: bool = False,
        stats: dict[str, dict] | None = None,
        online_players: int = 3,
        max_players: int = 20,
        tps: float = 19.8,
    ) -> None:
        self.server_name = server_name
        self.security_mode = security_mode
        self.prefix = prefix
        self.token = token
        self.heartbeat = heartbeat
        self.protocol_mismatch = protocol_mismatch
        self.auth_deny = auth_deny
        self.stats = stats or {}
        self.online_players = online_players
        self.max_players = max_players
        self.tps = tps
        self.crypto = AESCrypto(aes_key) if security_mode == "encrypted" else None
        self.envelope = Envelope(crypto=self.crypto, prefix=prefix)

        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.ws: web.WebSocketResponse | None = None
        self.ws_port = 0
        self.http_port = 0

        # 记录（供断言）
        self.received: list[dict[str, Any]] = []
        self.auth_count = 0
        self.whitelist_updates: list[dict[str, Any]] = []
        self.http_calls: list[str] = []
        self.ws_connects = 0
        self.rejected: list[str] = []
        self._push_status_enabled = push_status
        self._heartbeat_task: asyncio.Task | None = None
        self._seq = 0

    # ------------------------------------------------------------- 生命周期
    def _routes(self) -> None:
        self.app.router.add_get("/ws", self._ws_handler)
        self.app.router.add_get("/v2/mcwhitelist/health", self._health)
        self.app.router.add_get("/v2/mcwhitelist/whitelist", self._whitelist)
        self.app.router.add_post("/v2/mcwhitelist/whitelist/sync", self._whitelist_sync)
        self.app.router.add_get("/v2/mcwhitelist/stats/player", self._stats_player)

    async def start(self) -> None:
        self._routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        ws_site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await ws_site.start()
        http_site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await http_site.start()
        self.ws_port = self._port_of(ws_site)
        self.http_port = self._port_of(http_site)
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    @staticmethod
    def _port_of(site: web.TCPSite) -> int:
        for address in site._server.sockets:  # noqa: SLF001 - 测试用
            return int(address.getsockname()[1])
        return 0

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await asyncio.wait_for(self._heartbeat_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._heartbeat_task = None
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/ws"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def set_push_status(self, enabled: bool) -> None:
        self._push_status_enabled = enabled

    # ------------------------------------------------------------- WS
    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=2**22)
        await ws.prepare(request)
        self.ws = ws
        self.ws_connects += 1
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._handle_text(message.data)
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            if self.ws is ws:
                self.ws = None
        return ws

    async def _handle_text(self, raw: str) -> None:
        try:
            message = self.envelope.decode(raw)
        except Exception as exc:  # noqa: BLE001 - 模组侧按契约拒收
            self.rejected.append(f"{type(exc).__name__}: {exc}")
            return
        data = dict(message.data or {})
        self.received.append({"type": message.type, "data": data, "msg_id": message.msg_id})

        if message.type == MsgType.AUTH:
            self.auth_count += 1
            await self._reply_auth(data)
        elif message.type == MsgType.WHITELIST_UPDATE:
            self.whitelist_updates.append(data)
            await self._reply_whitelist_ack(data)
        elif message.type == MsgType.CHAT:
            pass  # QQ→MC：模组会插进游戏聊天，这里只记录
        else:
            self.rejected.append(f"未知类型 {message.type}")

    async def _reply_auth(self, data: dict) -> None:
        success = not (self.auth_deny or self.protocol_mismatch)
        reason = None
        if self.protocol_mismatch:
            reason = "protocol_mismatch"
        elif self.auth_deny:
            reason = "bad_token"
        if data.get("token") != self.token and self.token:
            success = False
            reason = "bad_token"
        await self.send(
            MsgType.AUTH_RESULT,
            {
                "success": success,
                "server_name": self.server_name,
                "online_players": self.online_players,
                "proto_version": PROTO_VERSION,
                **({"reason": reason} if reason else {}),
            },
        )
        if success:
            # 契约 B4：连上就来一条全量同步请求（模组重启/换目录自愈）
            await self.send(MsgType.WHITELIST_SYNC_REQUEST, {})

    async def _reply_whitelist_ack(self, data: dict) -> None:
        entries = data.get("entries") or []
        await self.send(
            MsgType.WHITELIST_ACK,
            {
                "version": int(data.get("version") or 0),
                "count": len(entries),
                "success": True,
            },
        )

    async def send(self, msg_type: str, data: dict, *, prefix: str | None = None) -> None:
        """按契约编码后发给插件。"""
        payload = self.envelope.encode(msg_type, data)
        if prefix is not None:
            # 故意用错误前缀：重新构造一条（用于拒收测试）
            payload = self.envelope.encode(msg_type, data)
            outer = json.loads(payload)
            if self.crypto is not None:
                inner = json.loads(self.crypto.decrypt(outer["encrypted"]))
                inner["prefix"] = prefix
                outer["encrypted"] = self.crypto.encrypt(
                    json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
                )
            else:
                outer["prefix"] = prefix
            payload = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
        if self.ws and not self.ws.closed:
            await self.ws.send_str(payload)

    async def send_raw(self, raw: str) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.send_str(raw)

    def next_msg_id(self) -> str:
        self._seq += 1
        return f"mock-msg-{self._seq}"

    async def push_chat(
        self, sender: str, content: str, target_groups: list[int] | None = None
    ) -> str:
        msg_id = self.next_msg_id()
        await self.send(
            MsgType.CHAT,
            {
                "source": "mc",
                "sender": sender,
                "content": content,
                "server": self.server_name,
                "target_groups": target_groups if target_groups is not None else [123456],
                "msg_id": msg_id,
            },
        )
        return msg_id

    async def push_player_event(
        self, event: str, player: str, target_groups: list[int] | None = None
    ) -> None:
        await self.send(
            MsgType.PLAYER_EVENT,
            {
                "event": event,
                "player": player,
                "server": self.server_name,
                "target_groups": target_groups if target_groups is not None else [123456],
                "msg_id": self.next_msg_id(),
            },
        )

    async def push_sync_request(self) -> None:
        await self.send(MsgType.WHITELIST_SYNC_REQUEST, {})

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.heartbeat)
                if not self._push_status_enabled:
                    continue
                await self.send(
                    MsgType.SERVER_STATUS,
                    {
                        "server": self.server_name,
                        "online_players": self.online_players,
                        "max_players": self.max_players,
                        "tps": self.tps,
                        "msg_id": self.next_msg_id(),
                    },
                )
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------- HTTP
    def _check_auth(self, request: web.Request) -> bool:
        if not self.token:
            return True
        return request.headers.get("Authorization", "") == f"Bearer {self.token}"

    @staticmethod
    def _response(code: int, message: str, data: Any) -> web.Response:
        return web.json_response(
            {"code": code, "message": message, "data": data, "timestamp": _now()}
        )

    async def _health(self, request: web.Request) -> web.Response:
        self.http_calls.append("health")
        if not self._check_auth(request):
            return self._response(401, "unauthorized", None)
        return self._response(
            0,
            "success",
            {
                "server": self.server_name,
                "online_players": self.online_players,
                "whitelist_count": len(self.whitelist_updates[-1].get("entries", []))
                if self.whitelist_updates
                else 0,
                "uptime": int(time.time()) % 100000,
            },
        )

    async def _whitelist(self, request: web.Request) -> web.Response:
        self.http_calls.append("whitelist")
        if not self._check_auth(request):
            return self._response(401, "unauthorized", None)
        data = self.whitelist_updates[-1] if self.whitelist_updates else {"version": 0, "entries": []}
        return self._response(
            0, "success", {"version": data.get("version", 0), "entries": data.get("entries", [])}
        )

    async def _whitelist_sync(self, request: web.Request) -> web.Response:
        self.http_calls.append("whitelist_sync")
        if not self._check_auth(request):
            return self._response(401, "unauthorized", None)
        body = await request.json()
        self.whitelist_updates.append(body)
        entries = body.get("entries") or []
        return self._response(0, "success", {"version": body.get("version", 0), "count": len(entries)})

    async def _stats_player(self, request: web.Request) -> web.Response:
        self.http_calls.append("stats_player")
        if not self._check_auth(request):
            return self._response(401, "unauthorized", None)
        name = request.query.get("name", "")
        record = self.stats.get(name)
        if not record:
            # 契约 §3.3：无记录不是错误
            return self._response(0, "success", None)
        payload = dict(record)
        payload.setdefault("server", self.server_name)
        return self._response(0, "success", payload)
