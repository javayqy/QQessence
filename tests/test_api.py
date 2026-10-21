"""Flask 接口冒烟 + 端到端离线链路测试。

全部使用 ``test_client()``，不占用真实端口；网络层一律 mock，绝不访问线上接口。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from core import storage

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _body(response: Any) -> dict[str, Any]:
    """取出 JSON 响应体。"""
    return json.loads(response.get_data(as_text=True))


def _install_fake_get(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[str],
    image: bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 32,
) -> list[str]:
    """把 ``requests.get`` 替换成「第 N 次调用返回第 N 页」的假实现。

    Args:
        monkeypatch: pytest monkeypatch。
        pages: 依次返回的页面 HTML；超出范围时重复最后一页。
        image: 命中图片链接时返回的二进制内容。

    Returns:
        被记录下来的请求 URL 列表。
    """
    import requests

    visited: list[str] = []

    def _fake_get(url: str, **kwargs: Any) -> Any:
        visited.append(url)
        if "essence/indexPc" in url:
            index = min(len(visited) - 1, len(pages) - 1)
            return _FakeHttp(text=pages[index])
        return _FakeHttp(content=image, content_type="image/png")

    monkeypatch.setattr(requests, "get", _fake_get)
    return visited


class _FakeHttp:
    """最小可用的伪 HTTP 响应。"""

    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        payload: Any = None,
        content: bytes = b"",
        content_type: str = "image/png",
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type}
        self.closed = False
        self._payload = payload
        self._content = content

    def json(self) -> Any:
        """返回预设 JSON。"""
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload

    def iter_content(self, chunk_size: int = 1024) -> Any:
        """按块迭代内容。"""
        data = self._content
        return (data[i : i + chunk_size] for i in range(0, len(data), chunk_size))

    def close(self) -> None:
        """关闭响应。"""
        self.closed = True


@pytest.fixture
def configured_client(client: Any, valid_cookie: str) -> Any:
    """已完成 Cookie 配置的测试客户端。"""
    response = client.post("/api/config", json={"cookie": valid_cookie})
    assert response.status_code == 200
    return client


# --------------------------------------------------------------------------- #
# 首页与静态资源
# --------------------------------------------------------------------------- #


class TestIndexAndStatic:
    """首页与静态资源。"""

    def test_index_returns_html(self, client: Any) -> None:
        response = client.get("/")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<title>QQ 群精华 · 本地存档</title>" in html
        assert "/static/app.js" in html
        assert "/static/styles.css" in html

    def test_static_assets_served(self, client: Any) -> None:
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200

    def test_missing_static_returns_404(self, client: Any) -> None:
        assert client.get("/static/not-exist.js").status_code == 404

    def test_frontend_referenced_assets_exist(self, flask_app: Any) -> None:
        html = (flask_app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        referenced = re.findall(r'(?:href|src)="/static/([^"]+)"', html)
        assert referenced, "index.html 必须引用静态资源"
        for name in referenced:
            assert (flask_app.STATIC_DIR / name).is_file(), f"缺少静态资源：{name}"


# --------------------------------------------------------------------------- #
# 配置接口
# --------------------------------------------------------------------------- #


class TestConfigApi:
    """``/api/config``。"""

    def test_empty_config(self, client: Any) -> None:
        body = _body(client.get("/api/config"))
        assert body["ok"] is True
        data = body["data"]
        assert data["has_cookie"] is False
        assert data["uin"] == ""
        assert data["cookie_masked"] == ""

    def test_save_dirty_cookie(self, client: Any) -> None:
        dirty = "Cookie:  pgv_pvid=123 ;  uin=o12345678 ; skey=@AbCdEf12 ; p_skey=Zz9\n trailing=junk"
        response = client.post("/api/config", json={"cookie": dirty})
        assert response.status_code == 200
        data = _body(response)["data"]
        assert data["uin"] == "12345678"
        assert data["has_cookie"] is True
        assert data["has_skey"] is True
        assert data["has_p_skey"] is True

    def test_saved_cookie_never_leaks_full_skey(self, client: Any) -> None:
        client.post("/api/config", json={"cookie": "uin=o12345678; skey=@AbCdEf12"})
        raw = client.get("/api/config").get_data(as_text=True)
        assert "@AbCdEf12" not in raw
        assert "***" in raw
        assert "Ef12" in raw

    def test_cookie_persisted_for_next_read(self, client: Any) -> None:
        client.post("/api/config", json={"cookie": "uin=o12345678; skey=@AbCdEf12"})
        stored = storage.load_config()
        assert stored["uin"] == "12345678"
        assert "skey=@AbCdEf12" in stored["cookie"]

    def test_empty_cookie_rejected(self, client: Any) -> None:
        response = client.post("/api/config", json={"cookie": "   "})
        assert response.status_code == 400
        assert _body(response)["ok"] is False

    def test_non_string_cookie_rejected(self, client: Any) -> None:
        response = client.post("/api/config", json={"cookie": 12345})
        assert response.status_code == 400

    def test_cookie_without_skey_rejected(self, client: Any) -> None:
        response = client.post("/api/config", json={"cookie": "a=b; c=d"})
        assert response.status_code == 400
        body = _body(response)
        assert "skey" in body["error"]
        assert body["hint"]

    def test_clear_config(self, client: Any) -> None:
        client.post("/api/config", json={"cookie": "uin=o12345678; skey=@AbCdEf12"})
        response = client.post("/api/config/clear")
        assert response.status_code == 200
        assert _body(response)["data"]["has_cookie"] is False
        assert storage.load_config()["cookie"] == ""


# --------------------------------------------------------------------------- #
# 群列表接口
# --------------------------------------------------------------------------- #


class TestGroupsApi:
    """``/api/groups`` 的优雅降级。"""

    def test_requires_cookie(self, client: Any) -> None:
        response = client.get("/api/groups")
        assert response.status_code == 502
        body = _body(response)
        assert body["ok"] is False
        assert body["hint"]

    def test_ec4_returns_friendly_error_not_500(
        self, configured_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        monkeypatch.setattr(
            requests,
            "post",
            lambda *a, **k: _FakeHttp(payload={"ec": 4, "em": "no login [errcode:4:0]"}),
        )
        response = configured_client.get("/api/groups")
        assert response.status_code == 502
        body = _body(response)
        assert body["ok"] is False
        assert "ec=4" in body["error"]
        assert body["groups"] == []
        assert "手动输入群号" in body["hint"]

    def test_success_payload(self, configured_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import requests

        payload = {
            "ec": 0,
            "join": [{"gc": 123456789, "name": "测试群", "owner": 10001, "member_count": 42}],
        }
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeHttp(payload=payload))
        response = configured_client.get("/api/groups")
        assert response.status_code == 200
        data = _body(response)["data"]
        assert data["count"] == 1
        assert data["groups"][0]["gc"] == "123456789"
        assert data["groups"][0]["name"] == "测试群"

    def test_network_error_is_wrapped(
        self, configured_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise requests.ConnectionError("boom")

        monkeypatch.setattr(requests, "post", _boom)
        response = configured_client.get("/api/groups")
        assert response.status_code == 502
        assert _body(response)["ok"] is False

    def test_non_json_response_is_wrapped(
        self, configured_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeHttp(text="<html>login</html>"))
        response = configured_client.get("/api/groups")
        assert response.status_code == 502
        assert "非 JSON" in _body(response)["error"]


# --------------------------------------------------------------------------- #
# 抓取接口
# --------------------------------------------------------------------------- #


class TestFetchApi:
    """``POST /api/fetch``。"""

    def test_requires_cookie(self, client: Any) -> None:
        response = client.post("/api/fetch", json={"gc": "123456789"})
        assert response.status_code == 400
        assert "Cookie" in _body(response)["error"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"gc": "abc"},
            {"gc": "1234"},
            {"gc": "1" * 40},
            {"gc": ""},
            {},
            {"gc": None},
        ],
    )
    def test_gc_validation(self, configured_client: Any, payload: dict[str, Any]) -> None:
        response = configured_client.post("/api/fetch", json=payload)
        assert response.status_code == 400
        body = _body(response)
        assert body["ok"] is False
        assert body["error"]

    def test_invalid_max_pages_falls_back_to_default(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
    ) -> None:
        page = page_builder(state_builder([{"msgContent": "只有一页"}]))
        _install_fake_get(monkeypatch, [page])
        response = configured_client.post(
            "/api/fetch", json={"gc": "123456789", "max_pages": "many"}
        )
        assert response.status_code == 200
        assert _body(response)["data"]["snapshot"]["count"] == 1

    def test_non_object_body_rejected(self, configured_client: Any) -> None:
        response = configured_client.post("/api/fetch", json=[1, 2, 3])
        assert response.status_code == 400
        assert "JSON 对象" in _body(response)["error"]

    def test_broken_json_body_rejected(self, configured_client: Any) -> None:
        response = configured_client.post(
            "/api/fetch", data="not-json", content_type="application/json"
        )
        assert response.status_code == 400

    def test_happy_path_creates_snapshot(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
    ) -> None:
        page = page_builder(
            state_builder(
                [
                    {"msgSeq": "1", "msgContent": "你好", "senderName": "张三"},
                    {"msgSeq": "2", "msgContent": "世界", "senderName": "李四"},
                ]
            )
        )
        _install_fake_get(monkeypatch, [page])

        response = configured_client.post(
            "/api/fetch", json={"gc": "123456789", "download_images": False}
        )
        assert response.status_code == 200
        body = _body(response)
        assert body["ok"] is True
        data = body["data"]
        assert data["snapshot"]["count"] == 2
        assert len(data["messages"]) == 2
        assert [item["text"] for item in data["messages"]] == ["你好", "世界"]
        assert storage.count_snapshots() == 1

    def test_server_error_page_returns_empty_snapshot(
        self, configured_client: Any, monkeypatch: pytest.MonkeyPatch, page_builder: Any
    ) -> None:
        page = page_builder({"msgList": [], "hasServerError": True, "isEnd": True})
        _install_fake_get(monkeypatch, [page])

        response = configured_client.post("/api/fetch", json={"gc": "123456789"})
        assert response.status_code == 200
        body = _body(response)
        assert body["data"]["snapshot"]["count"] == 0
        assert body["warnings"], "无权限/无精华时必须给出提示"

    def test_client_error_returns_502(
        self, configured_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeHttp(status_code=403))
        response = configured_client.post("/api/fetch", json={"gc": "123456789"})
        assert response.status_code == 502
        body = _body(response)
        assert "403" in body["error"]
        assert body["hint"]

    @pytest.mark.parametrize("max_pages", [0, -5, -1])
    def test_small_max_pages_is_clamped_to_one(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
        max_pages: int,
    ) -> None:
        page = page_builder(state_builder([{"msgContent": "p1"}], isEnd=False, pageStart=10))
        visited = _install_fake_get(monkeypatch, [page])
        response = configured_client.post(
            "/api/fetch", json={"gc": "123456789", "max_pages": max_pages}
        )
        assert response.status_code == 200
        assert len(visited) == 1

    def test_pagination_follows_page_start(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
    ) -> None:
        first = page_builder(
            state_builder([{"msgSeq": "1", "msgContent": "第一页"}], isEnd=False, pageStart=10)
        )
        second = page_builder(
            state_builder([{"msgSeq": "2", "msgContent": "第二页"}], isEnd=True, pageStart=20)
        )
        visited = _install_fake_get(monkeypatch, [first, second])

        response = configured_client.post(
            "/api/fetch", json={"gc": "123456789", "max_pages": 5}
        )
        assert response.status_code == 200
        data = _body(response)["data"]
        assert len(visited) == 2
        assert data["snapshot"]["count"] == 2
        assert "pageStart=10" in visited[1]

    def test_pagination_continues_when_is_end_field_missing(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        page_builder: Any,
    ) -> None:
        """服务端漏发 isEnd 时，只要 pageStart 还在推进就必须继续翻页。"""
        pages = [
            page_builder({"msgList": [{"msgSeq": "1", "msgContent": "第一页"}], "pageStart": 10}),
            page_builder({"msgList": [{"msgSeq": "2", "msgContent": "第二页"}], "pageStart": 20}),
            page_builder({"msgList": [{"msgSeq": "3", "msgContent": "第三页"}]}),
        ]
        visited = _install_fake_get(monkeypatch, pages)

        data = _body(
            configured_client.post(
                "/api/fetch", json={"gc": "123456789", "max_pages": 5}
            )
        )["data"]
        assert len(visited) == 3
        assert data["snapshot"]["count"] == 3
        assert [item["text"] for item in data["messages"]] == ["第一页", "第二页", "第三页"]

    def test_avatar_is_metadata_not_content_image(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        page_builder: Any,
    ) -> None:
        """头像不得污染 image_count / 导出 HTML / 图片下载链路。"""
        page = page_builder(
            {
                "isEnd": True,
                "msgList": [
                    {
                        "msgSeq": "1",
                        "senderName": "张三",
                        "msgContent": "纯文本一",
                        "avatar": "https://q1.qlogo.cn/g?b=sdk&nk=10001&s=140",
                    },
                    {
                        "msgSeq": "2",
                        "senderName": "李四",
                        "msgContent": "纯文本二",
                        "face": "https://q1.qlogo.cn/g?b=sdk&nk=10002&s=140",
                    },
                ],
            }
        )
        _install_fake_get(monkeypatch, [page])

        data = _body(configured_client.post("/api/fetch", json={"gc": "123456789"}))["data"]
        snapshot = data["snapshot"]
        assert snapshot["count"] == 2
        assert snapshot["image_count"] == 0
        assert snapshot["has_images"] is False
        assert all(item["images"] == [] for item in data["messages"])
        assert all(item["type"] == "text" for item in data["messages"])

        html = configured_client.get(
            f"/api/snapshots/{snapshot['id']}/export?format=html"
        ).get_data(as_text=True)
        assert 'class="images' not in html, "纯文本消息不应渲染图片网格"
        assert 'class="avatar' in html, "头像仍应正常渲染"

        stats = _body(
            configured_client.post(f"/api/snapshots/{snapshot['id']}/download")
        )["data"]["stats"]
        assert stats["total"] == 0, "下载器不应把头像当成精华图片"


# --------------------------------------------------------------------------- #
# 快照接口
# --------------------------------------------------------------------------- #


class TestSnapshotApi:
    """快照列表 / 详情 / 删除。"""

    def test_list_empty(self, client: Any) -> None:
        body = _body(client.get("/api/snapshots"))
        assert body["ok"] is True
        assert body["data"] == {"snapshots": [], "count": 0}

    def test_list_after_create(self, client: Any) -> None:
        meta = storage.create_snapshot("123456789", [{"id": "1", "text": "x"}])
        body = _body(client.get("/api/snapshots"))
        assert body["data"]["count"] == 1
        assert body["data"]["snapshots"][0]["id"] == meta["id"]

    def test_detail(self, client: Any) -> None:
        meta = storage.create_snapshot("123456789", [{"id": "1", "text": "x"}])
        body = _body(client.get(f"/api/snapshots/{meta['id']}"))
        assert body["ok"] is True
        assert body["data"]["snapshot"]["id"] == meta["id"]
        assert body["data"]["messages"] == [{"id": "1", "text": "x"}]

    def test_detail_missing_returns_404(self, client: Any) -> None:
        assert client.get("/api/snapshots/999999999_20240101-000000").status_code == 404

    @pytest.mark.parametrize("bad_id", ["abc", "..", "1-2"])
    def test_detail_invalid_id_returns_400(self, client: Any, bad_id: str) -> None:
        response = client.get(f"/api/snapshots/{bad_id}")
        assert response.status_code in (400, 404)

    def test_delete_removes_snapshot(self, client: Any) -> None:
        meta = storage.create_snapshot("123456789", [])
        response = client.delete(f"/api/snapshots/{meta['id']}")
        assert response.status_code == 200
        assert _body(response)["data"]["deleted"] is True
        assert client.get(f"/api/snapshots/{meta['id']}").status_code == 404
        assert storage.count_snapshots() == 0

    def test_delete_missing_returns_404(self, client: Any) -> None:
        assert client.delete("/api/snapshots/999999999_20240101-000000").status_code == 404

    def test_delete_invalid_id_returns_400(self, client: Any) -> None:
        assert client.delete("/api/snapshots/abc").status_code == 400


# --------------------------------------------------------------------------- #
# 导出接口
# --------------------------------------------------------------------------- #


class TestExportApi:
    """``/api/snapshots/<id>/export``。"""

    @pytest.fixture
    def snapshot_id(self) -> str:
        messages = [
            {
                "id": "1",
                "sender_uin": "10001",
                "sender_name": "张三<script>alert(1)</script>",
                "avatar": "",
                "time_raw": "2024-01-01 12:00:00",
                "time_iso": "2024-01-01T12:00:00",
                "text": "<script>alert(1)</script> 正文",
                "images": [],
                "links": [],
                "files": [],
                "type": "text",
            }
        ]
        return storage.create_snapshot("123456789", messages)["id"]

    def test_export_html_is_xss_safe(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=html")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<script" not in html.lower()
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_export_html_headers(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=html")
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment;")
        assert "filename*=UTF-8''" in disposition
        assert "text/html" in response.headers["Content-Type"]

    def test_export_markdown(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=md")
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert text.startswith("# 群 123456789 精华消息")
        disposition = response.headers["Content-Disposition"]
        assert ".md\"" in disposition
        assert "filename*=UTF-8''" in disposition

    def test_export_markdown_alias(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=markdown")
        assert response.status_code == 200
        assert "text/markdown" in response.headers["Content-Type"]

    def test_export_json(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=json")
        assert response.status_code == 200
        payload = json.loads(response.get_data(as_text=True))
        assert payload["meta"]["gc"] == "123456789"
        assert payload["messages"][0]["id"] == "1"

    def test_default_format_is_html(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export")
        assert "text/html" in response.headers["Content-Type"]

    def test_unknown_format_rejected(self, client: Any, snapshot_id: str) -> None:
        response = client.get(f"/api/snapshots/{snapshot_id}/export?format=pdf")
        assert response.status_code == 400
        assert _body(response)["hint"]

    def test_export_missing_snapshot(self, client: Any) -> None:
        assert client.get("/api/snapshots/999999999_20240101-000000/export").status_code == 404

    def test_export_invalid_id(self, client: Any) -> None:
        assert client.get("/api/snapshots/abc/export").status_code == 400


# --------------------------------------------------------------------------- #
# 媒体与下载
# --------------------------------------------------------------------------- #


class TestMediaAndDownload:
    """``/media`` 与下载接口。"""

    def test_serves_existing_image(self, client: Any) -> None:
        meta = storage.create_snapshot("123456789", [])
        images = storage.images_dir(meta["id"])
        (images / "abc.png").write_bytes(b"PNGDATA")
        response = client.get(f"/media/{meta['id']}/abc.png")
        assert response.status_code == 200
        assert response.get_data() == b"PNGDATA"

    @pytest.mark.parametrize(
        "filename",
        ["..%2f..%2fconfig.json", "..%5c..%5cconfig.json", "..", "../config.json"],
    )
    def test_path_traversal_rejected(self, client: Any, filename: str) -> None:
        meta = storage.create_snapshot("123456789", [])
        storage.images_dir(meta["id"])
        response = client.get(f"/media/{meta['id']}/{filename}")
        assert response.status_code in (400, 404)
        assert response.status_code != 200

    def test_invalid_snapshot_id_rejected(self, client: Any) -> None:
        assert client.get("/media/abc/x.png").status_code == 400

    def test_missing_images_dir_returns_404(self, client: Any) -> None:
        snapshot_id = "123456789_20240101-000000"
        (storage.SNAPSHOTS_DIR / snapshot_id).mkdir(parents=True)
        assert client.get(f"/media/{snapshot_id}/x.png").status_code == 404

    def test_missing_file_returns_404(self, client: Any) -> None:
        meta = storage.create_snapshot("123456789", [])
        storage.images_dir(meta["id"])
        assert client.get(f"/media/{meta['id']}/nope.png").status_code == 404

    def test_download_endpoint_updates_meta(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
    ) -> None:
        page = page_builder(
            state_builder([{"msgSeq": "1", "picUrl": "https://gchat.qpic.cn/a.jpg"}])
        )
        _install_fake_get(monkeypatch, [page])

        fetched = _body(configured_client.post("/api/fetch", json={"gc": "123456789"}))
        snapshot_id = fetched["data"]["snapshot"]["id"]

        response = configured_client.post(f"/api/snapshots/{snapshot_id}/download")
        assert response.status_code == 200
        data = _body(response)["data"]
        assert data["stats"]["total"] == 1
        assert data["stats"]["success"] == 1
        assert data["snapshot"]["downloaded_images"] == 1

        local = storage.load_snapshot(snapshot_id)["meta"]["local_images"]
        assert local, "下载后必须建立 远程URL → 本地文件名 映射"
        media_url = next(iter(local.values()))
        assert configured_client.get(media_url).status_code == 200

    def test_download_without_images_is_harmless(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
    ) -> None:
        page = page_builder(state_builder([{"msgSeq": "1", "msgContent": "纯文字"}]))
        _install_fake_get(monkeypatch, [page])
        fetched = _body(configured_client.post("/api/fetch", json={"gc": "123456789"}))
        snapshot_id = fetched["data"]["snapshot"]["id"]

        response = configured_client.post(f"/api/snapshots/{snapshot_id}/download")
        assert response.status_code == 200
        assert _body(response)["data"]["stats"]["total"] == 0

    def test_download_missing_snapshot(self, client: Any) -> None:
        assert client.post("/api/snapshots/999999999_20240101-000000/download").status_code == 404


# --------------------------------------------------------------------------- #
# 错误处理
# --------------------------------------------------------------------------- #


class TestErrorHandlers:
    """统一错误响应。"""

    def test_unknown_api_returns_json_404(self, client: Any) -> None:
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404
        body = _body(response)
        assert body["ok"] is False
        assert body["error"]

    def test_method_not_allowed(self, client: Any) -> None:
        response = client.put("/api/config")
        assert response.status_code == 405
        assert _body(response)["ok"] is False


# --------------------------------------------------------------------------- #
# 前后端路由一致性
# --------------------------------------------------------------------------- #


class TestFrontendContract:
    """前端调用的每个路径都必须能在后端路由表里匹配到。"""

    def test_api_paths_match_backend_rules(self, flask_app: Any) -> None:
        js = (flask_app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        raw_paths = set(re.findall(r"[`'](/api/[^`'?]*)", js))
        assert raw_paths, "未能从 app.js 中解析出任何接口路径"

        sample_id = "123456789_20240101-120000"
        rules = set()
        for rule in flask_app.app.url_map.iter_rules():
            path = rule.rule
            path = path.replace("<path:filename>", "a.jpg").replace("<filename>", "a.jpg")
            path = path.replace("<snapshot_id>", sample_id)
            rules.add(path)

        unresolved = []
        for raw in raw_paths:
            path = re.sub(r"\$\{[^}]*\}", sample_id, raw).rstrip("/") or "/"
            if path not in rules:
                unresolved.append(raw)
        assert unresolved == [], f"前端调用了后端不存在的路径：{unresolved}"

    def test_media_urls_are_routable(self, flask_app: Any) -> None:
        rule_paths = {rule.rule for rule in flask_app.app.url_map.iter_rules()}
        assert any(path.startswith("/media/") for path in rule_paths)


# --------------------------------------------------------------------------- #
# 端到端离线链路
# --------------------------------------------------------------------------- #


class TestEndToEndOffline:
    """抓取 → 存快照 → 读快照 → 导出，全链路离线跑通并产出真实文件。"""

    def test_full_chain_produces_openable_export(
        self,
        configured_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        state_builder: Any,
        page_builder: Any,
        artifacts_dir: Path,
    ) -> None:
        page = page_builder(
            state_builder(
                [
                    {"msgSeq": "1", "msgTime": 1700000000, "senderName": "张三", "msgContent": "端到端第一条"},
                    {
                        "msgSeq": "2",
                        "msgTime": 1700000100,
                        "senderName": "李四",
                        "msgContent": "",
                        "picUrl": "https://gchat.qpic.cn/a.jpg",
                    },
                    {
                        "msgSeq": "3",
                        "senderName": "王五",
                        "fileName": "报表.xlsx",
                        "fileSize": 2048,
                        "url": "https://qfile.qq.com/x",
                    },
                ]
            )
        )
        _install_fake_get(monkeypatch, [page])

        # 1) 抓取并落盘
        response = configured_client.post(
            "/api/fetch", json={"gc": "123456789", "download_images": True}
        )
        assert response.status_code == 200
        fetched = _body(response)["data"]
        snapshot_id = fetched["snapshot"]["id"]
        assert fetched["snapshot"]["count"] == 3
        assert fetched["snapshot"]["downloaded_images"] == 1

        # 2) 从索引里读回来
        listing = _body(configured_client.get("/api/snapshots"))["data"]
        assert listing["count"] == 1
        detail = _body(configured_client.get(f"/api/snapshots/{snapshot_id}"))["data"]
        assert detail["snapshot"]["id"] == snapshot_id
        assert len(detail["messages"]) == 3

        # 3) 导出三种格式，其中 HTML 落盘为真实可打开文件
        html_response = configured_client.get(f"/api/snapshots/{snapshot_id}/export?format=html")
        assert html_response.status_code == 200
        html = html_response.get_data(as_text=True)
        assert "<script" not in html.lower()
        assert "端到端第一条" in html
        assert "data:image/png;base64," in html, "已下载图片应内联进导出文件"

        html_file = artifacts_dir / "e2e_export.html"
        html_file.write_text(html, encoding="utf-8")
        assert html_file.is_file()
        assert html_file.stat().st_size > 1000
        assert html_file.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

        md_response = configured_client.get(f"/api/snapshots/{snapshot_id}/export?format=md")
        assert md_response.status_code == 200
        markdown = md_response.get_data(as_text=True)
        assert "## 1. 张三" in markdown
        (artifacts_dir / "e2e_export.md").write_text(markdown, encoding="utf-8")

        json_response = configured_client.get(f"/api/snapshots/{snapshot_id}/export?format=json")
        payload = json.loads(json_response.get_data(as_text=True))
        assert payload["meta"]["id"] == snapshot_id
        assert len(payload["messages"]) == 3

        # 4) 删除后索引与目录都被清理
        assert configured_client.delete(f"/api/snapshots/{snapshot_id}").status_code == 200
        assert configured_client.get(f"/api/snapshots/{snapshot_id}").status_code == 404
        assert not (storage.SNAPSHOTS_DIR / snapshot_id).exists()
