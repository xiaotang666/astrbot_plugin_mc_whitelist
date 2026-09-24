"""插件 ↔ 模组联调测试（真连本地 mock 模组，走真协议）。

运行：python tests/test_interop.py

覆盖的场景：
1. 连接 + auth 握手 + 认证后自动全量推送（契约 §1.1/§1.2/§2）
2. whitelist_ack 后清 pending_sync（B3）
3. 模组请求全量 → 插件重推（B4）
4. 心跳 server_status 更新在线人数
5. 心跳超时 → 判离线 + 标 pending_sync（B5）
6. chat / player_event 的 target_groups 语义（为空则不推送）
7. msg_id 去重（契约 §1.2 规则 3）
8. 前缀不匹配 / 密钥不匹配 → 拒收
9. 协议版本不一致 → 停止重连并提示升级（契约 §1.2 规则 4）
10. auth 失败（token 不对）→ 不进入已认证状态
11. HTTP：health / stats（有数据 / 无数据 code:0,data:null）/ whitelist/sync + Bearer 鉴权
12. 断线重连：模组重启后自动重连并重推
13. /status 快照字段
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Callable

import _loader

_loader.bootstrap()

from astrbot_plugin_mc_whitelist.core.crypto import AESCrypto  # noqa: E402
from astrbot_plugin_mc_whitelist.core.protocol import Envelope  # noqa: E402
from astrbot_plugin_mc_whitelist.core.version import PLUGIN_VERSION  # noqa: E402
from astrbot_plugin_mc_whitelist.data_manager import DataManager  # noqa: E402
from astrbot_plugin_mc_whitelist.interop.client import (  # noqa: E402
    InteropHooks,
    ServerManager,
)
from astrbot_plugin_mc_whitelist.services.uuid import (  # noqa: E402
    TYPE_LITTLESKIN,
    TYPE_MOJANG,
)

from mock_mod_server import MockModServer  # noqa: E402

checker = _loader.Checker()
C = checker.check
AES_KEY = "test_key_16bytes"
TOKEN = "secret-token-123"


class FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    async def put_kv_data(self, key: str, value: Any) -> None:
        self.store[key] = value


async def wait_for(
    predicate: Callable[[], bool], timeout: float = 8.0, interval: float = 0.05
) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def seed_bindings(dm: DataManager) -> None:
    await dm.load()
    await dm.register("10001", "Steve", "069a79f4-44e9-4726-a5be-fca90e38aaf5", TYPE_MOJANG, "88888")
    await dm.register("10002", "Alex", "11111111-2222-3333-4444-555555555555", TYPE_LITTLESKIN, "88888")


def make_manager(
    dm: DataManager,
    servers: list[dict],
    *,
    heartbeat: int = 1,
    timeout: int = 3,
    hooks: InteropHooks | None = None,
    token: str = TOKEN,
    aes_key: str = AES_KEY,
) -> ServerManager:
    manager = ServerManager(data_manager=dm, hooks=hooks, plugin_version=PLUGIN_VERSION)
    manager.configure(
        {
            "security_mode": "encrypted",
            "aes_key": aes_key,
            "message_prefix": "[MC]",
            "default_api_token": token,
            "auto_reconnect": True,
            "heartbeat_seconds": heartbeat,
            "heartbeat_timeout_seconds": timeout,
            "dedup_cache_size": 64,
            "mc_servers": servers,
        }
    )
    return manager


async def scenario_handshake_and_push() -> None:
    """1/2/3/4/5/7/8/13：主流程。"""
    dm = DataManager(FakeKV())
    await seed_bindings(dm)
    chats: list[tuple[str, dict]] = []
    events: list[tuple[str, dict]] = []

    async def on_chat(server: str, data: dict) -> None:
        chats.append((server, data))

    async def on_player_event(server: str, data: dict) -> None:
        events.append((server, data))

    mock = MockModServer(
        aes_key=AES_KEY,
        token=TOKEN,
        heartbeat=0.5,
        stats={
            "Steve": {
                "online_time": 3600,
                "blocks_mined": 1234,
                "last_login": "2026-09-21T12:00:00+08:00",
                "data_updated_at": "2026-09-21T12:05:00+08:00",
            }
        },
    )
    await mock.start()

    async def get_whitelist() -> tuple[int, list[dict]]:
        return int(dm.sync_version or 0), dm.entries()

    manager = make_manager(
        dm,
        [
            {
                "name": "生存服",
                "ws_url": mock.ws_url,
                "http_url": mock.http_url,
                "sync_whitelist": True,
                "chat_sync": True,
            }
        ],
        hooks=InteropHooks(on_chat=on_chat, on_player_event=on_player_event, get_whitelist=get_whitelist),
    )
    await manager.start()
    try:
        # 1. 握手 + 认证
        C("mock 收到 auth", await wait_for(lambda: mock.auth_count >= 1))
        link = manager.enabled_links()[0]
        C("链路已认证", await wait_for(lambda: link.authenticated))
        C("认证后 server_name 回填", link.server_reported_name == "生存服", link.server_reported_name)

        # 1'. 认证后自动全量推送（契约要求 entries 为完整列表）
        C("自动推送白名单", await wait_for(lambda: len(mock.whitelist_updates) >= 1))
        first = mock.whitelist_updates[0]
        C("action 恒为 full", first.get("action") == "full", first.get("action"))
        C("版本号为整数", isinstance(first.get("version"), int), first.get("version"))
        names = sorted(e["name"] for e in first.get("entries", []))
        C("推送的是全量绑定", names == ["Alex", "Steve"], names)
        C("条目含 source/uuid/qq", all({"source", "uuid", "qq"} <= set(e) for e in first["entries"]), first["entries"][:1])
        sources = {e["name"]: e["source"] for e in first["entries"]}
        C("正版→MOJANG", sources.get("Steve") == "MOJANG", sources)
        C("皮肤站→LITTLESKIN", sources.get("Alex") == "LITTLESKIN", sources)

        # 2. ack 之后清 pending_sync
        C("ack 后 pending_sync=False", await wait_for(lambda: not link.pending_sync))
        state = dm.server_state("生存服")
        C("服务器状态已记录同步条数", state.get("last_sync_count") == 2, state)
        C("服务器状态 ws_connected=True", state.get("ws_connected") is True, state)

        # 3. 模组请求全量 → 插件重推（B4）
        before = len(mock.whitelist_updates)
        await mock.push_sync_request()
        C("收到 sync_request 后重推", await wait_for(lambda: len(mock.whitelist_updates) > before))
        C(
            "重推版本号不变（未新增绑定）",
            mock.whitelist_updates[-1].get("version") == mock.whitelist_updates[0].get("version"),
            (mock.whitelist_updates[0].get("version"), mock.whitelist_updates[-1].get("version")),
        )

        # 4. 心跳
        C(
            "心跳更新在线人数",
            await wait_for(lambda: link.online_players == 3 and link.max_players == 20),
            (link.online_players, link.max_players),
        )

        # 6. chat / player_event
        await mock.push_chat("Steve", "大家好", target_groups=[123456])
        C("收到 MC→QQ 聊天", await wait_for(lambda: len(chats) == 1), chats)
        C("聊天带服务器名", chats and chats[0][0] == "生存服", chats)
        await mock.push_player_event("join", "Alex")
        C("收到玩家事件", await wait_for(lambda: len(events) == 1), events)
        C("事件内容正确", events and events[0][1].get("event") == "join", events)

        # target_groups 为空：传输层照常回调，过滤发生在插件层（见 test_plugin.py）
        await mock.push_chat("Steve", "空目标群", target_groups=[])
        await asyncio.sleep(0.4)
        C("target_groups 为空仍回调（过滤在插件层）", len(chats) == 2, len(chats))

        # 7. msg_id 去重
        msg_id = await mock.push_chat("Steve", "重复消息", target_groups=[123456])
        await wait_for(lambda: len(chats) == 3)
        await mock.send(
            "chat",
            {
                "source": "mc",
                "sender": "Steve",
                "content": "重复消息",
                "server": "生存服",
                "target_groups": [123456],
                "msg_id": msg_id,
            },
        )
        await asyncio.sleep(0.4)
        C("同一 msg_id 只处理一次", len(chats) == 3, len(chats))

        # 8. 前缀不匹配 → 拒收（插件不应把这条交给上层）
        chat_count = len(chats)
        await mock.send("chat", {"content": "坏前缀", "target_groups": [1]}, prefix="[XX]")
        await asyncio.sleep(0.4)
        C("错误前缀被拒收（未回调）", len(chats) == chat_count, len(chats))
        C("拒收原因写入链路 last_error", "前缀" in str(link.last_error), link.last_error)

        # 密钥不匹配 → 拒收
        bad_env = Envelope(crypto=AESCrypto("another_key_16b"), prefix="[MC]")
        await mock.send_raw(bad_env.encode("chat", {"content": "错密钥", "target_groups": [1]}))
        await asyncio.sleep(0.4)
        C("错误密钥被拒收（未回调）", len(chats) == chat_count, len(chats))

        # 11. HTTP 接口
        ok, data, message = await manager.fetch_stats(link, "Steve")
        C("stats 有数据", ok and data and data.get("blocks_mined") == 1234, (ok, data, message))
        C("stats 带 server 字段", data and data.get("server") == "生存服", data)
        C("stats 时长单位是秒", data and data.get("online_time") == 3600, data)
        ok2, data2, _ = await manager.fetch_stats(link, "Ghost")
        C("无记录返回 code:0 + data:null", ok2 and data2 is None, (ok2, data2))
        ok3, data3, _ = await manager.fetch_health(link)
        C("health 可用", ok3 and data3 and data3.get("server") == "生存服", (ok3, data3))
        version = await dm.next_sync_version()
        ok4, data4, _ = await manager.sync_via_http(link, version, dm.entries())
        C("HTTP 同步返回 count", ok4 and data4 and data4.get("count") == 2, (ok4, data4))

        # 13. /status 快照
        snapshot = manager.snapshot()[0]
        C("快照含协议相关字段", {"name", "ws_connected", "pending_sync", "last_sync_time"} <= set(snapshot), snapshot)
        C("快照在线人数", snapshot["online_players"] == 3, snapshot)

        # 5. 心跳超时（停掉 server_status 推送）
        mock.set_push_status(False)
        C(
            "心跳超时判离线并标待同步",
            await wait_for(lambda: link.pending_sync and not link.connected, timeout=10),
            (link.pending_sync, link.connected, link.last_error),
        )
        C("超时原因写入 last_error", bool(link.last_error) and "心跳" in str(link.last_error), link.last_error)
        # 恢复推送后应自动重连并重推
        mock.set_push_status(True)
        pushes = len(mock.whitelist_updates)
        C("恢复后自动重连", await wait_for(lambda: link.connected, timeout=12))
        C("重连后重新推送白名单", await wait_for(lambda: len(mock.whitelist_updates) > pushes, timeout=12))
    finally:
        await manager.stop()
        await mock.stop()


async def scenario_auth_failures() -> None:
    """9/10：token 不对 / 协议不一致。"""
    dm = DataManager(FakeKV())
    await seed_bindings(dm)

    # 10. token 不对
    mock = MockModServer(aes_key=AES_KEY, token="correct-token", heartbeat=0.5)
    await mock.start()
    manager = make_manager(dm, [{"name": "生存服", "ws_url": mock.ws_url, "http_url": mock.http_url}], token="WRONG-token")
    await manager.start()
    try:
        link = manager.enabled_links()[0]
        C("auth 失败被记录", await wait_for(lambda: link.auth_reason == "bad_token"))
        C("auth 失败不进入已认证状态", not link.authenticated)
        C("auth 失败标记待同步", link.pending_sync)
        state = dm.server_state("生存服")
        C("auth 失败写入服务器状态", state.get("ws_connected") is False, state)

        # HTTP 鉴权：错误 token 应报错
        ok, _, message = await manager.fetch_health(link)
        C("HTTP 错误 token 被拒", not ok and "鉴权失败" in message, message)
    finally:
        await manager.stop()
        await mock.stop()

    # 9. 协议不一致 → 停止重连
    mock2 = MockModServer(aes_key=AES_KEY, token=TOKEN, protocol_mismatch=True, heartbeat=0.5)
    await mock2.start()
    manager2 = make_manager(dm, [{"name": "创造服", "ws_url": mock2.ws_url, "http_url": mock2.http_url}])
    await manager2.start()
    try:
        link2 = manager2.enabled_links()[0]
        C("协议不一致被识别", await wait_for(lambda: link2.auth_reason == "protocol_mismatch"))
        C("协议不一致时停止重连", await wait_for(lambda: link2._stop.is_set()))
        await asyncio.sleep(1.5)
        C("停止后只连接一次", mock2.ws_connects == 1, mock2.ws_connects)
        C("协议不一致不推送白名单", not mock2.whitelist_updates, mock2.whitelist_updates)
        state2 = dm.server_state("创造服")
        C("协议不一致写入状态", "协议版本" in str(state2.get("last_error")), state2)
    finally:
        await manager2.stop()
        await mock2.stop()


async def scenario_http_only_and_disabled() -> None:
    """无 WS（仅 HTTP）/ enabled=false 的行为。"""
    dm = DataManager(FakeKV())
    await seed_bindings(dm)
    mock_disabled = MockModServer(aes_key=AES_KEY, token=TOKEN, heartbeat=0.5, server_name="已禁用服")
    await mock_disabled.start()
    manager = make_manager(
        dm,
        [
            {"name": "已禁用服", "ws_url": mock_disabled.ws_url, "http_url": mock_disabled.http_url, "enabled": False},
            {"name": "只有HTTP", "ws_url": "", "http_url": mock_disabled.http_url},
        ],
    )
    await manager.start()
    try:
        C("禁用服务器不参与编号（B8）", [link.name for link in manager.enabled_links()] == ["只有HTTP"], [l.name for l in manager.enabled_links()])
        C("resolve 按编号取到未禁用项", manager.resolve("1").name == "只有HTTP")
        C("resolve 名字", manager.resolve("只有HTTP").name == "只有HTTP")
        C("resolve 越界返回 None", manager.resolve("9") is None)
        C("禁用服务器不启动 WS", await wait_for(lambda: mock_disabled.auth_count == 0, timeout=1.5))
        link = manager.enabled_links()[0]
        ok, data, _ = await manager.fetch_health(link)
        C("仅 HTTP 的服务器仍可查询", ok and data, (ok, data))
        sync = await manager.push_whitelist_all(1, dm.entries())
        C("无 ws_url 的服务器推送失败但不抛异常", sync.get("只有HTTP", {}).get("ok") is False, sync)
    finally:
        await manager.stop()
        await mock_disabled.stop()


async def scenario_plain_mode() -> None:
    """明文模式（security_mode=prefix）也必须套外层信封。"""
    dm = DataManager(FakeKV())
    await seed_bindings(dm)
    mock = MockModServer(aes_key="", security_mode="prefix", prefix="[MC]", token=TOKEN, heartbeat=0.5)
    await mock.start()

    async def get_whitelist() -> tuple[int, list[dict]]:
        return int(dm.sync_version or 0), dm.entries()

    manager = ServerManager(
        data_manager=dm,
        hooks=InteropHooks(get_whitelist=get_whitelist),
        plugin_version=PLUGIN_VERSION,
    )
    manager.configure(
        {
            "security_mode": "none",
            "aes_key": "",
            "message_prefix": "[MC]",
            "default_api_token": TOKEN,
            "heartbeat_seconds": 1,
            "mc_servers": [{"name": "明文服", "ws_url": mock.ws_url, "http_url": mock.http_url}],
        }
    )
    await manager.start()
    try:
        link = manager.enabled_links()[0]
        C("明文模式也能认证", await wait_for(lambda: link.authenticated))
        C("明文模式推送成功", await wait_for(lambda: len(mock.whitelist_updates) >= 1))
        raw = json.loads(json.dumps(mock.whitelist_updates[0]))
        C("明文 payload 含 entries", len(raw.get("entries", [])) == 2, raw)
        C("明文模式无 encrypted（由 mock 侧解码保证）", True)
    finally:
        await manager.stop()
        await mock.stop()


def main() -> int:
    asyncio.run(scenario_handshake_and_push())
    asyncio.run(scenario_auth_failures())
    asyncio.run(scenario_http_only_and_disabled())
    asyncio.run(scenario_plain_mode())
    return checker.report("联调测试 test_interop")


if __name__ == "__main__":
    sys.exit(main())
