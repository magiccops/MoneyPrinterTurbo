import os
import sys
import time
import webbrowser
from datetime import datetime
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# 容器默认走 UTC，但日志和历史记录期望显示北京时间（UTC+8）。
# 在 loguru 首次 format / 首次 datetime.now() 之前就把进程的本地时区切到上海，
# 这样 {time:%Y-%m-%d %H:%M:%S} 和 datetime.now() 都拿到北京时间。
# Windows 没有 time.tzset()，那边系统时区本来就不是 UTC，try/except 兜底。
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except (AttributeError, OSError):
    pass

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

# resource/ 目录不是 Python package，但里面有 text_card.py 这种脚本式模块可被 WebUI 复用
resource_dir = os.path.join(root_dir, "resource")
if resource_dir not in sys.path:
    sys.path.append(resource_dir)

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, voice
from app.services import task as tm
from app.utils import utils
from webui import history as history_store
from text_card import render_text_card, render_text_card_thumbnail
from scene_store import (
    list_templates, load_template, save_template, delete_template,
    list_drafts, load_draft, save_draft, delete_draft,
)
import record_template_store

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "video_script_prompt" not in st.session_state:
    st.session_state["video_script_prompt"] = ""
if "custom_system_prompt" not in st.session_state:
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
if "use_custom_system_prompt" not in st.session_state:
    st.session_state["use_custom_system_prompt"] = False
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "local_video_materials" not in st.session_state:
    # 记住用户最近一次已经落盘的本地素材，避免仅修改文案后二次生成时丢失素材列表。
    st.session_state["local_video_materials"] = []
if "custom_scenes" not in st.session_state:
    # 「🎬 场景编排」面板里用户排好的场景列表，每条是一个 dict（含 id/type/参数）
    st.session_state["custom_scenes"] = []
if "custom_scenes_enabled" not in st.session_state:
    # 是否启用场景编排；启用时提交会覆盖 video_source="local" + video_materials
    st.session_state["custom_scenes_enabled"] = False

# 加载语言文件
locales = utils.load_locales(i18n_dir)


# ── 视图路由（list / detail）────────────────────────────────────────
# view="list"：列表页，显示所有单据 + 新建/复制/下载/删除入口。
# view="detail"：详情页，单据的编辑 + 生成视频。
# current_record_id：详情页正在编辑/查看的记录 id。
#   - 进入详情页时如果是某个 id：把它的 params 写回 session_state
#   - 进入详情页时如果为 None：表单从空白开始；点「保存草稿」或
#     「生成视频」时再 append 一条新记录
# 一次性：消费 current_record_id，避免 rerun 反复覆盖用户编辑。
if "view" not in st.session_state:
    st.session_state["view"] = "list"
if "current_record_id" not in st.session_state:
    st.session_state["current_record_id"] = None
if "pending_record_id" not in st.session_state:
    # 「点击列表行后跳详情页」时先把 id 暂存在这里，下一次 rerun
    # 才被详情页消费，避免点击瞬间连续两次 rerun 互相干扰。
    st.session_state["pending_record_id"] = None


def _open_record_detail(record_id):
    st.session_state["pending_record_id"] = record_id
    st.session_state["view"] = "detail"
    st.rerun()


def _back_to_list():
    st.session_state["view"] = "list"
    st.session_state["current_record_id"] = None
    st.session_state["pending_record_id"] = None
    st.rerun()


# ── 从模板新建 ────────────────────────────────────────────────────────
def _apply_record_template(tpl: dict) -> None:
    """从模板新建一条 status=draft 的单据，params 用模板预设。

    流程与「➕ 新建单据」一致，但用模板的 params 覆盖 VideoParams 默认
    值；这样详情页 history 恢复时把 params 写回 session_state，widget
    自动显示模板预设（文案/视频/音频/字幕各项）。
    """
    new_id = str(uuid4())
    tpl_params = tpl.get("params", {}) or {}
    base = VideoParams(video_subject="").model_dump(mode="json")
    base.update(tpl_params)
    # 主题用模板的 video_subject；为空时回退到模板名
    subject = (tpl_params.get("video_subject") or tpl.get("name", "")).strip() or tpl.get("name", "")
    history_store.append_record(
        {
            "id": new_id,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "subject": subject,
            "status": "draft",
            "videos": [],
            "params": base,
        }
    )
    # 模板里的场景列表（可选）也种到 session_state，让详情页场景编辑器自动加载
    if tpl.get("scenes"):
        # 重新分配 id，避免与用户已有 session_state 撞 widget key
        for sc in tpl["scenes"]:
            sc["id"] = str(uuid4())
        st.session_state["custom_scenes"] = list(tpl["scenes"])
        st.session_state["custom_scenes_enabled"] = True
    _open_record_detail(new_id)


@st.dialog("📑 从模板新建", width="large")
def _show_template_picker() -> None:
    """模板选择器：列出所有模板卡片，点「使用」创建单据并跳详情。"""
    templates = record_template_store.list_record_templates()
    if not templates:
        st.info("还没有任何模板。")
        if st.button("关闭", key="tpl_picker_close_empty"):
            st.rerun()
        return

    st.caption(f"共 {len(templates)} 个模板")
    for tpl in templates:
        with st.container(border=True):
            tpl_id = tpl.get("id", "")
            tpl_name = tpl.get("name", "(未命名)")
            tpl_desc = tpl.get("description", "")
            tpl_source = tpl.get("_source", "user")
            tpl_params = tpl.get("params", {}) or {}

            col_info, col_action = st.columns([5, 1])
            with col_info:
                st.markdown(f"**{tpl_name}**")
                if tpl_desc:
                    st.caption(tpl_desc)
                source_label = "🔒 内置" if tpl_source == "builtin" else "👤 用户"
                st.caption(f"{source_label} · {len(tpl_params)} 个字段")
            with col_action:
                if st.button(
                    "使用",
                    key=f"tpl_use_{tpl_id}",
                    use_container_width=True,
                    type="primary",
                ):
                    _apply_record_template(tpl)
                    st.rerun()


@st.dialog("📋 任务日志", width="large")
def _show_task_log(record_id: str) -> None:
    """在弹窗中显示 record_id 对应任务的运行日志。"""
    # 日志文件位于 storage/tasks/<record_id>/task.log
    log_path = os.path.join(
        root_dir, "storage", "tasks", record_id, "task.log"
    )
    if not os.path.isfile(log_path):
        st.info("暂无日志。视频生成过程中会自动记录。")
        return
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        st.error("无法读取日志文件。")
        return
    if not content.strip():
        st.info("日志为空。")
        return
    st.code(content, language=None)


# ─────────────────────────────────────────────────────────────────────


# ── 🎬 场景编排实现 ────────────────────────────────────────────────────
# 数据模型：st.session_state["custom_scenes"] = list[dict]
# 每条 dict 至少含 {id, type, ...}，详情见 plan。
SCENE_DEFAULT_TEXT = {
    "title": "合谷穴",
    "body": "面口合谷收",
    "bg_color": "#0F2A4A",
    "title_color": "#FFD700",
    "body_color": "#FFFFFF",
    "accent_color": "#FF6B6B",
    "title_size": 140,
    "body_size": 72,
}
SCENE_DEFAULT_IMAGE = {
    "file_path": "",
    "original_name": "",
}
LOCAL_VIDEOS_DIR_FOR_SCENES = os.path.join(root_dir, "storage", "local_videos")


def _new_scene_id() -> str:
    return str(uuid4())


def _delete_scene_files(scene: dict) -> None:
    """删除场景关联的产物文件（text 渲染的 PNG / image 上传的源文件）。"""
    for key in ("rendered_path", "file_path"):
        p = scene.get(key)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _render_scene_thumbnail(scene: dict) -> None:
    """在 scene 卡片内显示缩略图：文字卡实时渲染 200×356；图片直接预览。"""
    if scene["type"] == "text":
        try:
            thumb_path = os.path.join(
                "/tmp", f"scene_{scene['id']}_thumb.png"
            )
            render_text_card_thumbnail(
                thumb_path,
                title=scene.get("title", ""),
                body=scene.get("body", ""),
                bg_color=scene.get("bg_color", "#0F2A4A"),
                title_color=scene.get("title_color", "#FFD700"),
                body_color=scene.get("body_color", "#FFFFFF"),
                accent_color=scene.get("accent_color", "#FF6B6B"),
                title_size=int(scene.get("title_size", 140)),
                body_size=int(scene.get("body_size", 72)),
            )
            st.image(thumb_path, width=180, caption="缩略图预览（自动）")
        except Exception as e:
            st.caption(f"⚠️ 缩略图渲染失败: {e}")
    elif scene["type"] == "image":
        fp = scene.get("file_path", "")
        if fp and os.path.isfile(fp):
            try:
                st.image(fp, width=180, caption=scene.get("original_name", ""))
            except Exception as e:
                st.caption(f"⚠️ 图片预览失败: {e}")
        else:
            st.caption("📷 尚未上传图片")


