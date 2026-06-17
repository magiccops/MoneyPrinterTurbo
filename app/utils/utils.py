import json
import locale
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from loguru import logger

from app.models import const


def get_response(status: int, data: Any = None, message: str = ""):
    obj = {
        "status": status,
    }
    if data:
        obj["data"] = data
    if message:
        obj["message"] = message
    return obj


def to_json(obj):
    try:
        # Define a helper function to handle different types of objects
        def serialize(o):
            # If the object is a serializable type, return it directly
            if isinstance(o, (int, float, bool, str)) or o is None:
                return o
            # If the object is binary data, convert it to a base64-encoded string
            elif isinstance(o, bytes):
                return "*** binary data ***"
            # If the object is a dictionary, recursively process each key-value pair
            elif isinstance(o, dict):
                return {k: serialize(v) for k, v in o.items()}
            # If the object is a list or tuple, recursively process each element
            elif isinstance(o, (list, tuple)):
                return [serialize(item) for item in o]
            # If the object is a custom type, attempt to return its __dict__ attribute
            elif hasattr(o, "__dict__"):
                return serialize(o.__dict__)
            # Return None for other cases (or choose to raise an exception)
            else:
                return None

        # Use the serialize function to process the input object
        serialized_obj = serialize(obj)

        # Serialize the processed object into a JSON string
        return json.dumps(serialized_obj, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"failed to serialize object to json: {str(e)}")
        return None


def get_uuid(remove_hyphen: bool = False):
    u = str(uuid4())
    if remove_hyphen:
        u = u.replace("-", "")
    return u


def root_dir():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def storage_dir(sub_dir: str = "", create: bool = False):
    d = os.path.join(root_dir(), "storage")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if create and not os.path.exists(d):
        os.makedirs(d)

    return d


def resource_dir(sub_dir: str = ""):
    d = os.path.join(root_dir(), "resource")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    return d


def task_dir(sub_dir: str = ""):
    d = os.path.join(storage_dir(), "tasks")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def font_dir(sub_dir: str = ""):
    d = resource_dir("fonts")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def song_dir(sub_dir: str = ""):
    d = resource_dir("songs")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def public_dir(sub_dir: str = ""):
    d = resource_dir("public")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def get_ffmpeg_binary() -> str:
    """
    解析当前进程应该使用的 FFmpeg 可执行文件。

    增加原因：
    1. 视频编码、静音音频生成、pydub 音频转码都依赖 FFmpeg；
    2. Windows 便携包、Docker 和用户自定义安装目录经常出现 PATH 不一致；
    3. 集中解析可以让所有调用方使用同一套优先级，减少某条链路能跑、
       另一条链路找不到 FFmpeg 的现场问题。

    优先级：
    1. IMAGEIO_FFMPEG_EXE：MoviePy/imageio 约定的显式配置；
    2. 系统 PATH 中的 ffmpeg；
    3. imageio-ffmpeg 依赖提供的内置二进制；
    4. 字符串 "ffmpeg" 兜底，交给 subprocess 在运行时暴露更具体错误。
    """
    configured_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured_ffmpeg:
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg:
            return bundled_ffmpeg
    except Exception as exc:
        logger.warning(f"failed to resolve bundled ffmpeg binary: {str(exc)}")

    return "ffmpeg"


def run_in_background(func, *args, **kwargs):
    def run():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"run_in_background error: {e}", exc_info=True)

    thread = threading.Thread(target=run, daemon=False)
    thread.start()
    return thread


