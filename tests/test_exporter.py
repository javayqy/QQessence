"""导出层测试：JSON / Markdown / HTML 内容正确性与 XSS 安全。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from core import exporter


def _meta(**extra: Any) -> dict[str, Any]:
    """构造一份快照 meta。"""
    meta: dict[str, Any] = {
        "id": "123456789_20240101-120000",
        "gc": "123456789",
        "created_at": "2024-01-01T12:00:00",
        "created_at_display": "2024-01-01 12:00:00",
        "count": 2,
        "image_count": 1,
    }
    meta.update(extra)
    return meta


def _messages() -> list[dict[str, Any]]:
    """构造覆盖文本/图片/文件/链接的消息。"""
    return [
        {
            "id": "1",
            "sender_uin": "10001",
            "sender_name": "张三",
            "avatar": "",
            "time_raw": "2024-01-01 12:00:00",
            "time_iso": "2024-01-01T12:00:00",
            "text": "第一行\n第二行",
            "images": [],
            "links": [],
            "files": [],
            "type": "text",
        },
        {
            "id": "2",
            "sender_uin": "10002",
            "sender_name": "李四",
            "avatar": "https://q1.qlogo.cn/x.png",
            "time_raw": "",
            "time_iso": "",
            "text": "",
            "images": ["https://gchat.qpic.cn/a.jpg"],
            "links": [{"url": "https://example.com/a?b=1", "title": "示例标题"}],
            "files": [{"name": "报表.xlsx", "size": "2.0 KB", "url": "https://qfile.qq.com/x"}],
            "type": "image",
        },
    ]


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


class TestExportJson:
    """JSON 导出。"""

    def test_structure_and_roundtrip(self) -> None:
        payload = exporter.export_json(_messages(), _meta())
        loaded = json.loads(payload.decode("utf-8"))
        assert set(loaded) == {"meta", "messages"}
        assert loaded["messages"] == _messages()
        assert loaded["meta"]["gc"] == "123456789"

    def test_local_images_excluded(self) -> None:
        meta = _meta(local_images={"https://a/b.jpg": "/media/x/y.jpg"})
        loaded = json.loads(exporter.export_json(_messages(), meta).decode("utf-8"))
        assert "local_images" not in loaded["meta"]

    def test_download_failed_preserved(self) -> None:
        meta = _meta(download_failed=[{"url": "https://a/b.jpg", "error": "HTTP 404"}])
        loaded = json.loads(exporter.export_json(_messages(), meta).decode("utf-8"))
        assert loaded["meta"]["download_failed"] == [{"url": "https://a/b.jpg", "error": "HTTP 404"}]

    def test_chinese_not_escaped(self) -> None:
        payload = exporter.export_json(_messages(), _meta())
        assert "张三".encode("utf-8") in payload


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


class TestExportMarkdown:
    """Markdown 导出。"""

    def test_header_and_stats(self) -> None:
        text = exporter.export_markdown(_messages(), _meta())
        assert text.startswith("# 群 123456789 精华消息")
        assert "- 消息条数：2" in text
        assert "- 图片数量：1" in text
        assert "- 抓取时间：2024-01-01 12:00:00" in text

    def test_message_sections(self) -> None:
        text = exporter.export_markdown(_messages(), _meta())
        assert "## 1. 张三 · 2024-01-01 12:00:00" in text
        assert "## 2. 李四 · 时间未知" in text
        assert "第一行" in text
        assert "第二行" in text

    def test_images_files_links(self) -> None:
        text = exporter.export_markdown(_messages(), _meta())
        assert "![](https://gchat.qpic.cn/a.jpg)" in text
        assert "📎 **报表.xlsx**（2.0 KB） — https://qfile.qq.com/x" in text
        assert "- [示例标题](https://example.com/a?b=1)" in text

    def test_local_image_mapping_used(self) -> None:
        mapping = {"https://gchat.qpic.cn/a.jpg": "deadbeef.jpg"}
        text = exporter.export_markdown(_messages(), _meta(), image_map=mapping)
        assert "![](images/deadbeef.jpg)" in text

    def test_empty_snapshot_placeholder(self) -> None:
        text = exporter.export_markdown([], _meta(count=0))
        assert "_该快照没有消息。_" in text

    def test_missing_fields_do_not_raise(self) -> None:
        text = exporter.export_markdown([{}], {})
        assert "未知成员" in text
        assert "时间未知" in text


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


class TestExportHtml:
    """HTML 导出与转义。"""

    def test_document_shape(self) -> None:
        html = exporter.export_html(_messages(), _meta())
        assert html.startswith("<!DOCTYPE html>")
        assert '<html lang="zh-CN">' in html
        assert html.count('<article class="card">') == 2
        assert "群 123456789 · 精华消息" in html

    def test_no_script_tag_ever_emitted(self) -> None:
        assert "<script" not in exporter.export_html(_messages(), _meta()).lower()

    def test_text_is_escaped(self) -> None:
        message = {
            "id": "1",
            "sender_name": "张三",
            "text": "<script>alert(1)</script> & <b>x</b>",
            "images": [],
            "links": [],
            "files": [],
        }
        html = exporter.export_html([message], _meta())
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&amp;" in html
        assert "<b>x</b>" not in html

    def test_sender_name_is_escaped(self) -> None:
        message = {"id": "1", "sender_name": "<img src=x onerror=alert(1)>", "text": "x"}
        html = exporter.export_html([message], _meta())
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html

    def test_link_title_and_file_name_are_escaped(self) -> None:
        message = {
            "id": "1",
            "sender_name": "李四",
            "text": "",
            "images": [],
            "links": [{"url": "https://example.com/a", "title": "<script>bad()</script>"}],
            "files": [{"name": "报表<script>.xlsx", "size": "1 KB", "url": "https://qfile.qq.com/x"}],
        }
        html = exporter.export_html([message], _meta())
        assert "<script>bad()" not in html
        assert "<script>.xlsx" not in html
        assert "&lt;script&gt;bad()&lt;/script&gt;" in html

    def test_quote_in_attribute_is_escaped(self) -> None:
        message = {
            "id": "1",
            "sender_name": "张三",
            "text": "",
            "images": [],
            "links": [{"url": 'https://example.com/a?q="onmouseover=alert(1)', "title": "t"}],
            "files": [],
        }
        html = exporter.export_html([message], _meta())
        assert '"onmouseover=' not in html.split("</style>", 1)[1]

    def test_empty_snapshot_placeholder(self) -> None:
        html = exporter.export_html([], _meta(count=0))
        assert "该快照没有消息。" in html

    def test_local_image_inlined_as_data_uri(self, tmp_path: Path) -> None:
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        raw = b"\x89PNG\r\n\x1a\nfake"
        (images_dir / "abc.png").write_bytes(raw)

        message = {
            "id": "1",
            "sender_name": "张三",
            "text": "",
            "images": ["https://gchat.qpic.cn/a.jpg"],
            "links": [],
            "files": [],
        }
        html = exporter.export_html(
            [message],
            _meta(),
            images_dir=images_dir,
            image_map={"https://gchat.qpic.cn/a.jpg": "abc.png"},
        )
        encoded = base64.b64encode(raw).decode("ascii")
        assert f"data:image/png;base64,{encoded}" in html
        assert "已内联 1 张" in html

    def test_oversized_image_falls_back_to_remote_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "big.jpg").write_bytes(b"0" * 64)
        monkeypatch.setattr(exporter, "MAX_INLINE_IMAGE_BYTES", 8)

        message = {
            "id": "1",
            "sender_name": "张三",
            "text": "",
            "images": ["https://gchat.qpic.cn/a.jpg"],
            "links": [],
            "files": [],
        }
        html = exporter.export_html(
            [message],
            _meta(),
            images_dir=images_dir,
            image_map={"https://gchat.qpic.cn/a.jpg": "big.jpg"},
        )
        assert "data:image/jpeg;base64" not in html
        assert "https://gchat.qpic.cn/a.jpg" in html


# --------------------------------------------------------------------------- #
# 下载头与文件名
# --------------------------------------------------------------------------- #


class TestContentDisposition:
    """RFC 5987 中文文件名。"""

    def test_attachment_with_utf8_filename(self) -> None:
        header = exporter.content_disposition("QQ精华_123456789_20240101.html")
        assert header.startswith("attachment;")
        assert "filename*=UTF-8''" in header
        assert quote("QQ精华_123456789_20240101.html", safe="") in header

    def test_ascii_fallback_present(self) -> None:
        header = exporter.content_disposition("报告.html")
        assert 'filename="' in header
        assert header.count("filename") == 2

    def test_inline_flag(self) -> None:
        assert exporter.content_disposition("a.html", inline=True).startswith("inline;")

    def test_ascii_name_stays_readable(self) -> None:
        header = exporter.content_disposition("report.html")
        assert 'filename="report.html"' in header


class TestDefaultFilename:
    """默认文件名生成。"""

    def test_contains_gc_and_extension(self) -> None:
        name = exporter.default_filename(_meta(), "html")
        assert name.startswith("QQ精华_123456789_")
        assert name.endswith(".html")

    @pytest.mark.parametrize("extension", ["html", ".md", "json"])
    def test_extension_normalized(self, extension: str) -> None:
        name = exporter.default_filename(_meta(), extension)
        assert name.endswith("." + extension.lstrip("."))

    def test_without_timestamp_uses_now(self) -> None:
        name = exporter.default_filename({"gc": "123456789"}, "md")
        assert name.startswith("QQ精华_123456789_")
        assert name.endswith(".md")

    def test_missing_gc_falls_back(self) -> None:
        assert "unknown" in exporter.default_filename({}, "json")