def _render_scene_card(scene: dict, idx: int, total: int) -> None:
    """渲染单张 scene 卡片（在场景编排 expander 内）。"""
    with st.container(border=True):
        # 标题行：[N] 类型标签 · 名称 | ↑ ↓ 🗑️
        type_label = "文字" if scene["type"] == "text" else "图片"
        if scene["type"] == "text":
            name = (scene.get("title") or "(无标题)").strip()[:20] or "(无标题)"
        else:
            name = scene.get("original_name") or "(未上传)"
        header = f"[{idx + 1}] {type_label} · {name}"
        hcol1, hcol2, hcol3, hcol4 = st.columns([6, 1, 1, 1])
        with hcol1:
            st.markdown(f"**{header}**")
        with hcol2:
            if st.button("↑", key=f"scene_{scene['id']}_up", disabled=(idx == 0), use_container_width=True):
                scenes = st.session_state["custom_scenes"]
                scenes[idx - 1], scenes[idx] = scenes[idx], scenes[idx - 1]
                st.rerun()
        with hcol3:
            if st.button("↓", key=f"scene_{scene['id']}_down", disabled=(idx == total - 1), use_container_width=True):
                scenes = st.session_state["custom_scenes"]
                scenes[idx + 1], scenes[idx] = scenes[idx], scenes[idx + 1]
                st.rerun()
        with hcol4:
            if st.button("🗑️", key=f"scene_{scene['id']}_del", use_container_width=True):
                _delete_scene_files(scene)
                st.session_state["custom_scenes"].pop(idx)
                st.rerun()

        # 类型切换（只允许 text / image）
        new_type = st.selectbox(
            "类型",
            options=["text", "image"],
            index=0 if scene["type"] == "text" else 1,
            format_func=lambda x: "文字卡" if x == "text" else "图片",
            key=f"scene_{scene['id']}_type",
        )
        if new_type != scene["type"]:
            # 类型切换：删旧文件 + 初始化新字段
            _delete_scene_files(scene)
            scene["type"] = new_type
            for k, v in (SCENE_DEFAULT_TEXT if new_type == "text" else SCENE_DEFAULT_IMAGE).items():
                scene.setdefault(k, v)
            st.rerun()

        # 字段编辑
        if scene["type"] == "text":
            tcol1, tcol2 = st.columns([3, 2])
            with tcol1:
                scene["title"] = st.text_input(
                    "标题",
                    value=scene.get("title", ""),
                    key=f"scene_{scene['id']}_title",
                )
                scene["body"] = st.text_area(
                    "正文（\\n 强制换行 + 自动按宽换行）",
                    value=scene.get("body", ""),
                    key=f"scene_{scene['id']}_body",
                    height=100,
                )
            with tcol2:
                scene["bg_color"] = st.color_picker(
                    "背景色",
                    value=scene.get("bg_color", "#0F2A4A"),
                    key=f"scene_{scene['id']}_bg",
                )
                scene["title_color"] = st.color_picker(
                    "标题色",
                    value=scene.get("title_color", "#FFD700"),
                    key=f"scene_{scene['id']}_titlecolor",
                )
                scene["body_color"] = st.color_picker(
                    "正文字色",
                    value=scene.get("body_color", "#FFFFFF"),
                    key=f"scene_{scene['id']}_bodycolor",
                )
                scene["accent_color"] = st.color_picker(
                    "装饰条色",
                    value=scene.get("accent_color", "#FF6B6B"),
                    key=f"scene_{scene['id']}_accent",
                )
            scol1, scol2 = st.columns(2)
            with scol1:
                scene["title_size"] = st.slider(
                    "标题字号",
                    min_value=60, max_value=240, value=int(scene.get("title_size", 140)), step=10,
                    key=f"scene_{scene['id']}_titlesize",
                )
            with scol2:
                scene["body_size"] = st.slider(
                    "正文字号",
                    min_value=40, max_value=160, value=int(scene.get("body_size", 72)), step=4,
                    key=f"scene_{scene['id']}_bodysize",
                )
        else:  # image
            uploaded = st.file_uploader(
                "上传图片 (jpg/jpeg/png)",
                type=["jpg", "jpeg", "png", "JPG", "JPEG", "PNG"],
                key=f"scene_{scene['id']}_upload",
                accept_multiple_files=False,
            )
            if uploaded is not None and uploaded.name != scene.get("original_name"):
                # 写盘到 storage/local_videos/scene_<id>_<name>（preprocess_video 白名单路径）
                os.makedirs(LOCAL_VIDEOS_DIR_FOR_SCENES, exist_ok=True)
                save_path = os.path.join(
                    LOCAL_VIDEOS_DIR_FOR_SCENES, f"scene_{scene['id']}_{uploaded.name}"
                )
                with open(save_path, "wb") as f:
                    f.write(uploaded.getbuffer())
                # 旧文件清理
                if scene.get("file_path") and scene["file_path"] != save_path:
                    try:
                        if os.path.isfile(scene["file_path"]):
                            os.remove(scene["file_path"])
                    except OSError:
                        pass
                scene["file_path"] = save_path
                scene["original_name"] = uploaded.name
                st.rerun()

        # 缩略图预览
        _render_scene_thumbnail(scene)


def _render_scene_editor() -> None:
    """在中面板 Video Settings container 末尾渲染「🎬 场景编排」expander。"""
    scenes = st.session_state["custom_scenes"]
    n = len(scenes)
    with st.expander(f"🎬 场景编排 ({n})", expanded=False):
        st.checkbox(
            "启用场景编排（启用后将覆盖 Video Source）",
            key="custom_scenes_enabled",
            help="启用后，提交时会把下面这些场景渲染/拼成 video_materials，强制走 local 视频源。",
        )

        # ── 模板 / 草稿 I/O 子面板（默认折叠，干净） ──────────────
        with st.expander("📚 模板 / 草稿", expanded=False):
            _render_scene_io()

        if not scenes:
            st.caption("👇 点下方按钮添加第一张场景，或点上面「📚 模板 / 草稿」加载现成的。")
        else:
            st.caption("💡 调整字段时缩略图会自动重渲；提交时会用 1080×1920 全尺寸重渲一次。")
            for i, sc in enumerate(list(scenes)):  # list() 防止中途修改
                _render_scene_card(sc, i, n)

        # 添加按钮
        acol1, acol2 = st.columns(2)
        with acol1:
            if st.button("➕ 文字场景", key="add_text_scene", use_container_width=True):
                new_sc = {"id": _new_scene_id(), "type": "text", **SCENE_DEFAULT_TEXT}
                scenes.append(new_sc)
                st.rerun()
        with acol2:
            if st.button("➕ 图片场景", key="add_image_scene", use_container_width=True):
                new_sc = {"id": _new_scene_id(), "type": "image", **SCENE_DEFAULT_IMAGE}
                scenes.append(new_sc)
                st.rerun()


# ── 场景 I/O 实现（template / draft 加载/保存） ─────────────────────
def _refresh_scene_io_widgets() -> None:
    """清掉 selectbox 等 widget 缓存，让列表刷新。"""
    for k in (
        "scene_io_tpl_sel", "scene_io_tpl_name",
        "scene_io_draft_sel", "scene_io_draft_name",
    ):
        if k in st.session_state:
            # 保留值，update 触发 list 变化后 widget 会重新渲染
            pass


