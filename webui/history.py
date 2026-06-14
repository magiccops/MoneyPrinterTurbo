"""WebUI 视频生成历史记录的持久化模块。

存储格式：JSONL（每行一个 JSON object）。
文件位置：<项目根>/storage/task_history.jsonl。
容量：单文件最多保留 200 条（超过后只保留最后 200 条）。
"""

import json
import os
from pathlib import Path
from typing import List, Optional

# 复用 Main.py 里的 root_dir 计算方式
_THIS_FILE = os.path.realpath(__file__)
root_dir = os.path.dirname(os.path.dirname(_THIS_FILE))

HISTORY_PATH: Path = Path(root_dir) / "storage" / "task_history.jsonl"
MAX_RECORDS: int = 200


def _ensure_parent() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_record(record: dict) -> None:
    """追加一条历史记录；超过 MAX_RECORDS 时截断只保留尾部。"""
    _ensure_parent()
    line = json.dumps(record, ensure_ascii=False)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _truncate_if_needed()


def _truncate_if_needed() -> None:
    """如果文件超过 MAX_RECORDS 行，只保留最后 MAX_RECORDS 行。"""
    if not HISTORY_PATH.is_file():
        return
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_RECORDS:
        return
    kept = lines[-MAX_RECORDS:]
    tmp_path = HISTORY_PATH.with_suffix(HISTORY_PATH.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.writelines(kept)
    os.replace(tmp_path, HISTORY_PATH)


def load_records(limit: int = 30) -> List[dict]:
    """倒序返回最近 limit 条记录。损坏行静默跳过。"""
    if not HISTORY_PATH.is_file():
        return []
    out: List[dict] = []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def get_record(record_id: str) -> Optional[dict]:
    """按 id 查找单条记录。"""
    if not record_id:
        return None
    if not HISTORY_PATH.is_file():
        return None
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == record_id:
                    return rec
    except OSError:
        return None
    return None


def update_record(record_id: str, fields: dict) -> bool:
    """按 id 找到对应行，浅合并 fields 后整行替换。返回是否找到并更新。

    文件采用「读全部 → 改一行 → 写回」的简单实现；MAX_RECORDS=200 的体量
    下没有性能问题，也不会引入并发写风险（Streamlit 单进程）。
    """
    if not record_id or not isinstance(fields, dict):
        return False
    if not HISTORY_PATH.is_file():
        return False
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    updated = False
    new_lines: list[str] = []
    for raw in lines:
        raw_s = raw.strip()
        if not raw_s:
            new_lines.append(raw)
            continue
        try:
            rec = json.loads(raw_s)
        except json.JSONDecodeError:
            new_lines.append(raw)
            continue
        if rec.get("id") == record_id and not updated:
            rec.update(fields)
            new_lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
            updated = True
        else:
            new_lines.append(raw)

    if not updated:
        return False

    tmp_path = HISTORY_PATH.with_suffix(HISTORY_PATH.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp_path, HISTORY_PATH)
    except OSError:
        return False
    return True


def delete_record(record_id: str) -> bool:
    """按 id 找到对应行，删除整行。返回是否找到并删除。"""
    if not record_id:
        return False
    if not HISTORY_PATH.is_file():
        return False
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    kept: list[str] = []
    deleted = False
    for raw in lines:
        raw_s = raw.strip()
        if not raw_s:
            kept.append(raw)
            continue
        try:
            rec = json.loads(raw_s)
        except json.JSONDecodeError:
            kept.append(raw)
            continue
        if rec.get("id") == record_id and not deleted:
            deleted = True
            continue  # 跳过这一行
        kept.append(raw)

    if not deleted:
        return False

    tmp_path = HISTORY_PATH.with_suffix(HISTORY_PATH.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp_path, HISTORY_PATH)
    except OSError:
        return False
    return True


def clear_all() -> None:
    """清空历史文件。"""
    if HISTORY_PATH.is_file():
        try:
            HISTORY_PATH.unlink()
        except OSError:
            pass
