"""
端到端短视频生成模板（30s / 60s / 90s 通用）。

适用场景：本地素材库 + 固定脚本 + TTS + 字幕 + 可选解剖学角标。
绕开 LLM / Pexels 路径，避免 LLM 抽离的抽象词在 Pexels 没库存导致跑题。

参数：
  subject         - 视频主题（仅记录到日志和文件名）
  script          - 旁白文案（中文 / 英文均可）
  clips_dir       - 本地素材目录（mp4 文件）
  output_dir      - 产物输出目录
  voice_name      - edge_tts voice 名称（必须与脚本语言匹配，否则 hang）
  overlay_image   - 解剖学角标 PNG 路径（None = 不叠）
  overlay_position- "bottom-right" / "top-left" 等
  overlay_scale   - 角标宽度占比（9:16 视频下 0.18 约 360×450）
  video_clip_duration - 单段时长（秒）
  font_name       - 字体文件名（在 resource/fonts/ 下）

用法（容器内）：
  docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/run_template.py \\
    --subject "四总穴之合谷穴" \\
    --script-file /MoneyPrinterTurbo/resource/scripts/hegu.txt \\
    --clips-dir /MoneyPrinterTurbo/storage/local_videos \\
    --output-dir /MoneyPrinterTurbo/storage/hegu_demo \\
    --voice-name zh-CN-XiaoxiaoNeural-Female \\
    --overlay-image /MoneyPrinterTurbo/resource/anatomy/hegu_corner.png

也可以作为模块 import，让 run_<topic>_<duration>s.py 薄壳化复用。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/MoneyPrinterTurbo")

from loguru import logger

from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import video, voice


# 最小素材文件大小（字节），< 500KB 多为 Pexels 404 假文件
_MIN_CLIP_BYTES = 500_000


def _build_materials(clips_dir: str) -> list[MaterialInfo]:
    """扫描 clips_dir 下的 mp4，跳过过小文件，构造 MaterialInfo 列表。"""
    materials: list[MaterialInfo] = []
    for path in sorted(Path(clips_dir).glob("*.mp4")):
        if path.stat().st_size < _MIN_CLIP_BYTES:
            logger.warning(f"skip too-small clip: {path.name}")
            continue
        materials.append(
            MaterialInfo(
                provider="local",
                url=str(path),
                duration=0.0,  # 让 preprocess_video 用 ffprobe 重测
            )
        )
    logger.info(f"loaded {len(materials)} clips from {clips_dir}")
    return materials


def _build_params(
    subject: str,
    script: str,
    voice_name: str,
    video_clip_duration: int,
    font_name: str,
) -> VideoParams:
    """构造通用 VideoParams。"""
    return VideoParams(
        video_subject=subject,
        video_script=script,
        video_terms=[],  # 本地模式不走 LLM 搜索
        video_aspect=VideoAspect.portrait.value,
        video_concat_mode=VideoConcatMode.random.value,
        video_transition_mode=VideoTransitionMode.none.value,
        video_clip_duration=video_clip_duration,
        video_count=1,
        video_source="local",
        video_materials=[],
        voice_name=voice_name,
        voice_volume=1.0,
        voice_rate=1.0,
        bgm_type="random",
        bgm_volume=0.2,
        subtitle_enabled=True,
        subtitle_position="bottom",
        custom_position=70.0,
        font_name=font_name,
        text_fore_color="#FFFFFF",
        text_background_color=True,
        rounded_subtitle_background=False,
        font_size=60,
        stroke_color="#000000",
        stroke_width=1.5,
        n_threads=2,
    )


def run(
    subject: str,
    script: str,
    clips_dir: str,
    output_dir: str,
    voice_name: str = "zh-CN-XiaoxiaoNeural-Female",
    overlay_image: str | None = None,
    overlay_position: str = "bottom-right",
    overlay_scale: float = 0.18,
    overlay_margin: int = 24,
    video_clip_duration: int = 3,
    font_name: str = "MicrosoftYaHeiBold.ttc",
) -> str:
    """跑一条端到端短视频，返回最终产物路径。"""
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    task_id = f"{subject}-{int(time.time())}"

    # 1) 写脚本
    script_path = os.path.join(output_dir, "script.txt")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    logger.info(f"script written: {script_path}")

    # 2) TTS 配音
    logger.info("[1/4] generating TTS audio")
    audio_file = os.path.join(output_dir, "audio.mp3")
    sub_maker = voice.azure_tts_v1(
        text=script,
        voice_name=voice_name,
        voice_rate=1.0,
        voice_file=audio_file,
    )
    if sub_maker is None or not os.path.exists(audio_file):
        logger.error(f"TTS failed for voice={voice_name}")
        sys.exit(1)
    audio_duration = voice.get_audio_duration(sub_maker)
    logger.info(f"audio duration: {audio_duration:.2f}s")

    # 3) 写字幕
    subtitle_path = os.path.join(output_dir, "subtitle.srt")
    with open(subtitle_path, "w", encoding="utf-8") as f:
        f.write(sub_maker.get_srt())

    # 4) 拼素材
    logger.info("[2/4] combining clips")
    materials = _build_materials(clips_dir)
    if len(materials) < 3:
        logger.error(f"not enough clips in {clips_dir}, got {len(materials)}")
        sys.exit(1)

    params = _build_params(
        subject=subject,
        script=script,
        voice_name=voice_name,
        video_clip_duration=video_clip_duration,
        font_name=font_name,
    )
    params.video_materials = materials

    preprocessed = video.preprocess_video(
        materials=materials, clip_duration=params.video_clip_duration
    )
    video_paths = [m.url for m in preprocessed]
    logger.info(f"preprocessed {len(video_paths)} clips")

    combined_path = os.path.join(output_dir, "combined-1.mp4")
    video.combine_videos(
        combined_video_path=combined_path,
        video_paths=video_paths,
        audio_file=audio_file,
        video_aspect=params.video_aspect,
        video_concat_mode=params.video_concat_mode,
        video_transition_mode=params.video_transition_mode,
        max_clip_duration=params.video_clip_duration,
        threads=params.n_threads,
    )

    # 5) 加音频 + 字幕
    logger.info("[3/4] generating final video with audio + subtitle")
    final_path = os.path.join(output_dir, "final-1.mp4")
    video.generate_video(
        video_path=combined_path,
        audio_path=audio_file,
        subtitle_path=subtitle_path,
        output_file=final_path,
        params=params,
    )

    # 6) 叠加解剖学角标（可选）
    if overlay_image and os.path.isfile(overlay_image):
        logger.info(f"[4/4] applying anatomy overlay: {overlay_image}")
        final_path = os.path.join(output_dir, "final-1-overlay.mp4")
        try:
            video.apply_anatomy_overlay(
                input_video=os.path.join(output_dir, "final-1.mp4"),
                output_video=final_path,
                overlay_image=overlay_image,
                position=overlay_position,
                scale=overlay_scale,
                margin=overlay_margin,
            )
        except Exception as e:
            logger.warning(f"overlay failed, fallback to plain video: {e}")
            final_path = os.path.join(output_dir, "final-1.mp4")
    else:
        logger.info("[4/4] no overlay image, skip")

    size = os.path.getsize(final_path)
    logger.success(f"done: {final_path} ({size // 1024} KB)")
    return final_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MoneyPrinterTurbo 短视频生成模板")
    p.add_argument("--subject", required=True, help="视频主题")
    p.add_argument("--script", default=None, help="旁白脚本（直接传字符串）")
    p.add_argument("--script-file", default=None, help="旁白脚本文件路径（utf-8）")
    p.add_argument("--clips-dir", required=True, help="本地素材目录")
    p.add_argument("--output-dir", required=True, help="产物输出目录")
    p.add_argument(
        "--voice-name", default="zh-CN-XiaoxiaoNeural-Female", help="edge_tts voice"
    )
    p.add_argument("--overlay-image", default=None, help="解剖学角标 PNG（可选）")
    p.add_argument(
        "--overlay-position", default="bottom-right", help="角标位置"
    )
    p.add_argument("--overlay-scale", type=float, default=0.18, help="角标缩放")
    p.add_argument("--overlay-margin", type=int, default=24, help="角标边距")
    p.add_argument(
        "--video-clip-duration", type=int, default=3, help="单段时长（秒）"
    )
    p.add_argument(
        "--font-name", default="MicrosoftYaHeiBold.ttc", help="字体文件名"
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.script_file:
        with open(args.script_file, encoding="utf-8") as f:
            script = f.read()
    else:
        script = args.script or ""
    if not script:
        logger.error("either --script or --script-file is required")
        sys.exit(1)

    run(
        subject=args.subject,
        script=script,
        clips_dir=args.clips_dir,
        output_dir=args.output_dir,
        voice_name=args.voice_name,
        overlay_image=args.overlay_image,
        overlay_position=args.overlay_position,
        overlay_scale=args.overlay_scale,
        overlay_margin=args.overlay_margin,
        video_clip_duration=args.video_clip_duration,
        font_name=args.font_name,
    )


if __name__ == "__main__":
    main()
