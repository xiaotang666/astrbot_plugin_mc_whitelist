"""统计图片渲染（Pillow）。

契约依据：docs/接口契约冻结_v0.3.md B10
    "统计图片必须用内置 CJK 字体（不依赖系统字体），无背景图时纯色兜底"

字体解析顺序（任一步成功即止）：
    1. 插件目录 fonts/ 下自带的字体（推荐放 Noto Sans SC / 思源黑体，OFL 可再分发）
    2. 系统字体（Windows: msyh.ttc / simhei.ttf；Linux: Noto CJK / wqy；macOS: PingFang）
    3. 从镜像下载 Noto Sans SC 到 fonts/（可用配置关闭）
    4. 都没有 → 纯色底 + 默认位图字体（中文会显示为方块，日志里明确警告）

本模块是**同步纯函数**（不触碰事件循环），插件侧用 asyncio.to_thread 调用。
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("astrbot")

CST = timezone(timedelta(hours=8))

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"

FONT_URLS = (
    "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansSC-Regular.otf",
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/googlefonts/noto-cjk/LatestRelease/NotoSansSC-Regular.otf",
)

SYSTEM_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_font_path_cache: dict[str, str | None] = {}

# 配色（深色卡片风，与 MC 语境一致）
BG_COLOR = (24, 26, 33)
CARD_COLOR = (38, 41, 51)
CARD_BORDER = (74, 79, 96)
TEXT_MAIN = (238, 240, 245)
TEXT_SUB = (166, 172, 188)
ACCENT = (108, 200, 132)
WARN = (240, 176, 96)


def _bundled_fonts() -> list[str]:
    if not FONT_DIR.exists():
        return []
    found: list[str] = []
    for pattern in ("*.ttf", "*.otf", "*.ttc"):
        found.extend(str(p) for p in sorted(FONT_DIR.glob(pattern)))
    # 优先带 Regular 字样的文件
    found.sort(key=lambda p: (0 if "regular" in os.path.basename(p).lower() else 1, p))
    return found


def resolve_font_path(download: bool = True) -> str | None:
    """返回可用的 CJK 字体路径（无则 None）。结果缓存，避免反复扫盘。"""
    if "path" in _font_path_cache:
        return _font_path_cache["path"]

    for path in _bundled_fonts():
        _font_path_cache["path"] = path
        logger.info(f"[MCWL] 使用插件内置字体：{path}")
        return path

    for path in SYSTEM_FONT_CANDIDATES:
        if os.path.exists(path):
            _font_path_cache["path"] = path
            logger.info(f"[MCWL] 使用系统字体：{path}")
            return path

    if download:
        downloaded = download_font()
        if downloaded:
            _font_path_cache["path"] = downloaded
            return downloaded

    logger.warning(
        "[MCWL] 未找到中文字体（插件 fonts/ 与系统均无）。"
        "统计图片中文会显示为方块，请把字体文件放进插件 fonts/ 目录。"
    )
    _font_path_cache["path"] = None
    return None


def download_font(timeout: float = 30.0) -> str | None:
    """从镜像下载 Noto Sans SC 到插件 fonts/ 目录（失败静默返回 None）。"""
    import urllib.request

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    target = FONT_DIR / "NotoSansSC-Regular.otf"
    if target.exists() and target.stat().st_size > 1024:
        return str(target)
    for url in FONT_URLS:
        try:
            logger.info(f"[MCWL] 尝试下载中文字体：{url}")
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                data = resp.read()
            if len(data) > 1024:
                target.write_bytes(data)
                logger.info(f"[MCWL] 中文字体已保存到 {target}")
                return str(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MCWL] 字体下载失败（{url}）：{exc}")
    return None


def get_font(size: int) -> ImageFont.ImageFont:
    if size in _font_cache:
        return _font_cache[size]
    path = resolve_font_path()
    if path:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[size] = font
            return font
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MCWL] 加载字体失败（{path}）：{exc}")
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


# ------------------------------------------------------------------ 数据格式化
def format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "无数据"
    if total <= 0:
        return "0 分钟"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes or not parts:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts)


def format_number(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "无数据"


def _format_time(value: Any) -> str:
    if not value:
        return "无数据"
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return text


# ------------------------------------------------------------------ 背景
def _load_background(path: str | None, size: tuple[int, int]) -> Image.Image | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with Image.open(path) as raw:
            image = raw.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[MCWL] 背景图加载失败（{path}）：{exc}")
        return None

    target_w, target_h = size
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return None
    scale = max(target_w / src_w, target_h / src_h)
    new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
    image = image.resize(new_size, Image.LANCZOS)
    left = (new_size[0] - target_w) // 2
    top = (new_size[1] - target_h) // 2
    return image.crop((left, top, left + target_w, top + target_h))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


# ------------------------------------------------------------------ 主渲染
def render_stats_image(
    player: str,
    sections: list[dict[str, Any]],
    *,
    background_path: str | None = None,
    title: str = "Minecraft 玩家统计",
    width: int = 760,
    footer: str | None = None,
) -> bytes:
    """渲染统计图片，返回 PNG bytes。

    sections 每项：
        {"server": 服务器名, "data": {...} | None, "note": "错误/无数据说明"}
    """
    padding = 28
    header_h = 104
    card_h = 148
    gap = 14
    footer_h = 40 if footer else 0
    height = header_h + padding + max(1, len(sections)) * (card_h + gap) + footer_h + padding

    canvas = Image.new("RGB", (width, height), BG_COLOR)
    background = _load_background(background_path, (width, height))
    if background is not None:
        # 压暗背景，保证文字可读
        canvas = Image.blend(background, Image.new("RGB", (width, height), (10, 12, 18)), 0.55)
    draw = ImageDraw.Draw(canvas)

    font_title = get_font(30)
    font_player = get_font(24)
    font_server = get_font(22)
    font_label = get_font(17)
    font_value = get_font(19)
    font_note = get_font(16)

    # ---- 头部
    accent_bar = (196, 96, 176)
    draw.rectangle([0, 0, width, 6], fill=accent_bar)
    draw.text((padding, 24), title, font=font_title, fill=TEXT_MAIN)
    # 玩家名用「色条 + 文字」代替 emoji：msyh/思源等中文字体不含彩色 emoji，
    # 直接画 emoji 会变成豆腐块（□），渲染时不依赖任何符号字体。
    draw.rounded_rectangle(
        [padding, 66, padding + 8, 88], radius=3, fill=accent_bar
    )
    draw.text((padding + 18, 62), player, font=font_player, fill=ACCENT)
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    stamp_w, _ = _text_size(draw, stamp, font_label)
    draw.text((width - padding - stamp_w, 34), stamp, font=font_label, fill=TEXT_SUB)
    draw.line([(padding, header_h - 12), (width - padding, header_h - 12)], fill=CARD_BORDER, width=2)

    # ---- 每服一张卡
    y = header_h + padding // 2
    for section in sections:
        name = str(section.get("server") or "未知服务器")
        data = section.get("data")
        note = section.get("note")
        draw.rounded_rectangle(
            [padding // 2, y, width - padding // 2, y + card_h],
            radius=14,
            fill=CARD_COLOR,
            outline=CARD_BORDER,
            width=1,
        )
        status_ok = bool(data)
        dot = ACCENT if status_ok else WARN
        draw.ellipse([padding, y + 22, padding + 12, y + 34], fill=dot)
        draw.text((padding + 22, y + 16), name, font=font_server, fill=TEXT_MAIN)

        if not data:
            draw.text(
                (padding, y + 66),
                str(note or "无数据"),
                font=font_note,
                fill=TEXT_SUB,
            )
        else:
            left_x = padding
            right_x = width // 2 + 4
            rows_left = [
                ("在线时长", format_duration(data.get("online_time"))),
                ("挖掘方块", format_number(data.get("blocks_mined"))),
            ]
            rows_right = [
                ("上次登录", _format_time(data.get("last_login"))),
                ("数据更新", _format_time(data.get("data_updated_at"))),
            ]
            for idx, (label, value) in enumerate(rows_left):
                row_y = y + 58 + idx * 34
                draw.text((left_x, row_y), label, font=font_label, fill=TEXT_SUB)
                draw.text((left_x + 84, row_y - 2), value, font=font_value, fill=TEXT_MAIN)
            for idx, (label, value) in enumerate(rows_right):
                row_y = y + 58 + idx * 34
                draw.text((right_x, row_y), label, font=font_label, fill=TEXT_SUB)
                draw.text((right_x + 84, row_y - 2), value, font=font_value, fill=TEXT_MAIN)
        y += card_h + gap

    if footer:
        draw.text((padding, height - footer_h), footer, font=font_note, fill=TEXT_SUB)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def list_backgrounds(directory: str | None) -> list[str]:
    """按文件名排序返回可用背景图（jpg/jpeg/png/webp）。"""
    if not directory:
        return []
    path = Path(directory).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_dir():
        return []
    files = [
        str(p)
        for p in sorted(path.iterdir())
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and p.is_file()
    ]
    return files


# ------------------------------------------------------ WebUI 上传的背景图
def plugin_data_root(plugin_name: str) -> Path | None:
    """AstrBot 插件数据目录（`file` 类型配置项的上传落点）。

    内核里上传/删除走的是 `<AstrBot 根>/data/plugin_data/<插件名>/files/<配置项>/`，
    配置值存的是相对该目录的路径（`files/<配置项>/<文件名>`）。
    拿不到内核路径时返回 None（外层退回目录方式）。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
    except Exception:  # noqa: BLE001 - 测试/旧内核环境
        return None
    try:
        return Path(get_astrbot_plugin_data_path()) / plugin_name
    except Exception:  # noqa: BLE001
        return None


