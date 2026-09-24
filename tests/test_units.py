"""单元测试：数据管理 / UUID 校验 / 消息转换 / 权限 / 统计图片。

运行：python tests/test_units.py
"""

from __future__ import annotations

import asyncio
import logging
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


# ------------------------------------------------------ 配置页文案（WebUI 单行显示）
def _display_units(text: str) -> int:
    """估算半角宽度单位：CJK / 全角 = 2，其余 = 1。

    AstrBot 配置页把 description 渲染成**单行标题**（nowrap + 省略号）、hint 渲染成
    副标题行（默认也只 1 行）。超长就变省略号 —— 用户实测 150% 缩放下约 53 单位就截断，
    所以这里卡住上限，防止又把长句写回标题。
    """
    import unicodedata

    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def test_schema_text() -> None:
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def walk(items: dict, prefix: str = "") -> None:
        for key, item in items.items():
            if not isinstance(item, dict) or "type" not in item:
                continue
            path = f"{prefix}{key}"
            desc = str(item.get("description") or "")
            hint = str(item.get("hint") or "")
            C(f"{path} 有标题且不超一行", bool(desc) and _display_units(desc) <= 30,
              f"{_display_units(desc)} 单位：{desc}")
            C(f"{path} 有副标题说明且不超一行", bool(hint) and _display_units(hint) <= 60,
              f"{_display_units(hint)} 单位：{hint}")
            C(f"{path} hint 无 HTML 危险字符", "<" not in hint and "&" not in hint, hint)
            C(f"{path} 标题+键名不超一行",
              _display_units(f"{desc} ({key})") <= 50,
              f"{_display_units(f'{desc} ({key})')} 单位：{desc} ({key})")
            if isinstance(item.get("items"), dict):
                walk(item["items"], prefix=f"{path}.")
            for tpl in (item.get("templates") or {}).values():
                if isinstance(tpl, dict) and isinstance(tpl.get("items"), dict):
                    walk(tpl["items"], prefix=f"{path}.")

    walk(schema)


# ------------------------------------------------------ WS 连通性自检（插件页面「测试」按钮）
def _conn_key(host: str = "127.0.0.1", port: int = 9999) -> Any:
    """跨 aiohttp 版本构造 ConnectionKey（字段数随版本变过）。"""
    from aiohttp.client_reqrep import ConnectionKey

    values = {
        "host": host,
        "port": port,
        "is_ssl": False,
        "ssl": None,
        "proxy": None,
        "proxy_auth": None,
        "proxy_headers_hash": None,
        "server_hostname": None,
    }
    return ConnectionKey(*[values[name] for name in ConnectionKey._fields])


