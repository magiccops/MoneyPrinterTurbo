"""
PIL 程序化生成合谷穴 4 张关键帧 PNG（9:16 portrait 1080x1920）。

设计：真人手部 + 标注 + 文字 的"教科书插图"风格，不是抽象医学术语图。
参照中医针灸标准：拇食指并拢，肌肉最高点（LI4 桡侧）。

用法（容器内）：
  docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/anatomy/make_hegu_keyframes.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 中文字体 fallback 链（容器内）
FONT_CANDIDATES = [
    "/MoneyPrinterTurbo/resource/fonts/MicrosoftYaHeiBold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

OUT_DIR = "/MoneyPrinterTurbo/resource/anatomy/keyframes"
os.makedirs(OUT_DIR, exist_ok=True)

W, H = 1080, 1920  # 9:16 portrait


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ---------- 公共元素 ----------

def _draw_background(draw: ImageDraw.ImageDraw, color: tuple[int, int, int] = (250, 245, 235)) -> None:
    """米色背景，模拟中医养生图册风格。"""
    draw.rectangle([(0, 0), (W, H)], fill=color)


def _draw_title_bar(draw: ImageDraw.ImageDraw, title: str, subtitle: str = "") -> None:
    """顶部标题栏。"""
    title_font = _get_font(72)
    subtitle_font = _get_font(36)
    # 标题背景条
    draw.rectangle([(0, 0), (W, 200)], fill=(180, 50, 50))
    draw.text((W // 2, 100), title, font=title_font, fill="white", anchor="mm")
    if subtitle:
        draw.text((W // 2, 175), subtitle, font=subtitle_font, fill=(255, 230, 200), anchor="mm")


def _draw_simplified_hand(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float = 1.0) -> None:
    """简化手部轮廓（手背+拇食指并拢），用于合谷穴定位图。
    不是写实，是教科书线稿风格。
    """
    s = scale
    skin = (245, 220, 195)
    skin_line = (140, 90, 60)
    # 腕部（圆角矩形）
    wrist = [
        (cx - 180 * s, cy + 600 * s),
        (cx + 180 * s, cy + 600 * s),
        (cx + 220 * s, cy + 200 * s),
        (cx - 220 * s, cy + 200 * s),
    ]
    draw.polygon(wrist, fill=skin, outline=skin_line, width=4)
    # 手掌（椭圆）
    palm = [
        (cx, cy + 200 * s, 320 * s, 220 * s),
    ]
    draw.ellipse(
        [palm[0][0] - palm[0][2], palm[0][1] - palm[0][3],
         palm[0][0] + palm[0][2], palm[0][1] + palm[0][3]],
        fill=skin, outline=skin_line, width=4,
    )
    # 食指（向上）
    idx = [
        (cx + 80 * s, cy - 380 * s, 70 * s, 360 * s),
    ]
    draw.rounded_rectangle(
        [idx[0][0] - idx[0][2], idx[0][1] - idx[0][3],
         idx[0][0] + idx[0][2], idx[0][1] + idx[0][3]],
        radius=35 * s, fill=skin, outline=skin_line, width=4,
    )
    # 拇指（向左斜上，搭在食指根部）
    thb = [
        (cx - 200 * s, cy - 100 * s, 280 * s, 70 * s),
    ]
    # 旋转的拇指用一个多边形近似
    thumb_poly = [
        (cx - 60 * s, cy + 100 * s),
        (cx - 350 * s, cy - 150 * s),
        (cx - 380 * s, cy - 50 * s),
        (cx - 100 * s, cy + 200 * s),
    ]
    draw.polygon(thumb_poly, fill=skin, outline=skin_line, width=4)
    # 中指、无名指、小指（合拢的 3 根）
    for offset_x, offset_y in [(160, -250), (240, -100), (300, 50)]:
        finger = [
            (cx + offset_x * s, cy + offset_y * s, 60 * s, 200 * s),
        ]
        draw.rounded_rectangle(
            [finger[0][0] - finger[0][2], finger[0][1] - finger[0][3],
             finger[0][0] + finger[0][2], finger[0][1] + finger[0][3]],
            radius=30 * s, fill=skin, outline=skin_line, width=4,
        )


def _draw_red_dot_marker(draw: ImageDraw.ImageDraw, x: int, y: int, label: str = "合谷穴 LI4") -> None:
    """红点标注 + 标签引线。"""
    # 红点
    for r, alpha in [(60, 80), (45, 120), (32, 200), (24, 255)]:
        draw.ellipse([(x - r, y - r), (x + r, y + r)], fill=(220, 30, 30, alpha))
    # 引线
    end_x, end_y = x + 250, y - 250
    draw.line([(x, y), (end_x, end_y)], fill=(60, 30, 30), width=4)
    # 标签
    label_font = _get_font(40)
    draw.text((end_x + 10, end_y - 30), label, font=label_font, fill=(180, 30, 30))


# ---------- 4 张关键帧 ----------

def make_keyframe_A_title_and_locating() -> str:
    """关键帧 A：标题 + 合谷穴定位图（拇食指并拢 + 红点标注）。"""
    img = Image.new("RGBA", (W, H), (250, 245, 235, 255))
    draw = ImageDraw.Draw(img)
    _draw_background(draw)
    _draw_title_bar(draw, "合谷穴", "LI4  ·  四总穴之一")

    # 副标题
    sub_font = _get_font(38)
    draw.text((W // 2, 280), "拇食指并拢，肌肉最高点", font=sub_font, fill=(80, 50, 30), anchor="mm")

    # 简化手图（手背视角，拇食指并拢）
    hand_cx, hand_cy = W // 2, 1050
    _draw_simplified_hand(draw, hand_cx, hand_cy, scale=1.4)

    # 红点（合谷穴位置：拇食指并拢时第 1、2 掌骨间肌肉最高点）
    hegu_x, hegu_y = hand_cx - 100, hand_cy - 100
    _draw_red_dot_marker(draw, hegu_x, hegu_y, "合谷穴 LI4")

    # 底部说明
    foot_font = _get_font(36)
    draw.text(
        (W // 2, H - 150),
        "手背第 1、2 掌骨之间，靠近第 2 掌骨中点的桡侧处",
        font=foot_font, fill=(60, 40, 20), anchor="mm",
    )

    out = os.path.join(OUT_DIR, "00_A_title_locating.png")
    img.convert("RGB").save(out, quality=92)
    return out


def make_keyframe_B_pressing() -> str:
    """关键帧 B：按压合谷穴动作图（另一手指腹按压 + 力度示意）。"""
    img = Image.new("RGBA", (W, H), (250, 245, 235, 255))
    draw = ImageDraw.Draw(img)
    _draw_background(draw)
    _draw_title_bar(draw, "按压手法", "拇指指腹 · 力度以酸胀为宜")

    # 左手（被按压的手）
    hand_cx, hand_cy = W // 2, 1100
    _draw_simplified_hand(draw, hand_cx, hand_cy, scale=1.3)
    hegu_x, hegu_y = hand_cx - 100, hand_cy - 100
    # 穴位点
    draw.ellipse([(hegu_x - 24, hegu_y - 24), (hegu_x + 24, hegu_y + 24)], fill=(220, 30, 30))

    # 右手（按压的手指）— 简化：从右上方斜插下来的拇指
    finger_color = (255, 235, 215)
    finger_line = (140, 90, 60)
    press_finger = [
        (hand_cx + 350, hand_cy - 350),  # 起点右上
        (hand_cx + 200, hand_cy - 250),
        (hegu_x, hegu_y),                # 终点合谷穴
        (hegu_x - 80, hegu_y - 30),
        (hand_cx + 150, hand_cy - 180),
        (hand_cx + 300, hand_cy - 280),
    ]
    draw.polygon(press_finger, fill=finger_color, outline=finger_line)

    # 力度示意（向下的小箭头 + 力度文字）
    arr_font = _get_font(40)
    draw.polygon([
        (hegu_x, hegu_y - 150),
        (hegu_x - 30, hegu_y - 110),
        (hegu_x + 30, hegu_y - 110),
    ], fill=(180, 30, 30))
    draw.text((hegu_x + 50, hegu_y - 160), "按压", font=arr_font, fill=(180, 30, 30))

    # 酸胀感示意（3 条放射线）
    for ang in [60, 90, 120]:
        rad = math.radians(ang)
        sx, sy = hegu_x - 50 * math.cos(rad), hegu_y + 50 * math.sin(rad)
        ex, ey = hegu_x - 90 * math.cos(rad), hegu_y + 90 * math.sin(rad)
        draw.line([(sx, sy), (ex, ey)], fill=(220, 100, 30), width=6)
    # 酸胀文字
    tingle_font = _get_font(38)
    draw.text(
        (hegu_x - 200, hegu_y + 120),
        "酸  胀  感",
        font=tingle_font, fill=(200, 80, 30),
    )

    # 底部
    foot_font = _get_font(36)
    draw.text(
        (W // 2, H - 150),
        "力度以感到酸胀为宜，每次 3-5 分钟",
        font=foot_font, fill=(60, 40, 20), anchor="mm",
    )

    out = os.path.join(OUT_DIR, "01_B_pressing.png")
    img.convert("RGB").save(out, quality=92)
    return out


def make_keyframe_C_indications() -> str:
    """关键帧 C：3 个应用 icon（牙痛/头痛/咽喉肿痛）。"""
    img = Image.new("RGBA", (W, H), (250, 245, 235, 255))
    draw = ImageDraw.Draw(img)
    _draw_background(draw)
    _draw_title_bar(draw, "面口合谷收", "头面部疾病 · 均可按压合谷穴缓解")

    # 3 个 icon 横排
    icons = [
        ("牙痛", "🦷", (220, 30, 30)),
        ("头痛", "🧠", (60, 30, 180)),
        ("咽喉肿痛", "🗣", (30, 130, 60)),
    ]
    icon_w, icon_h = 280, 280
    spacing = 40
    total_w = 3 * icon_w + 2 * spacing
    start_x = (W - total_w) // 2
    icon_y = 500

    for i, (label, _, color) in enumerate(icons):
        x = start_x + i * (icon_w + spacing)
        # 圆角矩形背景
        draw.rounded_rectangle(
            [(x, icon_y), (x + icon_w, icon_y + icon_h)],
            radius=30, fill=tuple(min(255, c + 220) for c in color)[:3],
            outline=color, width=6,
        )
        # 简单画 icon：圆 + 内部图形
        cx, cy = x + icon_w // 2, icon_y + icon_h // 2
        if i == 0:  # 牙齿
            draw.ellipse([(cx - 50, cy - 60), (cx + 50, cy + 60)], fill="white", outline=color, width=6)
            draw.line([(cx - 25, cy + 30), (cx - 25, cy + 70)], fill=color, width=8)
            draw.line([(cx + 25, cy + 30), (cx + 25, cy + 70)], fill=color, width=8)
        elif i == 1:  # 头/脑
            draw.ellipse([(cx - 70, cy - 70), (cx + 70, cy + 70)], fill="white", outline=color, width=6)
            # 脑波纹
            for r in [30, 50]:
                draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=color, width=3)
        else:  # 咽喉
            draw.rounded_rectangle(
                [(cx - 50, cy - 70), (cx + 50, cy + 70)],
                radius=20, fill="white", outline=color, width=6,
            )
            for j in range(3):
                yy = cy - 30 + j * 30
                draw.line([(cx - 30, yy), (cx + 30, yy)], fill=color, width=5)
        # 标签
        lbl_font = _get_font(48)
        draw.text((cx, icon_y + icon_h + 60), label, font=lbl_font, fill=color, anchor="mm")

    # 底部
    foot_font = _get_font(40)
    draw.text(
        (W // 2, H - 200),
        "历代医家：面口合谷收",
        font=_get_font(56), fill=(180, 30, 30), anchor="mm",
    )
    draw.text(
        (W // 2, H - 110),
        "凡是头面部的疾病，都可按压合谷穴来缓解",
        font=foot_font, fill=(60, 40, 20), anchor="mm",
    )

    out = os.path.join(OUT_DIR, "02_C_indications.png")
    img.convert("RGB").save(out, quality=92)
    return out


def make_keyframe_D_warning() -> str:
    """关键帧 D：⚠️ 孕妇禁忌图。"""
    img = Image.new("RGBA", (W, H), (250, 245, 235, 255))
    draw = ImageDraw.Draw(img)
    _draw_background(draw)
    # 警示色顶部条
    draw.rectangle([(0, 0), (W, 200)], fill=(220, 150, 30))
    # ⚠️ 图标（圆+三角警告）
    draw.ellipse([(W // 2 - 60, 70), (W // 2 + 60, 190)], fill="white", outline=(220, 100, 30), width=6)
    draw.polygon([
        (W // 2, 95),
        (W // 2 - 30, 165),
        (W // 2 + 30, 165),
    ], fill=(220, 100, 30))
    # 警告文字
    warn_font = _get_font(80)
    draw.text((W // 2, 280), "孕妇禁忌", font=warn_font, fill=(180, 30, 30), anchor="mm")

    # 中央插图：孕妇剪影 + ❌ 符号
    silh_color = (200, 150, 130)
    # 简化孕妇（圆头 + 椭圆身体）
    head_x, head_y = W // 2, 600
    draw.ellipse([(head_x - 80, head_y - 80), (head_x + 80, head_y + 80)], fill=silh_color)
    # 身体（凸起的腹部）
    body_poly = [
        (head_x - 100, head_y + 80),
        (head_x + 100, head_y + 80),
        (head_x + 180, head_y + 250),
        (head_x + 100, head_y + 450),
        (head_x - 100, head_y + 450),
        (head_x - 180, head_y + 250),
    ]
    draw.polygon(body_poly, fill=silh_color)

    # ❌ 大叉
    cross_size = 200
    cross_cx, cross_cy = W // 2 + 250, head_y + 150
    draw.line(
        [(cross_cx - cross_size, cross_cy - cross_size), (cross_cx + cross_size, cross_cy + cross_size)],
        fill=(220, 30, 30), width=24,
    )
    draw.line(
        [(cross_cx + cross_size, cross_cy - cross_size), (cross_cx - cross_size, cross_cy + cross_size)],
        fill=(220, 30, 30), width=24,
    )

    # 解释
    expl_font = _get_font(48)
    draw.text(
        (W // 2, 1200),
        "孕妇不宜按压此穴",
        font=expl_font, fill=(80, 30, 30), anchor="mm",
    )
    draw.text(
        (W // 2, 1300),
        "以免引起子宫收缩",
        font=expl_font, fill=(80, 30, 30), anchor="mm",
    )

    # 底部
    foot_font = _get_font(40)
    draw.text(
        (W // 2, H - 150),
        "如需穴位按摩，请先咨询专业医生",
        font=foot_font, fill=(60, 40, 20), anchor="mm",
    )

    out = os.path.join(OUT_DIR, "03_D_warning.png")
    img.convert("RGB").save(out, quality=92)
    return out


def main() -> None:
    paths = [
        make_keyframe_A_title_and_locating(),
        make_keyframe_B_pressing(),
        make_keyframe_C_indications(),
        make_keyframe_D_warning(),
    ]
    print("Generated:")
    for p in paths:
        size = os.path.getsize(p) // 1024
        print(f"  {p} ({size} KB)")


if __name__ == "__main__":
    main()
