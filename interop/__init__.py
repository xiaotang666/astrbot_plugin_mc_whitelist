"""插件 ↔ 模组互联层：WS/HTTP 客户端与消息转换。"""

from .client import InteropHooks, ServerConfig, ServerLink, ServerManager
from .convert import message_to_text

__all__ = [
    "InteropHooks",
    "ServerConfig",
    "ServerLink",
    "ServerManager",
    "message_to_text",
]
