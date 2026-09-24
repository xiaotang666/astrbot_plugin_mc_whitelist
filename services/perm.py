"""权限适配层。

文档 §6 定义的是「AstrBot Plus 权限节点」。实测本机 AstrBot（4.25.2 定制版）内核
**不含 PermissionGroup / 权限节点 API**（已 grep 全部 core/，无 permission_node /
PermissionGroup 字样），因此这里做**能力探测 + 降级**：

1. 若 AstrBot 真的提供了权限节点接口（插件式扩展 / 未来版本），优先调用它；
2. 否则按配置里的角色名单判定：
   - 群主（群信息里的 group_owner）→ 自动拥有全部权限节点（= 文档里的超级管理员）
   - `admin_qqs` 里的 QQ、群管理员（event.is_admin()）→ 拥有管理类节点
   - 普通成员 → 默认节点（permission_defaults）

> 文档 §6.1 的 `mcwhitelist.admin.override`（超级管理员）不单独开配置项：
> 它等价于「群主」，写死在代码里即可，配置里只留管理员名单。

节点到「默认是否开放」的映射来自文档 §6.1。
"""

from __future__ import annotations

from typing import Any

# 文档 §6.1 的权限节点
NODE_REGISTER = "mcwhitelist.register"
NODE_BLACKLIST = "mcwhitelist.blacklist.manage"
NODE_STATS = "mcwhitelist.stats.view"
NODE_SYNC = "mcwhitelist.sync.trigger"
NODE_INTEROP = "mcwhitelist.interop.manage"
NODE_OVERRIDE = "mcwhitelist.admin.override"

NODE_DEFAULTS: dict[str, bool] = {
    NODE_REGISTER: True,
    NODE_BLACKLIST: False,
    NODE_STATS: True,
    NODE_SYNC: False,
    NODE_INTEROP: False,
    NODE_OVERRIDE: False,
}

ADMIN_NODES = {NODE_BLACKLIST, NODE_SYNC, NODE_INTEROP, NODE_OVERRIDE}

NODE_LABELS = {
    NODE_REGISTER: "注册/注销/迁移/更新昵称/定向发送",
    NODE_BLACKLIST: "黑名单管理",
    NODE_STATS: "查看统计",
    NODE_SYNC: "手动同步",
    NODE_INTEROP: "群服互联管理",
    NODE_OVERRIDE: "超级管理员",
}


def _safe_list(value: Any) -> list[str]:
    """AstrBot 的 list 类型配置可能是 JSON 字符串、list，或 None。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            import json

            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
    return []


class PermissionService:
    """把权限节点判定收敛到一个类里，方便以后接真正的权限后端。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._source: dict[str, Any] | None = None
        self.reload(config or {})

    def reload(self, config: dict[str, Any]) -> None:
        cfg = config or {}
        self._source = cfg if isinstance(cfg, dict) else None
        self.admin_qqs = set(_safe_list(cfg.get("admin_qqs")))
        raw_defaults = cfg.get("permission_defaults") or {}
        self.defaults = dict(NODE_DEFAULTS)
        if isinstance(raw_defaults, dict):
            for node, value in raw_defaults.items():
                if node in NODE_DEFAULTS:
                    self.defaults[node] = bool(value)
        self.enabled = bool(cfg.get("permission_enabled", True))

    # ------------------------------------------------------------- 判定
    async def check(
        self,
        event: Any,
        node: str,
        qq: str | None = None,
    ) -> tuple[bool, str]:
        """返回 (是否允许, 拒绝原因)。"""
        # WebUI 保存配置是原地 update，这里重读一次 —— 改名单后不用重载插件就生效
        if self._source is not None:
            self.reload(self._source)
        if not self.enabled:
            return True, ""
        sender = str(qq or getattr(event, "sender_id", "") or "").strip()
        if not sender and hasattr(event, "get_sender_id"):
            sender = str(event.get_sender_id() or "").strip()

        external = self._check_external(event, node, sender)
        if external is not None:
            return external

        if sender and sender in self.admin_qqs:
            return True, ""
        if await self._is_group_owner(event, sender):
            return True, ""
        if self.defaults.get(node, False):
            return True, ""
        return False, f"❌ 权限不足（需要 {NODE_LABELS.get(node, node)}）"

    def _check_external(
        self, event: Any, node: str, sender: str
    ) -> tuple[bool, str] | None:
        """若 AstrBot 提供权限节点 API 则使用它，否则返回 None 走降级逻辑。"""
        checker = getattr(event, "check_permission", None) or getattr(
            event, "has_permission", None
        )
        if not callable(checker):
            return None
        try:
            result = checker(node)
        except Exception:  # noqa: BLE001 - 外部实现不可信，失败即降级
            return None
        if hasattr(result, "__await__"):
            return None  # 异步接口：本版不接管，交给降级逻辑
        return (bool(result), "" if result else "❌ 权限不足")

    async def _is_group_owner(self, event: Any, sender: str) -> bool:
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass

        get_group = getattr(event, "get_group", None)
        if not callable(get_group):
            return False
        try:
            group = await get_group()
        except Exception:  # noqa: BLE001
            return False
        if group is None:
            return False
        if str(getattr(group, "group_owner", "") or "") == sender and sender:
            return True
        admins = getattr(group, "group_admins", None) or []
        return sender in {str(x) for x in admins} and bool(sender)
