"""AstrBot SDK 的最小替身（仅用于离线单测，签名/路径严格对照 4.25.2 内核源码）。

对照来源（真实内核，已逐条核对）：
    astrbot/api/star/__init__.py                 -> Context / Star / StarTools / register
    astrbot/core/star/base.py                    -> Star(context, config) + PluginKVStoreMixin
    astrbot/core/utils/plugin_kv_store.py        -> put_kv_data / get_kv_data / delete_kv_data
    astrbot/core/star/register/star_handler.py   -> register_command(command_name, sub_command, alias, **kw)
    astrbot/core/star/filter/command.py          -> CommandFilter + GreedyStr
    astrbot/core/star/filter/event_message_type.py -> EventMessageType(Flag)
    astrbot/core/platform/astr_message_event.py  -> AstrMessageEvent 方法名
    astrbot/core/platform/message_session.py     -> MessageSession 字符串格式
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

_event_message_type_values = {
    "GROUP_MESSAGE": 1,
    "PRIVATE_MESSAGE": 2,
    "OTHER_MESSAGE": 4,
    "ALL": 7,
}


class _Flag:
    """EventMessageType 的替身（只支持 & / | / in 判断）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.value = _event_message_type_values[name]

    def __and__(self, other: "_Flag") -> int:
        return self.value & other.value

    def __or__(self, other: "_Flag") -> "_Flag":
        return self

    def __repr__(self) -> str:
        return f"EventMessageType.{self.name}"


_flags = {key: _Flag(key) for key in _event_message_type_values}
EventMessageType = types.SimpleNamespace(**_flags)

REGISTERED: dict[str, Any] = {"commands": [], "handlers": [], "stars": []}


class logger:
    """astrbot.api.logger 的替身。

    真实内核导出的是 Logger 实例；这里以类形式导出，因此显式补上
    debug/info/warning/error 四个类方法——否则 `logger.warning(...)`
    这类调用在单测里会 AttributeError（类属性查找不走 __getattr__）。
    """

    _log = logging.getLogger("astrbot.stub")

    def __getattr__(self, item: str):  # pragma: no cover
        return getattr(self._log, item)

    @classmethod
    def debug(cls, *args: Any, **kwargs: Any) -> None:
        cls._log.debug(*args, **kwargs)

    @classmethod
    def info(cls, *args: Any, **kwargs: Any) -> None:
        cls._log.info(*args, **kwargs)

    @classmethod
    def warning(cls, *args: Any, **kwargs: Any) -> None:
        cls._log.warning(*args, **kwargs)

    @classmethod
    def error(cls, *args: Any, **kwargs: Any) -> None:
        cls._log.error(*args, **kwargs)


class AstrBotConfig(dict):
    """astrbot.core.config.AstrBotConfig 的替身。"""

    def __getattr__(self, item: str):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(item) from exc


class _KV:
    """模拟插件 KV 存储（真实实现落在 AstrBot 的 sp 存储上）。"""

    _store: dict[str, Any] = {}

    async def put_kv_data(self, key: str, value: Any) -> None:
        _KV._store[f"{self.plugin_id}:{key}"] = value

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return _KV._store.get(f"{self.plugin_id}:{key}", default)

    async def delete_kv_data(self, key: str) -> None:
        _KV._store.pop(f"{self.plugin_id}:{key}", None)


class Star(_KV):
    """astrbot.core.star.base.Star 的替身。"""

    plugin_id = "stub_plugin"

    def __init__(self, context: Any, config: dict | None = None) -> None:
        self.context = context

    async def initialize(self) -> None: ...

    async def terminate(self) -> None: ...


