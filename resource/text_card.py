"""参数化文字卡渲染器。

同一函数既支持 WebUI 预览缩略图（200×356），也支持视频管线用的全尺寸（1080×1920），
调用方传不同的 width/height 即可。字体回退链复用 resource/anatomy/make_hegu.py 的套路。
"""

import os
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont


# 字体回退链（与 resource/anatomy/make_hegu.py:21-34 一致）
_FONT_CANDIDATES = [
    "resource/fonts/MicrosoftYaHeiBold.ttc",
    "resource/fonts/MicrosoftYaHeiNormal.ttc",
    "resource/fonts/STHeitiMedium.ttc",
    "resource/fonts/STHeitiLight.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    """按候选顺序加载字体；全部失败则用 PIL default。"""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(text: str, font, max_width: int) -> List[str]:
    """按像素宽度换行（中英文都 OK）。"""
    if not text:
        return []
    lines: List[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            if current:
                lines.append(current)
                current = ""
            continue
        candidate = current + ch
        if not current or font.getlength(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _hex_to_rgb(s: str, default=(15, 42, 74)) -> tuple:
    """把 '#RRGGBB' 转 (R, G, B)，失败回退到 default。"""
    if not s or not isinstance(s, str):
        return default
    s = s.strip().lstrip("#")
    if len(s) != 6:
        return default
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return default


def render_text_card(
    out_path: str,
    title: str = "",
    body: str = "",
    bg_color: str = "#0F2A4A",
    title_color: str = "#FFD700",
    body_color: str = "#FFFFFF",
    accent_color: str = "#FF6B6B",
    width: int = 1080,
    height: int = 1920,
    title_size: int = 140,
    body_size: int = 72,
    line_spacing: float = 1.6,
    show_accent: bool = True,
) -> str:
    """渲染一张文字卡为 PNG，返回 out_path。

    Args:
        out_path: 输出 PNG 文件路径（父目录不存在会自动建）
        title: 标题（居中）
        body: 正文（支持 "\n" 强制换行 + 自动按宽换行）
        bg_color / title_color / body_color / accent_color: '#RRGGBB' 字符串
        width / height: 画布尺寸；200×356 用作 WebUI 预览，1080×1920 用作视频素材
        title_size / body_size: 字号（px）
        line_spacing: 正文行距倍数
        show_accent: 是否画顶部/底部的装饰条（缩略图可关掉）

    Returns:
        out_path（同入参）
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    bg = _hex_to_rgb(bg_color, default=(15, 42, 74))
    title_c = _hex_to_rgb(title_color, default=(255, 215, 0))
    body_c = _hex_to_rgb(body_color, default=(255, 255, 255))
    accent = _hex_to_rgb(accent_color, default=(255, 107, 107))

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # 装饰条（缩略图缩到 200×356 时 16px 看着挺显眼，按比例缩放）
    if show_accent:
        bar_h = max(2, int(width * 0.015))  # 1080 宽 → 16px
        draw.rectangle([0, 0, width, bar_h], fill=accent)
        draw.rectangle([0, height - bar_h, width, height], fill=accent)

    # 标题
    if title:
        tf = _font(title_size)
        draw.text(
            (width // 2, int(height * 0.30)),
            title,
            font=tf,
            fill=title_c,
            anchor="mm",
        )

    # 正文
    if body:
        bf = _font(body_size)
        # 用户 "\n" 强制换行 + 像素宽自动换行
        raw_lines = body.split("\n")
        wrapped: List[str] = []
        for line in raw_lines:
            wrapped.extend(_wrap(line, bf, width - int(width * 0.18)))
        if wrapped:
            line_h = int(body_size * line_spacing)
            total_h = line_h * len(wrapped)
            # 起始 y：标题下方一点，让整体视觉居中
            y_start = int(height * 0.45) if title else (height - total_h) // 2
            for line in wrapped:
                draw.text(
                    (width // 2, y_start),
                    line,
                    font=bf,
                    fill=body_c,
                    anchor="mm",
                )
                y_start += line_h

    img.save(out_path, "PNG", optimize=True)
    return out_path


def render_text_card_thumbnail(out_path: str, **kwargs) -> str:
    """便利方法：渲染 200×356 缩略图，字体按比例缩放。

    用法：
        render_text_card_thumbnail("/tmp/x.png", title="...", body="...", bg_color="#0F2A4A")
    等价于 render_text_card(..., width=200, height=356, title_size=26, body_size=13)
    """
    kwargs["width"] = 200
    kwargs["height"] = 356
    # 缩到 ~1/5.4 倍，与 1080×1920 → 200×356 一致
    scale = 200 / 1080
    kwargs["title_size"] = max(10, int(kwargs.get("title_size", 140) * scale))
    kwargs["body_size"] = max(8, int(kwargs.get("body_size", 72) * scale))
    return render_text_card(out_path, **kwargs)
