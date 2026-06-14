"""单据模板（form 预设）持久化。

布局：
    <root>/resource/builtin_record_templates/   — 内置模板（只读，跟随代码发布）
    <root>/storage/record_templates/            — 用户模板（未来扩展，当前为空）

每条模板格式：
    {
        "id": "hegu_acupoint",
        "name": "合谷穴科普（标准版）",
        "description": "4 句标准科普脚本 + 配套参数",
        "params": { ... 见下 ... },
        "scenes": [ ... 可选；同 scene_store 格式 ... ]
    }

params 字段命名：使用 form widget 的 session_state key（与 VideoParams
field 大部分重合；少数不同如 paragraph_number_input 会在模板里直接
使用 widget key，避免 restore 时还需要再映射）。这样 apply 模板时直接
st.session_state[k] = v 即可，widget 自动显示。
"""

import json
import os
from pathlib import Path
from typing import List, Optional

_THIS_FILE = os.path.realpath(__file__)
root_dir = os.path.dirname(os.path.dirname(_THIS_FILE))

USER_TEMPLATES_DIR: Path = Path(root_dir) / "storage" / "record_templates"
BUILTIN_TEMPLATES_DIR: Path = (
    Path(root_dir) / "resource" / "builtin_record_templates"
)


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_record_templates() -> List[dict]:
    """返回所有模板 dict 列表，按 (内置优先, name) 排序。"""
    items: List[dict] = []
    if BUILTIN_TEMPLATES_DIR.is_dir():
        for p in sorted(BUILTIN_TEMPLATES_DIR.glob("*.json")):
            data = _read_json(p)
            if data is None:
                continue
            items.append({**data, "_source": "builtin"})
    if USER_TEMPLATES_DIR.is_dir():
        for p in sorted(USER_TEMPLATES_DIR.glob("*.json")):
            data = _read_json(p)
            if data is None:
                continue
            items.append({**data, "_source": "user"})
    items.sort(
        key=lambda x: (0 if x.get("_source") == "builtin" else 1, x.get("name", ""))
    )
    return items


def load_record_template(template_id: str) -> Optional[dict]:
    """按 id 查找模板；找不到返回 None。"""
    if not template_id:
        return None
    for d in (BUILTIN_TEMPLATES_DIR, USER_TEMPLATES_DIR):
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            data = _read_json(p)
            if data and data.get("id") == template_id:
                return data
    return None