class Context:
    """astrbot.core.star.context.Context 的替身。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.registered_web_apis: list[tuple[str, Any, list[str], str]] = []

    async def send_message(self, session: str, message_chain: Any) -> bool:
        self.sent.append((session, message_chain))
        return True

    def get_config(self, umo: str | None = None) -> Any:
        return AstrBotConfig()

    def register_web_api(
        self,
        route: str,
        view_handler: Any,
        methods: list[str],
        desc: str,
    ) -> None:
        """与内核 core/star/context.py 同语义：同路由 + 同方法则替换。"""
        for idx, api in enumerate(self.registered_web_apis):
            if api[0] == route and methods == api[2]:
                self.registered_web_apis[idx] = (route, view_handler, methods, desc)
                return
        self.registered_web_apis.append((route, view_handler, methods, desc))


def register(name: str, display_name: str = "", desc: str = "", version: str = ""):
    """astrbot.core.star.register.register_star 的替身。"""

    def decorator(cls):
        cls.__plugin_name__ = name
        cls.__plugin_version__ = version
        cls.__plugin_desc__ = desc
        cls.__plugin_display_name__ = display_name
        REGISTERED["stars"].append(
            {
                "name": name,
                "display_name": display_name,
                "desc": desc,
                "version": version,
                "cls": cls,
            }
        )
        return cls

    return decorator


def _make_decorator(kind: str):
    def decorator(*args, **kwargs):
        def wrapper(func):
            REGISTERED["handlers"].append(
                {"kind": kind, "args": args, "kwargs": kwargs, "func": func}
            )
            return func

        return wrapper

    return decorator


class _Plain:
    type = "plain"

    def __init__(self, text: str = "") -> None:
        self.text = text


class _At:
    type = "at"

    def __init__(self, qq: str = "", name: str = "") -> None:
        self.qq = qq
        self.name = name


class _AtAll:
    type = "at"

    def __init__(self) -> None:
        self.qq = "all"
        self.name = "全体成员"


class _Face:
    type = "face"

    def __init__(self, id: int = 0) -> None:
        self.id = id


class _Image:
    type = "image"

    def __init__(self, url: str = "", file: str = "") -> None:
        self.url = url
        self.file = file

    @staticmethod
    def fromBytes(data: bytes) -> "_Image":
        instance = _Image()
        instance.data = data
        return instance


class _Record:
    type = "record"


class _Video:
    type = "video"


class _Reply:
    type = "reply"


class _File:
    type = "file"

    def __init__(self, name: str = "") -> None:
        self.name = name


class _Json:
    type = "json"


class AstrMessageEvent:
    """只实现插件真正用到的方法（名称/语义与真实内核一致）。"""

    def __init__(
        self,
        *,
        sender_id: str = "10001",
        sender_name: str = "Steve",
        group_id: str = "88888",
        messages: list | None = None,
        message_str: str = "",
        wake: bool = True,
        at_or_wake: bool = False,
        admin: bool = False,
        platform_id: str = "aiocqhttp",
    ) -> None:
        self.sender_id = sender_id
        self.group_id = group_id
        self.message_str = message_str
        self.messages = messages if messages is not None else [_Plain(message_str)]
        # 真实内核里 is_wake 的语义很容易踩坑：waking_check 阶段只要**任何** handler 的
        # filter 通过就会把 event.is_wake 置为 True（stage.py:196-214）。本插件注册了
        # 「群消息」过滤器，对每条群消息都通过 → is_wake 恒为 True。
        # 因此这里默认 wake=True（贴合真实内核），谁用 is_wake_up() 当门禁都会被抓出来。
        self._wake = wake
        # 真正表示「消息是发给机器人的」：唤醒前缀 / @机器人 / 回复机器人
        self.is_at_or_wake_command = at_or_wake
        self._admin = admin
        self._sender_name = sender_name
        self.session = types.SimpleNamespace(
            platform_id=platform_id,
            message_type=types.SimpleNamespace(value="GroupMessage"),
            session_id=group_id,
        )
        self.results: list[Any] = []
        self.group = types.SimpleNamespace(
            group_id=group_id, group_owner="99999", group_admins=[]
        )

    # ---- 内核同名方法
    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self.group_id

    def get_messages(self) -> list:
        return self.messages

    def get_message_str(self) -> str:
        return self.message_str

    def is_wake_up(self) -> bool:
        return self._wake

    def is_admin(self) -> bool:
        return self._admin

    async def get_group(self):
        return self.group

    @property
    def unified_msg_origin(self) -> str:
        return f"{self.session.platform_id}:GroupMessage:{self.group_id}"

    def plain_result(self, text: str):
        return {"type": "plain", "text": text}

    def image_result(self, path: str):
        return {"type": "image", "path": path}

    def chain_result(self, chain: list):
        return {"type": "chain", "chain": chain}

    def set_result(self, result) -> None:
        self.results.append(result)


def install() -> None:
    """把替身模块塞进 sys.modules，让插件代码可以正常 import。"""
    if "astrbot" in sys.modules and getattr(
        sys.modules["astrbot"], "__is_stub__", False
    ):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__is_stub__ = True
    astrbot.logger = logger
    astrbot.AstrBotConfig = AstrBotConfig

    api = types.ModuleType("astrbot.api")
    api.logger = logger
    api.AstrBotConfig = AstrBotConfig

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = AstrMessageEvent

    api_event_filter = types.ModuleType("astrbot.api.event.filter")
    api_event_filter.EventMessageType = EventMessageType

    def command(name: str, *args, **kwargs):
        return _make_decorator("command")(name, *args, **kwargs)

    def event_message_type(kind):
        return _make_decorator("event_message_type")(kind)

    api_event_filter.command = command
    api_event_filter.event_message_type = event_message_type
    api_event_filter.custom_filter = _make_decorator("custom_filter")
    api_event.filter = api_event_filter
    api_event.command = command
    api_event.event_message_type = event_message_type

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    api_star.StarTools = types.SimpleNamespace()

    api_message_components = types.ModuleType("astrbot.api.message_components")
    for cls in (_Plain, _At, _AtAll, _Face, _Image, _Record, _Video, _Reply, _File, _Json):
        setattr(api_message_components, cls.__name__.lstrip("_"), cls)

    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_star_filter = types.ModuleType("astrbot.core.star.filter")

    core_star_filter_command = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        """标记指令完成其他参数接收后的所有剩余文本。"""

    core_star_filter_command.GreedyStr = GreedyStr

    core_star_filter_emt = types.ModuleType(
        "astrbot.core.star.filter.event_message_type"
    )
    core_star_filter_emt.EventMessageType = EventMessageType

    core_message = types.ModuleType("astrbot.core.message")
    core_message_components = types.ModuleType("astrbot.core.message.components")
    for cls in (_Plain, _At, _AtAll, _Face, _Image, _Record, _Video, _Reply, _File, _Json):
        setattr(core_message_components, cls.__name__.lstrip("_"), cls)

    api.all = types.SimpleNamespace()

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": api_event,
        "astrbot.api.event.filter": api_event_filter,
        "astrbot.api.star": api_star,
        "astrbot.api.message_components": api_message_components,
        "astrbot.core": core,
        "astrbot.core.star": core_star,
        "astrbot.core.star.filter": core_star_filter,
        "astrbot.core.star.filter.command": core_star_filter_command,
        "astrbot.core.star.filter.event_message_type": core_star_filter_emt,
        "astrbot.core.message": core_message,
        "astrbot.core.message.components": core_message_components,
    }
    sys.modules.update(modules)
    api.event = api_event
    api.star = api_star
    api.message_components = api_message_components
