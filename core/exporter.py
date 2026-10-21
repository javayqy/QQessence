"""导出层：把快照导出为自包含 HTML、Markdown 或 JSON。

三种格式的定位
--------------
- ``json``：机器可读，原样输出归一化消息，便于二次处理；
- ``markdown``：纯文本归档，便于贴进笔记或 Git 仓库；
- ``html``：**单文件自包含**，图片转 base64 内联，可直接双击打开或转发分享。

安全约定
--------
所有用户可控内容（昵称、正文、链接标题、文件名）都经过 :func:`html.escape` 转义，
不做任何未转义的字符串拼接，避免导出文件成为 XSS 载体。
"""

from __future__ import annotations

import base64
import html as html_lib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

#: 允许内联的最大单图体积（6 MB），超过则保留远程链接，避免导出文件过大。
MAX_INLINE_IMAGE_BYTES: int = 6 * 1024 * 1024

_IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".svg": "image/svg+xml",
}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #


def content_disposition(filename: str, inline: bool = False) -> str:
    """构造兼容中文文件名的 ``Content-Disposition`` 头。

    使用 RFC 5987 的 ``filename*=UTF-8''...`` 形式，并保留一个 ASCII 兜底名给老客户端。

    Args:
        filename: 期望的下载文件名（可含中文）。
        inline: 是否使用 ``inline`` 而非 ``attachment``。

    Returns:
        可直接放入响应头的字符串。
    """
    disposition = "inline" if inline else "attachment"
    ascii_fallback = filename.encode("ascii", errors="replace").decode("ascii").replace("?", "_")
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def default_filename(meta: dict[str, Any], extension: str) -> str:
    """根据快照元信息生成默认导出文件名。

    Args:
        meta: 快照 meta 字典。
        extension: 扩展名（不含点或含点均可）。

    Returns:
        形如 ``QQ精华_123456789_20261021-131500.html`` 的文件名。
    """
    gc = str(meta.get("gc", "unknown") or "unknown")
    created = str(meta.get("created_at_display") or meta.get("created_at") or "")
    stamp = created.replace(":", "").replace(" ", "_").replace("T", "_") or datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = extension if extension.startswith(".") else f".{extension}"
    return f"QQ精华_{gc}_{stamp}{suffix}"


