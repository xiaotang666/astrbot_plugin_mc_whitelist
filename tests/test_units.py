"""单元测试：数据管理 / UUID 校验 / 消息转换 / 权限 / 统计图片。

运行：python tests/test_units.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import _loader

_loader.bootstrap()
import astrbot_stub  # noqa: E402  (SDK 替身，测试里用于构造 event)

from astrbot_plugin_mc_whitelist.data_manager import DataManager  # noqa: E402
from astrbot_plugin_mc_whitelist.interop.convert import message_to_text  # noqa: E402
from astrbot_plugin_mc_whitelist.services.perm import (  # noqa: E402
    NODE_BLACKLIST,
    NODE_REGISTER,
    NODE_STATS,
    PermissionService,
)
from astrbot_plugin_mc_whitelist.services.stats_image import (  # noqa: E402
    format_duration,
    format_number,
    list_backgrounds,
    render_stats_image,
    resolve_background_files,
)
from astrbot_plugin_mc_whitelist.services.uuid import (  # noqa: E402
    TYPE_LITTLESKIN,
    TYPE_MOJANG,
    format_uuid,
    parse_type,
    validate_username,
)

UUID_MOJANG = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_SKIN = "11111111-2222-3333-4444-555555555555"

checker = _loader.Checker()
C = checker.check


class FakeKV:
    """模拟 AstrBot 插件 KV。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    async def put_kv_data(self, key: str, value: Any) -> None:
        self.store[key] = value


# ------------------------------------------------------------------ 数据管理
def test_data_manager() -> None:
    kv = FakeKV()
    dm = DataManager(kv)

    async def scenario() -> None:
        await dm.load()
        ok, msg = await dm.register("10001", "Steve", UUID_MOJANG, TYPE_MOJANG, "88888")
        C("注册成功", ok, msg)
        ok2, _ = await dm.register("10001", "Alex", "11111111-2222-3333-4444-555555555555", TYPE_MOJANG)
        C("同一 QQ 二次绑定被拒", not ok2)
        ok3, _ = await dm.register("10002", "steve", UUID_SKIN, TYPE_LITTLESKIN)
        C("用户名大小写不敏感查重（B9）", not ok3)
        C("username_exists 大小写不敏感", dm.username_exists("STEVE"))
        C("绑定计数", dm.binding_count() == 1, dm.binding_count())

        entries = dm.entries()
        C("条目数量", len(entries) == 1, entries)
        C("条目 source=MOJANG", entries[0]["source"] == "MOJANG", entries)
        C("条目带 qq", entries[0]["qq"] == "10001", entries)
        C("条目 uuid 标准化", entries[0]["uuid"] == UUID_MOJANG, entries)

        # 皮 → 条目 source=LITTLESKIN
        await dm.register("10003", "Notch", UUID_SKIN, TYPE_LITTLESKIN)
        skin = [e for e in dm.entries() if e["name"] == "Notch"][0]
        C("皮类型条目 source=LITTLESKIN", skin["source"] == "LITTLESKIN", skin)

        # 迁移
        ok4, _ = await dm.update_binding("10001", "Steve_New", UUID_SKIN, TYPE_LITTLESKIN)
        C("迁移成功", ok4)
        C("迁移后旧名释放", not dm.username_exists("Steve"))
        C("迁移后新名占用", dm.username_exists("steve_new"))
        C("迁移后条目名字更新", any(e["name"] == "Steve_New" for e in dm.entries()))

        # 黑名单：自动解绑 + 从白名单剔除
        added, removed = await dm.add_blacklist("10003")
        C("加入黑名单", added)
        C("黑名单自动解绑", removed is not None and removed.get("username") == "Notch", removed)
        C("黑名单用户不在白名单", all(e["qq"] != "10003" for e in dm.entries()))
        C("is_blacklisted", dm.is_blacklisted("10003"))
        C("移出黑名单", await dm.remove_blacklist("10003"))
        C("重复移出返回 False", not await dm.remove_blacklist("10003"))

        # 版本号 & 背景图轮询
        v1 = await dm.next_sync_version()
        v2 = await dm.next_sync_version()
        C("同步版本自增", (v1, v2) == (1, 2), (v1, v2))
        idx = [await dm.next_background_index() for _ in range(3)]
        C("背景图下标轮询", idx == [0, 1, 2], idx)

        # 服务器状态
        await dm.mark_synced("生存服", 3)
        state = dm.server_state("生存服")
        C("标记已同步", state["pending_sync"] is False and state["last_sync_count"] == 3, state)

        # 持久化往返
        dm2 = DataManager(kv)
        await dm2.load()
        C("重新加载后绑定一致", dm2.binding_count() == dm.binding_count(), dm2.binding_count())
        C("重新加载后黑名单一致", dm2.blacklist() == dm.blacklist(), dm2.blacklist())
        C("重新加载后 server_states 一致", "生存服" in dm2.server_states)

        # 并发注册竞态：同名 20 个 QQ 同时注册，只能成功 1 个（B7 锁）
        dm3 = DataManager(FakeKV())
        await dm3.load()
        results = await asyncio.gather(
            *(
                dm3.register(f"2000{i}", "Contested", UUID_MOJANG, TYPE_MOJANG)
                for i in range(20)
            )
        )
        success = [r for r in results if r[0]]
        C("并发注册同名只成功 1 次", len(success) == 1, f"成功 {len(success)} 次")
        C("并发注册后白名单只有 1 条", len(dm3.entries()) == 1, dm3.entries())

    asyncio.run(scenario())