def test_diagnose_units() -> None:
    import socket

    import aiohttp

    from astrbot_plugin_mc_whitelist.interop.client import ServerConfig, ServerManager
    from astrbot_plugin_mc_whitelist.services.diagnose import (
        STAGE_LABELS,
        WsCheck,
        WsConnectivityTester,
        _explain_dial_error,
    )

    C("阶段标签齐备", set(STAGE_LABELS) == {"config", "dial", "auth", "ok"})
    payload = WsCheck(server="生存服", ok=True, stage="ok", detail="认证通过", ms=12).to_dict()
    C(
        "to_dict 字段齐备",
        {
            "server", "ok", "stage", "stage_label", "detail", "ms",
            "suggestion", "reported_name", "reason", "close_code", "ws_url", "enabled",
        }
        <= set(payload),
    )
    C("stage_label 中文化", payload["stage_label"] == "通过")

    # 建连异常必须分类成人话——这几种原因的处置方式完全不同
    msg, hint = _explain_dial_error(aiohttp.InvalidURL("ws://a b"), "ws://a b")
    C("地址格式错单独识别", "地址格式不对" in msg and hint, msg)
    msg, _ = _explain_dial_error(
        aiohttp.ClientConnectorError(_conn_key(), OSError(61, "Connection refused")),
        "ws://127.0.0.1:1/ws",
    )
    C("TCP 拒连 → 提示端口/防火墙", "TCP 连不上" in msg, msg)
    msg, _ = _explain_dial_error(
        aiohttp.ClientConnectorError(_conn_key("no.such.host"), socket.gaierror(11001, "nope")),
        "ws://no.such.host/ws",
    )
    C("域名解析失败单独识别", "域名解析失败" in msg, msg)
    msg, hint = _explain_dial_error(
        aiohttp.WSServerHandshakeError(None, (), status=401, message="nope"), "ws://x/ws"
    )
    C("握手 401 → 提示鉴权/反向代理", "401" in msg and "反向代理" in hint, f"{msg} / {hint}")
    msg, hint = _explain_dial_error(
        aiohttp.WSServerHandshakeError(None, (), status=404, message="nope"), "ws://x/ws"
    )
    C("握手 404 → 提示路径写错", "404" in msg and "路径" in hint, f"{msg} / {hint}")
    msg, _ = _explain_dial_error(asyncio.TimeoutError(), "ws://x/ws")
    C("超时单独识别", "超时" in msg, msg)
    msg, _ = _explain_dial_error(RuntimeError("boom"), "ws://x/ws")
    C("未知异常保留原始类型名", "RuntimeError" in msg, msg)

    # 服务器解析 / 清单
    manager = ServerManager(data_manager=None, plugin_version="0.0.0-test")
    manager.configure(
        {
            "security_mode": "encrypted",
            "aes_key": "test_key_16bytes",
            "mc_servers": [
                {"name": "生存服", "ws_url": "ws://127.0.0.1:1/ws", "http_url": "http://127.0.0.1:1"},
                {"name": "空岛服", "ws_url": "", "enabled": False},
                {"name": "SkyBlock", "ws_url": "ws://127.0.0.1:2/ws"},
            ],
        }
    )
    tester = WsConnectivityTester(manager)
    C("按名字解析服务器", tester.resolve_link("生存服") is manager.links[0])
    C("名字忽略大小写", tester.resolve_link("skyblock") is manager.links[2])
    C("按编号解析（1 起）", tester.resolve_link("2") is manager.links[1])
    C("不存在的名字返回 None", tester.resolve_link("没有这个服") is None)
    rows = tester.server_rows()
    C("server_rows 覆盖全部服务器（含已停用）", len(rows) == 3, len(rows))
    C(
        "server_rows 带常驻连接状态",
        {"index", "name", "ws_url", "enabled", "connected", "authenticated", "last_error"}
        <= set(rows[0]),
    )
    C("server_rows 顺序与配置一致", [row["name"] for row in rows] == ["生存服", "空岛服", "SkyBlock"])

    # 纯配置层面的问题不该发网络请求
    empty = asyncio.run(tester.test_target("2"))
    C("没配 ws_url → config 阶段", empty.stage == "config" and empty.ok is False, empty.detail)
    C("没配 ws_url 给人话建议", "ws_url" in empty.suggestion, empty.suggestion)
    bad_scheme = asyncio.run(
        tester.test_link(
            manager.links[0].__class__(
                manager, ServerConfig(name="协议错", ws_url="ftp://127.0.0.1:1/ws")
            )
        )
    )
    C("协议不对 → config 阶段", bad_scheme.stage == "config" and "ws://" in bad_scheme.suggestion)
    missing = asyncio.run(tester.test_target("没有这个服"))
    C("名字找不到 → config 阶段并说明", missing.stage == "config" and "没有找到" in missing.detail)


