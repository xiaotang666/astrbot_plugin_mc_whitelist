"""游戏名 → UUID 查询（Mojang / LittleSkin 双源）+ 名称规范化与校验。

契约依据：docs/接口契约冻结_v0.3.md §3.1
    source: 正 → "MOJANG"，皮 → "LITTLESKIN"（模组端直接使用该值）

D2 决策：本版只服务正版验证服务器（online-mode=true，含 authlib-injector + 皮肤站），
        **不做 offline UUID 模式**；UUID 一律使用插件查询结果。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass

import aiohttp

from ..core.version import PLUGIN_VERSION

# ---- 类型常量 --------------------------------------------------------------
TYPE_MOJANG = "mojang"
TYPE_LITTLESKIN = "littleskin"
SOURCE_MOJANG = "MOJANG"
SOURCE_LITTLESKIN = "LITTLESKIN"

SOURCE_BY_TYPE = {TYPE_MOJANG: SOURCE_MOJANG, TYPE_LITTLESKIN: SOURCE_LITTLESKIN}
TYPE_ALIASES = {
    "正": TYPE_MOJANG,
    "正版": TYPE_MOJANG,
    "mojang": TYPE_MOJANG,
    "皮": TYPE_LITTLESKIN,
    "皮肤": TYPE_LITTLESKIN,
    "littleskin": TYPE_LITTLESKIN,
    "littleskin.cn": TYPE_LITTLESKIN,
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

DEFAULT_MOJANG_API = "https://api.mojang.com"
DEFAULT_LITTLESKIN_API = "https://littleskin.cn"


def parse_type(text: str | None) -> str | None:
    """把用户输入的绑定类型（正/皮/mojang/littleskin）规范成内部类型。"""
    if not text:
        return None
    return TYPE_ALIASES.get(text.strip().lower())


def type_label(type_: str) -> str:
    return {"mojang": "正版", "littleskin": "皮肤站"}.get(type_, type_)


def normalize_fullwidth(text: str) -> str:
    """全角字符转半角（文档 §7.2）。"""
    return "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in text
    )


def clean_username(name: str | None) -> str:
    """去空格 + 全角转半角 + 去掉零宽字符。"""
    if not name:
        return ""
    text = normalize_fullwidth(str(name)).strip()
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return unicodedata.normalize("NFC", text)


def validate_username(name: str | None) -> tuple[bool, str]:
    """返回 (是否合法, 规范化后的名字 或 错误信息)。文档 §7.2。"""
    cleaned = clean_username(name)
    if not cleaned:
        return False, "昵称为空"
    if not USERNAME_RE.match(cleaned):
        return False, (
            f"昵称 '{cleaned}' 不符合MC用户名规则（3-16位字母/数字/下划线）"
        )
    return True, cleaned


def format_uuid(raw: str | None) -> str | None:
    """把 32 位无连字符 UUID 转成标准 8-4-4-4-12 小写形式；已是标准形式则原样返回。"""
    if not raw:
        return None
    text = str(raw).strip().lower().replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        return None
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


@dataclass
class LookupResult:
    ok: bool
    uuid: str | None = None
    username: str | None = None
    error: str = ""


class UUIDService:
    """双源 UUID 查询，带内存 TTL 缓存（含负缓存，避免刷接口）。"""

    def __init__(
        self,
        *,
        mojang_api: str = DEFAULT_MOJANG_API,
        littleskin_api: str = DEFAULT_LITTLESKIN_API,
        timeout: float = 10.0,
        cache_ttl: int = 3600,
        user_agent: str | None = None,
    ) -> None:
        self.mojang_api = (mojang_api or DEFAULT_MOJANG_API).rstrip("/")
        self.littleskin_api = (littleskin_api or DEFAULT_LITTLESKIN_API).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = max(0, int(cache_ttl))
        self.user_agent = user_agent or f"astrbot_plugin_mc_whitelist/{PLUGIN_VERSION}"
        self._cache: dict[tuple[str, str], tuple[float, LookupResult]] = {}

    # ------------------------------------------------------------- 缓存
    def _cache_get(self, key: tuple[str, str]) -> LookupResult | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, result = hit
        if self.cache_ttl and time.time() - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return result

    def _cache_put(self, key: tuple[str, str], result: LookupResult) -> None:
        self._cache[key] = (time.time(), result)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------- 查询
    async def lookup(
        self,
        session: aiohttp.ClientSession,
        name: str,
        type_: str,
    ) -> LookupResult:
        valid, cleaned = validate_username(name)
        if not valid:
            return LookupResult(False, error=cleaned)
        type_ = parse_type(type_) or TYPE_MOJANG
        cache_key = (type_, cleaned.lower())
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        try:
            if type_ == TYPE_MOJANG:
                result = await self._lookup_mojang(session, cleaned)
            else:
                result = await self._lookup_littleskin(session, cleaned)
        except aiohttp.ClientError as exc:
            result = LookupResult(False, error=f"网络错误：{exc}")
        except TimeoutError:
            result = LookupResult(False, error="查询超时")

        self._cache_put(cache_key, result)
        return result

    async def _lookup_mojang(
        self,
        session: aiohttp.ClientSession,
        name: str,
    ) -> LookupResult:
        url = f"{self.mojang_api}/users/profiles/minecraft/{name}"
        async with session.get(url, timeout=self.timeout) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                uuid = format_uuid(data.get("id"))
                if not uuid:
                    return LookupResult(False, error="Mojang 返回的 UUID 格式非法")
                return LookupResult(True, uuid=uuid, username=data.get("name") or name)
            if resp.status in (204, 404):
                return LookupResult(False, error=f"正版账号 {name} 不存在")
            if resp.status == 429:
                return LookupResult(False, error="Mojang 接口限流，请稍后再试")
            return LookupResult(False, error=f"Mojang 接口返回 {resp.status}")

    async def _lookup_littleskin(
        self,
        session: aiohttp.ClientSession,
        name: str,
    ) -> LookupResult:
        """LittleSkin Yggdrasil：POST /api/yggdrasil/api/profiles/minecraft，body = ["name"]。"""
        url = f"{self.littleskin_api}/api/yggdrasil/api/profiles/minecraft"
        async with session.post(
            url, json=[name], timeout=self.timeout
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    data = [data]
                if not data:
                    return LookupResult(False, error=f"皮肤站角色 {name} 不存在")
                uuid = format_uuid(data[0].get("id"))
                if not uuid:
                    return LookupResult(False, error="皮肤站返回的 UUID 格式非法")
                return LookupResult(
                    True, uuid=uuid, username=data[0].get("name") or name
                )
            if resp.status in (204, 404):
                return LookupResult(False, error=f"皮肤站角色 {name} 不存在")
            return LookupResult(False, error=f"皮肤站接口返回 {resp.status}")
