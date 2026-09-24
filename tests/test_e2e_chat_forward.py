"""端到端验证：QQ 群消息能否真的转发到 MC。

真实部分：AstrBot 4.25.2 内核（事件对象、唤醒检查阶段、插件类、日志）、
        本插件的 WS 链路、真实协议（对端为 tests/mock_mod_server.py 假模组）。
替身部分：平台适配器（无 QQ 平台，用内核自己的 AstrMessageEvent 造一条群消息）。

用法：<AstrBot>/backend/python/python.exe tests/test_e2e_chat_forward.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

# 内核的 root 默认是当前工作目录（astrbot_path.get_astrbot_root()），
# 不重定向的话导入内核会在**仓库目录**里建 data/（cmd_config.json、data_v4.db、
# t2i_templates/），这些内核运行时会话文件会被误当成「项目文件」。
_E2E_ROOT = Path(tempfile.gettempdir()) / "mcwl_e2e_root"
_E2E_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ASTRBOT_ROOT", str(_E2E_ROOT))

APP = (
    r"D:/AstrBot_4.25.2-custom.20260604.e7d6d49c_windows_amd64_portable/backend/app"
)
sys.path.insert(0, APP)

from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import (  # noqa: E402
    AstrBotMessage,
    Group,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402

import astrbot_plugin_mc_whitelist as pkg  # noqa: E402  注册 handler 到真实内核注册表
from mock_mod_server import MockModServer  # noqa: E402
from test_interop import FakeKV  # noqa: E402

GROUP_ID = "88888888"
SENDER_ID = "10002"
SENDER_NAME = "测试玩家"
MESSAGE = "1"


def build_group_event(text: str) -> AstrMessageEvent:
    """造一条真实内核的群消息事件（与 aiocqhttp 走进来的对象同类型）。"""
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = "10000"
    msg.session_id = GROUP_ID
    msg.message_id = "msg-1"
    msg.sender = MessageMember(user_id=SENDER_ID, nickname=SENDER_NAME)
    msg.group = Group(group_id=GROUP_ID)
    msg.message_str = text
    msg.raw_message = MessageChain().message(text)
    msg.message = [Plain(text)]
    return AstrMessageEvent(
        text,
        msg,
        PlatformMetadata(name="aiocqhttp", id="napcat1", description="napcat"),
        GROUP_ID,
    )


class Ctx:
    """唤醒检查阶段所需的最小 ctx。"""

    def __init__(self) -> None:
        self.astrbot_config = {
            "wake_prefix": ["/"],
            "platform_settings": {
                "friend_message_needs_wake_prefix": False,
                "ignore_at_all": False,
                "ignore_bot_self_message": False,
                "no_permission_reply": True,
                "unique_session": False,
            },
            "plugin_set": ["*"],
            "disable_builtin_commands": False,
            "admins_id": [],
        }


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = True

    # ---- ① 真实内核唤醒检查：普通群消息跑完这一阶段后 is_wake 是什么？ ----
    event = build_group_event(MESSAGE)
    stage = WakingCheckStage()  # 无参构造，配置由 initialize(ctx) 注入
    ctx = Ctx()
    try:
        await stage.initialize(ctx)
        await stage.process(event)
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️ 唤醒阶段未能直接驱动（{type(exc).__name__}: {exc}），仅跳过该步")
    print(
        f"\n① 真实内核判定：is_wake={event.is_wake}  "
        f"is_at_or_wake_command={event.is_at_or_wake_command}\n"
        f"   → 旧代码用 is_wake_up() 当门禁：{'会被拦下（bug 复现）' if event.is_wake else '不会拦下'}\n"
        f"   → 新代码用 is_at_or_wake_command：{'放行 ✅' if not event.is_at_or_wake_command else '拦下'}"
    )
    if not event.is_wake:
        print("   ⚠️ 本次运行没复现出 is_wake=True（handler 是否注册？），结论需人工确认")
        ok = False

    # ---- ② 真插件 + 真 WS + 真协议：把这条消息交给插件处理器 ----
    mock = MockModServer(security_mode="none", token=None, heartbeat=0.5)
    await mock.start()
    cfg = {
        "mc_servers": [
            {"name": "测试服", "ws_url": mock.ws_url, "http_url": mock.http_url, "enabled": True}
        ],
        "security_mode": "none",
        "interop_enabled": True,
        "chat_sync_enabled": True,
        "group_mode": "whitelist",
        "group_list": [GROUP_ID],
        "enable_at_conversion": True,
        "enable_image_forward": True,
        "max_message_length": 256,
    }
    plugin = pkg.MCWhitelistPlugin(Ctx(), dict(cfg))
    # 内存 KV：真实内核的 KV 存储需要 plugin_id（本脚本没走完整的 Star 生命周期）
    plugin.data_manager.kv = FakeKV()
    await plugin.data_manager.load()
    plugin.manager.configure(cfg)
    await plugin.manager.start()
    link = plugin.manager.enabled_links()[0]
    for _ in range(60):
        if link.connected and link.authenticated:
            break
        await asyncio.sleep(0.2)
    print(f"\n② 链路状态：connected={link.connected} authenticated={link.authenticated}")

    # 群号识别：真实内核事件上取到的群号必须与内核 API 一致
    from astrbot_plugin_mc_whitelist.main import _safe_group_id

    seen = _safe_group_id(event)
    print(f"   群号识别：_safe_group_id={seen} / 内核 get_group_id={event.get_group_id()}")
    if seen != GROUP_ID:
        print("   ❌ 群号识别失败，白名单一定匹配不上")
        ok = False

    # 白/黑名单 × 群号正确/写错 —— 矩阵验证「配置里写对了就必须转发」
    cases = [
        ("白名单 + 群号正确", {"group_mode": "whitelist", "group_list": [GROUP_ID]}, True),
        ("白名单 + 整数型群号", {"group_mode": "whitelist", "group_list": [int(GROUP_ID)]}, True),
        ("白名单 + 群号写错", {"group_mode": "whitelist", "group_list": ["99999999"]}, False),
        ("黑名单 + 未命中", {"group_mode": "blacklist", "group_list": ["99999999"]}, True),
        ("黑名单 + 命中", {"group_mode": "blacklist", "group_list": [GROUP_ID]}, False),
    ]
    for label, over, expect in cases:
        plugin._config.update(over)

        def chats() -> list[dict]:
            return [m for m in mock.received if m["type"] == "chat"]

        before = len(chats())
        fresh = build_group_event(MESSAGE)  # 每条都用干净事件，避免上一次的状态残留
        await plugin.on_group_message(fresh)
        await asyncio.sleep(0.4)
        got = len(chats()) - before
        case_ok = got == (1 if expect else 0)
        ok = ok and case_ok
        print(f"   {'✅' if case_ok else '❌'} {label}：转发 {got} 条（期望 {1 if expect else 0}）")
        if not expect:
            # 被拒时原因必须点名「本插件看到的群号」，否则用户没法自查
            block = plugin._chat_forward_block(build_group_event(MESSAGE))
            reason = block[1] if block else ""
            told = GROUP_ID in reason and "group_list" in reason
            ok = ok and told
            print(f"      {'✅' if told else '❌'} 拒绝原因点名群号：{reason}")

    await plugin.manager.stop()
    await mock.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
