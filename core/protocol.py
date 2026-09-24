"""消息信封与类型 —— 严格实现 docs/接口契约冻结_v10.3.md §1.2 / §2。

信封（加密模式）:
    {"type","seq","msg_id","proto_version","encrypted","timestamp"}
信封（明文模式 none/prefix）:
    {"type","seq","msg_id","proto_version","prefix","data","timestamp"}
内层 payload（加密模式的明文内容）:
    {"prefix","type","msg_id","timestamp","data"}
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .crypto import AESCrypto, CryptoError

PROTO_VERSION = 1
CST = timezone(timedelta(hours=8))


class MsgType:
    """WS 消息类型（契约 §2）。"""

    AUTH = "auth"
    AUTH_RESULT = "auth_result"
    WHITELIST_UPDATE = "whitelist_update"
    WHITELIST_ACK = "whitelist_ack"
    WHITELIST_SYNC_REQUEST = "whitelist_sync_request"
    CHAT = "chat"
    PLAYER_EVENT = "player_event"
    SERVER_STATUS = "server_status"


class ProtocolError(Exception):
    """协议层错误：JSON 非法、前缀缺失/不匹配、版本不兼容、解密失败。"""


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def new_msg_id() -> str:
    from uuid import uuid4

    return str(uuid4())


@dataclass
class DecodedMessage:
    """解码后的消息。"""

    type: str
    data: dict
    msg_id: str | None = None
    seq: int | None = None
    proto_version: int | None = None
    timestamp: str | None = None
    encrypted: bool = False
    raw: dict = field(default_factory=dict)


class MsgIdCache:
    """最近 N 条 msg_id 的 LRU 集合，用于去重（契约 §1.2 规则 3）。"""

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = max(1, int(capacity))
        self._seen: OrderedDict[str, None] = OrderedDict()

    def is_duplicate(self, msg_id: str | None) -> bool:
        """返回 True 表示该 msg_id 已出现过（应当静默丢弃）。None 视为不重复。"""
        if not msg_id:
            return False
        if msg_id in self._seen:
            self._seen.move_to_end(msg_id)
            return True
        self._seen[msg_id] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._seen)


class Envelope:
    """按契约编解码消息信封。"""

    def __init__(
        self,
        *,
        crypto: AESCrypto | None = None,
        prefix: str = "[MC]",
        proto_version: int = PROTO_VERSION,
    ) -> None:
        self.crypto = crypto
        self.prefix = prefix
        self.proto_version = proto_version
        self._seq = 0

    @property
    def plain_mode(self) -> bool:
        return self.crypto is None

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ 编码
    def encode(
        self,
        msg_type: str,
        data: dict | None = None,
        *,
        msg_id: str | None = None,
        seq: int | None = None,
    ) -> str:
        """构造一条待发送的消息（JSON 字符串）。"""
        msg_id = msg_id or new_msg_id()
        seq = self.next_seq() if seq is None else seq
        data = data or {}

        if self.plain_mode:
            outer: dict[str, Any] = {
                "type": msg_type,
                "seq": seq,
                "msg_id": msg_id,
                "proto_version": self.proto_version,
                "prefix": self.prefix,
                "data": data,
                "timestamp": now_iso(),
            }
        else:
            inner = {
                "prefix": self.prefix,
                "type": msg_type,
                "msg_id": msg_id,
                "timestamp": now_iso(),
                "data": data,
            }
            outer = {
                "type": msg_type,
                "seq": seq,
                "msg_id": msg_id,
                "proto_version": self.proto_version,
                "encrypted": self.crypto.encrypt(  # type: ignore[union-attr]
                    json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
                ),
                "timestamp": inner["timestamp"],
            }
        return json.dumps(outer, ensure_ascii=False, separators=(",", ":"))

    # ------------------------------------------------------------------ 解码
    def decode(self, raw: str | bytes | dict) -> DecodedMessage:
        if isinstance(raw, dict):
            outer = raw
        else:
            try:
                outer = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(outer, dict):
            raise ProtocolError("信封不是 JSON 对象")

        if self.crypto is not None:
            payload = self._decode_encrypted(outer)
        else:
            payload = self._decode_plain(outer)

        # 契约：非 none 模式前缀必填且必须匹配
        prefix = payload.get("prefix", "")
        if prefix != self.prefix:
            raise ProtocolError(f"前缀缺失或不匹配: {prefix!r} != {self.prefix!r}")

        msg_type = payload.get("type") or outer.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            raise ProtocolError("缺少 type 字段")

        proto = outer.get("proto_version", payload.get("proto_version"))
        if proto is not None:
            try:
                proto = int(proto)
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"proto_version 非法: {proto!r}") from exc
            if proto != self.proto_version:
                raise ProtocolError(f"协议版本不兼容: 收到 {proto}，本端 {self.proto_version}")

        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise ProtocolError("data 不是 JSON 对象")

        return DecodedMessage(
            type=msg_type,
            data=data,
            msg_id=outer.get("msg_id") or payload.get("msg_id"),
            seq=outer.get("seq"),
            proto_version=proto,
            timestamp=payload.get("timestamp") or outer.get("timestamp"),
            encrypted=self.crypto is not None,
            raw=outer,
        )

    # ------------------------------------------------------------------ 内部
    def _decode_encrypted(self, outer: dict) -> dict:
        blob = outer.get("encrypted")
        if not isinstance(blob, str) or not blob:
            raise ProtocolError("加密模式下缺少 encrypted 字段")
        try:
            plain = self.crypto.decrypt(blob)  # type: ignore[union-attr]
        except CryptoError as exc:
            raise ProtocolError(f"解密失败: {exc}") from exc
        try:
            payload = json.loads(plain)
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(f"解密后的内容不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("解密后的内容不是 JSON 对象")
        return payload

    @staticmethod
    def _decode_plain(outer: dict) -> dict:
        if "data" in outer or "prefix" in outer:
            return outer
        raise ProtocolError("明文模式下缺少 prefix/data 字段")