def time_convert_seconds_to_hmsm(seconds) -> str:
    hours = int(seconds // 3600)
    seconds = seconds % 3600
    minutes = int(seconds // 60)
    milliseconds = int(seconds * 1000) % 1000
    seconds = int(seconds % 60)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, seconds, milliseconds)


def text_to_srt(idx: int, msg: str, start_time: float, end_time: float) -> str:
    start_time = time_convert_seconds_to_hmsm(start_time)
    end_time = time_convert_seconds_to_hmsm(end_time)
    srt = """%d
%s --> %s
%s
        """ % (
        idx,
        start_time,
        end_time,
        msg,
    )
    return srt


def str_contains_punctuation(word):
    for p in const.PUNCTUATIONS:
        if p in word:
            return True
    return False


def split_string_by_punctuations(s):
    result = []
    txt = ""

    previous_char = ""
    next_char = ""
    for i in range(len(s)):
        char = s[i]
        if char == "\n":
            result.append(txt.strip())
            txt = ""
            continue

        if i > 0:
            previous_char = s[i - 1]
        if i < len(s) - 1:
            next_char = s[i + 1]

        if char == "." and previous_char.isdigit() and next_char.isdigit():
            # # In the case of "withdraw 10,000, charged at 2.5% fee", the dot in "2.5" should not be treated as a line break marker
            txt += char
            continue

        if char == "," and previous_char.isdigit() and next_char.isdigit():
            # 英文数字里的千分位逗号不是断句符，例如 "1,000 years"。
            # Edge TTS 的 word boundary 通常会把这种数字整体作为连续内容返回；
            # 如果这里拆成 "1" 和 "000 years"，后续字幕聚合会无法匹配脚本原文，
            # 进而错误回退到 Whisper。
            txt += char
            continue

        if char not in const.PUNCTUATIONS:
            txt += char
        else:
            result.append(txt.strip())
            txt = ""
    result.append(txt.strip())
    # filter empty string
    result = list(filter(None, result))
    return result


# 「按数字标记切分文案」：识别脚本里的 `1.` / `2、` / `3)` / `4）` 段起始标记，
# 用于「🎬 场景编排」与旁白自动对齐：用户在主表单 video_script 里写 N 段
# 并在每段开头标注序号，task.py 据此把整段 TTS 拆成 N 段独立生成、按段测时长，
# 注入到每张 scene 的 duration，避免「N×clip_duration < audio_duration」时
# combine_videos 循环追加造成的"语音已读到下一段、画面还停在当前段"错位。
#
# 标记语法：
#   - 数字 N（1..99）
#   - 后接分隔符：「.  / 「、」 / 「)」 / 「）」
#   - 分隔符后接空白或行尾（避免误把 "1.5%" 里的 "1." 识别为标记）
#   - N 必须从 1 开始严格连续升序（1, 2, 3, ...），缺号 / 乱序不算"按标记切分"
import re as _re_for_marker_split

_SCRIPT_MARKER_RE = _re_for_marker_split.compile(
    r"(?<!\d)(\d{1,2})[、.)）](?!\d)"
)


def split_script_by_markers(script: str):
    """
    按 `1.` / `2、` / `3)` / `4）` 标记把脚本切成 N 段。

    Returns:
        - list[str] 长度 >= 2：成功切分
        - None：未识别到有效标记（不强制按标记切分，由调用方决定 fallback）
    """
    if not script:
        return None
    matches = list(_SCRIPT_MARKER_RE.finditer(script))
    if len(matches) < 2:
        return None
    # 严格 1..N 升序
    nums = [int(m.group(1)) for m in matches]
    if nums != list(range(1, len(nums) + 1)):
        return None
    # 切分：每段起点 = 当前 marker 之后（m.end()），
    # 终点 = 下一个 marker 起始处（m.start()）或 script 末尾。
    # 注意：end 不能用 m.end()（否则会漏进下一个 marker 自身）。
    starts = [m.end() for m in matches]
    ends = [m.start() for m in matches[1:]] + [len(script)]
    segments = []
    for s, e in zip(starts, ends):
        seg = script[s:e].strip()
        if not seg:
            return None  # 存在空段（连续标记）→ 视为无效
        segments.append(seg)
    return segments


# ── TTS 时长估算 ────────────────────────────────────────────────────
# 中文 edge-tts 经验值：
#   - 正常语速（voice_rate=1.0）≈ 3.5 字/秒（短句 4.0，长句 3.0）
#   - 句末标点（。！？.!?）多 ~0.45s 停顿
#   - 句中标点（，、,;；）多 ~0.20s 停顿
#   - 英文字母/数字按 0.4 字权（一个英文词 ≈ 0.5-0.7 字）
# 公式：duration = (字数 + 停顿 buffer) / (chars_per_sec × voice_rate)
# 实际 TTS 跟估算偏差通常在 ±15% 以内（TTS 实际测后仍可微调）
_CN_CHARS_PER_SEC = 3.5  # 中文朗读速度（字/秒），正常语速
_PAUSE_FULL = 0.45  # 句末停顿
_PAUSE_HALF = 0.20  # 句中停顿
_RE_CN = re.compile(r"[一-鿿]")
_RE_EN_WORD = re.compile(r"[A-Za-z]+")
_RE_DIGIT = re.compile(r"\d+")
_RE_PUNCT_END = re.compile(r"[。！？.!?]")  # 句末
_RE_PUNCT_MID = re.compile(r"[，、,;；:]")  # 句中


def estimate_tts_duration(
    text: str, voice_rate: float = 1.0, min_duration: float = 1.0
) -> float:
    """
    根据文案估算 TTS 朗读时长（秒）。

    Args:
        text: TTS 要朗读的文本（中英混合均可）
        voice_rate: 语速倍率（1.0=正常，1.2=快 20%）
        min_duration: 最短时长保底（默认 1.0s）

    Returns:
        估算秒数（float），最小为 min_duration
    """
    if not text or not text.strip():
        return min_duration

    text = text.strip()
    # 字数：中文按 1 字，英文按词算（1 词 ≈ 0.6 字），数字按 0.5 字
    cn = len(_RE_CN.findall(text))
    en_words = len(_RE_EN_WORD.findall(text))
    digits = len(_RE_DIGIT.findall(text))
    char_weight = cn + en_words * 0.6 + digits * 0.5

    # 停顿 buffer
    full_pause = len(_RE_PUNCT_END.findall(text)) * _PAUSE_FULL
    half_pause = len(_RE_PUNCT_MID.findall(text)) * _PAUSE_HALF

    rate = max(0.1, float(voice_rate) or 1.0)
    base = (char_weight + full_pause + half_pause) / (_CN_CHARS_PER_SEC * rate)
    return max(min_duration, round(base, 2))


def estimate_scene_durations(
    segments: list[str], voice_rate: float = 1.0
) -> list[float]:
    """
    批量估算 N 段 TTS 时长（每段独立算 + 总长加 0.1s 间隔 buffer）。

    Returns:
        list[float]，长度 == len(segments)
    """
    if not segments:
        return []
    rates = [max(0.1, float(voice_rate) or 1.0)] * len(segments)
    durations = [estimate_tts_duration(s, r) for s, r in zip(segments, rates)]
    return durations


def normalize_script_for_subtitle_matching(video_script: str) -> str:
    """
    清理字幕匹配前的脚本文本。

    用户可能手动输入 Markdown 分隔符、标题强调或 `_` 这类格式符号。
    这些字符通常不会出现在 TTS/Whisper 的识别结果里；如果继续参与
    字幕逐行匹配，脚本行数量会大于真实字幕行数量，最终可能补出
    `00:00:00,000 --> 00:00:00,000`，导致剪辑软件无法导入 SRT。
    """
    video_script = video_script or ""
    underscore_count = video_script.count("_")
    video_script = video_script.replace("_", "")
    cleaned_lines = []
    removed_separator_lines = 0
    for line in video_script.splitlines():
        line = line.strip()
        # Markdown 分隔符或强调符号单独成行时不会被 TTS 朗读，必须从
        # 脚本行里移除，避免字幕聚合卡在这类“不可发声”的目标行上。
        if re.fullmatch(r"[-*_]{3,}", line):
            removed_separator_lines += 1
            continue
        cleaned_lines.append(line)

    normalized_script = "\n".join(cleaned_lines).strip()
    if underscore_count or removed_separator_lines:
        logger.debug(
            "normalized script for subtitle matching, "
            f"removed underscores: {underscore_count}, "
            f"removed markdown separator lines: {removed_separator_lines}"
        )
    return normalized_script


def md5(text):
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def get_system_locale():
    try:
        loc = locale.getdefaultlocale()
        # zh_CN, zh_TW return zh
        # en_US, en_GB return en
        language_code = loc[0].split("_")[0]
        return language_code
    except Exception:
        return "en"


@lru_cache(maxsize=None)
def load_locales(i18n_dir):
    # WebUI 每次交互都会触发 Streamlit 重新执行脚本，语言文件运行期不会变化，
    # 因此缓存解析结果，避免反复读取和解析所有 i18n JSON 文件。
    _locales = {}
    for root, dirs, files in os.walk(i18n_dir):
        for file in files:
            if file.endswith(".json"):
                lang = file.split(".")[0]
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    _locales[lang] = json.loads(f.read())
    return _locales


def parse_extension(filename):
    return Path(filename).suffix.lower().lstrip('.')