# ------------------------------------------------------ 插件页面 / Web API 注册
def test_plugin_page_and_web_api() -> None:
    import re
    from pathlib import Path

    from astrbot_plugin_mc_whitelist.core.version import PLUGIN_VERSION
    from astrbot_plugin_mc_whitelist.main import MCWhitelistPlugin

    root = Path(__file__).resolve().parent.parent
    page_dir = root / "pages" / "连通测试"
    entry = page_dir / "index.html"
    C("插件页面目录存在", page_dir.is_dir(), str(page_dir))
    C("页面入口是 index.html", entry.is_file(), str(entry))
    html = entry.read_text(encoding="utf-8") if entry.is_file() else ""
    C("页面通过 bridge 调接口", "AstrBotPluginPage" in html and "apiPost" in html and "apiGet" in html)
    C("页面 endpoint 不带插件名（内核会自动补）", 'apiPost("test/' in html, "")
    C("页面同时有单测与全部测试按钮", "全部测试" in html and ">测试</button>" in html)
    C("页面不引外部资源（离线也能开）", not re.search(r'(?:src|href)="https?://', html))
    C("页面目录名是单段（内核 normalize 要求）", "/" not in page_dir.name and "\\" not in page_dir.name)
    # 内核注入 bridge SDK 用的是 html.replace("</body>", tag + "</body>", 1)，即**第一个**
    # 匹配处。若页面在真正的结束标签之前还有一处（哪怕在 JS 注释里），SDK 会被注进注释，
    # 页面脚本会被提早截断成 SyntaxError —— 真机上整页报废。这条就是防它的。
    body_closers = [m.start() for m in re.finditer(r"</body\s*>", html, re.I)]
    C("页面里只有一个 body 结束标签", len(body_closers) == 1, f"出现 {len(body_closers)} 次")
    if body_closers:
        tail = html[body_closers[0] + len("</body>") :].strip()
        C("body 结束标签就是最后一个标签（内核会往它前面注入）", tail.endswith("</html>"), tail[:60])
        script_text = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))
        C("脚本里不含 body 结束标签字面量", "</body" not in script_text.lower(), "")

    ctx = astrbot_stub.Context()
    plugin = MCWhitelistPlugin(
        ctx,
        {
            "mc_servers": [{"name": "生存服", "ws_url": "ws://127.0.0.1:1/ws", "token": "t"}],
            "security_mode": "encrypted",
            "aes_key": "test_key_16bytes",
        },
    )
    C("接口前缀 = 插件名", plugin.plugin_api_prefix() == "astrbot_plugin_mc_whitelist", plugin.plugin_api_prefix())
    specs = plugin.web_api_specs()
    routes = [item[0] for item in specs]
    C(
        "路由必须带插件名前缀（内核按 /api/plug/<插件名>/<路由> 分发）",
        all(route.startswith("/astrbot_plugin_mc_whitelist/") for route in routes),
        str(routes),
    )
    C(
        "三条接口齐备",
        routes
        == [
            "/astrbot_plugin_mc_whitelist/servers",
            "/astrbot_plugin_mc_whitelist/test/all",
            "/astrbot_plugin_mc_whitelist/test/<path:target>",
        ],
        str(routes),
    )
    C("HTTP 方法正确", [item[2] for item in specs] == [["GET"], ["POST"], ["POST"]])
    plugin._register_web_apis()
    C("注册进内核注册表", len(ctx.registered_web_apis) == 3, len(ctx.registered_web_apis))
    C("注册的是插件自己的方法", ctx.registered_web_apis[1][1].__self__ is plugin)
    C("每项都有描述", all(item[3] for item in ctx.registered_web_apis))
    plugin._register_web_apis()
    C("重复注册不产生重复项（内核语义）", len(ctx.registered_web_apis) == 3, len(ctx.registered_web_apis))

    served = asyncio.run(plugin.api_servers())
    C("servers 接口 status=ok", served["status"] == "ok", served.get("message"))
    C("servers 接口带插件版本", served["data"]["plugin_version"] == PLUGIN_VERSION)
    C("servers 接口列出配置里的服务器", served["data"]["total"] == 1)
    C("servers 接口带互操作开关", "interop_enabled" in served["data"])
    C("拒绝空目标", asyncio.run(plugin.api_test_one(""))["status"] == "error")
    # 内核 <path:...> 不解码 → 插件要把编码过的中文名解回来（不然后端会找不到这台服务器）
    encoded = asyncio.run(plugin.api_test_one("%E7%94%9F%E5%AD%98%E6%9C%8D%E4%B8%8D%E5%AD%98%E5%9C%A8"))
    detail = encoded["data"]["results"][0]["detail"]
    C("路径参数里的编码名会被解码", "生存服不存在" in detail and "%" not in detail, detail)

    bare = MCWhitelistPlugin(ctx, {"mc_servers": []})
    all_result = asyncio.run(bare.api_test_all())
    C("mc_servers 为空时给人话错误", all_result["status"] == "error" and "mc_servers" in all_result["message"])


