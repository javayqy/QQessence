"""QQ 群精华网络客户端。

职责
----
- 解析并规范化 Cookie（提取 ``uin`` / ``skey`` / ``p_skey``）；
- 用 5381 哈希算法从 ``skey`` 计算 ``bkn``；
- 请求群精华页面（``https://qun.qq.com/essence/indexPc``）并驱动分页；
- 请求群列表接口（``https://qun.qq.com/cgi-bin/qun_mgr/get_group_list``）。

设计说明
--------
页面返回的 ``uin`` / ``bkn`` / ``skey`` 是从 Cookie 回显的，**不能**用来判断登录态；
判断依据只有三个信号：``msgList`` 是否非空、``hasServerError``、群列表接口的 ``ec`` 码。
所有网络异常都会被转换为 :class:`QQClientError`，携带用户可读的提示信息。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from . import parser as message_parser

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

ESSENCE_URL: str = "https://qun.qq.com/essence/indexPc"
GROUP_LIST_URL: str = "https://qun.qq.com/cgi-bin/qun_mgr/get_group_list"
REFERER_ESSENCE: str = "https://qun.qq.com/"
REFERER_MEMBER: str = "https://qun.qq.com/member.html"

DEFAULT_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT: float = 20.0
MAX_PAGES_LIMIT: int = 20
GC_PATTERN = re.compile(r"^\d{5,12}$")
_UIN_KEYS: tuple[str, ...] = ("uin", "p_uin", "luin")
_SKEY_KEYS: tuple[str, ...] = ("skey", "p_skey")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class QQClientError(Exception):
    """客户端统一异常，``message`` 面向用户，``hint`` 给出可操作建议。"""

    def __init__(self, message: str, hint: str = "", status: int = 0) -> None:
        """初始化异常。

        Args:
            message: 面向用户的错误描述。
            hint: 可操作的处理建议。
            status: 关联的 HTTP 状态码（无则 0）。
        """
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        """转换成 API 返回结构。"""
        payload: dict[str, Any] = {"error": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.status:
            payload["status"] = self.status
        return payload


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #


def calc_bkn(skey: str) -> int:
    """用 QQ 的 5381 哈希算法由 ``skey`` 计算 ``bkn``。

    Args:
        skey: Cookie 中的 ``skey`` 值。

    Returns:
        31 位非负整数 ``bkn``；``skey`` 为空时返回 0。
    """
    if not skey:
        return 0
    hash_value = 5381
    for char in skey:
        hash_value += (hash_value << 5) + ord(char)
        hash_value &= 0x7FFFFFFF
    return hash_value


def random_digits(length: int) -> str:
    """生成指定长度的随机数字串（首位非 0）。

    Args:
        length: 长度，必须 >= 1。

    Returns:
        随机数字字符串。
    """
    safe_length = max(1, int(length))
    low = 10 ** (safe_length - 1)
    high = 10**safe_length - 1
    return str(random.randint(low, high))


def _strip_uin_prefix(value: str) -> str:
    """去掉 QQ Cookie 里 uin 常见的 ``o`` 前缀（``o123456`` → ``123456``）。"""
    text = str(value).strip().strip('"').strip("'")
    if text[:1].lower() == "o":
        text = text[1:]
    return text if text.isdigit() else ""


@dataclass
class CookieInfo:
    """规范化后的 Cookie 信息。"""

    raw: str = ""
    uin: str = ""
    skey: str = ""
    p_skey: str = ""
    pairs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw_cookie: str) -> "CookieInfo":
        """解析用户粘贴的 Cookie 文本。

        支持整段 ``a=b; c=d``、带 ``Cookie:`` 前缀、多行粘贴、多余空白等情况。

        Args:
            raw_cookie: 用户输入的 Cookie 文本。

        Returns:
            解析后的 :class:`CookieInfo` 实例（解析失败时各字段为空）。
        """
        info = cls()
        if not raw_cookie or not isinstance(raw_cookie, str):
            return info

        text = raw_cookie.strip()
        # 去掉可能的 "Cookie:" 前缀（大小写不敏感），兼容多行粘贴。
        text = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text)
        text = text.replace("\r", ";").replace("\n", ";")

        pairs: dict[str, str] = {}
        for chunk in text.split(";"):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            key, _, value = item.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            pairs[key] = value

        if not pairs:
            return info

        info.pairs = pairs
        info.raw = "; ".join(f"{key}={value}" for key, value in pairs.items())

        for key in _UIN_KEYS:
            if key in pairs:
                uin = _strip_uin_prefix(pairs[key])
                if uin:
                    info.uin = uin
                    break

        info.skey = pairs.get("skey", "")
        info.p_skey = pairs.get("p_skey", "")
        return info

    @property
    def has_cookie(self) -> bool:
        """是否具备可用 Cookie（至少有 ``skey`` 或 ``p_skey``）。"""
        return bool(self.raw and (self.skey or self.p_skey))

    def bkn(self) -> int:
        """基于 ``skey`` 计算 ``bkn``；``skey`` 缺失时退回 ``p_skey``。"""
        return calc_bkn(self.skey or self.p_skey)

    def masked(self) -> dict[str, Any]:
        """生成脱敏后的展示信息，**绝不**包含完整 ``skey``。

        Returns:
            含 ``uin``、``cookie_masked``、``has_cookie``、``has_skey``、``has_p_skey`` 的字典。
        """
        skey_tail = self.skey[-4:] if self.skey else ""
        masked_parts: list[str] = []
        if self.uin:
            masked_parts.append(f"uin=o{self.uin}")
        if skey_tail:
            masked_parts.append(f"skey=***{skey_tail}")
        return {
            "uin": self.uin,
            "cookie_masked": "; ".join(masked_parts),
            "has_cookie": self.has_cookie,
            "has_skey": bool(self.skey),
            "has_p_skey": bool(self.p_skey),
        }


# --------------------------------------------------------------------------- #
# 抓取结果
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    """一次精华消息抓取的结果。"""

    gc: str
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    is_end: bool = True
    has_server_error: bool = False
    source: str = "none"
    state: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        """转换成写入 ``meta.json`` 的摘要字段。"""
        return {
            "gc": self.gc,
            "pages": self.pages,
            "is_end": self.is_end,
            "has_server_error": self.has_server_error,
            "source": self.source,
            "state": self.summary,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class QQEssenceClient:
    """QQ 群精华页面的轻量客户端。"""

    def __init__(
        self,
        cookie: Optional[CookieInfo] = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        """初始化客户端。

        Args:
            cookie: 已解析的 Cookie 信息。
            timeout: 单次请求超时时间（秒）。
            user_agent: 请求使用的 User-Agent。
        """
        self.cookie: CookieInfo = cookie or CookieInfo()
        self.timeout: float = float(timeout)
        self.user_agent: str = user_agent

    # -- 内部工具 --------------------------------------------------------- #

    def _headers(self, referer: str = REFERER_ESSENCE) -> dict[str, str]:
        """构造请求头。"""
        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie.raw:
            headers["Cookie"] = self.cookie.raw
        return headers

    def _post_headers(self) -> dict[str, str]:
        """构造 POST 请求头（补充 Origin 与表单类型）。"""
        headers = self._headers(REFERER_MEMBER)
        headers["Origin"] = "https://qun.qq.com"
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    # -- 精华页面 --------------------------------------------------------- #

    def build_essence_url(
        self,
        gc: str,
        page_start: Optional[int] = None,
        location_id: Optional[str] = None,
    ) -> str:
        """构造精华页面 URL。

        Args:
            gc: 群号（5-12 位数字）。
            page_start: 翻页游标（服务端回传的 ``pageStart``）。
            location_id: 定位标识（服务端回传的 ``locationID``）。

        Returns:
            完整 URL。
        """
        params: dict[str, str] = {
            "gc": str(gc),
            "seq": random_digits(8),
            "random": random_digits(10),
        }
        if page_start is not None and str(page_start) != "":
            params["pageStart"] = str(page_start)
        if location_id:
            params["locationID"] = str(location_id)
        return f"{ESSENCE_URL}?{urlencode(params)}"

    def fetch_essence_page(
        self,
        gc: str,
        page_start: Optional[int] = None,
        location_id: Optional[str] = None,
    ) -> str:
        """请求一页精华消息页面。

        Args:
            gc: 群号。
            page_start: 翻页游标。
            location_id: 定位标识。

        Returns:
            页面 HTML 文本。

        Raises:
            QQClientError: 网络异常、超时或非 200 响应。
        """
        if not GC_PATTERN.match(str(gc)):
            raise QQClientError("群号格式不正确", "群号必须是 5-12 位数字")
        if not self.cookie.has_cookie:
            raise QQClientError("尚未配置 Cookie", "请先在左侧「连接」卡片粘贴 Cookie 并保存")

        url = self.build_essence_url(gc, page_start=page_start, location_id=location_id)
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.Timeout as exc:
            raise QQClientError(
                "请求精华页面超时", "请检查网络或代理设置后重试"
            ) from exc
        except requests.RequestException as exc:
            raise QQClientError(
                f"请求精华页面失败：{exc.__class__.__name__}", "请检查网络连接"
            ) from exc

        if response.status_code != 200:
            raise QQClientError(
                f"精华页面返回 HTTP {response.status_code}",
                "Cookie 可能已失效，请重新登录 qun.qq.com 后重新复制 Cookie",
                status=response.status_code,
            )

        response.encoding = response.encoding or "utf-8"
        return response.text

    def fetch_essence(self, gc: str, max_pages: int = 3) -> FetchResult:
        """抓取精华消息，并按服务端游标自动翻页。

        Args:
            gc: 群号。
            max_pages: 最大抓取页数（1-20）。

        Returns:
            :class:`FetchResult` 实例。

        Raises:
            QQClientError: 首次请求即失败时抛出。
        """
        gc_text = str(gc).strip()
        page_limit = max(1, min(MAX_PAGES_LIMIT, int(max_pages or 1)))

        result = FetchResult(gc=gc_text)
        seen_keys: set[str] = set()
        page_start: Optional[int] = None
        location_id: Optional[str] = None
        first_page = True

        while result.pages < page_limit:
            page_html = self.fetch_essence_page(gc_text, page_start=page_start, location_id=location_id)
            result.pages += 1
            parsed = message_parser.parse_page(page_html)

            state = parsed["state"]
            if result.pages == 1:
                result.state = state
                result.summary = parsed["summary"]
                result.source = parsed["source"]
            result.has_server_error = bool(state.get("hasServerError")) or result.has_server_error

            raw_items = parsed["raw_messages"]
            added = 0
            for item in raw_items:
                key = _message_fingerprint(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                result.raw_messages.append(item)
                added += 1

            # isEnd 缺失时**不假设**已结束，交给后面「pageStart 是否推进」来判断，
            # 避免线上某次响应漏了 isEnd 导致少抓后续页。
            is_end_flag = state.get("isEnd")
            result.is_end = is_end_flag is True

            if not raw_items:
                # 本页没有任何消息，说明数据已取尽。
                result.is_end = True
                break
            if result.has_server_error:
                result.warnings.append("页面返回 hasServerError=true，可能无权限或该群无精华消息")
                break
            if is_end_flag is True:
                break

            next_start = state.get("pageStart")
            next_location = state.get("locationID")
            if next_start is None:
                result.warnings.append("服务端未返回 pageStart，已安全停止翻页")
                break
            try:
                next_start_int = int(next_start)
            except (TypeError, ValueError):
                result.warnings.append("pageStart 非数值，已安全停止翻页")
                break
            if not first_page and next_start_int == page_start:
                result.warnings.append("pageStart 未推进，已安全停止翻页")
                break
            if added == 0:
                result.warnings.append("本页未获得新消息，已安全停止翻页")
                break

            first_page = False
            page_start = next_start_int
            location_id = str(next_location) if next_location else None

        # 统一在最后做一次归一化，保证顺序与去重后的原始列表一致。
        result.messages = message_parser.normalize_messages(result.raw_messages)
        if result.has_server_error and not result.raw_messages:
            result.warnings.append("未获取到任何精华消息：可能是 Cookie 失效、群号错误或该群无精华")
        elif not result.raw_messages:
            result.warnings.append("该群暂无精华消息")
        return result

    # -- 群列表 ----------------------------------------------------------- #

    def fetch_group_list(self) -> list[dict[str, Any]]:
        """获取当前账号加入的群列表。

        接口在 Cookie 未通过鉴权时返回 ``{"ec":4,...}``，此时抛出
        :class:`QQClientError`，由上层转换为友好错误并提示用户手动输入群号。

        Returns:
            群条目列表，每项形如 ``{"gc","name","owner","member_count"}``。

        Raises:
            QQClientError: Cookie 缺失、网络异常、接口返回非 0 错误码。
        """
        if not self.cookie.has_cookie:
            raise QQClientError("尚未配置 Cookie", "请先在左侧「连接」卡片粘贴 Cookie 并保存")

        bkn = self.cookie.bkn()
        if not bkn:
            raise QQClientError("无法计算 bkn", "Cookie 中缺少 skey，请重新复制完整 Cookie")

        try:
            response = requests.post(
                GROUP_LIST_URL,
                data={"bkn": str(bkn)},
                headers=self._post_headers(),
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise QQClientError("请求群列表超时", "请检查网络或代理设置后重试") from exc
        except requests.RequestException as exc:
            raise QQClientError(
                f"请求群列表失败：{exc.__class__.__name__}", "请检查网络连接"
            ) from exc

        if response.status_code != 200:
            raise QQClientError(
                f"群列表接口返回 HTTP {response.status_code}",
                "Cookie 可能已失效，请重新登录 qun.qq.com 后重新复制 Cookie",
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise QQClientError(
                "群列表接口返回了非 JSON 内容",
                "Cookie 可能已失效，可手动输入群号继续使用",
            ) from exc

        if not isinstance(payload, dict):
            raise QQClientError("群列表接口返回结构异常", "可手动输入群号继续使用")

        ec = payload.get("ec", 0)
        if ec not in (0, "0", None):
            em = payload.get("em") or ""
            raise QQClientError(
                f"群列表接口返回错误码 ec={ec}",
                "Cookie 未通过该接口鉴权（常见为 skey 失效）。可手动输入群号直接拉取精华；"
                "或重新登录 qun.qq.com 后复制完整 Cookie。" + (f" 服务端说明：{em}" if em else ""),
            )

        return _extract_groups(payload)

    # -- 图片代理下载（供 downloader 复用会话配置） ------------------------ #

    def image_headers(self, referer: str = REFERER_ESSENCE) -> dict[str, str]:
        """构造拉取图片时使用的请求头。"""
        headers = self._headers(referer)
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        return headers


# --------------------------------------------------------------------------- #
# 内部函数
# --------------------------------------------------------------------------- #


def _message_fingerprint(raw: Any) -> str:
    """为原始消息生成指纹，用于跨页去重。"""
    try:
        import json

        return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)[:4000]
    except (TypeError, ValueError):
        return str(id(raw))


def _extract_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从群列表接口返回体中抽取群条目。

    接口常见结构为 ``{"ec":0,"create":[...],"manage":[...],"join":[...]}``，
    但字段名并不稳定，这里做防御式遍历。

    Args:
        payload: 接口 JSON。

    Returns:
        去重后的群条目列表。
    """
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            gc_value = None
            for key in ("gc", "group_id", "groupCode", "group_code", "groupid"):
                candidate = node.get(key)
                if isinstance(candidate, (int, str)) and str(candidate).isdigit():
                    gc_value = str(candidate)
                    break
            if gc_value and gc_value not in seen:
                name = ""
                for key in ("name", "group_name", "groupName", "gn"):
                    candidate = node.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        name = candidate.strip()
                        break
                owner = ""
                for key in ("owner", "owner_uin", "creator"):
                    candidate = node.get(key)
                    if isinstance(candidate, (int, str)) and str(candidate).strip():
                        owner = str(candidate).strip()
                        break
                member_count = node.get("member_count") or node.get("membernum") or node.get("memberNum") or ""
                seen.add(gc_value)
                groups.append(
                    {
                        "gc": gc_value,
                        "name": name or f"群 {gc_value}",
                        "owner": owner,
                        "member_count": str(member_count) if member_count != "" else "",
                    }
                )
            for value in node.values():
                _walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    _walk(payload)
    return groups
