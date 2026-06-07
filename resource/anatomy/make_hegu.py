"""
生成合谷穴解剖学示意图，作为视频角标叠加素材。

合谷穴 (LI4 / Hegu) 定位：
  手背第 1、2 掌骨之间，靠近第 2 掌骨中点的桡侧处。
  取穴：拇食指并拢，肌肉最高点。

输出：
  resource/anatomy/hegu_corner.png  —— 9:16 短视频右下角小图 (~360x405, 透明背景)
  resource/anatomy/hegu_full.png    —— 起止全屏图 (~1080x1920, 解释用)
"""
from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        os.path.join(os.path.dirname(OUT_DIR), "fonts", "MicrosoftYaHeiBold.ttc"),
        os.path.join(os.path.dirname(OUT_DIR), "fonts", "MicrosoftYaHeiNormal.ttc"),
        os.path.join(os.path.dirname(OUT_DIR), "fonts", "STHeitiMedium.ttc"),
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_hand_outline(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float = 1.0) -> None:
    """画手背轮廓（右手，背面朝上）。"""
    s = scale

    # 掌心矩形（手背）
    palm_w = int(180 * s)
    palm_h = int(230 * s)
    palm_box = (cx - palm_w // 2, cy - palm_h // 2, cx + palm_w // 2, cy + palm_h // 2)
    draw.rounded_rectangle(palm_box, radius=int(40 * s), fill=(255, 224, 198, 255), outline=(140, 80, 50, 255), width=int(4 * s))

    # 4 根手指（食指 / 中指 / 无名指 / 小指）
    finger_specs = [
        # (offset_x_from_cx, top_y, width, height)
        ( -int(50 * s), cy - palm_h // 2 - int(130 * s), int(38 * s), int(150 * s)),   # 食指
        ( -int(15 * s), cy - palm_h // 2 - int(150 * s), int(40 * s), int(170 * s)),   # 中指
        (  int(20 * s), cy - palm_h // 2 - int(140 * s), int(38 * s), int(160 * s)),   # 无名指
        (  int(55 * s), cy - palm_h // 2 - int(110 * s), int(34 * s), int(130 * s)),   # 小指
    ]
    for fx, fy, fw, fh in finger_specs:
        fcx = cx + fx
        box = (fcx - fw // 2, fy, fcx + fw // 2, fy + fh)
        draw.rounded_rectangle(box, radius=int(20 * s), fill=(255, 224, 198, 255), outline=(140, 80, 50, 255), width=int(4 * s))
        # 指甲
        nail_box = (fcx - int(fw * 0.25), fy + int(8 * s), fcx + int(fw * 0.25), fy + int(28 * s))
        draw.ellipse(nail_box, fill=(245, 200, 175, 255), outline=(140, 80, 50, 255), width=int(2 * s))

    # 大拇指（向左侧伸出）
    thumb_box = (
        cx - palm_w // 2 - int(90 * s),
        cy + int(20 * s),
        cx - palm_w // 2 + int(10 * s),
        cy + int(140 * s),
    )
    draw.rounded_rectangle(thumb_box, radius=int(25 * s), fill=(255, 224, 198, 255), outline=(140, 80, 50, 255), width=int(4 * s))

    # 掌骨（掌背隐约可见的骨骼线）
    bone_color = (220, 170, 140, 120)
    for offset in (-int(45 * s), -int(15 * s), int(15 * s), int(50 * s)):
        draw.line(
            [(cx + offset, cy - palm_h // 2 + int(20 * s)),
             (cx + offset, cy + palm_h // 2 - int(30 * s))],
            fill=bone_color, width=int(3 * s),
        )

    # 大鱼际（拇食指之间那块肌肉，合谷穴所在区域）
    # 范围：拇指根部和食指根部之间
    hegu_region = [
        (cx - palm_w // 2 + int(8 * s), cy + int(10 * s)),   # 拇指根部
        (cx - int(15 * s), cy - int(30 * s)),                # 食指根部
        (cx - int(20 * s), cy + int(60 * s)),                # 掌心
        (cx - palm_w // 2 + int(30 * s), cy + int(80 * s)),
    ]
    draw.polygon(hegu_region, fill=(255, 200, 175, 255), outline=(180, 100, 70, 255))


def _draw_hegu_marker(draw: ImageDraw.ImageDraw, x: int, y: int, r: int) -> None:
    """在 (x, y) 处画红点 + 圆圈标记。"""
    # 外圈
    draw.ellipse((x - r, y - r, x + r, y + r), outline=(220, 30, 30, 255), width=4)
    # 内圈
    draw.ellipse((x - r * 0.55, y - r * 0.55, x + r * 0.55, y + r * 0.55),
                 fill=(220, 30, 30, 255))
    # 高光
    draw.ellipse((x - r * 0.25, y - r * 0.35, x + r * 0.05, y - r * 0.05),
                 fill=(255, 180, 180, 255))


def _draw_label_box(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                    text_lines: list[tuple[str, int, str]], bg=(255, 255, 255, 230)) -> None:
    """在 (x, y) 画一个圆角白底框，里面写多行文字。"""
    draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=bg,
                           outline=(180, 180, 180, 255), width=2)
    cy = y + 8
    for text, size, color in text_lines:
        font = _font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((x + (w - tw) // 2, cy), text, font=font, fill=color)
        cy += th + 6


def make_corner_overlay() -> str:
    """生成右下角叠加小图 (~360x450, 透明背景)。"""
    W, H = 360, 450
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角白底框
    pad = 8
    draw.rounded_rectangle((pad, pad, W - pad, H - pad), radius=18,
                           fill=(255, 255, 255, 245), outline=(220, 30, 30, 255), width=3)

    # 标题（顶部居中，加大留白避免与手图重叠）
    title_font = _font(20)
    draw.text((W // 2, 18), "合谷穴 LI4", font=title_font,
              fill=(180, 30, 30, 255), anchor="mt")

    # 手图（缩小版，往下移）
    cx, cy = W // 2, 200
    _draw_hand_outline(draw, cx, cy, scale=0.55)

    # Hegu 标记点
    marker_x = cx - 20
    marker_y = cy + 16
    _draw_hegu_marker(draw, marker_x, marker_y, 11)

    # 文字说明
    info_y = 320
    _draw_label_box(
        draw, 18, info_y, W - 36, 95,
        [
            ("拇食指并拢", 18, (40, 40, 40, 255)),
            ("肌肉最高点", 18, (40, 40, 40, 255)),
        ],
    )

    out = os.path.join(OUT_DIR, "hegu_corner.png")
    img.save(out, "PNG")
    return out


def make_full_screen() -> str:
    """生成起止全屏图 (1080x1920, 含定位说明)。"""
    W, H = 1080, 1920
    img = Image.new("RGBA", (W, H), (250, 245, 240, 255))
    draw = ImageDraw.Draw(img)

    # 标题
    title_font = _font(72)
    draw.text((W // 2, 120), "合谷穴 · LI4 · Hegu", font=title_font,
              fill=(140, 30, 30, 255), anchor="mt")

    # 副标题
    sub_font = _font(40)
    draw.text((W // 2, 220), "四总穴之一 · 面口合谷收", font=sub_font,
              fill=(80, 80, 80, 255), anchor="mt")

    # 手图（大）
    cx, cy = W // 2, 950
    _draw_hand_outline(draw, cx, cy, scale=2.0)

    # 标记
    marker_x = cx - int(22 * 2)
    marker_y = cy + int(18 * 2)
    _draw_hegu_marker(draw, marker_x, marker_y, 28)

    # 定位说明（左侧引线）
    line_x_start = marker_x - 28
    line_x_end = 80
    line_y = marker_y
    draw.line([(line_x_start, line_y), (line_x_end, line_y)], fill=(220, 30, 30, 255), width=4)
    draw.polygon([(line_x_start, line_y - 10), (line_x_start, line_y + 10),
                  (line_x_start - 18, line_y)], fill=(220, 30, 30, 255))

    # 文字框
    box_x, box_y, box_w, box_h = 60, line_y - 130, 380, 260
    _draw_label_box(
        draw, box_x, box_y, box_w, box_h,
        [
            ("位置", 36, (140, 30, 30, 255)),
            ("手背第 1、2 掌骨之间", 28, (40, 40, 40, 255)),
            ("靠近第 2 掌骨中点的", 28, (40, 40, 40, 255)),
            ("桡侧处", 28, (40, 40, 40, 255)),
        ],
    )

    # 取穴方法（下方文字）
    method_y = 1500
    _draw_label_box(
        draw, 60, method_y, W - 120, 360,
        [
            ("取穴方法", 42, (140, 30, 30, 255)),
            ("将拇食指并拢", 32, (40, 40, 40, 255)),
            ("肌肉最高点即是合谷穴", 32, (40, 40, 40, 255)),
        ],
    )

    out = os.path.join(OUT_DIR, "hegu_full.png")
    img.save(out, "PNG")
    return out


if __name__ == "__main__":
    corner = make_corner_overlay()
    full = make_full_screen()
    print(f"corner: {corner}")
    print(f"full:   {full}")
    print(f"sizes:  corner={Image.open(corner).size}, full={Image.open(full).size}")