def test_chat_forward_diagnostics() -> None:
    """QQ → MC 转发：每种「没转发」都要说得出原因（修复静默丢弃、查不出问题）。"""
    from astrbot_plugin_mc_whitelist.main import MCWhitelistPlugin

    base: dict[str, Any] = {
        "mc_servers": [{"name": "生存服", "ws_url": "ws://127.0.0.1:1/ws", "token": "t"}],
        "security_mode": "none",
        "interop_enabled": True,
    }

    def make(**over: Any) -> Any:
        cfg = dict(base)
        cfg.update(over)
        plugin = MCWhitelistPlugin(astrbot_stub.Context(), cfg)
        plugin._refresh_links()  # 与 api_servers 同路径：按实时配置刷新链路
        return plugin

    def ev(**over: Any) -> Any:
        return astrbot_stub.AstrMessageEvent(**over)

    C("条件齐备时可转发", make()._chat_forward_block(ev(message_str="hello")) is None)

    r = make(interop_enabled=False)._chat_forward_block(ev())
    C("群服互联未启用 → 报因 interop_off", r is not None and r[0] == "interop_off", str(r))
    C("原因里点名该开的开关", r is not None and "启用群服互联" in r[1], str(r))

    r = make(chat_sync_enabled=False)._chat_forward_block(ev())
    C("QQ→MC 广播关闭 → 报因 chat_sync_off", r is not None and r[0] == "chat_sync_off", str(r))

    # 回归锁：真实内核里 is_wake 恒为 True（任何 handler filter 通过都会置真），
    # 曾用 is_wake_up() 当门禁 → 所有群消息被静默拦下，QQ→MC 彻底失效。
    C(
        "is_wake=True（真实内核常态）不构成拦截理由",
        make()._chat_forward_block(ev(message_str="1")) is None,
        "又用 is_wake_up() 当门禁了",
    )

    r = make()._chat_forward_block(ev(at_or_wake=True, message_str="/注册 正"))
    C("发给机器人的消息（唤醒前缀/@）→ 报因 directed", r is not None and r[0] == "directed", str(r))
    C("该原因也给出文字说明（日志里看得见）", r is not None and "不会" not in r[1] and len(r[1]) > 10, str(r))

    r = make(group_list=["12345"])._chat_forward_block(ev(group_id="88888"))
    C("群不在允许列表 → 报因 group_denied", r is not None and r[0] == "group_denied", str(r))

    black = make()
    asyncio.run(black.data_manager.add_blacklist("10001"))
    r = black._chat_forward_block(ev(sender_id="10001"))
    C("黑名单用户 → 报因 blacklisted", r is not None and r[0] == "blacklisted", str(r))

    r = make(
        mc_servers=[{"name": "生存服", "ws_url": "ws://127.0.0.1:1/ws", "chat_sync": False}]
    )._chat_forward_block(ev())
    C("服务器关了「转发群消息」→ 报因 no_target", r is not None and r[0] == "no_target", str(r))

    r = make(
        mc_servers=[{"name": "生存服", "ws_url": "ws://127.0.0.1:1/ws", "enabled": False}]
    )._chat_forward_block(ev())
    C("服务器条目被禁用 → 报因 no_target", r is not None and r[0] == "no_target", str(r))

    captured: list[str] = []
    sink = logging.getLogger("astrbot.stub")
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())  # type: ignore[method-assign]
    sink.addHandler(handler)
    old_level = sink.level
    sink.setLevel(logging.DEBUG)
    try:
        plugin = make()
        plugin._log_throttled("k", "[MCWL] 群消息未转发：测试原因")
        plugin._log_throttled("k", "[MCWL] 群消息未转发：测试原因")
        C("同一原因 60 秒内只提示一次", captured == ["[MCWL] 群消息未转发：测试原因"], str(captured))
        plugin._log_throttled("k2", "另一条原因")
        C("节流不会吞掉别的原因", captured[-1] == "另一条原因", str(captured))
        plugin._throttle_at.clear()
        plugin._log_throttled("k", "第三条", interval=0.0)
        plugin._log_throttled("k", "第四条", interval=0.0)
        C("interval=0 时每次都提示", captured[-2:] == ["第三条", "第四条"], str(captured))
        # 未连接时转发必须返回失败标记：调用方据此告警，而不是以为发出去了
        result = asyncio.run(plugin.manager.broadcast_chat("Steve", "hello"))
        C("未连接时转发返回失败标记", result == {"生存服": False}, str(result))

        # 触发前缀不匹配：以前是静默 return，现在也必须留痕
        captured.clear()
        trig = make(chat_forward_trigger="#")
        asyncio.run(trig.on_group_message(ev(message_str="1")))
        C(
            "触发前缀不匹配 → 日志说明原因（不再静默）",
            any("触发前缀" in m for m in captured),
            str(captured),
        )
        C("触发前缀不匹配时不转发", not any("已转发" in m for m in captured), str(captured))
        captured.clear()
        asyncio.run(trig.on_group_message(ev(message_str="# 打到主城去")))
        C(
            "匹配触发前缀 → 剥掉前缀后转发（未连接故报失败）",
            any("未连接" in m for m in captured) and not any("触发前缀" in m for m in captured),
            str(captured),
        )

        # 启动自述：一眼看清转发条件
        s = make()._chat_forward_summary()
        C("启动自述含触发前缀与目标服务器", "全部消息转发" in s and "生存服" in s, s)
        C(
            "配了触发前缀则自述里点明",
            "「#」" in make(chat_forward_trigger="#")._chat_forward_summary(),
            make(chat_forward_trigger="#")._chat_forward_summary(),
        )
        C(
            "没有目标服务器时自述提醒",
            "没有勾选" in make(mc_servers=[{"name": "A", "ws_url": "ws://127.0.0.1:1/ws", "chat_sync": False}])._chat_forward_summary(),
            make(mc_servers=[{"name": "A", "ws_url": "ws://127.0.0.1:1/ws", "chat_sync": False}])._chat_forward_summary(),
        )
        C(
            "群服互联关闭时自述说明不可用",
            "不可用" in make(interop_enabled=False)._chat_forward_summary(),
            make(interop_enabled=False)._chat_forward_summary(),
        )
    finally:
        sink.removeHandler(handler)
        sink.setLevel(old_level)


def main() -> int:
    test_data_manager()
    test_username_and_uuid()
    test_convert()
    test_permission()
    test_stats_image()
    test_backgrounds()
    test_live_config()
    test_schema_text()
    test_diagnose_units()
    test_plugin_page_and_web_api()
    test_chat_forward_diagnostics()
    return checker.report("单元测试 test_units")


if __name__ == "__main__":
    sys.exit(main())
