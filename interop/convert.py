"""QQ 消息 → 文本（供 QQ→MC 转发使用）。安全：任何组件都不会抛异常。

文档依据：docs/开发文档_v10.3.md §十一 消息格式转换、§14 enable_image_forward /
enable_at_conversion / max_message_length。
"""

from __future__ import annotations

from typing import Any

# ---- 组件导入（兼容不同 AstrBot 版本的路径）---------------------------------
try:  # AstrBot >= 4.5
    from astrbot.api.message_components import (  # type: ignore
        At,
        Face,
        Image,
        Plain,
        Record,
        Reply,
        Video,
    )
except Exception:  # pragma: no cover - 老版本 / 测试环境
    try:
        from astrbot.core.message.components import (  # type: ignore
            At,
            Face,
            Image,
            Plain,
            Record,
            Reply,
            Video,
        )
    except Exception:  # 完全没有 SDK（离线单测）：用占位类，靠类名匹配
        class _Stub:
            def __init__(self, **kw: Any) -> None:
                self.__dict__.update(kw)

        At = Face = Image = Plain = Record = Reply = Video = _Stub  # type: ignore


def _class_name(component: Any) -> str:
    return type(component).__name__


def _get(component: Any, *names: str, default: str = "") -> str:
    for name in names:
        value = getattr(component, name, None)
        if value:
            return str(value)
    return default


def component_to_text(
    component: Any,
    *,
    enable_at_conversion: bool = True,
    enable_image_forward: bool = True,
) -> str:
    """把单个消息组件转成 MC 聊天里能显示的文本。"""
    name = _class_name(component)
    comp_type = str(getattr(component, "type", "") or "").lower()

    # 纯文本
    if name == "Plain" or comp_type == "plain":
        return _get(component, "text")

    # @提及
    if name in ("At", "AtAll") or comp_type == "at":
        if not enable_at_conversion:
            return ""
        if name == "AtAll" or getattr(component, "qq", "") == "all":
            return "@全体成员"
        target = _get(component, "name", "qq", default="@某人")
        return f"@{target}"

    # 图片 / 表情 / 语音 / 视频
    if name == "Face" or comp_type == "face":
        return "[表情]"
    if name == "Image" or comp_type == "image":
        if not enable_image_forward:
            return "[图片]"
        url = _get(component, "url", "file", "path", default="")
        return f"[图片] {url}" if url.startswith("http") else "[图片]"
    if name == "Record" or comp_type == "record":
        return "[语音]"
    if name == "Video" or comp_type == "video":
        return "[视频]"
    if name in ("File",) or comp_type == "file":
        return f"[文件] {_get(component, 'name')}".strip()
    if name == "Json" or comp_type == "json":
        return "[卡片消息]"
    if name in ("Reply", "Poke", "Node", "Nodes") or comp_type in (
        "reply",
        "poke",
        "node",
        "nodes",
    ):
        return ""

    # 未知组件：能拿到 text 就用，否则留空
    return _get(component, "text", "content")


def message_to_text(
    chain: Any,
    *,
    enable_at_conversion: bool = True,
    enable_image_forward: bool = True,
    max_length: int = 256,
) -> str:
    """把消息链（list[组件]）转成单行文本并按 max_length 截断。"""
    if chain is None:
        return ""
    if not isinstance(chain, (list, tuple)):
        chain = [chain]

    parts: list[str] = []
    for component in chain:
        try:
            text = component_to_text(
                component,
                enable_at_conversion=enable_at_conversion,
                enable_image_forward=enable_image_forward,
            )
        except Exception:  # noqa: BLE001 - 单个组件失败不影响整条消息
            continue
        if text:
            parts.append(text)

    text = " ".join(part.strip() for part in parts if part and part.strip())
    text = " ".join(text.split())
    if max_length and max_length > 0 and len(text) > max_length:
        text = text[: max(0, max_length - 1)] + "…"
    return text


def snapshot_for_log(chain: Any) -> str:
    """日志用：只输出组件类名，避免把用户内容写进日志。"""
    if not isinstance(chain, (list, tuple)):
        chain = [chain] if chain else []
    return "+".join(_class_name(c) for c in chain)
