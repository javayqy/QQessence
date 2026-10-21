"""图片并发下载器。

特性
----
- ``ThreadPoolExecutor(max_workers=6)`` 并发下载；
- 按 URL 去重，已在本地存在的文件直接跳过（断点续传式的「续跑」语义）；
- 文件名以 ``sha1(url)`` 前 16 位命名，扩展名由 ``Content-Type`` 推断，兜底 ``.jpg``；
- 单个失败不影响整体，失败项收集进报告，由上层写入 ``meta.json.download_failed``。
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from .qq_client import DEFAULT_TIMEOUT, DEFAULT_UA, REFERER_ESSENCE

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DEFAULT_CONCURRENCY: int = 6

#: Content-Type → 扩展名映射。
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/jfif": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/svg+xml": ".svg",
}

#: 允许的扩展名白名单。
_ALLOWED_EXT: frozenset[str] = frozenset(_CONTENT_TYPE_EXT.values())

#: Windows 文件名非法字符。
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# --------------------------------------------------------------------------- #
# 报告结构
# --------------------------------------------------------------------------- #


@dataclass
class DownloadReport:
    """一次下载任务的统计结果。"""

    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    mapping: dict[str, str] = field(default_factory=dict)
    failed_items: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换成 API 返回结构。"""
        return {
            "total": self.total,
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "mapping": dict(self.mapping),
            "failed_items": list(self.failed_items),
        }


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def sanitize_filename(name: str) -> str:
    """清洗文件名中的非法字符。

    Args:
        name: 原始文件名。

    Returns:
        可安全落盘的文件名。
    """
    cleaned = _ILLEGAL_CHARS.sub("_", str(name or ""))
    cleaned = cleaned.strip().strip(".")
    return cleaned or "image"


def url_extension(url: str) -> str:
    """从 URL 路径中猜测扩展名。

    Args:
        url: 图片链接。

    Returns:
        形如 ``.jpg`` 的扩展名；无法判断时返回空字符串。
    """
    if not isinstance(url, str) or not url:
        return ""
    path = url.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _ALLOWED_EXT else ""


def content_type_extension(content_type: str) -> str:
    """由 ``Content-Type`` 推断扩展名。

    Args:
        content_type: 响应头中的 Content-Type。

    Returns:
        形如 ``.png`` 的扩展名；无法判断时返回空字符串。
    """
    if not content_type:
        return ""
    base = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(base, "")


def url_to_stem(url: str) -> str:
    """把 URL 映射为稳定的文件名主干（sha1 前 16 位）。

    Args:
        url: 图片链接。

    Returns:
        文件名主干。
    """
    digest = hashlib.sha1(str(url).encode("utf-8", errors="ignore")).hexdigest()
    return sanitize_filename(digest[:16])


def find_existing(images_dir: Path, stem: str) -> Optional[str]:
    """查找该主干下是否已存在已下载文件。

    Args:
        images_dir: 图片目录。
        stem: 文件名主干。

    Returns:
        已存在的文件名；不存在返回 ``None``。
    """
    if not images_dir.exists():
        return None
    for candidate in images_dir.glob(f"{stem}.*"):
        if candidate.is_file() and candidate.suffix.lower() in _ALLOWED_EXT:
            return candidate.name
    return None


# --------------------------------------------------------------------------- #
# 下载实现
# --------------------------------------------------------------------------- #


def _download_one(
    url: str,
    images_dir: Path,
    timeout: float,
    user_agent: str,
) -> dict[str, Any]:
    """下载单张图片。

    Args:
        url: 图片链接。
        images_dir: 落盘目录。
        timeout: 超时秒数。
        user_agent: 请求 UA。

    Returns:
        ``{"url","status":"success|skipped|failed","filename","error"}``。
    """
    stem = url_to_stem(url)
    existing = find_existing(images_dir, stem)
    if existing:
        return {"url": url, "status": "skipped", "filename": existing, "error": ""}

    headers = {
        "User-Agent": user_agent or DEFAULT_UA,
        "Referer": REFERER_ESSENCE,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        return {"url": url, "status": "failed", "filename": "", "error": exc.__class__.__name__}

    if response.status_code != 200:
        response.close()
        return {
            "url": url,
            "status": "failed",
            "filename": "",
            "error": f"HTTP {response.status_code}",
        }

    extension = content_type_extension(response.headers.get("Content-Type", "")) or url_extension(url) or ".jpg"
    filename = f"{stem}{extension}"
    target = images_dir / filename
    temp_target = images_dir / f"{stem}.part"

    written = 0
    try:
        with temp_target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
    except (OSError, requests.RequestException) as exc:
        temp_target.unlink(missing_ok=True)
        return {"url": url, "status": "failed", "filename": "", "error": exc.__class__.__name__}
    finally:
        response.close()

    if written == 0:
        temp_target.unlink(missing_ok=True)
        return {"url": url, "status": "failed", "filename": "", "error": "空响应体"}

    try:
        temp_target.replace(target)
    except OSError as exc:
        temp_target.unlink(missing_ok=True)
        return {"url": url, "status": "failed", "filename": "", "error": exc.__class__.__name__}

    return {"url": url, "status": "success", "filename": filename, "error": ""}


def download_images(
    urls: Iterable[str],
    images_dir_path: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_UA,
) -> DownloadReport:
    """并发下载一组图片。

    Args:
        urls: 图片 URL 可迭代对象（内部会去重并过滤空值）。
        images_dir_path: 落盘目录（不存在会自动创建）。
        concurrency: 并发线程数。
        timeout: 单张图片请求超时。
        user_agent: 请求 UA。

    Returns:
        :class:`DownloadReport` 统计对象。
    """
    images_dir_path.mkdir(parents=True, exist_ok=True)

    unique_urls: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not isinstance(url, str):
            continue
        cleaned = url.strip()
        if not cleaned or not cleaned.lower().startswith("http"):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        unique_urls.append(cleaned)

    report = DownloadReport(total=len(unique_urls))
    if not unique_urls:
        return report

    workers = max(1, min(16, int(concurrency or DEFAULT_CONCURRENCY)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_download_one, url, images_dir_path, timeout, user_agent): url
            for url in unique_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # noqa: BLE001 - 任何线程异常都只记为失败项
                report.failed += 1
                report.failed_items.append({"url": url, "error": exc.__class__.__name__})
                continue

            status = outcome.get("status")
            filename = outcome.get("filename", "")
            if status == "success":
                report.success += 1
                report.mapping[url] = filename
            elif status == "skipped":
                report.skipped += 1
                report.mapping[url] = filename
            else:
                report.failed += 1
                report.failed_items.append({"url": url, "error": outcome.get("error", "unknown")})

    return report


def collect_image_urls(messages: Iterable[dict[str, Any]]) -> list[str]:
    """从消息列表中收集所有图片 URL（去重、保持顺序）。

    Args:
        messages: 归一化消息列表。

    Returns:
        图片 URL 列表。
    """
    collected: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for url in message.get("images", []) or []:
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                collected.append(url)
    return collected
