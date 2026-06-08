"""
合谷穴 30s 短视频（run_template 的薄壳示例）。

新主题用：复制本文件，改 TOPIC / SCRIPT / CLIPS_DIR / OVERLAY 四行即可。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/MoneyPrinterTurbo")
sys.path.insert(0, "/MoneyPrinterTurbo/resource")

from run_template import run

TOPIC = "四总穴之合谷穴"
SCRIPT = (
    "合谷穴是中医四总穴之一，位置在拇指与食指并拢时，"
    "手背肌肉隆起的最高点。"
    "历代医家有面口合谷收的说法，意思是头面部的疾病，"
    "比如牙痛、头痛、咽喉肿痛，都可以按压合谷穴来缓解。"
    "日常保健时，用拇指指腹按压合谷穴，"
    "力度以感到酸胀为宜，每次三到五分钟。"
    "需要注意的是，孕妇不宜按压此穴，以免引起子宫收缩。"
)
CLIPS_DIR = "/MoneyPrinterTurbo/storage/local_videos"
OUTPUT_DIR = "/MoneyPrinterTurbo/storage/hegu_demo"
OVERLAY = "/MoneyPrinterTurbo/resource/anatomy/hegu_corner.png"


if __name__ == "__main__":
    run(
        subject=TOPIC,
        script=SCRIPT,
        clips_dir=CLIPS_DIR,
        output_dir=OUTPUT_DIR,
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        overlay_image=OVERLAY,
        overlay_position="bottom-right",
        overlay_scale=0.18,
        video_clip_duration=3,
        check=True,  # 跑完自动抽 5 帧拼 preview.jpg，目视验证
    )
