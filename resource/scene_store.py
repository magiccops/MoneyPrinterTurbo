"""场景模板 / 草稿持久化存储。

存储布局：
    <root>/storage/scenes/templates/<name>.json   — 共享/可复用的场景模板
    <root>/storage/scenes/drafts/<name>.json      — 用户编辑中的草稿（半持久）

每条记录格式：
    {
        "name": "合谷穴示例",
        "type": "template" | "draft",
        "created_ts": "2026-06-14 19:30",
        "scenes": [ <scene dict>, ... ]
    }

首次启动时把 resource/builtin_templates/ 下的内置模板复制到 templates/，
用户即可在 WebUI 一键加载。
"""

import json
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

_THIS_FILE = os.path.realpath(__file__)
root_dir = os.path.dirname(os.path.dirname(_THIS_FILE))

SCENES_DIR: Path = Path(root_dir) / "storage" / "scenes"
TEMPLATES_DIR: Path = SCENES_DIR / "templates"
DRAFTS_DIR: Path = SCENES_DIR / "drafts"
BUILTIN_TEMPLATES_DIR: Path = Path(root_dir) / "resource" / "builtin_templates"


def _ensure_dirs() -> None:
    """确保 templates/ drafts/ 存在；首次启动复制内置模板。"""
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    _copy_builtin_templates()


def _copy_builtin_templates() -> None:
    """首次启动 / templates/ 为空时把内置模板复制过来（不覆盖已存在的）。"""
    if not BUILTIN_TEMPLATES_DIR.is_dir():
        return
    for src in BUILTIN_TEMPLATES_DIR.glob("*.json"):
        dst = TEMPLATES_DIR / src.name
        if not dst.exists():
            shutil.copy2(src, dst)


# ── 通用读写 ─────────────────────────────────────────────────────────

def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_name(name: str) -> str:
    """文件名安全化：去非法字符 + 去扩展名。"""
    name = (name or "").strip()
    # 去掉扩展名（防止 users 写 xxx.json）
    name = name.rsplit(".", 1)[0] if "." in name else name
    # 替换非法字符
    for ch in r'/\:*?"<>|':
        name = name.replace(ch, "_")
    return name or "未命名"


def _find_by_name(directory: Path, display_name: str) -> Optional[Path]:
    """在 directory 中查找 data['name'] == display_name 的 json 文件。"""
    if not display_name or not directory.is_dir():
        return None
    for p in directory.glob("*.json"):
        data = _read_json(p)
        if data and data.get("name") == display_name:
            return p
    return None


# ── 模板（templates/） ───────────────────────────────────────────────

def list_templates() -> List[Tuple[str, str]]:
    """返回 [(name, ts), ...] 按 name 排序。"""
    _ensure_dirs()
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*.json"), key=lambda x: x.stem):
        data = _read_json(p)
        if data is None:
            continue
        out.append((data.get("name") or p.stem, data.get("created_ts") or ""))
    return out


def load_template(name: str) -> Optional[List[dict]]:
    """加载模板的 scenes 列表；找不到返回 None。按 data['name'] 查找。"""
    _ensure_dirs()
    p = _find_by_name(TEMPLATES_DIR, name)
    if p is None:
        return None
    data = _read_json(p)
    if not data:
        return None
    return data.get("scenes") or []


def save_template(name: str, scenes: List[dict]) -> str:
    """保存为模板；name 冲突则覆盖。返回最终的文件名 stem。"""
    _ensure_dirs()
    from datetime import datetime
    data = {
        "name": name,
        "type": "template",
        "created_ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scenes": scenes,
    }
    # 如果已有同 name 的文件，覆盖；否则用 safe_name 命名
    existing = _find_by_name(TEMPLATES_DIR, name)
    target = existing or (TEMPLATES_DIR / f"{_safe_name(name)}.json")
    _write_json(target, data)
    return target.stem


def delete_template(name: str) -> bool:
    """删除模板；返回是否真删了。按 data['name'] 查找。"""
    _ensure_dirs()
    p = _find_by_name(TEMPLATES_DIR, name)
    if p and p.is_file():
        p.unlink()
        return True
    return False


# ── 草稿（drafts/） ──────────────────────────────────────────────────

def list_drafts() -> List[Tuple[str, str]]:
    """返回 [(name, ts), ...] 按 ts 倒序（最新在前）。"""
    _ensure_dirs()
    items = []
    for p in DRAFTS_DIR.glob("*.json"):
        data = _read_json(p)
        if data is None:
            continue
        items.append((data.get("name") or p.stem, data.get("created_ts") or ""))
    items.sort(key=lambda x: x[1], reverse=True)
    return items


def load_draft(name: str) -> Optional[List[dict]]:
    _ensure_dirs()
    p = _find_by_name(DRAFTS_DIR, name)
    if p is None:
        return None
    data = _read_json(p)
    if not data:
        return None
    return data.get("scenes") or []


def save_draft(name: str, scenes: List[dict]) -> str:
    _ensure_dirs()
    from datetime import datetime
    data = {
        "name": name,
        "type": "draft",
        "created_ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scenes": scenes,
    }
    existing = _find_by_name(DRAFTS_DIR, name)
    target = existing or (DRAFTS_DIR / f"{_safe_name(name)}.json")
    _write_json(target, data)
    return target.stem


def delete_draft(name: str) -> bool:
    _ensure_dirs()
    p = _find_by_name(DRAFTS_DIR, name)
    if p and p.is_file():
        p.unlink()
        return True
    return False