def _render_scene_io() -> None:
    """模板/草稿加载/保存 UI。放在 scene editor expander 内。"""
    # ── 加载模板 ──
    templates = list_templates()
    st.caption(f"📋 内置 + 用户模板（{len(templates)} 个）")
    tcol1, tcol2, tcol3 = st.columns([3, 1, 1])
    with tcol1:
        tpl_options = ["（选择模板）"] + [n for n, _ in templates]
        tpl_sel = st.selectbox(
            "加载模板", tpl_options, key="scene_io_tpl_sel", label_visibility="collapsed"
        )
    with tcol2:
        if st.button("📥 加载", key="scene_io_tpl_load", use_container_width=True):
            if tpl_sel and tpl_sel != "（选择模板）":
                loaded = load_template(tpl_sel)
                if loaded is not None:
                    # 重新分配 id，避免 widget key 冲突（加载多次/不同模板会撞 key）
                    for sc in loaded:
                        sc["id"] = _new_scene_id()
                    st.session_state["custom_scenes"] = loaded
                    st.toast(f"已加载模板：{tpl_sel}（{len(loaded)} 张场景）")
                    st.rerun()
                else:
                    st.error(f"加载失败：{tpl_sel}")
    with tcol3:
        if st.button("🗑️", key="scene_io_tpl_del", use_container_width=True,
                     help="删除当前选中的模板"):
            if tpl_sel and tpl_sel != "（选择模板）":
                if delete_template(tpl_sel):
                    st.toast(f"已删除模板：{tpl_sel}")
                    st.rerun()

    # ── 加载草稿 ──
    drafts = list_drafts()
    st.caption(f"📂 我的草稿（{len(drafts)} 个）")
    dcol1, dcol2, dcol3 = st.columns([3, 1, 1])
    with dcol1:
        draft_options = ["（选择草稿）"] + [n for n, _ in drafts]
        draft_sel = st.selectbox(
            "加载草稿", draft_options, key="scene_io_draft_sel", label_visibility="collapsed"
        )
    with dcol2:
        if st.button("📥 加载", key="scene_io_draft_load", use_container_width=True):
            if draft_sel and draft_sel != "（选择草稿）":
                loaded = load_draft(draft_sel)
                if loaded is not None:
                    for sc in loaded:
                        sc["id"] = _new_scene_id()
                    st.session_state["custom_scenes"] = loaded
                    st.toast(f"已加载草稿：{draft_sel}（{len(loaded)} 张场景）")
                    st.rerun()
                else:
                    st.error(f"加载失败：{draft_sel}")
    with dcol3:
        if st.button("🗑️", key="scene_io_draft_del", use_container_width=True,
                     help="删除当前选中的草稿"):
            if draft_sel and draft_sel != "（选择草稿）":
                if delete_draft(draft_sel):
                    st.toast(f"已删除草稿：{draft_sel}")
                    st.rerun()

    # ── 保存草稿 / 模板 ──
    st.divider()
    scol1, scol2 = st.columns(2)
    with scol1:
        st.text_input(
            "草稿名（保存到 drafts/）",
            key="scene_io_draft_name",
            placeholder="如：合谷穴v2-尝试新配色",
        )
        if st.button("💾 保存草稿", key="scene_io_save_draft", use_container_width=True):
            name = (st.session_state.get("scene_io_draft_name") or "").strip()
            if not name:
                st.error("请先在上方填草稿名")
            elif not st.session_state.get("custom_scenes"):
                st.error("当前场景列表为空，没有可保存的内容")
            else:
                save_draft(name, st.session_state["custom_scenes"])
                st.toast(f"已保存草稿：{name}")
                st.rerun()
    with scol2:
        st.text_input(
            "模板名（保存到 templates/）",
            key="scene_io_tpl_name",
            placeholder="如：中医穴位-6张标准版",
        )
        if st.button("💾 保存为模板", key="scene_io_save_tpl", use_container_width=True):
            name = (st.session_state.get("scene_io_tpl_name") or "").strip()
            if not name:
                st.error("请先在上方填模板名")
            elif not st.session_state.get("custom_scenes"):
                st.error("当前场景列表为空，没有可保存的内容")
            else:
                save_template(name, st.session_state["custom_scenes"])
                st.toast(f"已保存模板：{name}")
                st.rerun()





# ─────────────────────────────────────────────────────────────────────

# 创建一个顶部栏，包含标题和语言选择
# （顶部栏逻辑移到 _render_top_title()，由列表页/详情页各自调用）

# 详情页脚本语言下拉框的可选值（与历史记录里 params.video_language 保持一致）。
support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# ── 列表页 + 顶部栏 helper ──────────────────────────────────────────

def _render_top_title() -> None:
    """两个页面都用的顶部栏：项目标题 + 语言下拉。"""
    title_col, lang_col = st.columns([3, 1])
    with title_col:
        st.title(f"MoneyPrinterTurbo v{config.project_version}")
    with lang_col:
        display_languages = []
        selected_index = 0
        for i, code in enumerate(locales.keys()):
            display_languages.append(f"{code} - {locales[code].get('Language')}")
            if code == st.session_state.get("ui_language", ""):
                selected_index = i
        selected_language = st.selectbox(
            "Language / 语言",
            options=display_languages,
            index=selected_index,
            key="top_language_selector",
            label_visibility="collapsed",
        )
        if selected_language:
            code = selected_language.split(" - ")[0].strip()
            st.session_state["ui_language"] = code
            config.ui["language"] = code


def _render_video_download_button(
    file_path: str, key: str, use_container_width: bool = True
) -> None:
    """统一的视频下载按钮。永远渲染一个有状态的按钮：

    - 有文件 + 可读：active st.download_button，label 带文件名 + 大小
    - 有路径但文件丢失：disabled 按钮，label "⚠️ 视频文件已丢失"
    - 路径为空：disabled 按钮，label "📼 尚无视频可下载"
    - 读取抛错：disabled 按钮，label "⚠️ 下载失败: <err>"

    这样草稿记录也能看到一个「下载」占位，避免用户疑惑「按钮去哪了」。
    """
    if not file_path:
        st.button(
            "📼 尚无视频可下载",
            key=key,
            disabled=True,
            use_container_width=use_container_width,
        )
        return
    if not os.path.isfile(file_path):
        st.button(
            "⚠️ 视频文件已丢失",
            key=key,
            disabled=True,
            use_container_width=use_container_width,
            help=file_path,
        )
        return
    try:
        size = os.path.getsize(file_path)
        if size >= 1024 * 1024:
            size_label = f"{size / (1024 * 1024):.1f} MB"
        else:
            size_label = f"{size / 1024:.0f} KB"
        with open(file_path, "rb") as f:
            data = f.read()
        st.download_button(
            label=f"⬇️ 下载 {os.path.basename(file_path)} ({size_label})",
            data=data,
            file_name=os.path.basename(file_path),
            mime="video/mp4",
            key=key,
            use_container_width=use_container_width,
        )
    except Exception as _e:
        st.button(
            f"⚠️ 下载失败: {_e}",
            key=key,
            disabled=True,
            use_container_width=use_container_width,
        )


def _render_list_page() -> None:
    """列表页：单据卡片列表 + 新建/编辑/复制/下载/删除。

    「新建」和「复制」都立即 append 一条 status="draft" 的记录并跳到
    详情页（与用户已确认的语义一致）。
    """
    _render_top_title()
    st.markdown("### 📋 历史单据")

    top_cols = st.columns([4, 1, 1.2])
    with top_cols[1]:
        if st.button(
            "➕ 新建单据",
            key="list_new",
            use_container_width=True,
            type="primary",
        ):
            new_id = str(uuid4())
            history_store.append_record(
                {
                    "id": new_id,
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "subject": "",
                    "status": "draft",
                    "videos": [],
                    "params": VideoParams(video_subject="").model_dump(mode="json"),
                }
            )
            _open_record_detail(new_id)
    with top_cols[2]:
        # 从模板新建走 st.dialog 弹一个模板选择器，不立即创建记录
        if st.button(
            "📑 从模板新建",
            key="list_from_template",
            use_container_width=True,
        ):
            _show_template_picker()

    records = history_store.load_records(limit=200)
    if not records:
        st.info("还没有任何单据。点「➕ 新建单据」开始。")
        return

    st.caption(f"共 {len(records)} 条")

    for rec in records:
        with st.container(border=True):
            subject = (rec.get("subject") or "(无主题)").strip() or "(无主题)"
            ts = rec.get("ts", "")
            status = rec.get("status", "")
            videos = rec.get("videos") or []
            params = rec.get("params") or {}
            script = (
                (params.get("video_script") or "").strip()
                if isinstance(params, dict)
                else ""
            )

            top = st.columns([5, 2])
            with top[0]:
                st.markdown(f"**主题**：{subject}")
            with top[1]:
                st.caption(ts)

            if script:
                preview = script[:160] + ("…" if len(script) > 160 else "")
                st.markdown(f"**文案**：{preview}")

            status_map = {
                "success": "✅ 已生成",
                "failed": "❌ 失败",
                "draft": "📝 草稿",
            }
            st.caption(status_map.get(status, status or "—"))

            # 操作行：编辑 / 复制 / 日志 / 删除
            actions = st.columns([1, 1, 1, 1, 4])
            with actions[0]:
                if st.button(
                    "✏️ 编辑",
                    key=f"edit_{rec['id']}",
                    use_container_width=True,
                ):
                    _open_record_detail(rec["id"])
            with actions[1]:
                if st.button(
                    "📋 复制",
                    key=f"copy_{rec['id']}",
                    use_container_width=True,
                ):
                    new_id = str(uuid4())
                    new_subject = (
                        f"复制 - {subject}" if subject != "(无主题)" else "复制"
                    )
                    history_store.append_record(
                        {
                            "id": new_id,
                            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "subject": new_subject,
                            "status": "draft",
                            "videos": [],
                            "params": dict(params) if isinstance(params, dict) else {},
                        }
                    )
                    _open_record_detail(new_id)
            with actions[2]:
                if st.button(
                    "📋 日志",
                    key=f"log_{rec['id']}",
                    use_container_width=True,
                ):
                    _show_task_log(rec["id"])

            # 全宽下载行：即使草稿也显示按钮（disabled 占位），
            # 避免用户疑惑「按钮去哪了」。
            _first_video = videos[0] if videos else ""
            _render_video_download_button(
                _first_video, key=f"dl_{rec['id']}"
            )
            with actions[3]:
                if st.session_state.get(f"confirm_del_{rec['id']}"):
                    c = st.columns(2)
                    with c[0]:
                        if st.button(
                            "是",
                            key=f"yes_{rec['id']}",
                            use_container_width=True,
                        ):
                            try:
                                history_store.delete_record(rec["id"])
                            except OSError as _e:
                                st.error(
                                    f"删除失败：{_e}。通常是 "
                                    "storage/task_history.jsonl 或所在目录"
                                    "没有写权限，请在容器里 `chown -R` 或"
                                    " `chmod` 后再试。"
                                )
                            else:
                                st.session_state[
                                    f"confirm_del_{rec['id']}"
                                ] = False
                                st.rerun()
                    with c[1]:
                        if st.button(
                            "否",
                            key=f"no_{rec['id']}",
                            use_container_width=True,
                        ):
                            st.session_state[f"confirm_del_{rec['id']}"] = False
                            st.rerun()
                else:
                    if st.button(
                        "🗑️",
                        key=f"del_{rec['id']}",
                        use_container_width=True,
                    ):
                        st.session_state[f"confirm_del_{rec['id']}"] = True
                        st.rerun()


