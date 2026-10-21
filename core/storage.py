"""本地快照与配置存储层。

数据全部落在项目根目录的 ``data/`` 下，纯 JSON，无需数据库：

.. code-block:: text

    data/
    ├── config.json                 # Cookie 配置（脱敏对外）
    ├── index.json                  # 快照索引，按时间倒序
    └── snapshots/
        └── <gc>_<YYYYmmdd-HHmmss>/
            ├── meta.json           # 快照元信息
            ├── messages.json       # 归一化后的消息数组
            └── images/             # 下载的图片

本模块的公共函数都不依赖 Flask，可单独用于脚本或测试。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
SNAPSHOTS_DIR: Path = DATA_DIR / "snapshots"
CONFIG_PATH: Path = DATA_DIR / "config.json"
INDEX_PATH: Path = DATA_DIR / "index.json"

#: 快照目录名格式：``<群号>_<YYYYmmdd-HHmmss>``（可带 ``-N`` 冲突后缀）。
#: 用 ``\Z`` 而非 ``$``：Python 的 ``$`` 允许匹配末尾换行，会把 ``"123_2025\n"`` 误判为合法。
SNAPSHOT_ID_PATTERN = re.compile(r"^\d+_[\d-]+\Z")

#: 单进程内的读写锁，避免并发请求写坏索引文件。
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# 基础 IO
# --------------------------------------------------------------------------- #


def ensure_dirs() -> None:
    """确保运行期需要的目录都存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    """读取 JSON 文件，失败或不存在时返回默认值。

    Args:
        path: 文件路径。
        default: 兜底默认值。

    Returns:
        解析结果或 ``default``。
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    """原子地写入 JSON 文件（先写临时文件再替换）。

    Args:
        path: 目标路径。
        payload: 可序列化对象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _now_iso() -> str:
    """当前本地时间的 ISO 字符串（秒级）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _now_display() -> str:
    """当前本地时间的展示字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 配置（Cookie）
# --------------------------------------------------------------------------- #

_DEFAULT_CONFIG: dict[str, Any] = {
    "cookie": "",
    "uin": "",
    "updated_at": "",
}


def load_config() -> dict[str, Any]:
    """读取配置（不存在时返回默认结构）。

    Returns:
        配置字典，含 ``cookie`` / ``uin`` / ``updated_at``。
    """
    ensure_dirs()
    with _LOCK:
        data = _read_json(CONFIG_PATH, {})
    if not isinstance(data, dict):
        data = {}
    config = dict(_DEFAULT_CONFIG)
    config.update({key: data.get(key, config[key]) for key in config})
    return config


def save_config(cookie: str, uin: str = "") -> dict[str, Any]:
    """保存 Cookie 配置。

    Args:
        cookie: 规范化后的 Cookie 字符串（``a=b; c=d``）。
        uin: 解析出的 QQ 号。

    Returns:
        保存后的配置字典。
    """
    ensure_dirs()
    config = {
        "cookie": str(cookie or "").strip(),
        "uin": str(uin or "").strip(),
        "updated_at": _now_display(),
    }
    with _LOCK:
        _write_json(CONFIG_PATH, config)
        # 尽量收紧权限（Windows 上 chmod 基本无效果，失败可忽略）。
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    return config


def clear_config() -> dict[str, Any]:
    """清除 Cookie 配置（保留文件，内容置空）。

    Returns:
        清空后的配置字典。
    """
    ensure_dirs()
    config = {"cookie": "", "uin": "", "updated_at": _now_display()}
    with _LOCK:
        _write_json(CONFIG_PATH, config)
    return config


# --------------------------------------------------------------------------- #
# 快照 ID 与路径
# --------------------------------------------------------------------------- #


def is_valid_snapshot_id(snapshot_id: str) -> bool:
    """校验快照 ID 是否合法，防止目录穿越。

    Args:
        snapshot_id: 待校验的 ID。

    Returns:
        合法返回 ``True``。
    """
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return False
    if not SNAPSHOT_ID_PATTERN.match(snapshot_id):
        return False
    # 双保险：拒绝任何路径分隔符与上跳。
    if "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
        return False
    return True


