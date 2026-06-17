import math
import os.path
import re
import subprocess
import sys
from datetime import datetime, timedelta
from os import path

import edge_tts
from edge_tts.submaker import Subtitle
from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post
from app.services import state as sm
from app.utils import utils

# 「💾 history 兜底写入」：webui/Main.py 在 tm.start 返回后会用
# _write_history_for_current 写 status=success / failed + videos。
# 但 tm.start 是同步阻塞调用，期间任何 streamlit 异常 / 用户断网 /
# scriptrun abort 都会让 main.py 后续 _write_history_for_current 跑不到，
# 磁盘上视频已经生成但 history record 还是 draft + videos=[]（用户体感
# "视频没生成"）。这里在 task.py 内部 return kwargs 之前再写一次 history：
# - webui 进程：root_dir 已在 sys.path，import 成功
# - 纯 api / FastAPI 进程：import 失败时降级（不阻断任务），下次重试就行
_THIS_DIR = os.path.dirname(os.path.realpath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)
try:
    from webui import history as _history_store
except ImportError:
    _history_store = None


def _persist_history_record(task_id, params, status, videos):
    """task.py 内部兜底写 history record。失败也不抛（不阻断任务）。"""
    if _history_store is None:
        return False
    try:
        fields = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "subject": (params.video_subject or "").strip(),
            "status": status,
            "params": params.model_dump(mode="json"),
            "videos": list(videos or []),
        }
        # append_record 在 record 不存在时新建；存在时 update_record。
        # 这里两条都试一次：先查再 append/update，保证幂等。
        existing = _history_store.get_record(task_id)
        if existing is not None:
            return _history_store.update_record(task_id, fields)
        _history_store.append_record({"id": task_id, **fields})
        return True
    except Exception as _e:
        logger.warning(f"history 兜底写失败（不影响任务）: {_e}")
        return False


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject, video_script=video_script, amount=5
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def _concat_audio_files(segment_files, output_file):
    """用 ffmpeg concat demuxer 把 N 段 mp3 拼接成一段。"""
    list_file = output_file + ".concat.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for seg in segment_files:
                # 路径里有单引号会破坏 concat 语法；这里用绝对路径 + 双引号包裹。
                f.write(f"file '{seg.replace(chr(39), chr(39) + chr(92) + chr(39))}'\n")
        ffmpeg_binary = utils.get_ffmpeg_binary()
        cmd = [
            ffmpeg_binary,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_file,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(output_file):
            raise RuntimeError(
                f"ffmpeg concat audio failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
    finally:
        try:
            os.remove(list_file)
        except OSError:
            pass


def _concat_audio_files_with_gaps(segment_files, gap_seconds, output_file):
    """拼接 N 段 mp3，段间插入 gap_seconds 静音。

    对应「语音念完一段 → 静音 gap → 画面切到下一段 + 下一段语音起」的时间轴：
    静音期间画面停在当前场景，gap 结束才切场。

    用 concat demuxer + 重编码（统一 44100 mono libmp3lame）。各 TTS 段采样率
    /声道可能不一致（edge 24k mono、azure/mimo 不同），直接 -c copy 会让静音
    段变速或出现杂音；重编码统一后才稳，对 TTS 旁白音质影响可忽略。静音段用
    voice.generate_silent_audio 生成一次，在 concat 列表里段间复用同一文件。
    """
    if not segment_files:
        raise RuntimeError("no segment files to concat with gaps")

    silence_file = output_file + ".silence.mp3"
    if not voice.generate_silent_audio(gap_seconds, silence_file):
        raise RuntimeError(
            f"failed to generate inter-scene silence: {silence_file}"
        )

    list_file = output_file + ".concat.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segment_files):
                # 路径里有单引号会破坏 concat 语法；与 _concat_audio_files 一致地转义。
                safe_seg = seg.replace(chr(39), chr(39) + chr(92) + chr(39))
                f.write(f"file '{safe_seg}'\n")
                if i < len(segment_files) - 1:
                    f.write(f"file '{silence_file}'\n")
        ffmpeg_binary = utils.get_ffmpeg_binary()
        cmd = [
            ffmpeg_binary,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:a", "libmp3lame",
            "-q:a", "4",
            "-ar", "44100",
            "-ac", "1",
            output_file,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(output_file):
            raise RuntimeError(
                f"ffmpeg concat audio with gaps failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
    finally:
        for tmp in (list_file, silence_file):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _merge_sub_makers(sub_makers, durations, gap_seconds=0.0):
    """把 N 个 SubMaker 合并成一个：cues 累加 offset（timedelta）。

    gap_seconds > 0 时，段与段之间插入该长度的静音——offset 在每段（除最后
    一段，其后没有下一段）之后额外累加 gap，让后续段的字幕时间戳对齐「段间
    留白」后的真实音频时间轴，保证字幕和带 gap 的音频同步。
    """
    if not sub_makers:
        return None
    gap_us = int(max(gap_seconds, 0.0) * 1_000_000)
    has_cues = all(
        hasattr(sm_obj, "cues") and getattr(sm_obj, "cues", None)
        for sm_obj in sub_makers
    )
    if has_cues:
        merged = edge_tts.SubMaker()
        merged.type = getattr(sub_makers[0], "type", "WordBoundary")
        offset_us = 0  # microseconds（timedelta 用 microseconds 表达）
        idx = 0
        n = len(sub_makers)
        for sm_idx, (sm_obj, dur) in enumerate(zip(sub_makers, durations)):
            for cue in sm_obj.cues:
                idx += 1
                merged.cues.append(
                    Subtitle(
                        index=idx,
                        start=cue.start + timedelta(microseconds=offset_us),
                        end=cue.end + timedelta(microseconds=offset_us),
                        content=cue.content,
                    )
                )
            # 段间留白：只有「后面还有下一段」时才把 gap 计入 offset。
            offset_us += int(max(dur, 0.0) * 1_000_000)
            if sm_idx < n - 1:
                offset_us += gap_us
        return merged
    # legacy subs/offset 结构（项目里非 edge 路径仍使用）
    merged_sm = voice.ensure_legacy_submaker_fields(edge_tts.SubMaker())
    offset_100ns = 0
    n = len(sub_makers)
    for sm_idx, (sm_obj, dur) in enumerate(zip(sub_makers, durations)):
        for sub, (start, end) in zip(sm_obj.subs, sm_obj.offset):
            merged_sm.subs.append(sub)
            merged_sm.offset.append(
                (start + offset_100ns, end + offset_100ns)
            )
        offset_100ns += int(max(dur, 0.0) * 10_000_000)
        if sm_idx < n - 1:
            offset_100ns += int(max(gap_seconds, 0.0) * 10_000_000)
    return merged_sm


def _generate_audio_by_segments(task_id, params, segments, audio_file):
    """N 段独立 TTS → 拼接 mp3 + 合并 SubMaker + 测每段时长。

    段间插入 scene_gap_seconds（默认 0.5s）静音：语音念完一段后留白，画面在
    静音期间停在当前场景，gap 结束才切到下一段并同时起下一段语音/字幕。

    返回 (audio_file, total_duration, merged_sub_maker, scene_video_durations)。
    scene_video_durations = 每段 TTS 时长 + gap（最后一段不加，没有「下一段」
    可切），它会被 start() 透传给 get_video_materials / combine_videos，让每段
    画面停留 = 该段语音时长 + 段间留白，与带 gap 的音频时间轴对齐。
    """
    task_dir = utils.task_dir(task_id)
    segment_files = []
    segment_sub_makers = []
    segment_durations = []
    parsed_voice = voice.parse_voice_name(params.voice_name)

    for i, seg in enumerate(segments):
        seg_file = path.join(task_dir, f"audio_seg_{i:03d}.mp3")
        sm_obj = voice.tts(
            text=seg,
            voice_name=parsed_voice,
            voice_rate=params.voice_rate,
            voice_file=seg_file,
        )
        if sm_obj is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                f"segment TTS failed at index {i}: {seg[:50]!r}"
            )
            return None, None, None, None
        # 优先用 sub_maker 拿时长（无 mp4 re-decode 开销），兜底读 mp3。
        dur = voice.get_audio_duration(sm_obj)
        if dur <= 0:
            dur = voice.get_audio_duration(seg_file)
        segment_files.append(seg_file)
        segment_sub_makers.append(sm_obj)
        segment_durations.append(float(dur))

    # 段间留白（秒）。默认 0.5：念完一段→静音 0.5s→切下一段。设 0 关闭。
    n = len(segments)
    gap_seconds = float(config.app.get("scene_gap_seconds", 0.5) or 0.0)

    if gap_seconds > 0 and n >= 2:
        try:
            _concat_audio_files_with_gaps(segment_files, gap_seconds, audio_file)
        except Exception as concat_err:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(f"ffmpeg concat audio with gaps failed: {concat_err}")
            return None, None, None, None
        merged_sub_maker = _merge_sub_makers(
            segment_sub_makers, segment_durations, gap_seconds
        )
        gap_total = gap_seconds * (n - 1)
        # 每段画面停留 = 该段 TTS 时长 + 段间留白；最后一段无「下一段」可切，不加。
        scene_video_durations = [
            float(d) + gap_seconds for d in segment_durations[:-1]
        ] + [float(segment_durations[-1])]
    else:
        try:
            _concat_audio_files(segment_files, audio_file)
        except Exception as concat_err:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(f"ffmpeg concat audio failed: {concat_err}")
            return None, None, None, None
        merged_sub_maker = _merge_sub_makers(segment_sub_makers, segment_durations)
        gap_total = 0.0
        scene_video_durations = [float(d) for d in segment_durations]

    total_duration = sum(segment_durations) + gap_total
    # 与整段路径一致：向上取整，避免 0 触发下游失败
    total_duration_ceiled = math.ceil(total_duration) if total_duration > 0 else 0
    logger.info(
        f"segmented TTS done: {n} segments, "
        f"durations={[round(d, 2) for d in segment_durations]}, "
        f"gap={gap_seconds}s, total={total_duration_ceiled}s, "
        f"scene_video_durations={[round(d, 2) for d in scene_video_durations]}"
    )
    return audio_file, total_duration_ceiled, merged_sub_maker, scene_video_durations


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
        - scene_durations: list[float] 长度 = scene 数；仅在「按 1./2./3. 切分」
          路径下非空（值 = 每段 TTS 实际时长），其他情况为 None
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    custom_audio_file = getattr(params, "custom_audio_file", None)
    if not custom_audio_file or not os.path.exists(custom_audio_file):
        if custom_audio_file:
            logger.warning(
                f"custom audio file not found: {custom_audio_file}, using TTS to generate audio."
            )
        else:
            logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")

        # ── 检测「按 1./2./3. 切分」条件 ───────────────────────
        # 三个条件必须同时满足：
        #   1. 用户在场景编排里勾选了 auto_split_by_markers
        #   2. video_script 实际能被切出 N 段（>= 2）
        #   3. 切出的段数 == 场景编排的 scene 数
        # 否则 fallback 到整段 TTS（行为不变）。
        scene_segments = None
        if getattr(params, "auto_split_by_markers", False):
            custom_scenes = getattr(params, "custom_scenes", None) or []
            scene_segments = utils.split_script_by_markers(video_script)
            if scene_segments and len(scene_segments) == len(custom_scenes):
                logger.info(
                    f"auto-split by markers: {len(scene_segments)} segments, "
                    f"{len(custom_scenes)} scenes"
                )
            else:
                logger.warning(
                    "auto-split enabled but split result invalid: "
                    f"segments={len(scene_segments) if scene_segments else 0}, "
                    f"scenes={len(custom_scenes)}; "
                    "fallback to single TTS"
                )
                scene_segments = None

        if scene_segments:
            return _generate_audio_by_segments(
                task_id, params, scene_segments, audio_file
            )

        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration.")
            return None, None, None, None
        return audio_file, audio_duration, sub_maker, None
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None, None
        return custom_audio_file, audio_duration, None, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or sub_maker is None:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def get_video_materials(
    task_id, params, video_terms, audio_duration, tts_scene_durations=None
):
    # ── 来源优先级 ──────────────────────────────────────────
    # 1. TTS 实测分段时长（场景编排 + auto_split_by_markers 路径下，由
    #    _generate_audio_by_segments 测出并由 start() 透传）—— 反映"语音真
    #    的念这一段花了多少秒"，最准。
    # 2. UI 在场景卡片里填的 material.duration（旧的本地素材路径）—— 估算值，
    #    跟实际 TTS 时长常有偏差，仅在 TTS 没分段时兜底。
    # 3. 全局 video_clip_duration（历史默认）—— 兜底兜底。
    tts_durs = list(tts_scene_durations) if tts_scene_durations else None
    ui_durs = [
        float(m.duration) for m in params.video_materials if getattr(m, "duration", 0)
    ]

    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        # 优先 TTS 实测，缺则 UI 填的，N 段加总应 ≈ 音频总长。
        # 避免「N×clip_duration < audio_duration」触发 combine_videos 循环追加，
        # 造成"语音已读到下一段、画面还停在当前段"的错位。
        per_scene_durations = tts_durs or ui_durs or None
        materials = video.preprocess_video(
            materials=params.video_materials,
            clip_duration=params.video_clip_duration,
            durations=per_scene_durations,
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        return (
            [material_info.url for material_info in materials],
            per_scene_durations,
        )
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # Pexels/Pixabay 路径下没有 per-scene 素材概念，但若 generate_audio 已经
        # 测出 N 段 TTS 实测时长，把它们按 search_terms 数等比切分后传给
        # download_videos，让每个 term 的累计抓取时长上限对齐 TTS 分段，
        # combine_videos 收到的 max_clip_durations 跟单段 TTS 真实时长一致。
        per_term_durations = None
        if tts_durs and video_terms and len(video_terms) > 0:
            n_terms = len(video_terms)
            tts_total = sum(tts_durs)
            if tts_total > 0 and len(tts_durs) == n_terms:
                # 一一对应：TTS 第 i 段 = 第 i 个 term 的累计上限
                per_term_durations = tts_durs
                logger.info(
                    f"per-term max durations (TTS measured): {per_term_durations}"
                )
            elif tts_total > 0:
                # 数量不等：按比例把 TTS 总长摊到每个 term
                per_term = tts_total / n_terms
                per_term_durations = [per_term] * n_terms
                logger.info(
                    f"per-term max durations (proportional): {per_term_durations}"
                )

        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_contact_mode=params.video_concat_mode,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            per_term_durations=per_term_durations,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos, per_term_durations


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, scene_durations=None
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            max_clip_durations=scene_durations,
            threads=params.n_threads,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        # 可选：叠加解剖学角标图（合谷穴、足三里等穴位科普视频用）。
        # 触发条件：config.app.anatomy_overlay_image 指向已存在的 PNG 文件。
        # 输出文件用 .with-overlay.mp4 后缀，不破坏原 final-N.mp4。
        anatomy_overlay_image = str(
            config.app.get("anatomy_overlay_image", "") or ""
        ).strip()
        if anatomy_overlay_image and os.path.isfile(anatomy_overlay_image):
            overlay_output = path.join(
                utils.task_dir(task_id), f"final-{index}-overlay.mp4"
            )
            try:
                video.apply_anatomy_overlay(
                    input_video=final_video_path,
                    output_video=overlay_output,
                    overlay_image=anatomy_overlay_image,
                    position=str(
                        config.app.get("anatomy_overlay_position", "bottom-right")
                    ),
                    scale=float(config.app.get("anatomy_overlay_scale", 0.18)),
                    margin=int(config.app.get("anatomy_overlay_margin", 24)),
                )
                logger.info(f"anatomy overlay applied: {overlay_output}")
                # 让 WebUI 拿到的就是带角标的版本
                final_video_path = overlay_output
            except Exception as overlay_error:
                logger.warning(
                    f"anatomy overlay failed, fallback to plain video: {overlay_error}"
                )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def start(task_id, params: VideoParams, stop_at: str = "video"):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker, scene_durations = generate_audio(
        task_id, params, video_script
    )
    if not audio_file:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    # 透传 generate_audio 测出的「TTS 实测每段时长」（场景编排路径下非空），
    # 让 get_video_materials 用真实语音时长决定每段画面停留时间，
    # 避免 UI 填的 material.duration 跟实际 TTS 时长错位。
    downloaded_videos, scene_durations = get_video_materials(
        task_id, params, video_terms, audio_duration, scene_durations
    )
    if not downloaded_videos:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, downloaded_videos, audio_file, subtitle_path, scene_durations
    )

    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        _persist_history_record(task_id, params, "failed", [])
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Cross-post to TikTok/Instagram (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        logger.info("\n\n## cross-posting videos to TikTok/Instagram")
        for video_path in final_video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral"
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_results": cross_post_results if cross_post_results else None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    # 兜底：把 success 状态 + 视频路径写进 history，main.py 那边的
    # _write_history_for_current 是次要路径。即便 webui 同步阻塞 + scriptrun
    # abort 让 main.py 后续跑不到，磁盘上 history 也能反映真实结果。
    _persist_history_record(task_id, params, "success", final_video_paths)
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