# ── 视图路由 ─────────────────────────────────────────────────────────
# 列表页用 _render_list_page() 渲染后 st.stop()，避免继续执行下面的
# 详情页表单代码。详情页保留原 inline 结构（form widgets 用 key= 写
# session_state，必须在同一 script run 内渲染）。
if st.session_state["view"] == "list":
    _render_list_page()
    st.stop()


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 获取日志记录中的文件全路径
        file_path = record["file"].path
        # 将绝对路径转换为相对于项目根目录的路径
        relative_path = os.path.relpath(file_path, root_dir)
        # 更新记录中的文件路径
        record["file"].path = f"./{relative_path}"
        # 返回修改后的格式字符串
        # 您可以根据需要调整这里的格式
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)

@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []

# 创建基础设置折叠框
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=False):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        # 左侧面板 - 日志设置
        with left_config_panel:
            # 是否隐藏配置面板
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            # 是否禁用日志显示
            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        # 中间面板 - LLM 设置

        with middle_config_panel:
            st.write(tr("LLM Settings"))
            # 下拉框需要展示“AIHubMix（推荐）”这类面向用户的文案，
            # 但配置文件和后端逻辑必须继续使用稳定的小写 provider id。
            # 因此这里显式维护 display label 和 provider id 的映射，避免
            # UI 文案变化污染 `config.app["llm_provider"]`。
            aihubmix_label = f"AIHubMix ({tr('Recommended')})"
            if config.ui.get("language") == "zh":
                aihubmix_label = "AIHubMix（推荐）"
            llm_provider_options = [
                ("OpenAI", "openai"),
                (aihubmix_label, "aihubmix"),
                ("Moonshot", "moonshot"),
                ("Azure", "azure"),
                ("Qwen", "qwen"),
                ("DeepSeek", "deepseek"),
                ("ModelScope", "modelscope"),
                ("Gemini", "gemini"),
                ("Grok", "grok"),
                ("Groq", "groq"),
                ("Ollama", "ollama"),
                ("G4f", "g4f"),
                ("OneAPI", "oneapi"),
                ("Cloudflare", "cloudflare"),
                ("ERNIE", "ernie"),
                ("MiniMax", "minimax"),
                ("MiMo", "mimo"),
                ("Pollinations", "pollinations"),
                ("LiteLLM", "litellm"),
            ]
            llm_provider_labels = [label for label, _ in llm_provider_options]
            llm_provider_values = {
                label: provider_id for label, provider_id in llm_provider_options
            }
            saved_llm_provider = config.app.get("llm_provider", "openai").lower()
            saved_llm_provider_index = 0
            for i, (_, provider_id) in enumerate(llm_provider_options):
                if provider_id == saved_llm_provider:
                    saved_llm_provider_index = i
                    break

            llm_provider_label = st.selectbox(
                tr("LLM Provider"),
                options=llm_provider_labels,
                index=saved_llm_provider_index,
            )
            llm_helper = st.container()
            llm_provider = llm_provider_values[llm_provider_label]
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(
                f"{llm_provider}_secret_key", ""
            )  # only for baidu ernie
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = config.get_default_ollama_base_url()

                with llm_helper:
                    docker_hint = ""
                    if config.is_running_in_container():
                        docker_hint = "\n                            > 检测到容器环境，未配置 Base Url 时会默认使用 `http://host.docker.internal:11434/v1`\n"
                    tips = f"""
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`{docker_hint}
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 官方 OpenAI 可留空；如果使用 OpenAI 兼容供应商（例如 OpenRouter），请填写对应的兼容接口地址
                            - **Model Name**: 填写**有权限**的模型；如果使用兼容供应商，请填写该平台支持的模型 ID
                            """

            if llm_provider == "aihubmix":
                if not llm_model_name:
                    llm_model_name = "gpt-5.4-mini"
                if not llm_base_url:
                    llm_base_url = "https://aihubmix.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIHubMix 配置说明
                            - **注册链接**: [点击注册 AIHubMix](https://aihubmix.com/?aff=CEve)
                            - **Base Url**: 预填 https://aihubmix.com/v1
                            - **推荐模型**: 默认 gpt-5.4-mini，也可以填写 AIHubMix 支持的免费模型或其它模型 ID

                            推荐理由：
                            - **模型全**: Claude、GPT、Gemini、Grok、DeepSeek、通义等 700+ 模型一站覆盖
                            - **稳定**: 无限并发，永远在线，集群部署于谷歌云，长期为众多知名应用提供高并发服务
                            - **能力完整**: 文本、图片生成、视频生成、TTS、STT、向量嵌入、Rerank，多模态场景全搞定
                            - **计费透明**: 按量付费，无会员无包月，免费模型可使用
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = (
                        "claude-3-5-sonnet-20240620"  # 默认模型，可以根据需要调整
                    )
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "grok":
                if not llm_model_name:
                    llm_model_name = "grok-4.3"
                if not llm_base_url:
                    llm_base_url = "https://api.x.ai/v1"

                with llm_helper:
                    tips = """
                            ##### Grok 配置说明
                            - **API Key**: 填写您的 GrokAPI 密钥
                            - **Base Url**: 填写 GrokAPI 的基础 URL
                            - **Model Name**: 比如 grok-4.3
                            """

            if llm_provider == "groq":
                if not llm_model_name:
                    llm_model_name = "llama-3.3-70b-versatile"
                if not llm_base_url:
                    llm_base_url = "https://api.groq.com/openai/v1"

                with llm_helper:
                    tips = """
                            ##### Groq 配置说明
                            - **API Key**: [点击到官网申请](https://console.groq.com/keys)
                            - **Base Url**: 固定为 https://api.groq.com/openai/v1
                            - **Model Name**: 比如 llama-3.3-70b-versatile
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "mimo":
                if not llm_model_name:
                    llm_model_name = "mimo-v2.5-pro"
                if not llm_base_url:
                    llm_base_url = "https://api.xiaomimimo.com/v1"
                with llm_helper:
                    tips = """
                            ##### Xiaomi MiMo 配置说明
                            - **API Key**: [点击到官网申请](https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call)
                            - **Base Url**: 固定为 https://api.xiaomimimo.com/v1
                            - **Model Name**: 默认 mimo-v2.5-pro，也可以按官方文档填写其它可用模型
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if llm_provider == "litellm":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                with llm_helper:
                    tips = """
                            ##### LiteLLM Configuration
                            > [LiteLLM](https://github.com/BerriAI/litellm) routes to 100+ LLM providers via a unified interface.
                            > Set your provider's API key as an env var: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.
                            - **Model Name**: LiteLLM format — `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`, `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0`, `gemini/gemini-2.5-flash`. See [full provider list](https://docs.litellm.ai/docs/providers)
                            """

            if tips and config.ui["language"] == "zh":
                # AIHubMix 自身就是 OpenAI-compatible 聚合平台；用户主动选择
                # 该 provider 时，再显示 DeepSeek/Moonshot 的通用推荐会造成
                # 信息干扰，也不利于保持合作入口的轻量、清晰。
                if llm_provider != "aihubmix":
                    st.warning(
                        "中国用户建议使用 **DeepSeek** 或 **Moonshot** 作为大模型提供商\n- 国内可直接访问，不需要VPN \n- 注册就送额度，基本够用"
                    )
                st.info(tips)

            st_llm_api_key = st.text_input(
                tr("API Key"), value=llm_api_key, type="password"
            )
            st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
            st_llm_model_name = ""
            if llm_provider != "ernie":
                if llm_provider == "groq":
                    effective_api_key = st_llm_api_key or llm_api_key
                    effective_base_url = st_llm_base_url or llm_base_url
                    groq_models = get_groq_model_ids(
                        api_key=effective_api_key,
                        base_url=effective_base_url,
                    )

                    if groq_models:
                        selected_index = 0
                        if llm_model_name in groq_models:
                            selected_index = groq_models.index(llm_model_name)

                        st_llm_model_name = st.selectbox(
                            tr("Model Name"),
                            options=groq_models,
                            index=selected_index,
                            key="groq_model_name_select",
                        )
                    else:
                        st_llm_model_name = st.text_input(
                            tr("Model Name"),
                            value=llm_model_name,
                            key="groq_model_name_input",
                        )
                        if effective_api_key:
                            st.caption(
                                "Unable to load Groq model list right now. You can still enter a model name manually."
                            )
                        else:
                            st.caption(
                                "Add a Groq API key to load available models automatically."
                            )
                else:
                    st_llm_model_name = st.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key=f"{llm_provider}_model_name_input",
                    )
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            else:
                st_llm_model_name = None

            if st_llm_api_key:
                config.app[f"{llm_provider}_api_key"] = st_llm_api_key
            if st_llm_base_url:
                config.app[f"{llm_provider}_base_url"] = st_llm_base_url
            if st_llm_model_name:
                config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            if llm_provider == "ernie":
                st_llm_secret_key = st.text_input(
                    tr("Secret Key"), value=llm_secret_key, type="password"
                )
                config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

            if llm_provider == "cloudflare":
                st_llm_account_id = st.text_input(
                    tr("Account ID"), value=llm_account_id
                )
                if st_llm_account_id:
                    config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        # 右侧面板 - API 密钥设置
        with right_config_panel:

            def get_keys_from_config(cfg_key):
                api_keys = config.app.get(cfg_key, [])
                if isinstance(api_keys, str):
                    api_keys = [api_keys]
                api_key = ", ".join(api_keys)
                return api_key

            def save_keys_to_config(cfg_key, value):
                value = value.replace(" ", "")
                if value:
                    config.app[cfg_key] = value.split(",")

            st.write(tr("Video Source Settings"))

            pexels_api_key = get_keys_from_config("pexels_api_keys")
            pexels_api_key = st.text_input(
                tr("Pexels API Key"), value=pexels_api_key, type="password"
            )
            save_keys_to_config("pexels_api_keys", pexels_api_key)

            pixabay_api_key = get_keys_from_config("pixabay_api_keys")
            pixabay_api_key = st.text_input(
                tr("Pixabay API Key"), value=pixabay_api_key, type="password"
            )
            save_keys_to_config("pixabay_api_keys", pixabay_api_key)