def resolve_background_files(
    rel_paths: Any, root: str | Path | None
) -> list[str]:
    """把配置里的相对路径解析成绝对路径列表，保持配置顺序，跳过失效项。

    - `rel_paths` 可能是 list，也可能是 JSON 字符串（AstrBot 的 list 类型配置两种都出现过）
    - 缺失文件 / 越界路径 / 非图片后缀都安静跳过（WebUI 删完文件后配置里可能还留着旧值）
    """
    if root is None:
        return []
    root_path = Path(root).expanduser().resolve(strict=False)

    if rel_paths is None:
        items: list[Any] = []
    elif isinstance(rel_paths, str):
        text = rel_paths.strip()
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = [p.strip() for p in text.replace("，", ",").split(",")]
        items = parsed if isinstance(parsed, list) else []
    elif isinstance(rel_paths, (list, tuple, set)):
        items = list(rel_paths)
    else:
        items = []

    resolved: list[str] = []
    for item in items:
        text = str(item).strip().replace("\\", "/").lstrip("/")
        if not text:
            continue
        candidate = (root_path / text).resolve(strict=False)
        try:
            candidate.relative_to(root_path)  # 防路径穿越
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        resolved.append(str(candidate))
    return resolved


def render_smoke_test() -> int:
    """自检：渲染一张图并返回字节数（供测试脚本调用）。"""
    sections = [
        {
            "server": "生存服",
            "data": {
                "online_time": 7265,
                "blocks_mined": 12345,
                "last_login": "2026-09-21T12:00:00+08:00",
                "data_updated_at": "2026-09-21T12:05:00+08:00",
            },
        },
        {"server": "创造服", "data": None, "note": "该服无此玩家记录"},
    ]
    started = time.time()
    image = render_stats_image("小坣_Steve", sections, footer="字体来源：自动探测")
    logger.debug(f"渲染耗时 {time.time() - started:.2f}s")
    return len(image)