def _image_data_uri(path: Path) -> str:
    """把本地图片文件编码为 base64 data URI。

    Args:
        path: 图片文件路径。

    Returns:
        ``data:image/...;base64,...`` 字符串；失败或过大时返回空字符串。
    """
    try:
        if not path.is_file():
            return ""
        if path.stat().st_size > MAX_INLINE_IMAGE_BYTES:
            return ""
        payload = path.read_bytes()
    except OSError:
        return ""
    mime = _IMAGE_MIME.get(path.suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _resolve_image(url: str, image_map: dict[str, str], images_dir: Optional[Path]) -> str:
    """把远程 URL 解析为可用的 ``src``（优先本地内联）。

    Args:
        url: 远程图片 URL。
        image_map: 远程 URL → 本地文件名。
        images_dir: 图片目录。

    Returns:
        可直接放进 ``<img src>`` 的值。
    """
    filename = image_map.get(url, "")
    if filename and images_dir is not None:
        data_uri = _image_data_uri(images_dir / filename)
        if data_uri:
            return data_uri
    return url


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def export_json(messages: Iterable[dict[str, Any]], meta: dict[str, Any]) -> bytes:
    """导出为 JSON 字节流。

    Args:
        messages: 归一化消息列表。
        meta: 快照 meta。

    Returns:
        UTF-8 编码的 JSON 字节。顶层结构为 ``{"meta": ..., "messages": ...}``。
    """
    message_list = list(messages)
    payload = {
        "meta": {
            key: value
            for key, value in (meta or {}).items()
            if key not in {"local_images", "download_failed"}
        },
        "messages": message_list,
    }
    if meta:
        failed = meta.get("download_failed")
        if failed:
            payload["meta"]["download_failed"] = failed
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text.encode("utf-8")


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def export_markdown(
    messages: Iterable[dict[str, Any]],
    meta: dict[str, Any],
    image_map: Optional[dict[str, str]] = None,
) -> str:
    """导出为 Markdown 文本。

    Args:
        messages: 归一化消息列表。
        meta: 快照 meta。
        image_map: 远程 URL → 本地相对路径（可选，用于生成本地图片引用）。

    Returns:
        Markdown 文本。
    """
    gc = str(meta.get("gc", "") or "")
    created = str(meta.get("created_at_display") or meta.get("created_at") or "")
    mapping = image_map or {}
    message_list: list[dict[str, Any]] = list(messages)

    lines: list[str] = [f"# 群 {gc} 精华消息", ""]
    lines.append(f"- 抓取时间：{created or '未知'}")
    lines.append(f"- 消息条数：{len(message_list)}")
    if meta.get("image_count"):
        lines.append(f"- 图片数量：{meta.get('image_count')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for index, message in enumerate(message_list, start=1):
        name = str(message.get("sender_name", "") or "未知成员")
        time_text = str(message.get("time_raw", "") or "时间未知")
        lines.append(f"## {index}. {name} · {time_text}")
        lines.append("")

        text = str(message.get("text", "") or "").strip()
        if text:
            for paragraph in text.split("\n"):
                lines.append(paragraph.strip())
            lines.append("")

        for url in message.get("images", []) or []:
            local = mapping.get(url)
            target = f"images/{local}" if local else url
            lines.append(f"![]({target})")
        if message.get("images"):
            lines.append("")

        for file_item in message.get("files", []) or []:
            name_text = str(file_item.get("name", "") or "文件")
            size_text = str(file_item.get("size", "") or "")
            url_text = str(file_item.get("url", "") or "")
            descriptor = f"📎 **{name_text}**"
            if size_text:
                descriptor += f"（{size_text}）"
            if url_text:
                descriptor += f" — {url_text}"
            lines.append(f"- {descriptor}")
        if message.get("files"):
            lines.append("")

        for link in message.get("links", []) or []:
            url = str(link.get("url", "") or "")
            title = str(link.get("title", "") or url)
            lines.append(f"- [{title}]({url})")
        if message.get("links"):
            lines.append("")

        lines.append("---")
        lines.append("")

    if len(message_list) == 0:
        lines.append("_该快照没有消息。_")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def export_html(
    messages: Iterable[dict[str, Any]],
    meta: dict[str, Any],
    images_dir: Optional[Path] = None,
    image_map: Optional[dict[str, str]] = None,
) -> str:
    """导出为单文件自包含 HTML。

    Args:
        messages: 归一化消息列表。
        meta: 快照 meta。
        images_dir: 本地图片目录（用于 base64 内联）。
        image_map: 远程 URL → 本地文件名。

    Returns:
        完整的 HTML 文档字符串。
    """
    escape = html_lib.escape
    gc = escape(str(meta.get("gc", "") or ""))
    created = escape(str(meta.get("created_at_display") or meta.get("created_at") or "未知"))
    message_list = list(messages)
    mapping = image_map or {}

    cards: list[str] = []
    for message in message_list:
        cards.append(_render_html_card(message, mapping, images_dir, escape))

    if not cards:
        cards.append('<div class="empty">该快照没有消息。</div>')

    inline_count = sum(
        1 for url, filename in mapping.items() if images_dir and _image_data_uri(images_dir / filename)
    )

    return _HTML_TEMPLATE.format(
        gc=gc,
        created=created,
        count=len(message_list),
        image_count=int(meta.get("image_count", 0) or 0),
        inline_count=inline_count,
        cards="\n".join(cards),
    )


def _render_html_card(
    message: dict[str, Any],
    image_map: dict[str, str],
    images_dir: Optional[Path],
    escape: Any,
) -> str:
    """渲染单张消息卡片。

    Args:
        message: 归一化消息。
        image_map: 远程 URL → 本地文件名。
        images_dir: 图片目录。
        escape: ``html.escape`` 函数。

    Returns:
        卡片 HTML 片段。
    """
    name = escape(str(message.get("sender_name", "") or "未知成员"))
    initials = escape(str(message.get("sender_name", "") or "?")[:1])
    time_raw = escape(str(message.get("time_raw", "") or ""))
    sender_uin = escape(str(message.get("sender_uin", "") or ""))
    avatar = str(message.get("avatar", "") or "")

    avatar_html = (
        f'<img class="avatar" src="{escape(avatar)}" alt="" loading="lazy" '
        f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';">'
        f'<span class="avatar avatar-fallback" style="display:none">{initials}</span>'
        if avatar
        else f'<span class="avatar avatar-fallback">{initials}</span>'
    )

    parts: list[str] = [
        '<article class="card">',
        '<div class="card-head">',
        avatar_html,
        '<div class="card-meta">',
        f'<span class="name">{name}</span>',
        f'<span class="time">{time_raw}</span>',
        "</div>",
        "</div>",
    ]
    if sender_uin:
        parts.append(f'<div class="uin">QQ {sender_uin}</div>')

    text = str(message.get("text", "") or "").strip()
    if text:
        paragraphs = "".join(
            f"<p>{escape(line)}</p>" for line in text.split("\n") if line.strip()
        )
        parts.append(f'<div class="text">{paragraphs}</div>')

    image_urls = [url for url in (message.get("images") or []) if isinstance(url, str)]
    if image_urls:
        grid_class = "grid-1" if len(image_urls) == 1 else ("grid-2" if len(image_urls) == 2 else "grid-many")
        parts.append(f'<div class="images {grid_class}">')
        for url in image_urls:
            resolved = _resolve_image(url, image_map, images_dir)
            parts.append(
                f'<a class="image" href="{escape(resolved)}" target="_blank" rel="noopener">'
                f'<img src="{escape(resolved)}" alt="图片" loading="lazy" '
                f'onerror="this.parentElement.classList.add(\'broken\');this.style.display=\'none\';"></a>'
            )
        parts.append("</div>")

    for file_item in message.get("files", []) or []:
        file_name = escape(str(file_item.get("name", "") or "文件"))
        file_size = escape(str(file_item.get("size", "") or ""))
        file_url = str(file_item.get("url", "") or "")
        size_html = f'<span class="file-size">{file_size}</span>' if file_size else ""
        if file_url:
            parts.append(
                f'<a class="file" href="{escape(file_url)}" target="_blank" rel="noopener">'
                f'<span class="file-icon">📎</span>'
                f'<span class="file-name">{file_name}</span>{size_html}</a>'
            )
        else:
            parts.append(
                f'<div class="file"><span class="file-icon">📎</span>'
                f'<span class="file-name">{file_name}</span>{size_html}</div>'
            )

    for link in message.get("links", []) or []:
        url = str(link.get("url", "") or "")
        title = escape(str(link.get("title", "") or url))
        parts.append(
            f'<a class="link" href="{escape(url)}" target="_blank" rel="noopener">'
            f'<span class="link-icon">🔗</span>'
            f'<span class="link-body"><span class="link-title">{title}</span>'
            f'<span class="link-url">{escape(url)}</span></span></a>'
        )

    parts.append("</article>")
    return "\n".join(parts)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>群 {gc} 精华消息 · {created}</title>
<style>
  :root {{
    --bg: #f5f6f8;
    --surface: #ffffff;
    --text: #1b1d21;
    --text-soft: #6b7280;
    --border: #e6e8ec;
    --accent: #12b7f5;
    --accent-soft: rgba(18, 183, 245, 0.12);
    --radius: 16px;
    --shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 8px 24px rgba(16, 24, 40, 0.06);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 16px 64px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    line-height: 1.65;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 860px; margin: 0 auto; }}
  header.page {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 24px 28px;
    margin-bottom: 20px;
  }}
  header.page h1 {{ margin: 0 0 10px; font-size: 22px; letter-spacing: -0.01em; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .badge {{
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: #0a7ea6;
    font-weight: 500;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 18px 20px;
    margin-bottom: 14px;
  }}
  .card-head {{ display: flex; align-items: center; gap: 12px; }}
  .avatar {{
    width: 40px; height: 40px; border-radius: 50%;
    object-fit: cover; flex: 0 0 auto; background: var(--accent-soft);
  }}
  .avatar-fallback {{
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; color: #0a7ea6; font-size: 16px;
  }}
  .card-meta {{ display: flex; flex-direction: column; min-width: 0; }}
  .name {{ font-weight: 600; font-size: 15px; }}
  .time {{ font-size: 12px; color: var(--text-soft); }}
  .uin {{ font-size: 11px; color: var(--text-soft); margin: 6px 0 0 52px; }}
  .text {{ margin: 12px 0 0 52px; word-break: break-word; }}
  .text p {{ margin: 0 0 6px; white-space: pre-wrap; }}
  .text p:last-child {{ margin-bottom: 0; }}
  .images {{ margin: 12px 0 0 52px; display: grid; gap: 8px; }}
  .grid-1 {{ grid-template-columns: minmax(0, 1fr); }}
  .grid-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .grid-many {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .image {{ display: block; border-radius: 12px; overflow: hidden; background: #f0f2f5; }}
  .image img {{ display: block; width: 100%; height: auto; }}
  .image.broken::after {{
    content: "图片已失效";
    display: flex; align-items: center; justify-content: center;
    min-height: 90px; font-size: 12px; color: var(--text-soft);
  }}
  .file, .link {{
    display: flex; align-items: center; gap: 10px;
    margin: 10px 0 0 52px; padding: 10px 14px;
    border: 1px solid var(--border); border-radius: 12px;
    background: #fafbfc; text-decoration: none; color: inherit;
  }}
  .file-name {{ font-weight: 500; font-size: 14px; word-break: break-all; }}
  .file-size {{ font-size: 12px; color: var(--text-soft); margin-left: auto; }}
  .link-body {{ display: flex; flex-direction: column; min-width: 0; }}
  .link-title {{ font-size: 14px; font-weight: 500; }}
  .link-url {{ font-size: 12px; color: var(--text-soft); word-break: break-all; }}
  .empty {{
    background: var(--surface); border: 1px dashed var(--border);
    border-radius: var(--radius); padding: 48px; text-align: center;
    color: var(--text-soft);
  }}
  footer.page {{ margin-top: 28px; text-align: center; font-size: 12px; color: var(--text-soft); }}
  @media (max-width: 600px) {{
    body {{ padding: 16px 10px 40px; }}
    .text, .uin, .images, .file, .link {{ margin-left: 0; }}
    .grid-many {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="page">
    <h1>群 {gc} · 精华消息</h1>
    <div class="stats">
      <span class="badge">抓取时间 {created}</span>
      <span class="badge">共 {count} 条</span>
      <span class="badge">图片链接 {image_count} 张</span>
      <span class="badge">已内联 {inline_count} 张</span>
    </div>
  </header>
  <main>
{cards}
  </main>
  <footer class="page">由 QQessence 本地存档导出 · 单文件自包含，可直接分享</footer>
</div>
</body>
</html>
"""