llm_provider = config.app.get("llm_provider", "").lower()


# ── 详情页：当前记录 + 顶部栏 + 历史视频预览 ──────────────────────
# 「列表页 → 点击行 / 新建 / 复制」会把目标 id 写到 pending_record_id 并
# 把 view 切到 detail。这里把它消费成 current_record_id（详情页编辑的
# 目标），同时根据该记录往 session_state 回填表单初始值。整段只在该
# id 非空时跑一次（rerun 之后 pending_record_id 已经被消费掉）。
_pending = st.session_state.get("pending_record_id")
if _pending:
    st.session_state["current_record_id"] = _pending
    st.session_state["pending_record_id"] = None

_detail_rec_id = st.session_state.get("current_record_id")
_detail_rec = (
    history_store.get_record(_detail_rec_id) if _detail_rec_id else None
)
if _detail_rec and isinstance(_detail_rec.get("params"), dict):
    for _k, _v in _detail_rec["params"].items():
        # 跳过 None / 复杂对象：widget key 期望的是 scalar 或简单 dict/list
        if _v is None:
            continue
        try:
            st.session_state[_k] = _v
        except Exception:
            pass
    _vm = _detail_rec["params"].get("video_materials")
    if isinstance(_vm, list):
        st.session_state["local_video_materials"] = _vm
    if _detail_rec["params"].get("custom_audio_file"):
        st.session_state["custom_audio_file"] = _detail_rec["params"]["custom_audio_file"]

# 「🎬 场景编排」恢复：VideoParams 模型不含 custom_scenes，上面通用循环
# 虽会写回 list/bool，但必须重新分配 scene id（场景卡 widget key 形如
# scene_<id>_title，复用旧 id 会命中之前编辑留下的 widget state 残留值）。
# 用 scenes_restored_for 守卫保证每个 record id 只恢复一次，避免每次
# rerun 都重新分配 id 把当前编辑的 widget state 弄乱。
# ⚠️ 仅当 record 实际存过 scenes（list 类型，无论空非空）才覆盖
# session_state；否则保留「从模板新建」等流程预先塞好的 scenes（此时
# record 还没保存过草稿，params 里没 custom_scenes 字段）。
if _detail_rec:
    _rec_id = _detail_rec.get("id")
    if st.session_state.get("scenes_restored_for") != _rec_id:
        _rec_scenes = _detail_rec.get("params", {}).get("custom_scenes")
        if isinstance(_rec_scenes, list):
            if _rec_scenes:
                for _sc in _rec_scenes:
                    if isinstance(_sc, dict) and _sc.get("id"):
                        _sc["id"] = str(uuid4())
            st.session_state["custom_scenes"] = list(_rec_scenes)
            st.session_state["custom_scenes_enabled"] = bool(
                _detail_rec.get("params", {}).get(
                    "custom_scenes_enabled", False
                )
            )
        st.session_state["scenes_restored_for"] = _rec_id


# 顶部栏：返回 / 单据标题 / 删除此单据
_col_back, _col_subject, _col_del = st.columns([1, 4, 1])
with _col_back:
    if st.button("← 返回列表", key="detail_back", use_container_width=True):
        _back_to_list()
with _col_subject:
    _subject_display = (
        (_detail_rec.get("subject") if _detail_rec else "") or "(新建单据)"
    )
    st.markdown(f"**{_subject_display}**")
with _col_del:
    if _detail_rec and st.button(
        "🗑️ 删除此单据", key="detail_del", use_container_width=True
    ):
        try:
            history_store.delete_record(_detail_rec["id"])
        except OSError as _e:
            st.error(
                f"删除失败：{_e}。通常是 "
                "storage/task_history.jsonl 或所在目录"
                "没有写权限，请在容器里 `chown -R` 或"
                " `chmod` 后再试。"
            )
        else:
            _back_to_list()


# 历史视频预览：单据已有视频时在表单上方展示，避免用户疑惑「为什么表单是空的」
if _detail_rec and _detail_rec.get("videos"):
    with st.expander(
        f"📼 历史视频 — {_detail_rec.get('ts','')} · "
        f"{_detail_rec.get('subject','(无主题)')}",
        expanded=False,
    ):
        for _url in _detail_rec["videos"][:3]:
            try:
                if os.path.isfile(_url):
                    st.video(_url)
                else:
                    st.caption(f"⚠️ 视频文件已不在：{_url}")
            except Exception:
                pass
# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────


panel = st.columns(3)
left_panel = panel[0]
middle_panel = panel[1]
right_panel = panel[2]

params = VideoParams(video_subject="")
uploaded_files = []
uploaded_audio_file = None