# ------------------------------------------------------------------ 用户名/UUID
def test_username_and_uuid() -> None:
    ok, cleaned = validate_username("Ｓｔｅｖｅ")  # 全角
    C("全角昵称转半角", ok and cleaned == "Steve", (ok, cleaned))
    ok2, msg = validate_username("ab")
    C("过短被拒", not ok2, msg)
    ok3, msg3 = validate_username("史蒂夫")  # 中文用户名非法
    C("中文用户名被拒", not ok3, msg3)
    ok4, cleaned4 = validate_username("  Steve_01\u200b ")
    C("去空格与零宽字符", ok4 and cleaned4 == "Steve_01", (ok4, cleaned4))
    ok5, _ = validate_username("")
    C("空昵称被拒", not ok5)
    C("parse_type 正", parse_type("正") == TYPE_MOJANG)
    C("parse_type 皮", parse_type("皮") == TYPE_LITTLESKIN)
    C("parse_type 未知", parse_type("xyz") is None)
    C("format_uuid 补连字符", format_uuid("069a79f444e94726a5befca90e38aaf5") == UUID_MOJANG)
    C("format_uuid 非法返回 None", format_uuid("not-a-uuid") is None)


# ------------------------------------------------------------------ 消息转换
def test_convert() -> None:
    chain = [
        astrbot_stub._Plain("你好 @Steve"),
        astrbot_stub._At(qq="123", name="Steve"),
        astrbot_stub._Image(url="https://x/y.png"),
        astrbot_stub._Face(1),
        astrbot_stub._Record(),
        astrbot_stub._Video(),
        astrbot_stub._Reply(),
    ]
    text = message_to_text(chain, max_length=256)
    C("文本保留", "你好 @Steve" in text, text)
    C("@提及转换", "@Steve" in text and text.count("@Steve") >= 2, text)
    C("图片转链接", "[图片] https://x/y.png" in text, text)
    C("表情转换", "[表情]" in text, text)
    C("语音转换", "[语音]" in text, text)
    C("视频转换", "[视频]" in text, text)
    C("Reply 丢弃", "reply" not in text.lower(), text)

    no_at = message_to_text(chain, enable_at_conversion=False)
    C("关闭@转换后不出现两次 @Steve", no_at.count("@Steve") == 1, no_at)
    no_img = message_to_text(chain, enable_image_forward=False)
    C("关闭图片转发后只有占位", "[图片] https://" not in no_img, no_img)

    long_text = message_to_text([astrbot_stub._Plain("字" * 500)], max_length=100)
    C("超长截断", len(long_text) <= 100 and long_text.endswith("…"), len(long_text))
    C("空链返回空", message_to_text([], max_length=100) == "")
    C("None 链返回空", message_to_text(None) == "")


# ------------------------------------------------------------------ 权限
def test_permission() -> None:
    async def scenario() -> None:
        perm = PermissionService(
            {
                "permission_enabled": True,
                "admin_qqs": ["20001"],
            }
        )
        member = astrbot_stub.AstrMessageEvent(sender_id="10001")
        admin = astrbot_stub.AstrMessageEvent(sender_id="20001")
        owner = astrbot_stub.AstrMessageEvent(sender_id="99999")

        ok, _ = await perm.check(member, NODE_REGISTER)
        C("普通成员可注册（默认节点）", ok)
        ok2, reason2 = await perm.check(member, NODE_BLACKLIST)
        C("普通成员不可管黑名单", not ok2 and "权限不足" in reason2, reason2)
        ok3, _ = await perm.check(admin, NODE_BLACKLIST)
        C("管理员可管黑名单", ok3)
        ok5, _ = await perm.check(owner, NODE_BLACKLIST)
        C("群主自动拥有全部权限（超管语义）", ok5)
        ok6, _ = await perm.check(member, NODE_STATS)
        C("普通成员可看统计", ok6)

        disabled = PermissionService({"permission_enabled": False})
        ok7, _ = await disabled.check(member, NODE_BLACKLIST)
        C("关闭权限后全部放行", ok7)

        custom = PermissionService(
            {"permission_enabled": True, "permission_defaults": {"mcwhitelist.blacklist.manage": True}}
        )
        ok8, _ = await custom.check(member, NODE_BLACKLIST)
        C("permission_defaults 可放开节点", ok8)

        # 旧的 super_admin_qqs 已从配置中移除：即便历史配置里还留着，也不该再被当成超管
        legacy = PermissionService(
            {"permission_enabled": True, "super_admin_qqs": ["30001"]}
        )
        legacy_event = astrbot_stub.AstrMessageEvent(sender_id="30001")
        ok9, _ = await legacy.check(legacy_event, NODE_BLACKLIST)
        C("已移除的 super_admin_qqs 不再生效", not ok9)

    asyncio.run(scenario())