def snapshot_dir(snapshot_id: str) -> Path:
    """返回快照目录路径，并做路径穿越校验。

    Args:
        snapshot_id: 快照 ID。

    Returns:
        快照目录的绝对路径。

    Raises:
        ValueError: ID 非法或解析出的路径逃出快照根目录。
    """
    if not is_valid_snapshot_id(snapshot_id):
        raise ValueError(f"非法的快照 ID：{snapshot_id}")
    target = (SNAPSHOTS_DIR / snapshot_id).resolve()
    root = SNAPSHOTS_DIR.resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"非法的快照路径：{snapshot_id}")
    return target


def images_dir(snapshot_id: str) -> Path:
    """返回快照的图片目录（不存在时创建）。"""
    directory = snapshot_dir(snapshot_id) / "images"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def directory_size(path: Path) -> int:
    """递归统计目录下所有文件的总字节数。

    Args:
        path: 目录路径。

    Returns:
        总字节数；路径不存在返回 0。
    """
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------- #
# 快照读写
# --------------------------------------------------------------------------- #


def _unique_snapshot_id(gc: str) -> str:
    """生成一个当前时刻唯一可用的快照 ID。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{gc}_{stamp}"
    candidate = base
    suffix = 0
    while (SNAPSHOTS_DIR / candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
        if suffix > 999:
            candidate = f"{base}-{int(datetime.now().timestamp())}"
            break
    return candidate


def _build_index_entry(meta: dict[str, Any]) -> dict[str, Any]:
    """由 meta 生成索引条目。"""
    return {
        "id": meta.get("id", ""),
        "gc": meta.get("gc", ""),
        "created_at": meta.get("created_at", ""),
        "created_at_display": meta.get("created_at_display", meta.get("created_at", "")),
        "count": int(meta.get("count", 0) or 0),
        "image_count": int(meta.get("image_count", 0) or 0),
        "downloaded_images": int(meta.get("downloaded_images", 0) or 0),
        "size_bytes": int(meta.get("size_bytes", 0) or 0),
        "has_images": bool(meta.get("has_images", False)),
    }


def create_snapshot(
    gc: str,
    messages: list[dict[str, Any]],
    extra_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """创建一个新快照并写入磁盘。

    Args:
        gc: 群号。
        messages: 归一化后的消息列表。
        extra_meta: 附加到 ``meta.json`` 的字段（如 pages、state 摘要、cookie uin）。

    Returns:
        写入磁盘的 ``meta`` 字典。
    """
    ensure_dirs()
    gc_text = str(gc).strip()

    with _LOCK:
        snapshot_id = _unique_snapshot_id(gc_text)
        directory = SNAPSHOTS_DIR / snapshot_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "images").mkdir(parents=True, exist_ok=True)

        image_urls_count = sum(len(item.get("images", []) or []) for item in messages)
        meta: dict[str, Any] = {
            "id": snapshot_id,
            "gc": gc_text,
            "created_at": _now_iso(),
            "created_at_display": _now_display(),
            "count": len(messages),
            "image_count": image_urls_count,
            "downloaded_images": 0,
            "has_images": image_urls_count > 0,
            "size_bytes": 0,
            "download_failed": [],
        }
        if extra_meta:
            meta.update(extra_meta)

        _write_json(directory / "messages.json", messages)
        meta["size_bytes"] = directory_size(directory)
        _write_json(directory / "meta.json", meta)

        _update_index(meta)

    return meta


def _update_index(meta: dict[str, Any]) -> None:
    """把快照写入索引（同 ID 覆盖），并按时间倒序重排。"""
    entries = [item for item in _read_index_raw() if item.get("id") != meta.get("id")]
    entries.append(_build_index_entry(meta))
    entries.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    _write_json(INDEX_PATH, entries)


def _read_index_raw() -> list[dict[str, Any]]:
    """读取索引原始内容（不做排序与过滤）。"""
    ensure_dirs()
    data = _read_json(INDEX_PATH, [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def list_snapshots() -> list[dict[str, Any]]:
    """列出所有快照索引条目（按创建时间倒序）。

    Returns:
        索引条目列表，每项含 ``id``、``gc``、``created_at``、``count``、``image_count``、
        ``downloaded_images``、``size_bytes``、``has_images``。
    """
    with _LOCK:
        entries = _read_index_raw()
    entries.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return entries


def load_snapshot(snapshot_id: str) -> dict[str, Any]:
    """读取快照详情。

    Args:
        snapshot_id: 快照 ID。

    Returns:
        ``{"meta": {...}, "messages": [...]}``。

    Raises:
        ValueError: ID 非法。
        FileNotFoundError: 快照不存在。
    """
    directory = snapshot_dir(snapshot_id)
    if not directory.exists():
        raise FileNotFoundError(f"快照不存在：{snapshot_id}")

    meta = _read_json(directory / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("id", snapshot_id)
    meta.setdefault("gc", snapshot_id.split("_", 1)[0])
    meta.setdefault("created_at", "")
    meta.setdefault("created_at_display", meta.get("created_at", ""))
    meta.setdefault("count", 0)
    meta.setdefault("image_count", 0)
    meta.setdefault("downloaded_images", 0)
    meta.setdefault("size_bytes", directory_size(directory))
    meta.setdefault("has_images", bool(meta.get("image_count")))

    messages = _read_json(directory / "messages.json", [])
    if not isinstance(messages, list):
        messages = []

    # 附带本地已下载图片的映射：远程 URL → /media/<sid>/<filename>
    meta["local_images"] = {
        str(url): f"/media/{snapshot_id}/{filename}"
        for url, filename in _load_image_map(directory).items()
    }

    return {"meta": meta, "messages": messages}


def _load_image_map(directory: Path) -> dict[str, str]:
    """读取图片目录下的 ``_index.json``（远程 URL → 本地文件名）。"""
    mapping = _read_json(directory / "images" / "_index.json", {})
    if not isinstance(mapping, dict):
        return {}
    return {str(key): str(value) for key, value in mapping.items()}


def save_image_map(snapshot_id: str, mapping: dict[str, str]) -> None:
    """保存图片映射表。

    Args:
        snapshot_id: 快照 ID。
        mapping: 远程 URL → 本地文件名。
    """
    directory = snapshot_dir(snapshot_id)
    with _LOCK:
        _write_json(directory / "images" / "_index.json", mapping)


def merge_image_map(snapshot_id: str, mapping: dict[str, str]) -> dict[str, str]:
    """把新的图片映射合并进已有映射表。

    Args:
        snapshot_id: 快照 ID。
        mapping: 新增映射。

    Returns:
        合并后的完整映射。
    """
    directory = snapshot_dir(snapshot_id)
    with _LOCK:
        merged = _load_image_map(directory)
        merged.update(mapping)
        _write_json(directory / "images" / "_index.json", merged)
    return merged


def update_meta(snapshot_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """更新快照的 ``meta.json`` 并同步索引。

    Args:
        snapshot_id: 快照 ID。
        updates: 需要覆盖的字段。

    Returns:
        更新后的 meta。
    """
    directory = snapshot_dir(snapshot_id)
    if not directory.exists():
        raise FileNotFoundError(f"快照不存在：{snapshot_id}")

    with _LOCK:
        meta = _read_json(directory / "meta.json", {})
        if not isinstance(meta, dict):
            meta = {}
        meta.update(updates)
        meta["id"] = snapshot_id
        meta["size_bytes"] = directory_size(directory)
        _write_json(directory / "meta.json", meta)
        _update_index(meta)
    return meta


def delete_snapshot(snapshot_id: str) -> bool:
    """删除快照目录（含图片）并从索引移除。

    Args:
        snapshot_id: 快照 ID。

    Returns:
        是否真的删除了目录。
    """
    directory = snapshot_dir(snapshot_id)
    removed = False
    with _LOCK:
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            removed = True
        entries = [item for item in _read_index_raw() if item.get("id") != snapshot_id]
        _write_json(INDEX_PATH, entries)
    return removed


def count_snapshots() -> int:
    """返回快照总数。"""
    return len(list_snapshots())
