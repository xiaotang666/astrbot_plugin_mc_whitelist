"""一键跑全部测试。

用法：
    python tests/run_all.py                    # 三套替身测试（任何 Python 3.11+）
    python tests/run_all.py --with-kernel      # 追加真实内核测试（需 AstrBot 自带解释器）

真实内核那套必须由 AstrBot 自带的 python.exe 运行，因此本脚本用 subprocess 重新调起它；
找不到 AstrBot 安装目录时该套自动 SKIP，不算失败。
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
STUB_SUITES = ("test_units.py", "test_protocol.py", "test_interop.py")
# 必须跑在真实内核上的两套：前者查注册/schema，后者端到端验证 QQ→MC 真能送达
KERNEL_SUITES = ("test_real_kernel.py", "test_e2e_chat_forward.py")
APP_GLOBS = (r"D:/AstrBot_*/backend/app", r"C:/AstrBot_*/backend/app")


def _kernel_python() -> str | None:
    for pattern in APP_GLOBS:
        for app in sorted(glob.glob(pattern), reverse=True):
            candidate = Path(app).parent / "python" / "python.exe"
            if candidate.exists():
                return str(candidate)
    return None


def main() -> int:
    failed = 0
    for suite in STUB_SUITES:
        print(f"\n===== {suite} =====", flush=True)
        code = subprocess.call([sys.executable, str(TESTS_DIR / suite)])
        if code != 0:
            failed += 1

    if "--with-kernel" in sys.argv:
        python = _kernel_python()
        for suite in KERNEL_SUITES:
            print(f"\n===== {suite} =====", flush=True)
            if not python:
                print("SKIP：未找到 AstrBot 自带的 python.exe（设 ASTRBOT_APP_DIR 可指定）")
                continue
            code = subprocess.call([python, str(TESTS_DIR / suite)])
            if code != 0:
                failed += 1

    print("\n" + ("全部通过 ✅" if not failed else f"有 {failed} 套失败 ❌"))
    return 1 if failed else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