# ------------------------------------------------------------------ 图片渲染
def test_stats_image() -> None:
    C("时长格式化-小时", format_duration(7265) == "2 小时 1 分钟", format_duration(7265))
    C("时长格式化-天", format_duration(90000) == "1 天 1 小时", format_duration(90000))
    C("时长格式化-无数据", format_duration(None) == "无数据")
    C("数字千分位", format_number(12345) == "12,345", format_number(12345))

    sections = [
        {
            "server": "生存服",
            "data": {
                "online_time": 3600,
                "blocks_mined": 1234,
                "last_login": "2026-09-21T12:00:00+08:00",
                "data_updated_at": "2026-09-21T12:05:00+08:00",
            },
        },
        {"server": "创造服", "data": None, "note": "该服无此玩家记录"},
    ]
    image = render_stats_image("小坣_Steve", sections, background_path=None)
    C("渲染出 PNG", image[:8] == b"\x89PNG\r\n\x1a\n", image[:8])
    C("图片体积合理", 5000 < len(image) < 900_000, len(image))

    # 背景图缺失 / 路径非法都不应崩
    image2 = render_stats_image("Steve", sections, background_path="Z:/not-exist.png")
    C("非法背景图不崩", image2[:4] == b"\x89PNG")


# ------------------------------------------------------ WebUI 上传的背景图
def test_backgrounds() -> None:
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        root = _Path(tmp)
        folder = root / "files" / "background_images"
        folder.mkdir(parents=True)
        for name in ("a.png", "b.jpg", "c.webp"):
            (folder / name).write_bytes(b"x")
        (folder / "notes.txt").write_bytes(b"x")          # 非图片后缀
        (root / "outside.png").write_bytes(b"x")          # 目录外的图片

        rels = [
            "files/background_images/b.jpg",
            "files/background_images/a.png",
        ]
        got = resolve_background_files(rels, root)
        C(
            "上传的背景图按配置顺序返回",
            [ _Path(p).name for p in got ] == ["b.jpg", "a.png"],
            got,
        )

        mixed = resolve_background_files(
            rels
            + [
                "files/background_images/missing.png",   # 文件已删/失效
                "files/background_images/notes.txt",     # 非图片
                "../outside.png",                        # 路径穿越
            ],
            root,
        )
        C("失效项被安静跳过", [ _Path(p).name for p in mixed ] == ["b.jpg", "a.png"], mixed)

        json_rels = _json.dumps(rels)
        C(
            "JSON 字符串形式的配置也能解析",
            len(resolve_background_files(json_rels, root)) == 2,
        )
        C("无插件数据目录时返回空", resolve_background_files(rels, None) == [])
        C("空配置返回空", resolve_background_files([], root) == [])
        C(
            "目录方式按文件名排序",
            [ _Path(p).name for p in list_backgrounds(str(folder)) ]
            == ["a.png", "b.jpg", "c.webp"],
            list_backgrounds(str(folder)),
        )


# ------------------------------------------------------ 配置热生效（WebUI 保存即生效）
def test_live_config() -> None:
    from astrbot_plugin_mc_whitelist.main import MCWhitelistPlugin

    cfg = {
        "background_images": [],
        "permission_enabled": True,
        "admin_qqs": [],
        "nickname_mode": True,
    }
    plugin = MCWhitelistPlugin(astrbot_stub.Context(), cfg)
    C("插件持有内核配置引用（不拷贝）", plugin.cfg("admin_qqs") == [])
    C("live_config 在替身环境退回引用", plugin.live_config() is cfg)

    # 模拟 WebUI 保存：AstrBotConfig.save_config 是原地 update()
    cfg.update(
        {
            "background_images": ["files/background_images/a.png"],
            "admin_qqs": ["20001"],
        }
    )
    C(
        "WebUI 存背景图后插件立刻读到",
        plugin.cfg("background_images") == ["files/background_images/a.png"],
    )

    async def scenario() -> bool:
        event = astrbot_stub.AstrMessageEvent(sender_id="20001")
        ok, _ = await plugin.perm.check(event, NODE_BLACKLIST)
        return ok

    C("改管理员名单后权限即时生效（不用重载插件）", asyncio.run(scenario()))


def main() -> int:
    test_data_manager()
    test_username_and_uuid()
    test_convert()
    test_permission()
    test_stats_image()
    test_backgrounds()
    test_live_config()
    return checker.report("单元测试 test_units")


if __name__ == "__main__":
    sys.exit(main())
