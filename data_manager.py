"""绑定数据管理（KV 存储 + 并发锁 + 唯一性约束）。

契约依据：docs/接口契约冻结_v0.3.md
    B7  读-改-写必须加锁（asyncio.Lock），避免并发注册竞态
    B9  用户名唯一性判定不区分大小写（保留原始大小写显示）
    §3.1 白名单条目 Entry = {name, uuid, source, qq}

存储结构（文档 §5.1，全部落在 AstrBot 插件 KV 里）：
    bindings        {qq_<uid>: {type, username, uuid, registered_at, group_id}}
    username_map    {username_lower: "qq_<uid>"}      # 小写索引，仅用于查重
    qq_blacklist    ["<qq>", ...]
    sync_version    int
    background_index int
    server_states   {server_name: {...}}
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .services.uuid import SOURCE_BY_TYPE, format_uuid

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def qq_key(qq: str | int) -> str:
    return f"qq_{str(qq).strip()}"


class KVAdapter(Protocol):
    """AstrBot Star 已实现该协议（PluginKVStoreMixin）。"""

    async def get_kv_data(self, key: str, default: Any) -> Any: ...

    async def put_kv_data(self, key: str, value: Any) -> None: ...


class DataManager:
    """所有绑定数据的唯一入口；所有写操作都必须走 _lock。"""

    KEY_BINDINGS = "bindings"
    KEY_USERNAME_MAP = "username_map"
    KEY_BLACKLIST = "qq_blacklist"
    KEY_SYNC_VERSION = "sync_version"
    KEY_BG_INDEX = "background_index"
    KEY_SERVER_STATES = "server_states"

    def __init__(self, kv: KVAdapter) -> None:
        self.kv = kv
        self._lock = asyncio.Lock()
        self.bindings: dict[str, dict[str, Any]] = {}
        self.username_map: dict[str, str] = {}
        self.qq_blacklist: list[str] = []
        self.sync_version: int = 0
        self.background_index: int = 0
        self.server_states: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- 载入 / 落盘
    async def load(self) -> None:
        async with self._lock:
            self.bindings = dict(await self.kv.get_kv_data(self.KEY_BINDINGS, {}) or {})
            self.username_map = dict(
                await self.kv.get_kv_data(self.KEY_USERNAME_MAP, {}) or {}
            )
            self.qq_blacklist = list(
                await self.kv.get_kv_data(self.KEY_BLACKLIST, []) or []
            )
            self.sync_version = int(
                await self.kv.get_kv_data(self.KEY_SYNC_VERSION, 0) or 0
            )
            self.background_index = int(
                await self.kv.get_kv_data(self.KEY_BG_INDEX, 0) or 0
            )
            self.server_states = dict(
                await self.kv.get_kv_data(self.KEY_SERVER_STATES, {}) or {}
            )
            self._rebuild_username_map()

    def _rebuild_username_map(self) -> None:
        """兼容老数据：username_map 缺失时按 bindings 重建（小写索引）。"""
        rebuilt: dict[str, str] = {}
        for key, binding in self.bindings.items():
            name = str(binding.get("username") or "").lower()
            if name:
                rebuilt[name] = key
        for name, key in self.username_map.items():
            if key in self.bindings:
                rebuilt.setdefault(str(name).lower(), key)
        self.username_map = {k: v for k, v in rebuilt.items() if v in self.bindings}

    async def _persist(self, *keys: str) -> None:
        mapping = {
            self.KEY_BINDINGS: self.bindings,
            self.KEY_USERNAME_MAP: self.username_map,
            self.KEY_BLACKLIST: self.qq_blacklist,
            self.KEY_SYNC_VERSION: self.sync_version,
            self.KEY_BG_INDEX: self.background_index,
            self.KEY_SERVER_STATES: self.server_states,
        }
        for key in keys or tuple(mapping):
            await self.kv.put_kv_data(key, mapping[key])

    async def flush(self) -> None:
        async with self._lock:
            await self._persist()

    # ------------------------------------------------------------- 绑定查询
    def get_binding(self, qq: str | int) -> dict[str, Any] | None:
        binding = self.bindings.get(qq_key(qq))
        return dict(binding) if binding else None

    def binding_count(self) -> int:
        return len(self.bindings)

    def username_exists(self, username: str) -> bool:
        """大小写不敏感查重（B9）。"""
        return str(username or "").lower() in self.username_map

    def qq_of_username(self, username: str) -> str | None:
        key = self.username_map.get(str(username or "").lower())
        if not key:
            return None
        return key[3:] if key.startswith("qq_") else key

    # ------------------------------------------------------------- 绑定写入
    async def register(
        self,
        qq: str | int,
        username: str,
        uuid: str,
        type_: str,
        group_id: str | int | None = None,
    ) -> tuple[bool, str]:
        """新增绑定。返回 (成功?, 说明)。"""
        uuid = format_uuid(uuid) or ""
        if not uuid:
            return False, "UUID 非法"
        async with self._lock:
            key = qq_key(qq)
            if key in self.bindings:
                return False, "该 QQ 已绑定，如需更换请先使用 /注销"
            if str(username).lower() in self.username_map:
                return False, f"游戏名 {username} 已被其它 QQ 绑定"
            self.bindings[key] = {
                "type": type_,
                "username": username,
                "uuid": uuid,
                "registered_at": now_iso(),
                "group_id": str(group_id) if group_id else None,
            }
            self.username_map[str(username).lower()] = key
            await self._persist(self.KEY_BINDINGS, self.KEY_USERNAME_MAP)
            return True, "success"

    async def unbind(self, qq: str | int) -> tuple[bool, dict[str, Any] | None]:
        """删除绑定；返回 (是否真的删了, 旧绑定)。"""
        async with self._lock:
            key = qq_key(qq)
            old = self.bindings.pop(key, None)
            if not old:
                return False, None
            self.username_map.pop(str(old.get("username") or "").lower(), None)
            await self._persist(self.KEY_BINDINGS, self.KEY_USERNAME_MAP)
            return True, dict(old)

    async def update_binding(
        self,
        qq: str | int,
        username: str,
        uuid: str,
        type_: str,
    ) -> tuple[bool, str]:
        """迁移（正↔皮）：改名字/UUID/类型，保持注册时间。"""
        uuid = format_uuid(uuid) or ""
        if not uuid:
            return False, "UUID 非法"
        async with self._lock:
            key = qq_key(qq)
            binding = self.bindings.get(key)
            if not binding:
                return False, "尚未绑定"
            if str(username).lower() in self.username_map and (
                self.username_map[str(username).lower()] != key
            ):
                return False, f"游戏名 {username} 已被其它 QQ 绑定"
            self.username_map.pop(str(binding.get("username") or "").lower(), None)
            binding.update(
                {
                    "username": username,
                    "uuid": uuid,
                    "type": type_,
                    "updated_at": now_iso(),
                }
            )
            self.username_map[str(username).lower()] = key
            await self._persist(self.KEY_BINDINGS, self.KEY_USERNAME_MAP)
            return True, "success"

    # ------------------------------------------------------------- 白名单条目
    def entries(self) -> list[dict[str, str]]:
        """契约 §3.1：每次推送都是完整列表；黑名单用户不进白名单。"""
        result: list[dict[str, str]] = []
        blacklist = set(self.qq_blacklist)
        for key, binding in sorted(self.bindings.items()):
            qq = key[3:] if key.startswith("qq_") else key
            if qq in blacklist:
                continue
            uuid = format_uuid(binding.get("uuid"))
            username = str(binding.get("username") or "").strip()
            if not uuid or not username:
                continue
            result.append(
                {
                    "name": username,
                    "uuid": uuid,
                    "source": SOURCE_BY_TYPE.get(str(binding.get("type")), "MOJANG"),
                    "qq": qq,
                }
            )
        return result

    # ------------------------------------------------------------- 黑名单
    def is_blacklisted(self, qq: str | int) -> bool:
        return str(qq).strip() in self.qq_blacklist

    def blacklist(self) -> list[str]:
        return list(self.qq_blacklist)

    async def add_blacklist(self, qq: str | int) -> tuple[bool, dict[str, Any] | None]:
        """加入黑名单并自动解绑（文档 §13.5）。返回 (新增?, 被解绑的绑定)。"""
        target = str(qq).strip()
        if not target:
            return False, None
        async with self._lock:
            removed: dict[str, Any] | None = None
            key = qq_key(target)
            if key in self.bindings:
                old = self.bindings.pop(key)
                self.username_map.pop(str(old.get("username") or "").lower(), None)
                removed = dict(old)
            added = target not in self.qq_blacklist
            if added:
                self.qq_blacklist.append(target)
            await self._persist(
                self.KEY_BLACKLIST, self.KEY_BINDINGS, self.KEY_USERNAME_MAP
            )
            return added or removed is not None, removed

    async def remove_blacklist(self, qq: str | int) -> bool:
        target = str(qq).strip()
        async with self._lock:
            if target not in self.qq_blacklist:
                return False
            self.qq_blacklist = [x for x in self.qq_blacklist if x != target]
            await self._persist(self.KEY_BLACKLIST)
            return True

    async def merge_initial_blacklist(self, qqs: list[str]) -> None:
        async with self._lock:
            changed = False
            for qq in qqs:
                target = str(qq).strip()
                if target and target not in self.qq_blacklist:
                    self.qq_blacklist.append(target)
                    changed = True
            if changed:
                await self._persist(self.KEY_BLACKLIST)

    # ------------------------------------------------------------- 同步版本
    async def next_sync_version(self) -> int:
        async with self._lock:
            self.sync_version += 1
            await self._persist(self.KEY_SYNC_VERSION)
            return self.sync_version

    # ------------------------------------------------------------- 背景图
    async def next_background_index(self) -> int:
        """返回当前下标并自增（轮询背景图）。"""
        async with self._lock:
            idx = self.background_index
            self.background_index = idx + 1
            await self._persist(self.KEY_BG_INDEX)
            return idx

    # ------------------------------------------------------------- 服务器状态
    def server_state(self, name: str) -> dict[str, Any]:
        return dict(
            self.server_states.get(
                name,
                {
                    "ws_connected": False,
                    "pending_sync": True,
                    "last_sync_time": None,
                    "last_sync_count": 0,
                    "last_error": None,
                },
            )
        )

    async def update_server_state(self, name: str, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            state = dict(
                self.server_states.get(
                    name,
                    {
                        "ws_connected": False,
                        "pending_sync": True,
                        "last_sync_time": None,
                        "last_sync_count": 0,
                        "last_error": None,
                    },
                )
            )
            state.update(fields)
            self.server_states[name] = state
            await self._persist(self.KEY_SERVER_STATES)
            return dict(state)

    async def mark_synced(self, name: str, count: int) -> None:
        await self.update_server_state(
            name,
            last_sync_time=now_iso(),
            last_sync_count=count,
            pending_sync=False,
            last_error=None,
        )
