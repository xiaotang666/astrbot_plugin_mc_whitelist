"""测试引导：把插件按包方式加载（与 AstrBot 的 data.plugins.<name> 加载方式一致），
并在没有真实 AstrBot 的环境里注入 SDK 替身。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
PKG_NAME = "astrbot_plugin_mc_whitelist"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def bootstrap(use_stub: bool = True):
    """加载插件包，返回包模块对象。"""
    if use_stub:
        import astrbot_stub

        astrbot_stub.install()
    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def cases() -> tuple[list[str], list[tuple[str, str]]]:
    """简单的断言收集器，返回 (通过列表, 失败列表)。"""
    return [], []


class Checker:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed.append(name)
        else:
            self.failed.append((name, str(detail)))
        return bool(condition)

    def equal(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"期望 {expected!r}，实际 {actual!r}")

    async def expect_raises(self, name: str, exc_type: type, coro_factory) -> bool:
        try:
            await coro_factory()
        except exc_type:
            self.passed.append(name)
            return True
        except BaseException as exc:  # noqa: BLE001
            self.failed.append((name, f"抛出 {type(exc).__name__}: {exc}"))
            return False
        self.failed.append((name, "未抛出异常"))
        return False

    def report(self, title: str) -> int:
        print(f"\n===== {title} =====")
        for name in self.passed:
            print(f"  ✅ {name}")
        if self.failed:
            print(f"\n失败 {len(self.failed)} 项：")
            for name, detail in self.failed:
                print(f"  ❌ {name}\n     {detail}")
            return 1
        print(f"通过 {len(self.passed)} 项，全部通过 ✅")
        return 0