with left_panel:
    with st.container(border=True):
        st.write(tr("Video Script Settings"))
        params.video_subject = st.text_input(
            tr("Video Subject"),
            key="video_subject",
        ).strip()

        video_languages = [
            (tr("Auto Detect"), ""),
        ]
        for code in support_locales:
            video_languages.append((code, code))

        selected_index = st.selectbox(
            tr("Script Language"),
            index=0,
            options=range(
                len(video_languages)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_languages[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_language = video_languages[selected_index][1]

        with st.expander(tr("Advanced Script Settings"), expanded=False):
            params.paragraph_number = st.slider(
                tr("Script Paragraph Number"),
                min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                value=st.session_state.get("paragraph_number_input", 1),
                key="paragraph_number_input",
            )
            params.video_script_prompt = st.text_area(
                tr("Custom Script Requirements"),
                height=100,
                max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                placeholder=tr("Custom Script Requirements Placeholder"),
                key="video_script_prompt",
            ).strip()

            use_custom_system_prompt = st.checkbox(
                tr("Use Custom System Prompt"),
                help=tr("Use Custom System Prompt Help"),
                key="use_custom_system_prompt",
            )

            if use_custom_system_prompt:
                custom_system_prompt = st.text_area(
                    tr("Custom System Prompt"),
                    height=240,
                    max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                    key="custom_system_prompt",
                ).strip()
                params.custom_system_prompt = custom_system_prompt
            else:
                params.custom_system_prompt = ""

        if st.button(
            tr("Generate Video Script and Keywords"), key="auto_generate_script"
        ):
            with st.spinner(tr("Generating Video Script and Keywords")):
                script = llm.generate_script(
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                )
                terms = llm.generate_terms(params.video_subject, script)
                if "Error: " in script:
                    st.error(tr(script))
                elif "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_script"] = script
                    st.session_state["video_terms"] = ", ".join(terms)
        params.video_script = st.text_area(
            tr("Video Script"), value=st.session_state["video_script"], height=280
        )
        if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
            if not params.video_script:
                st.error(tr("Please Enter the Video Subject"))
                st.stop()

            with st.spinner(tr("Generating Video Keywords")):
                terms = llm.generate_terms(params.video_subject, params.video_script)
                if "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_terms"] = ", ".join(terms)

        params.video_terms = st.text_area(
            tr("Video Keywords"), value=st.session_state["video_terms"]
        )

with middle_panel:
    with st.container(border=True):
        st.write(tr("Video Settings"))
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        video_sources = [
            (tr("Pexels"), "pexels"),
            (tr("Pixabay"), "pixabay"),
            (tr("Local file"), "local"),
            (tr("TikTok"), "douyin"),
            (tr("Bilibili"), "bilibili"),
            (tr("Xiaohongshu"), "xiaohongshu"),
        ]

        saved_video_source_name = config.app.get("video_source", "pexels")
        saved_video_source_index = [v[1] for v in video_sources].index(
            saved_video_source_name
        )

        selected_index = st.selectbox(
            tr("Video Source"),
            options=range(len(video_sources)),
            format_func=lambda x: video_sources[x][0],
            index=saved_video_source_index,
        )
        params.video_source = video_sources[selected_index][1]
        config.app["video_source"] = params.video_source

        if params.video_source == "local":
            # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
            local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
            uploaded_files = st.file_uploader(
                tr("Upload Local Files"),
                type=local_file_types + [file_type.upper() for file_type in local_file_types],
                accept_multiple_files=True,
            )

        selected_index = st.selectbox(
            tr("Video Concat Mode"),
            index=1,
            options=range(
                len(video_concat_modes)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_concat_modes[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_concat_mode = VideoConcatMode(
            video_concat_modes[selected_index][1]
        )

        # 视频转场模式
        video_transition_modes = [
            (tr("None"), VideoTransitionMode.none.value),
            (tr("Shuffle"), VideoTransitionMode.shuffle.value),
            (tr("FadeIn"), VideoTransitionMode.fade_in.value),
            (tr("FadeOut"), VideoTransitionMode.fade_out.value),
            (tr("SlideIn"), VideoTransitionMode.slide_in.value),
            (tr("SlideOut"), VideoTransitionMode.slide_out.value),
        ]
        selected_index = st.selectbox(
            tr("Video Transition Mode"),
            options=range(len(video_transition_modes)),
            format_func=lambda x: video_transition_modes[x][0],
            index=0,
        )
        params.video_transition_mode = VideoTransitionMode(
            video_transition_modes[selected_index][1]
        )

        video_aspect_ratios = [
            (tr("Portrait"), VideoAspect.portrait.value),
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        selected_index = st.selectbox(
            tr("Video Ratio"),
            options=range(
                len(video_aspect_ratios)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_aspect_ratios[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])

        params.video_clip_duration = st.selectbox(
            tr("Clip Duration"), options=[2, 3, 4, 5, 6, 7, 8, 9, 10], index=1
        )
        params.video_count = st.selectbox(
            tr("Number of Videos Generated Simultaneously"),
            options=[1, 2, 3, 4, 5],
            index=0,
        )

        with st.expander(tr("Advanced Video Settings"), expanded=False):
            video_codec_options = [
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get("video_codec", "libx264")
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                saved_video_codec = "libx264"
            selected_codec_index = saved_video_codec_values.index(saved_video_codec)
            selected_codec_index = st.selectbox(
                tr("Video Encoder"),
                options=range(len(video_codec_options)),
                index=selected_codec_index,
                format_func=lambda x: video_codec_options[x][0],
                help=tr("Video Encoder Help"),
            )
            config.app["video_codec"] = video_codec_options[selected_codec_index][1]

        # ── 🎬 场景编排 ──────────────────────────────────────────────
        # 用户手动排一个时间线（文字卡 + 图片），提交时覆盖 video_source="local"
        # + video_materials=MaterialInfo 列表。不动 tm.start 内部。
        _render_scene_editor()
    with st.container(border=True):
        st.write(tr("Audio Settings"))

        # 添加TTS服务器选择下拉框
        tts_servers = [
            (voice.NO_VOICE_NAME, tr("No Voice")),
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
            ("mimo-tts", "Xiaomi MiMo TTS"),
        ]

        # 获取保存的TTS服务器，默认为v1
        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
        config.ui["tts_server"] = selected_tts_server

        # 根据选择的TTS服务器获取声音列表
        filtered_voices = []

        if selected_tts_server == voice.NO_VOICE_NAME:
            # 无配音是显式模式，只提供一个稳定 sentinel。这样普通 TTS 的空配置
            # 不会被误判为静音，后端也能继续通过同一条音频/字幕流程生成视频。
            filtered_voices = [voice.NO_VOICE_NAME]
        elif selected_tts_server == "siliconflow":
            # 获取硅基流动的声音列表
            filtered_voices = voice.get_siliconflow_voices()
        elif selected_tts_server == "gemini-tts":
            # 获取Gemini TTS的声音列表
            filtered_voices = voice.get_gemini_voices()
        elif selected_tts_server == "mimo-tts":
            # 获取 Xiaomi MiMo TTS 的预置音色列表
            filtered_voices = voice.get_mimo_voices()
        else:
            # 获取Azure的声音列表
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            # 根据选择的TTS服务器筛选声音
            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    # V2版本的声音名称中包含"v2"
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    # V1版本的声音名称中不包含"v2"
                    if "V2" not in v:
                        filtered_voices.append(v)

        if selected_tts_server == voice.NO_VOICE_NAME:
            friendly_names = {voice.NO_VOICE_NAME: tr("No Voice")}
        else:
            friendly_names = {
                v: v.replace("Female", tr("Female"))
                .replace("Male", tr("Male"))
                .replace("Neural", "")
                for v in filtered_voices
            }

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        # 检查保存的声音是否在当前筛选的声音列表中
        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            # 如果不在，则根据当前UI语言选择一个默认声音
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
                    break

        # 如果没有找到匹配的声音，使用第一个声音
        if saved_voice_name_index >= len(friendly_names) and friendly_names:
            saved_voice_name_index = 0

        # 确保有声音可选
        if friendly_names:
            selected_friendly_name = st.selectbox(
                tr("Speech Synthesis"),
                options=list(friendly_names.values()),
                index=min(saved_voice_name_index, len(friendly_names) - 1)
                if friendly_names
                else 0,
            )

            voice_name = list(friendly_names.keys())[
                list(friendly_names.values()).index(selected_friendly_name)
            ]
            params.voice_name = voice_name
            config.ui["voice_name"] = voice_name
        else:
            # 如果没有声音可选，显示提示信息
            st.warning(
                tr(
                    "No voices available for the selected TTS server. Please select another server."
                )
            )
            params.voice_name = ""
            config.ui["voice_name"] = ""

        # 无配音模式会生成静音占位音频，不展示试听按钮，避免用户误以为需要测试声音。
        if (
            friendly_names
            and selected_tts_server != voice.NO_VOICE_NAME
            and st.button(tr("Play Voice"))
        ):
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                # if the voice file generation failed, try again with a default content.
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    st.audio(audio_file, format="audio/mp3")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

        # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
        if selected_tts_server == "azure-tts-v2" or (
            voice_name and voice.is_azure_v2_voice(voice_name)
        ):
            saved_azure_speech_region = config.azure.get("speech_region", "")
            saved_azure_speech_key = config.azure.get("speech_key", "")
            azure_speech_region = st.text_input(
                tr("Speech Region"),
                value=saved_azure_speech_region,
                key="azure_speech_region_input",
            )
            azure_speech_key = st.text_input(
                tr("Speech Key"),
                value=saved_azure_speech_key,
                type="password",
                key="azure_speech_key_input",
            )
            config.azure["speech_region"] = azure_speech_region
            config.azure["speech_key"] = azure_speech_key

        # 当选择硅基流动时，显示API key输入框和说明信息
        if selected_tts_server == "siliconflow" or (
            voice_name and voice.is_siliconflow_voice(voice_name)
        ):
            saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

            siliconflow_api_key = st.text_input(
                tr("SiliconFlow API Key"),
                value=saved_siliconflow_api_key,
                type="password",
                key="siliconflow_api_key_input",
            )

            # 显示硅基流动的说明信息
            st.info(
                tr("SiliconFlow TTS Settings")
                + ":\n"
                + "- "
                + tr("Speed: Range [0.25, 4.0], default is 1.0")
                + "\n"
                + "- "
                + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
            )

            config.siliconflow["api_key"] = siliconflow_api_key

        # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
        # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
        if selected_tts_server == "mimo-tts" or (
            voice_name and voice.is_mimo_voice(voice_name)
        ):
            saved_mimo_api_key = config.app.get("mimo_api_key", "")

            mimo_api_key = st.text_input(
                tr("MiMo API Key"),
                value=saved_mimo_api_key,
                type="password",
                key="mimo_tts_api_key_input",
            )

            st.info(
                tr("MiMo TTS Settings")
                + ":\n"
                + "- "
                + tr("Uses Xiaomi MiMo V2.5 TTS preset voices")
                + "\n"
                + "- "
                + tr("Speed and volume are currently handled by the provider defaults")
            )

            config.app["mimo_api_key"] = mimo_api_key

        params.voice_volume = st.selectbox(
            tr("Speech Volume"),
            options=[0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0],
            index=2,
        )

        params.voice_rate = st.selectbox(
            tr("Speech Rate"),
            options=[0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0],
            index=2,
        )

        custom_audio_file_types = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
        uploaded_audio_file = st.file_uploader(
            tr("Custom Audio File"),
            type=custom_audio_file_types
            + [file_type.upper() for file_type in custom_audio_file_types],
            accept_multiple_files=False,
            key="custom_audio_file_uploader",
        )
        if uploaded_audio_file:
            st.audio(uploaded_audio_file, format="audio/mp3")
            st.info(
                tr(
                    "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                )
            )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Custom Background Music"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Background Music"),
            index=1,
            options=range(
                len(bgm_options)
            ),  # Use the index as the internal option value
            format_func=lambda x: bgm_options[x][
                0
            ],  # The label is displayed to the user
        )
        # Get the selected background music type
        params.bgm_type = bgm_options[selected_index][1]

        # Show or hide components based on the selection
        if params.bgm_type == "custom":
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"), key="custom_bgm_file_input"
            )
            if custom_bgm_file:
                # 这里不直接用 os.path.exists 判断，因为用户常见输入是
                # output000.mp3，这个文件名需要由服务层映射到 resource/songs
                # 目录后再校验。服务层会统一限制目录和文件类型，避免任意路径读取。
                params.bgm_file = custom_bgm_file.strip()
                # st.write(f":red[已选择自定义背景音乐]：**{custom_bgm_file}**")
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            index=2,
        )

with right_panel:
    with st.container(border=True):
        st.write(tr("Subtitle Settings"))
        params.subtitle_enabled = st.checkbox(tr("Enable Subtitles"), value=True)
        font_names = get_all_fonts()
        saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        saved_font_name_index = 0
        if saved_font_name in font_names:
            saved_font_name_index = font_names.index(saved_font_name)
        params.font_name = st.selectbox(
            tr("Font"), font_names, index=saved_font_name_index
        )
        config.ui["font_name"] = params.font_name

        subtitle_positions = [
            (tr("Top"), "top"),
            (tr("Center"), "center"),
            (tr("Bottom"), "bottom"),
            (tr("Custom"), "custom"),
        ]
        saved_subtitle_position = config.ui.get("subtitle_position", "bottom")
        saved_position_index = 2
        for i, (_, pos_value) in enumerate(subtitle_positions):
            if pos_value == saved_subtitle_position:
                saved_position_index = i
                break
        selected_index = st.selectbox(
            tr("Position"),
            index=saved_position_index,
            options=range(len(subtitle_positions)),
            format_func=lambda x: subtitle_positions[x][0],
        )
        params.subtitle_position = subtitle_positions[selected_index][1]
        config.ui["subtitle_position"] = params.subtitle_position

        if params.subtitle_position == "custom":
            saved_custom_position = config.ui.get("custom_position", 70.0)
            custom_position = st.text_input(
                tr("Custom Position (% from top)"),
                value=str(saved_custom_position),
                key="custom_position_input",
            )
            try:
                params.custom_position = float(custom_position)
                if params.custom_position < 0 or params.custom_position > 100:
                    st.error(tr("Please enter a value between 0 and 100"))
                else:
                    config.ui["custom_position"] = params.custom_position
            except ValueError:
                st.error(tr("Please enter a valid number"))

        font_cols = st.columns([0.3, 0.7])
        with font_cols[0]:
            saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
            params.text_fore_color = st.color_picker(
                tr("Font Color"), saved_text_fore_color
            )
            config.ui["text_fore_color"] = params.text_fore_color

        with font_cols[1]:
            saved_font_size = config.ui.get("font_size", 60)
            params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
            config.ui["font_size"] = params.font_size

        stroke_cols = st.columns([0.3, 0.7])
        with stroke_cols[0]:
            params.stroke_color = st.color_picker(tr("Stroke Color"), "#000000")
        with stroke_cols[1]:
            params.stroke_width = st.slider(tr("Stroke Width"), 0.0, 10.0, 1.5)

        subtitle_bg_cols = st.columns([0.4, 0.6])
        saved_subtitle_background_enabled = config.ui.get(
            "subtitle_background_enabled", True
        )
        with subtitle_bg_cols[0]:
            subtitle_background_enabled = st.checkbox(
                tr("Enable Subtitle Background"),
                value=saved_subtitle_background_enabled,
            )
        config.ui["subtitle_background_enabled"] = subtitle_background_enabled
        if subtitle_background_enabled:
            with subtitle_bg_cols[1]:
                saved_subtitle_background_color = config.ui.get(
                    "subtitle_background_color", "#000000"
                )
                params.text_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    saved_subtitle_background_color,
                )
                config.ui["subtitle_background_color"] = params.text_background_color
        else:
            params.text_background_color = False

        saved_rounded_subtitle_background = config.ui.get(
            "rounded_subtitle_background", False
        )
        # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件并保留原配置，
        # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
        params.rounded_subtitle_background = st.checkbox(
            tr("Rounded Subtitle Background"),
            value=(
                saved_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            ),
            help=tr("Rounded Subtitle Background Help"),
            disabled=not subtitle_background_enabled,
        )
        if subtitle_background_enabled:
            config.ui["rounded_subtitle_background"] = (
                params.rounded_subtitle_background
            )
    with st.expander(tr("Click to show API Key management"), expanded=False):
        st.subheader(tr("Manage Pexels and Pixabay API Keys"))

        col1, col2 = st.tabs([tr("Pexels API Keys"), tr("Pixabay API Keys")])

        with col1:
            st.subheader(tr("Pexels API Keys"))
            if config.app["pexels_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pexels_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pexels API Keys currently"))

            new_key = st.text_input(tr("Add Pexels API Key"), key="pexels_new_key")
            if st.button(tr("Add Pexels API Key")):
                if new_key and new_key not in config.app["pexels_api_keys"]:
                    config.app["pexels_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pexels API Key added successfully"))
                elif new_key in config.app["pexels_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pexels_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pexels API Key to delete"), config.app["pexels_api_keys"], key="pexels_delete_key"
                )
                if st.button(tr("Delete Selected Pexels API Key")):
                    config.app["pexels_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

        with col2:
            st.subheader(tr("Pixabay API Keys"))

            if config.app["pixabay_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pixabay_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pixabay API Keys currently"))

            new_key = st.text_input(tr("Add Pixabay API Key"), key="pixabay_new_key")
            if st.button(tr("Add Pixabay API Key")):
                if new_key and new_key not in config.app["pixabay_api_keys"]:
                    config.app["pixabay_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key added successfully"))
                elif new_key in config.app["pixabay_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pixabay_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pixabay API Key to delete"), config.app["pixabay_api_keys"], key="pixabay_delete_key"
                )
                if st.button(tr("Delete Selected Pixabay API Key")):
                    config.app["pixabay_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

# ── 提交按钮：💾 保存草稿 / 🎬 生成视频 ─────────────────────────────
# 两个动作都要把上传的音频/视频落盘 + 渲染场景编辑器 + 写历史；只有
# 「生成视频」额外跑 tm.start 实际生成视频。共享的「参数持久化」逻辑抽
# 成 helper，避免两边代码漂移。
def _persist_uploads_and_scene(params, task_id):
    """把本会话内的上传文件 / 场景编辑器渲染结果写入磁盘，并改写 params。

    返回是否有任何持久化错误（True = 有错但不中断；调用方决定是否 st.stop）。
    """
    if uploaded_audio_file:
        task_dir = utils.task_dir(task_id)
        # 上传文件名来自浏览器，不能直接拼到磁盘路径里；这里只保留扩展名，
        # 并使用固定文件名保存到当前任务目录，避免路径穿越或特殊字符问题。
        _, audio_ext = os.path.splitext(os.path.basename(uploaded_audio_file.name))
        audio_ext = audio_ext.lower() or ".mp3"
        custom_audio_path = os.path.join(task_dir, f"custom-audio{audio_ext}")
        with open(custom_audio_path, "wb") as f:
            f.write(uploaded_audio_file.getbuffer())
        params.custom_audio_file = custom_audio_path

    if uploaded_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
        params.video_materials = []
        persisted_local_materials = []
        for file in uploaded_files:
            file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
            with open(file_path, "wb") as f:
                f.write(file.getbuffer())
                m = MaterialInfo()
                m.provider = "local"
                m.url = file_path
                params.video_materials.append(m)
                persisted_local_materials.append(
                    {
                        "provider": m.provider,
                        "url": m.url,
                        "duration": m.duration,
                    }
                )
        # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
        st.session_state["local_video_materials"] = persisted_local_materials
    elif params.video_source == "local" and st.session_state["local_video_materials"]:
        # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
        params.video_materials = []
        for material in st.session_state["local_video_materials"]:
            m = MaterialInfo()
            m.provider = material.get("provider", "local")
            m.url = material.get("url", "")
            m.duration = material.get("duration", 0)
            if m.url:
                params.video_materials.append(m)

    # ── 启用「🎬 场景编排」→ 覆盖 video_source + video_materials ──────
    # 渲染文字场景为 1080×1920 全尺寸 PNG（preprocess_video 走图片分支会做 Ken Burns）
    # 之后强制 video_source="local"，按场景顺序拼。
    if st.session_state.get("custom_scenes_enabled") and st.session_state.get("custom_scenes"):
        _scene_paths = []
        _local_videos_dir = utils.storage_dir("local_videos", create=True)
        for _sc in st.session_state["custom_scenes"]:
            if _sc.get("type") == "text":
                _out = os.path.join(
                    _local_videos_dir, f"scene_{_sc['id']}.png"
                )
                try:
                    render_text_card(
                        out_path=_out,
                        title=_sc.get("title", ""),
                        body=_sc.get("body", ""),
                        bg_color=_sc.get("bg_color", "#0F2A4A"),
                        title_color=_sc.get("title_color", "#FFD700"),
                        body_color=_sc.get("body_color", "#FFFFFF"),
                        accent_color=_sc.get("accent_color", "#FF6B6B"),
                        width=1080,
                        height=1920,
                        title_size=int(_sc.get("title_size", 140)),
                        body_size=int(_sc.get("body_size", 72)),
                    )
                    _sc["rendered_path"] = _out
                    _scene_paths.append(_out)
                except Exception as _e:
                    logger.warning(f"scene text render failed: {_e}")
            elif _sc.get("type") == "image":
                _fp = _sc.get("file_path")
                if _fp and os.path.isfile(_fp):
                    _scene_paths.append(_fp)
                else:
                    logger.warning(
                        f"image scene {_sc.get('id')[:8]} missing file, skipped"
                    )
        if not _scene_paths:
            return "scene_empty"
        params.video_source = "local"
        params.video_materials = [
            MaterialInfo(provider="local", url=p) for p in _scene_paths
        ]
        # 保持用户排的顺序；金句卡与图的关系不能被打乱
        params.video_concat_mode = VideoConcatMode.sequential.value
        logger.info(
            f"scene editor: {len(_scene_paths)} materials, "
            f"forced video_source=local, concat=sequential"
        )
    return None


def _write_history_for_current(rec, params, status, videos, task_id):
    """把 params 写回历史：rec 存在则 update，不存在则 append。"""
    fields = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "subject": (params.video_subject or "").strip(),
        "status": status,
        "params": params.model_dump(mode="json"),
    }
    # 「🎬 场景编排」不在 VideoParams 模型里，单独塞到 params dict 里保存；
    # 这样编辑详情页时由下面的恢复逻辑自动写回 session_state["custom_scenes"]。
    # 不存 = 永远拿不回场景（用户体感："编辑时场景编排没带出来"）。
    _scenes = st.session_state.get("custom_scenes") or []
    fields["params"]["custom_scenes"] = list(_scenes)  # 拷贝防止下游误改原 list
    fields["params"]["custom_scenes_enabled"] = bool(
        st.session_state.get("custom_scenes_enabled")
    )
    # 草稿保留已有 videos（可能是上一轮成功生成留下的），其它状态直接覆盖
    if status != "draft":
        fields["videos"] = videos or []
    try:
        if rec is not None:
            history_store.update_record(rec["id"], fields)
            return rec["id"]
        else:
            new_id = task_id
            history_store.append_record({"id": new_id, **fields})
            return new_id
    except Exception as _h_err:
        logger.warning(f"history write failed: {_h_err}")
        return None


