"""pytest 全局配置。

两个 autouse fixture 保证测试的可重复性：
- ``isolated_data_dir``：把所有落盘路径重定向到 ``tmp_path``，绝不污染真实 ``data/``；
- ``blocked_network``：默认拦截 ``requests.get/post``，任何一个测试若忘记 mock 网络层，
  都会立刻以 AssertionError 暴露，而不是悄悄访问线上接口。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
import requests

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import storage  # noqa: E402  (必须在 sys.path 调整之后导入)

ARTIFACTS_DIR = TESTS_DIR / "artifacts"


# --------------------------------------------------------------------------- #
# 隔离与安全网
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把存储层的数据目录整体重定向到临时目录。"""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "SNAPSHOTS_DIR", data_dir / "snapshots")
    monkeypatch.setattr(storage, "CONFIG_PATH", data_dir / "config.json")
    monkeypatch.setattr(storage, "INDEX_PATH", data_dir / "index.json")
    storage.ensure_dirs()
    return data_dir


@pytest.fixture(autouse=True)
def blocked_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认禁止真实 HTTP 请求。"""

    def _blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "测试中禁止真实网络请求，请用 monkeypatch 替换 requests.get/requests.post"
        )

    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)


# --------------------------------------------------------------------------- #
# Flask 应用
# --------------------------------------------------------------------------- #


@pytest.fixture
def flask_app(isolated_data_dir: Path) -> Any:
    """导入 app 模块（模块级会执行 create_app）。"""
    import app as app_module  # noqa: PLC0415 - 延迟导入以复用隔离后的路径

    return app_module


@pytest.fixture
def client(flask_app: Any) -> Any:
    """Flask 测试客户端（不占用端口）。"""
    return flask_app.app.test_client()


@pytest.fixture
def artifacts_dir() -> Path:
    """端到端测试产生的真实导出文件目录。"""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


# --------------------------------------------------------------------------- #
# 测试数据工厂
# --------------------------------------------------------------------------- #


def build_page(state: Any, marker: str = "window.__INITIAL_STATE__ = ") -> str:
    """把状态字典包装成带 ``__INITIAL_STATE__`` 的完整页面 HTML。

    Args:
        state: 状态对象（会被 json 序列化）。
        marker: 赋值语句前缀，用于测试命名变体。

    Returns:
        页面 HTML 字符串。
    """
    payload = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    return (
        "<!DOCTYPE html><html><head><title>qq essence</title></head><body>"
        '<div id="app"></div>'
        f"<script>{marker}{payload};</script>"
        "</body></html>"
    )


def build_state(messages: list[Any], **extra: Any) -> dict[str, Any]:
    """构造一个最小可用的状态字典。

    Args:
        messages: ``msgList`` 内容。
        **extra: 覆盖/追加的顶层字段。

    Returns:
        状态字典。
    """
    state: dict[str, Any] = {
        "gc": "123456789",
        "uin": 10001,
        "skey": "@AbCdEf12",
        "bkn": 123456,
        "pageStart": 0,
        "isEnd": True,
        "hasFoundLocation": True,
        "hasServerError": False,
        "msgList": messages,
    }
    state.update(extra)
    return state


@pytest.fixture
def page_builder() -> Any:
    """返回 :func:`build_page`，避免各测试文件重复造轮子。"""
    return build_page


@pytest.fixture
def state_builder() -> Any:
    """返回 :func:`build_state`。"""
    return build_state


# --------------------------------------------------------------------------- #
# 网络层 mock
# --------------------------------------------------------------------------- #


class FakeResponse:
    """可配置的伪 ``requests.Response``。"""

    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        payload: Optional[Any] = None,
        content: bytes = b"",
        content_type: str = "image/png",
        encoding: str = "utf-8",
    ) -> None:
        """初始化伪响应。

        Args:
            text: 文本内容。
            status_code: HTTP 状态码。
            payload: ``json()`` 返回值；``None`` 时抛 ``ValueError``。
            content: 二进制内容（图片下载用）。
            content_type: 响应头 Content-Type。
            encoding: 文本编码。
        """
        self.text = text
        self.status_code = status_code
        self.encoding = encoding
        self.headers = {"Content-Type": content_type}
        self.closed = False
        self._payload = payload
        self._content = content

    def json(self) -> Any:
        """返回预设的 JSON 载荷。"""
        if self._payload is None:
            raise ValueError("响应体不是 JSON")
        return self._payload

    def iter_content(self, chunk_size: int = 1024) -> Iterator[bytes]:
        """按块迭代二进制内容。"""
        data = self._content
        return (data[index : index + chunk_size] for index in range(0, len(data), chunk_size))

    def close(self) -> None:
        """标记响应已关闭。"""
        self.closed = True


@pytest.fixture
def fake_response() -> Any:
    """返回 :class:`FakeResponse` 类本身。"""
    return FakeResponse


@pytest.fixture
def valid_cookie() -> str:
    """一段结构合法（但线上必然失效）的 Cookie。"""
    return "pgv_pvid=123456; uin=o12345678; sku=1; skey=@AbCdEf12; p_skey=Zz9Yy8"
