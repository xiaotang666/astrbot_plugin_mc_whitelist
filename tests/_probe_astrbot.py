"""探测：系统 Python 能否直接 import AstrBot 内核（只读探测，不启动任何服务）。"""

import os
import sys
import traceback

APP = r"D:\AstrBot_4.25.2-custom.20260604.e7d6d49c_windows_amd64_portable\backend\app"
if os.path.isdir(APP):
    sys.path.insert(0, APP)
else:
    print("NO_APP_DIR")

for name in (
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.star",
    "astrbot.api.message_components",
    "astrbot.core.star.filter.command",
    "astrbot.core.star.filter.event_message_type",
):
    try:
        __import__(name)
        print(f"OK   {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")

try:
    from astrbot.api.event.filter import EventMessageType
    from astrbot.core.star.filter.command import GreedyStr

    print("EventMessageType:", list(EventMessageType))
    print("GreedyStr:", GreedyStr)
except Exception:  # noqa: BLE001
    traceback.print_exc()