_action_cols = st.columns([1, 1, 4])
with _action_cols[0]:
    save_draft_btn = st.button(
        "💾 保存草稿", use_container_width=True
    )
with _action_cols[1]:
    generate_btn = st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
        key="generate_video_btn",
    )

if save_draft_btn or generate_btn:
    config.save_config()
    # 同一记录的多次编辑/生成共用一个 task_id（即记录 id），让 task_dir 可复用。
    task_id = _detail_rec["id"] if _detail_rec else str(uuid4())
    _persist_err = _persist_uploads_and_scene(params, task_id)
    if _persist_err == "scene_empty":
        st.error("场景编排已启用但没有有效的场景，请先添加文字或图片场景")
        scroll_to_bottom()
        st.stop()

    if save_draft_btn:
        # 草稿允许 subject/script 为空，仅落库，不生成视频。
        written_id = _write_history_for_current(
            _detail_rec, params, "draft", None, task_id
        )
        if written_id:
            st.session_state["current_record_id"] = written_id
            st.session_state["pending_record_id"] = None
            st.toast("💾 草稿已保存", icon="✅")
        else:
            st.error("草稿保存失败，请查看日志")
        scroll_to_bottom()
        st.rerun()

    # ── generate 路径：参数校验 + 跑 tm.start + 落历史 ──
    if not params.video_subject and not params.video_script:
        st.error(tr("Video Script and Subject Cannot Both Be Empty"))
        scroll_to_bottom()
        st.stop()

    if params.video_source not in ["pexels", "pixabay", "local"]:
        st.error(tr("Please Select a Valid Video Source"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        scroll_to_bottom()
        st.stop()

    log_container = st.empty()
    log_records = []

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_container:
            log_records.append(msg)
            st.code("\n".join(log_records))

    logger.add(log_received)
    # 把日志同步写到 task_dir/task.log，列表页「📋 日志」按钮可回看
    _log_dir = utils.task_dir(task_id)
    _log_path = os.path.join(_log_dir, "task.log")
    try:
        _log_fh = open(_log_path, "a", encoding="utf-8")
    except OSError:
        _log_fh = None

    def _log_to_disk(msg):
        if _log_fh:
            try:
                _log_fh.write(msg + "\n")
                _log_fh.flush()
            except OSError:
                pass

    logger.add(_log_to_disk, level="DEBUG")

    st.toast(tr("Generating Video"))
    logger.info(tr("Start Generating Video"))
    logger.info(utils.to_json(params))
    scroll_to_bottom()

    result = tm.start(task_id=task_id, params=params)
    # 关闭日志文件句柄
    if _log_fh:
        try:
            _log_fh.close()
        except OSError:
            pass
    if not result or "videos" not in result:
        st.error(tr("Video Generation Failed"))
        logger.error(tr("Video Generation Failed"))
        # 失败也要落历史（status=failed），方便回看当时参数
        _write_history_for_current(_detail_rec, params, "failed", [], task_id)
        scroll_to_bottom()
        st.stop()

    video_files = result.get("videos", [])
    st.success(tr("Video Generation Completed"))
    try:
        if video_files:
            for i, url in enumerate(video_files):
                # 视频 + 下载按钮并排显示；按钮读取本地文件后通过浏览器下载。
                _dl_cols = st.columns([4, 1])
                with _dl_cols[0]:
                    st.video(url)
                with _dl_cols[1]:
                    try:
                        with open(url, "rb") as _f:
                            _video_bytes = _f.read()
                        st.download_button(
                            label="⬇️ 下载视频",
                            data=_video_bytes,
                            file_name=os.path.basename(url),
                            mime="video/mp4",
                            key=f"download_{task_id}_{i}",
                            use_container_width=True,
                        )
                    except Exception as _dl_err:
                        st.caption(f"⚠️ 下载失败: {_dl_err}")
    except Exception:
        pass

    # 写历史（status=success）
    _write_history_for_current(_detail_rec, params, "success", video_files, task_id)
    open_task_folder(task_id)
    logger.info(tr("Video Generation Completed"))
    scroll_to_bottom()

config.save_config()
