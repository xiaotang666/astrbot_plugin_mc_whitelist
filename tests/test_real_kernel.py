"""真实内核加载测试：用 AstrBot 自带 Python 加载本插件，验证注册 / 元数据 / 配置 schema。

为什么必须单独有一个：tests/astrbot_stub.py 是替身，只能证明「逻辑自洽」，
证明不了「AstrBot 4.25.2 真的能加载这个插件」。本文件跑在真实内核上。

用法（必须用 AstrBot 自带的解释器，系统 Python 缺 astrbot 依赖）：
    "<AstrBot>/backend/python/python.exe" tests/test_real_kernel.py

安装目录探测顺序：环境变量 ASTRBOT_APP_DIR → D:/AstrBot_*/backend/app。
都找不到则打印 SKIP 并以退出码 0 结束（不阻塞 CI / 其他机器）。
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
PKG_NAME = "astrbot_plugin_mc_whitelist"

sys.path.insert(0, str(TESTS_DIR))
from _loader import Checker  # noqa: E402

DEFAULT_APP_GLOBS = (
    r"D:/AstrBot_*/backend/app",
    r"C:/AstrBot_*/backend/app",
    r"D:/Hermes/AstrBot*/backend/app",
)


def find_app_dir() -> str | None:
    env = os.environ.get("ASTRBOT_APP_DIR")
    if env and os.path.isdir(env):
        return env
    for pattern in DEFAULT_APP_GLOBS:
        hits = sorted(glob.glob(pattern))
        for hit in reversed(hits):
            if os.path.isdir(os.path.join(hit, "astrbot")):
                return hit
    return None


# 文档 §十三 的指令与别名（main.py 里的 @filter.command）
EXPECTED_COMMANDS: dict[str, set[str]] = {
    "注册": {"zc", "绑定"},
    "注销": {"zx", "解绑"},
    "迁移": {"qy"},
    "更新昵称": {"gxnc"},
    "info": {"统计", "我的统计"},
    "black": {"黑名单"},
    "sync": {"同步"},
    "status": {"状态"},
    "mc": set(),
    "mcwl": {"mc白名单", "白名单帮助"},
}


def _register_name_in_source() -> str | None:
    """从 main.py 源码里取 @register 第一个参数（挂 PLUGIN_NAME 常量时回溯该常量）。"""
    import re

    text = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    match = re.search(r"@register\(\s*([^,\n]+)", text)
    if not match:
        return None
    token = match.group(1).strip()
    if token.startswith(("'", '"')):
        return token.strip("'\"")
    const = re.search(
        rf"^{re.escape(token)}\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE
    )
    return const.group(1) if const else None


def main() -> int:
    app_dir = find_app_dir()
    if not app_dir:
        print("SKIP: 未找到 AstrBot 安装目录（设 ASTRBOT_APP_DIR 可指定）")
        return 0
    # 内核没打包时会拿 CWD 当 AstrBot 根目录（get_astrbot_root），
    # 不重定向就会在插件目录里生成 data/cmd_config.json、data/t2i_templates/ 等运行时文件。
    scratch = Path(tempfile.gettempdir()) / "mcwl_kernel_probe"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(scratch)

    sys.path.insert(0, app_dir)
    print(f"内核：{app_dir}")

    check = Checker()

    import astrbot  # noqa: F401
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager

    try:
        from astrbot.core.config.default import VERSION as CORE_VERSION
    except Exception:  # noqa: BLE001
        from astrbot.core import VERSION as CORE_VERSION  # type: ignore

    check.check("内核可导入", True, "")
    print(f"内核版本：{CORE_VERSION}")

    # ---------------------------------------------------------------- 1. 元数据
    meta_path = PLUGIN_DIR / "metadata.yaml"
    import yaml

    raw_meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    meta = PluginManager._load_plugin_metadata(str(PLUGIN_DIR))
    check.check("metadata.yaml 通过真实 loader", meta is not None, "loader 返回 None")
    if meta is None:
        return check.report("真实内核：元数据")

    check.equal("metadata name", meta.name, PKG_NAME)
    check.check(
        "metadata version 形如 X.Y.Z",
        bool(re.fullmatch(r"\d+\.\d+\.\d+", str(meta.version))),
        f"实际 {meta.version!r}",
    )
    check.equal("metadata display_name", meta.display_name, raw_meta.get("display_name"))
    check.check("metadata repo 非空", bool(meta.repo), "repo 为空则无法在线更新")
    check.check(
        "desc / author 齐全",
        bool(meta.desc) and bool(meta.author),
        f"desc={meta.desc!r} author={meta.author!r}",
    )

    try:
        PluginManager._validate_importable_name(meta.name)
        check.check("name 是合法模块名", True, "")
    except Exception as exc:  # noqa: BLE001
        check.check("name 是合法模块名", False, str(exc))

    ok, reason = PluginManager._validate_astrbot_version_specifier(meta.astrbot_version)
    check.check(
        f"astrbot_version 规格合法（{meta.astrbot_version}）",
        ok,
        str(reason),
    )
    if ok and meta.astrbot_version:
        from packaging.specifiers import SpecifierSet

        check.check(
            f"当前内核 {CORE_VERSION} 落在要求范围内",
            CORE_VERSION in SpecifierSet(meta.astrbot_version),
            f"{CORE_VERSION} not in {meta.astrbot_version}",
        )

    check.check(
        "metadata name == @register name",
        _register_name_in_source() == meta.name,
        f"源码里是 {_register_name_in_source()!r}",
    )

    # ---------------------------------------------------------------- 2. 加载插件
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    try:
        spec.loader.exec_module(module)
        check.check("插件包可导入（真实内核）", True, "")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check.check("插件包可导入（真实内核）", False, f"{type(exc).__name__}: {exc}")
        return check.report("真实内核：加载")

    plugin_cls = getattr(module, "MCWhitelistPlugin", None)
    check.check("导出 MCWhitelistPlugin", plugin_cls is not None, "找不到插件类")

    registered = [
        m for m in star_registry if (m.module_path or "").startswith(PKG_NAME)
    ]
    check.check("进入 star_registry", bool(registered), "内核注册表里没有本插件")

    # ---------------------------------------------------------------- 3. 指令注册
    from astrbot.api.event.filter import EventMessageType
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter

    # 内核自己也是按 startswith 关联「handler ↔ 插件」的（star_manager 卸载逻辑），
    # get_handlers_by_module_name 是精确匹配，这里不能用。
    all_handlers = [
        h
        for h in star_handlers_registry._handlers  # noqa: SLF001 - 测试用，无公开遍历口
        if (h.handler_module_path or "").startswith(PKG_NAME)
    ]
    check.check("注册到 handler 注册表", bool(all_handlers), "handlers 为空")

    found: dict[str, set[str]] = {}
    for handler in all_handlers:
        for flt in handler.event_filters:
            if isinstance(flt, CommandFilter):
                found.setdefault(flt.command_name, set()).update(flt.alias)

    for name, aliases in EXPECTED_COMMANDS.items():
        check.check(f"指令 /{name} 已注册", name in found, f"实际注册：{sorted(found)}")
        if name in found:
            missing = aliases - found[name]
            check.check(f"指令 /{name} 别名齐全", not missing, f"缺少 {sorted(missing)}")

    has_group_handler = any(
        isinstance(flt, EventMessageTypeFilter)
        and flt.event_message_type == EventMessageType.GROUP_MESSAGE
        for handler in all_handlers
        for flt in handler.event_filters
    )
    check.check("群消息处理器已注册（QQ→MC 广播入口）", has_group_handler, "")

    # 版本三处一致：metadata / main.PLUGIN_VERSION / @register
    check.equal("main.PLUGIN_VERSION", module.main.PLUGIN_VERSION, meta.version)
    check.equal(
        "core.version.PLUGIN_VERSION",
        module.core.version.PLUGIN_VERSION,
        meta.version,
    )

    # 防漂移：版本号只允许出现在 core/version.py（其它文件写死就会漏改）
    version_literal = re.compile(r"[\"']\d+\.\d+\.\d+[\"']")
    offenders = []
    for path in sorted(PLUGIN_DIR.rglob("*.py")):
        if path.name == "version.py" or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if version_literal.search(line):
                offenders.append(f"{path.relative_to(PLUGIN_DIR)}:{lineno}")
    check.check(
        "版本号字面量只在 core/version.py",
        not offenders,
        f"其它位置硬编码了版本号：{offenders}",
    )

    # ---------------------------------------------------------------- 4. 实例化
    class _StubContext:
        def get_config(self, umo=None):  # noqa: ANN001
            return {}

    try:
        plugin = plugin_cls(_StubContext(), dict(raw_meta))
        check.check("插件类可实例化（真实 Star 基类）", True, "")
        check.equal("构造后 KV 适配器就位", hasattr(plugin, "get_kv_data"), True)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check.check("插件类可实例化（真实 Star 基类）", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 5. 配置 schema
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    try:
        from astrbot.dashboard.routes.config import validate_config

        data = {k: v.get("default") for k, v in schema.items()}
        errors, _ = validate_config(data, schema, False)
        check.check("默认值通过真实配置校验器", not errors, "; ".join(errors[:5]))
    except Exception as exc:  # noqa: BLE001
        check.check("默认值通过真实配置校验器", False, f"{type(exc).__name__}: {exc}")

    allowed_types = {
        "string",
        "text",
        "int",
        "float",
        "bool",
        "list",
        "object",
        "template_list",
        "file",
    }
    bad = [
        f"{key}:{val.get('type')}"
        for key, val in schema.items()
        if val.get("type") not in allowed_types
    ]
    check.check("schema 类型均为内核承认的类型", not bad, f"非法类型 {bad}")

    # ------------------------------------------------- 6. 配置热生效（真实 star_map）
    # 内核把插件配置对象挂在 star_map[<模块名>].config 上，WebUI 保存走原地 update()。
    # live_config() 必须能从这里取到同一个对象，否则上传背景图后要重载插件才生效。
    try:
        from astrbot.core.star.star import star_map

        class _MetaStub:
            pass

        live_cfg: dict = dict(raw_meta)
        meta_stub = _MetaStub()
        meta_stub.config = live_cfg
        star_map[plugin_cls.__module__] = meta_stub
        try:
            probe = object.__new__(plugin_cls)
            probe._config = {}
            check.check(
                "live_config 从真实 star_map 取到内核配置对象",
                probe.live_config() is live_cfg,
                "",
            )
            live_cfg["background_images"] = ["files/background_images/x.png"]
            check.equal(
                "内核原地 update 后 live_config 立刻可见",
                probe.live_config().get("background_images"),
                ["files/background_images/x.png"],
            )
        finally:
            star_map.pop(plugin_cls.__module__, None)
    except Exception as exc:  # noqa: BLE001
        check.check(
            "live_config 从真实 star_map 取到内核配置对象",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    return check.report("真实内核：加载与注册")


if __name__ == "__main__":
    raise SystemExit(main())
