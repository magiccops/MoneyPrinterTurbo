"""
端到端跑一条 30s 合谷穴短视频：
  1. 复用 `resource/hand_clips/` 里 15 条手部特写作为视频源
  2. 用 `zh-CN-XiaoxiaoNeural` 念 30s 旁白
  3. 拼视频 + 烧字幕 + 叠加合谷穴解剖学角标
  4. 输出到 `storage/hegu_demo/final-1-overlay.mp4`

用法（容器内）：
  docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/run_hegu_30s.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# 让脚本能找到 app.* 包
sys.path.insert(0, "/MoneyPrinterTurbo")

from loguru import logger

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import video, voice
from app.utils import utils

HAND_CLIPS_DIR = "/MoneyPrinterTurbo/storage/local_videos"
ANATOMY_OVERLAY = "/MoneyPrinterTurbo/resource/anatomy/hegu_corner.png"
OUTPUT_DIR = "/MoneyPrinterTurbo/storage/hegu_demo"

# 30s 中文脚本 (~140 chars，念 ~30s)
SCRIPT = (
    "合谷穴是中医四总穴之一，位置在拇指与食指并拢时，"
    "手背肌肉隆起的最高点。"
    "历代医家有面口合谷收的说法，意思是头面部的疾病，"
    "比如牙痛、头痛、咽喉肿痛，都可以按压合谷穴来缓解。"
    "日常保健时，用拇指指腹按压合谷穴，"
    "力度以感到酸胀为宜，每次三到五分钟。"
    "需要注意的是，孕妇不宜按压此穴，以免引起子宫收缩。"
)

# 视频参数（不调 LLM，不查 Pexels）
PARAMS = VideoParams(
    video_subject="四总穴之合谷穴",
    video_script=SCRIPT,
    video_terms=["hand close up", "finger pressure", "wellness"],  # 备用，本地模式不查
    video_aspect=VideoAspect.portrait.value,
    video_concat_mode=VideoConcatMode.random.value,
    video_transition_mode=VideoTransitionMode.none.value,
    video_clip_duration=3,           # 每段 3s
    video_count=1,
    video_source="local",
    video_materials=[],             # 下面填
    voice_name="zh-CN-XiaoxiaoNeural-Female",
    voice_volume=1.0,
    voice_rate=1.0,
    bgm_type="random",
    bgm_volume=0.2,
    subtitle_enabled=True,
    subtitle_position="bottom",
    custom_position=70.0,
    font_name="MicrosoftYaHeiBold.ttc",
    text_fore_color="#FFFFFF",
    text_background_color=True,
    rounded_subtitle_background=False,
    font_size=60,
    stroke_color="#000000",
    stroke_width=1.5,
    n_threads=2,
)


def _build_materials() -> list[MaterialInfo]:
    """把 hand_clips 目录里的 mp4 转成 MaterialInfo。"""
    materials: list[MaterialInfo] = []
    for path in sorted(Path(HAND_CLIPS_DIR).glob("*.mp4")):
        size = path.stat().st_size
        if size < 500_000:  # 跳过 404 之类的小文件
            continue
        # 用本地绝对路径作为 url 字段；preprocess_video 会直接用这个路径
        materials.append(
            MaterialInfo(
                provider="local",
                url=str(path),
                duration=0.0,  # 让 preprocess_video 用 ffprobe 重测
            )
        )
    logger.info(f"loaded {len(materials)} hand clips")
    return materials


def main() -> None:
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    task_id = f"hegu-demo-{int(time.time())}"

    # 1) 写脚本
    script_path = os.path.join(OUTPUT_DIR, "script.txt")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(SCRIPT)

    # 2) TTS 配音
    logger.info("[1/4] generating TTS audio")
    audio_file = os.path.join(OUTPUT_DIR, "audio.mp3")
    sub_maker = voice.azure_tts_v1(
        text=SCRIPT,
        voice_name=PARAMS.voice_name,
        voice_rate=PARAMS.voice_rate,
        voice_file=audio_file,
    )
    if sub_maker is None or not os.path.exists(audio_file):
        logger.error("TTS failed")
        sys.exit(1)
    audio_duration = voice.get_audio_duration(sub_maker)
    logger.info(f"audio duration: {audio_duration:.2f}s")

    # 3) 写字幕（直接用 SubMaker 写 SRT）
    subtitle_path = os.path.join(OUTPUT_DIR, "subtitle.srt")
    with open(subtitle_path, "w", encoding="utf-8") as f:
        f.write(sub_maker.get_srt())

    # 4) 拼手部素材
    logger.info("[2/4] combining hand clips")
    materials = _build_materials()
    if len(materials) < 3:
        logger.error(f"not enough hand clips, got {len(materials)}")
        sys.exit(1)
    PARAMS.video_materials = materials

    preprocessed = video.preprocess_video(
        materials=materials, clip_duration=PARAMS.video_clip_duration
    )
    video_paths = [m.url for m in preprocessed]
    logger.info(f"preprocessed {len(video_paths)} clips")

    combined_path = os.path.join(OUTPUT_DIR, "combined-1.mp4")
    video.combine_videos(
        combined_video_path=combined_path,
        video_paths=video_paths,
        audio_file=audio_file,
        video_aspect=PARAMS.video_aspect,
        video_concat_mode=PARAMS.video_concat_mode,
        video_transition_mode=PARAMS.video_transition_mode,
        max_clip_duration=PARAMS.video_clip_duration,
        threads=PARAMS.n_threads,
    )

    # 5) 加音频 + 字幕
    logger.info("[3/4] generating final video with audio + subtitle")
    final_path = os.path.join(OUTPUT_DIR, "final-1.mp4")
    video.generate_video(
        video_path=combined_path,
        audio_path=audio_file,
        subtitle_path=subtitle_path,
        output_file=final_path,
        params=PARAMS,
    )

    # 6) 叠加解剖学角标
    logger.info("[4/4] applying anatomy overlay")
    overlay_path = os.path.join(OUTPUT_DIR, "final-1-overlay.mp4")
    video.apply_anatomy_overlay(
        input_video=final_path,
        output_video=overlay_path,
        overlay_image=ANATOMY_OVERLAY,
        position="bottom-right",
        scale=0.18,
        margin=24,
    )

    size = os.path.getsize(overlay_path)
    logger.success(f"done: {overlay_path} ({size//1024} KB)")
    print(f"\nFINAL: {overlay_path}")


if __name__ == "__main__":
    main()
