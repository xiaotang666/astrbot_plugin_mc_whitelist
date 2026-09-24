"""WS 连通性自检：临时建连 + 认证握手，回答「这个服到底连上了没有」。

设计要点
    * 与常驻的 ServerLink **完全隔离**：独立建连、独立信封、读完即关，
      既不影响正在跑的长连接，也不会触发白名单推送 / 状态变更。
    * 认证消息与常驻链路用同一套构造方式（同 token、同 proto_version、同一信封），
      所以「测通了」就等于真实链路能通。
    * 失败按阶段分类（config / dial / auth），并给出人话处置建议——
      端口不通、没装模组、token 不一致、密钥不一致、协议版本不一致，
      这几种原因的处置方式完全不同，不能糊成一句「连接失败」。

依赖契约：docs/接口契约冻结_v10.3.md §1.1（模组 = WS Server）/§1.2（信封）/§2（auth、auth_result）
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..core.crypto import CryptoError
from ..core.protocol import PROTO_VERSION, MsgType, ProtocolError
from ..interop.client import CLIENT_NAME, ServerLink, ServerManager

logger = logging.getLogger("astrbot")

DEFAULT_TEST_TIMEOUT = 12.0

# 阶段 → 中文标签（插件页面直接显示）
STAGE_LABELS: dict[str, str] = {
    "config": "配置",
    "dial": "建连",
    "auth": "认证",
    "ok": "通过",
}

# 模组侧 auth_result.reason → 人话
_AUTH_REASON_TEXT: dict[str, str] = {
    "bad_token": "token 与模组不一致",
    "token_mismatch": "token 与模组不一致",
    "invalid_token": "token 与模组不一致",
    "protocol_mismatch": f"协议版本不一致（插件 proto_version={PROTO_VERSION}）",
    "server_disabled": "模组侧已禁用该服务器",
    "disabled": "模组侧已禁用该服务器",
}

_AUTH_REASON_HINT: dict[str, str] = {
    "bad_token": "把插件 mc_servers 里的 token 与模组配置里的 token 改成一致",
    "token_mismatch": "把插件 mc_servers 里的 token 与模组配置里的 token 改成一致",
    "invalid_token": "把插件 mc_servers 里的 token 与模组配置里的 token 改成一致",
    "protocol_mismatch": "插件与模组必须同时升级到同一契约版本（看双方 CHANGELOG）",
    "server_disabled": "在模组侧配置里启用该服务器",
    "disabled": "在模组侧配置里启用该服务器",
}


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _explain_dial_error(exc: BaseException, url: str) -> tuple[str, str]:
    """把建连阶段的异常翻译成「发生了什么」+「怎么办」。"""
    name = type(exc).__name__

    if isinstance(exc, aiohttp.InvalidURL):
        return (
            f"地址格式不对：{url}",
            "ws_url 要以 ws:// 或 wss:// 开头，不能带空格",
        )

    proxy_error = getattr(aiohttp, "ClientProxyConnectionError", None)
    if proxy_error is not None and isinstance(exc, proxy_error):
        return (
            "代理连不上",
            "检查插件所在机器的系统代理 / 本机 HTTP_PROXY 设置",
        )

    if isinstance(exc, aiohttp.WSServerHandshakeError):
        status = getattr(exc, "status", None)
        if status in (401, 403):
            return (
                f"服务端拒绝了 WS 握手（HTTP {status}）",
                "模组侧开了 WS 鉴权，或被反向代理挡住，检查反代与模组鉴权开关",
            )
        if status == 404:
            return (
                "服务端返回 404（WS 路径不存在）",
                "ws_url 的路径要和模组配置一致（默认 /ws）",
            )
        return (
            f"WS 握手失败（HTTP {status}）",
            "看服务端日志，确认 WS 服务正常监听",
        )

    if isinstance(exc, aiohttp.ClientSSLError):
        return (
            "TLS 握手失败（证书问题）",
            "wss:// 需要有效证书；自签证书可改用 ws:// 或让服务端配好证书",
        )

    connector_error = getattr(aiohttp, "ClientConnectorError", None)
    if connector_error is not None and isinstance(exc, connector_error):
        os_error = getattr(exc, "os_error", None)
        if isinstance(os_error, socket.gaierror):
            return (
                "域名解析失败",
                "ws_url 里的主机名写错了，或插件所在机器 DNS 不通——换成 IP 试试",
            )
        return (
            "TCP 连不上（服务端没启动 / 端口不对 / 防火墙）",
            "确认 Minecraft 服务端已启动并装了模组、端口与模组配置一致，且防火墙已放行",
        )

    if isinstance(exc, aiohttp.ClientOSError):
        return (
            "TCP 连不上（服务端没启动 / 端口不对 / 防火墙）",
            "确认服务端已启动、端口与模组配置一致，且防火墙已放行",
        )

    if isinstance(exc, asyncio.TimeoutError):  # aiohttp.ServerTimeoutError 也走这里
        return (
            f"连接超时（{url} 没回应）",
            "地址不可达或被丢包：先 telnet 一下 IP 端口，再查服务器防火墙",
        )

    return (
        f"建连失败：{name}: {exc}",
        "把这条原始报错发给服务端一起看",
    )


@dataclass
class WsCheck:
    """单次 WS 自检的结果。"""

    server: str
    ok: bool = False
    stage: str = "config"
    detail: str = ""
    ms: int = 0
    suggestion: str = ""
    reported_name: str | None = None
    reason: str | None = None
    close_code: int | None = None
    ws_url: str = ""
    enabled: bool = True

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "ok": self.ok,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "detail": self.detail,
            "ms": self.ms,
            "suggestion": self.suggestion,
            "reported_name": self.reported_name,
            "reason": self.reason,
            "close_code": self.close_code,
            "ws_url": self.ws_url,
            "enabled": self.enabled,
        }


async def _close_quietly(ws: Any) -> None:
    if ws is None:
        return
    try:
        if not ws.closed:
            await ws.close()
    except Exception:  # noqa: BLE001 - 收尾失败不影响结论
        pass


class WsConnectivityTester:
    """对配置里的服务器逐个做「建连 → 认证」自检。"""

    def __init__(self, manager: ServerManager) -> None:
        self.manager = manager

    # ------------------------------------------------------------- 对外
    async def test_all(self, timeout: float = DEFAULT_TEST_TIMEOUT) -> list[WsCheck]:
        checks = [await self.test_link(link, timeout=timeout) for link in self.manager.links]
        return checks

    async def test_target(self, target: str, timeout: float = DEFAULT_TEST_TIMEOUT) -> WsCheck:
        """target 支持服务器名（精确 → 忽略大小写）或 1 起的编号。"""
        link = self.resolve_link(target)
        if link is None:
            return WsCheck(
                server=target or "（未指定）",
                stage="config",
                detail=f"没有找到名为「{target}」的服务器",
                suggestion="刷新页面重新读取 mc_servers 列表，或检查名称是否写对",
            )
        return await self.test_link(link, timeout=timeout)

    def resolve_link(self, target: str | None) -> ServerLink | None:
        raw = (target or "").strip()
        if not raw:
            return None
        for link in self.manager.links:
            if link.name == raw:
                return link
        lowered = raw.lower()
        for link in self.manager.links:
            if link.name.lower() == lowered:
                return link
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(self.manager.links):
                return self.manager.links[index]
        return None

    def server_rows(self) -> list[dict[str, Any]]:
        """给插件页面用的服务器清单（含常驻连接当前状态，便于对照）。

        状态直接读链路对象自身的实时属性——不要用 `snapshot()`：
        那个走 data_manager，把「进程内实时状态」和「落库状态」混在一起了。
        """
        rows: list[dict[str, Any]] = []
        for index, link in enumerate(self.manager.links, start=1):
            rows.append(
                {
                    "index": index,
                    "name": link.name,
                    "ws_url": link.cfg.ws_url,
                    "http_url": link.cfg.http_url,
                    "enabled": link.cfg.enabled,
                    "connected": bool(link.connected),
                    "authenticated": bool(link.authenticated),
                    "pending_sync": bool(link.pending_sync),
                    "last_error": link.last_error,
                    "server_reported_name": link.server_reported_name,
                    "online_players": link.online_players,
                    "retry_delay": link.retry_delay,
                }
            )
        return rows

    # ------------------------------------------------------------- 单个
    async def test_link(
        self, link: ServerLink, timeout: float = DEFAULT_TEST_TIMEOUT
    ) -> WsCheck:
        cfg = link.cfg
        check = WsCheck(server=cfg.name, ws_url=cfg.ws_url, enabled=cfg.enabled)

        # --- 阶段 0：先看配置本身对不对（省得白等一次超时）
        if not cfg.ws_url:
            check.stage = "config"
            check.detail = "没有配置 ws_url"
            check.suggestion = "在该服务器的配置里填 ws_url（形如 ws://IP:端口/ws）"
            return check
        if not cfg.ws_url.lower().startswith(("ws://", "wss://")):
            check.stage = "config"
            check.detail = f"ws_url 协议不对：{cfg.ws_url}"
            check.suggestion = "要以 ws:// 或 wss:// 开头"
            return check

        session = self.manager.session
        temp_session: aiohttp.ClientSession | None = None
        if session is None or session.closed:
            temp_session = session = aiohttp.ClientSession()
        envelope = self.manager.build_envelope()
        started = time.perf_counter()
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            # --- 阶段 1：建连
            try:
                ws = await session.ws_connect(
                    cfg.ws_url,
                    heartbeat=None,
                    max_msg_size=2**22,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                )
            except Exception as exc:  # noqa: BLE001 - 全部翻译成人话
                check.stage = "dial"
                check.ms = _ms(started)
                check.detail, check.suggestion = _explain_dial_error(exc, cfg.ws_url)
                return check

            check.ms = _ms(started)

            # --- 阶段 2：发认证（与常驻链路同构）
            try:
                payload = envelope.encode(
                    MsgType.AUTH,
                    {
                        "token": link.token(),
                        "client": CLIENT_NAME,
                        "version": self.manager.plugin_version,
                        "proto_version": PROTO_VERSION,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 多半是 aes_key 不合法
                check.stage = "auth"
                check.detail = f"构建认证消息失败：{type(exc).__name__}: {exc}"
                check.suggestion = "检查 aes_key（encrypted 模式要求 16/24/32 字符）"
                return check

            try:
                await ws.send_str(payload)
            except Exception as exc:  # noqa: BLE001
                check.stage = "auth"
                check.detail = f"发送认证消息失败：{type(exc).__name__}: {exc}"
                check.suggestion = "连接刚建好就被断开，看服务端日志"
                return check

            # --- 阶段 3：等认证应答
            check.stage = "auth"
            deadline = time.perf_counter() + timeout
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    check.ms = _ms(started)
                    check.detail = f"连上了，但 {timeout:.0f} 秒内没收到认证应答"
                    check.suggestion = (
                        "TCP 能连不等于模组在应答。按可能性排查：① 服务端没装 / 没启用模组；"
                        "② ws_url 指向了别的服务（比如反代或另一个端口）；"
                        "③ message_prefix / aes_key / security_mode / proto_version 与模组不一致"
                        "—— 模组会静默丢弃解不开的消息，表现就是「没应答」"
                    )
                    return check
                try:
                    message = await ws.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    continue

                if message.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = (
                        message.data
                        if isinstance(message.data, str)
                        else message.data.decode("utf-8", "replace")
                    )
                    try:
                        decoded = envelope.decode(raw)
                    except (ProtocolError, CryptoError) as exc:
                        check.ms = _ms(started)
                        check.detail = f"收到应答但被拒收：{exc}"
                        check.suggestion = (
                            "信封对不上：检查 message_prefix、aes_key、"
                            "security_mode 与 proto_version 是否与模组一致"
                        )
                        return check

                    if decoded.type != MsgType.AUTH_RESULT:
                        # 心跳之类的其他消息 —— 说明对面确实是模组，继续等认证应答
                        continue

                    data = dict(decoded.data or {})
                    check.ms = _ms(started)
                    check.reported_name = str(data.get("server_name") or "") or None
                    check.reason = data.get("reason")
                    if bool(data.get("success")):
                        check.ok = True
                        check.stage = "ok"
                        check.detail = (
                            f"认证通过（模组自称：{check.reported_name}）"
                            if check.reported_name
                            else "认证通过"
                        )
                    else:
                        check.stage = "auth"
                        reason = str(check.reason or "unknown")
                        check.detail = f"认证被拒：{_AUTH_REASON_TEXT.get(reason, reason)}"
                        check.suggestion = _AUTH_REASON_HINT.get(
                            reason, "看服务端日志里这条 auth 失败的原因"
                        )
                    return check

                if message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    check.ms = _ms(started)
                    check.close_code = ws.close_code
                    check.detail = f"认证过程中 WS 被服务端关闭（code={ws.close_code}）"
                    check.suggestion = "看服务端日志；若是 1006 多为网络中断或反代超时"
                    return check
        finally:
            await _close_quietly(ws)
            if temp_session is not None:
                try:
                    await temp_session.close()
                except Exception:  # noqa: BLE001
                    pass


__all__ = ["DEFAULT_TEST_TIMEOUT", "STAGE_LABELS", "WsCheck", "WsConnectivityTester"]
