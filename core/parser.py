"""精华消息解析层。

职责
----
1. 从 QQ 群精华页面中提取 ``window.__INITIAL_STATE__`` 的 JSON 数据；
2. 把形状未知的原始消息对象**防御式**归一化为统一消息模型；
3. 当 ``__INITIAL_STATE__`` 缺失时，用旧版 DOM 结构（``#app/div[2]/div[i]``）做兜底解析。

设计说明
--------
线上 ``msgList`` 的字段结构没有任何公开文档，且随时可能变化，因此本模块**不硬编码**
字段路径，而是递归遍历对象的所有键值，按「常见键名优先 + 值形态兜底」的方式抽取。
任何一条消息解析失败都不会丢失数据：异常会被收敛成 ``type="unknown"`` 且完整保留 ``raw``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional

try:  # pragma: no cover - lxml 为可选依赖
    from lxml import html as _lxml_html  # type: ignore
except Exception:  # noqa: BLE001 - 缺少 lxml 时降级为纯正则兜底
    _lxml_html = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 中国标准时间（UTC+8）。QQ 服务端返回的时间戳与字符串均以该时区为准。
CN_TZ: timezone = timezone(timedelta(hours=8))

#: 一个快照里最多保留的正文字符数，防止异常数据把内存撑爆。
MAX_TEXT_LENGTH: int = 20000

#: 递归遍历的最大深度，避免循环引用或超深结构造成栈溢出。
MAX_WALK_DEPTH: int = 8

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")

#: 用于从任意字符串中抓取 URL 的正则（遇到引号、尖括号、空白即停止）。
_URL_RE = re.compile(r"https?://[^\s\"'<>\\\u3000]+")

#: 图片 CDN 域名特征，命中即判定为图片。
_IMAGE_HOST_HINTS: tuple[str, ...] = (
    "gchat.qpic.cn",
    "qpic.cn",
    "multimedia.qq.com",
    "qlogo.cn",
    "gtimg.cn",
    "qzone.gtimg.cn",
    "photo.store.qq.com",
)

#: 图片扩展名特征。
_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".jfif",
    ".bmp",
    ".avif",
    ".heic",
)

#: 昵称类候选键（小写比较）。
_NAME_KEYS: frozenset[str] = frozenset(
    {
        "sendername",
        "send_name",
        "sendmembername",
        "send_member_name",
        "nickname",
        "nick_name",
        "nick",
        "name",
        "uinname",
        "uin_name",
        "fromname",
        "from_name",
        "membername",
        "member_name",
    }
)

#: QQ 号类候选键（小写比较）。
_UIN_KEYS: frozenset[str] = frozenset(
    {"senderuin", "sender_uin", "fromuin", "from_uin", "uin", "qq", "memberuin", "member_uin", "tinyid"}
)

#: 时间类候选键（小写比较）。
_TIME_KEYS: frozenset[str] = frozenset(
    {"msgtime", "msg_time", "time", "sendtime", "send_time", "createtime", "create_time", "timestamp", "date"}
)

#: 头像类候选键（小写比较）。
_AVATAR_KEYS: frozenset[str] = frozenset(
    {"avatar", "avatarurl", "avatar_url", "face", "faceurl", "portrait", "headimg", "head_img", "icon"}
)

#: 正文类候选键（小写比较）。
_TEXT_KEYS: frozenset[str] = frozenset(
    {"msgcontent", "msg_content", "content", "text", "summary", "title", "desc", "description", "message"}
)

#: 文件名类候选键（小写比较）。
_FILE_NAME_KEYS: frozenset[str] = frozenset({"filename", "file_name", "name", "filesummary"})

#: 文件大小类候选键（小写比较）。
_FILE_SIZE_KEYS: frozenset[str] = frozenset({"filesize", "file_size", "size", "sizestr"})

#: 文件链接类候选键（小写比较）。
_FILE_URL_KEYS: frozenset[str] = frozenset({"url", "fileurl", "file_url", "downloadurl", "download_url", "link"})

#: 消息 ID 类候选键（小写比较）。
_ID_KEYS: frozenset[str] = frozenset({"msgseq", "msg_seq", "msgid", "msg_id", "seq", "id"})


# --------------------------------------------------------------------------- #
# 数据类型
# --------------------------------------------------------------------------- #


@dataclass
class WalkResult:
    """一次递归遍历的抽取结果，内部使用。"""

    sender_name: str = ""
    sender_uin: str = ""
    avatar: str = ""
    time_value: Any = None
    texts: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    link_urls: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    msg_id: str = ""


# --------------------------------------------------------------------------- #
# 1. __INITIAL_STATE__ 提取
# --------------------------------------------------------------------------- #


def _scan_balanced_json(text: str, start: int) -> Optional[str]:
    """从 ``start`` 处的 ``{`` 开始，扫描出配平的 JSON 对象字面量。

    Args:
        text: 完整页面源码。
        start: 左花括号的索引位置。

    Returns:
        配平的 JSON 字符串；若扫描失败（未闭合）则返回 ``None``。
    """
    depth: int = 0
    in_string: bool = False
    escaped: bool = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _loads_lenient(payload: str) -> dict[str, Any]:
    """宽松地解析 JSON：失败时做少量清理后重试，最终失败返回空字典。

    Args:
        payload: 疑似 JSON 的字符串。

    Returns:
        解析出的字典；无法解析时返回 ``{}``。
    """
    try:
        loaded = json.loads(payload)
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 服务端偶尔会注入 undefined / 尾逗号，做一次轻量清洗再试。
    cleaned = re.sub(r"\bundefined\b", "null", payload)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        loaded = json.loads(cleaned)
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def extract_initial_state(page_html: str) -> dict[str, Any]:
    """从页面源码中提取 ``window.__INITIAL_STATE__``。

    Args:
        page_html: 页面 HTML 文本。

    Returns:
        解析出的状态字典；页面中不存在或解析失败时返回 ``{}``。
    """
    if not isinstance(page_html, str) or not page_html:
        return {}

    match = _INITIAL_STATE_RE.search(page_html)
    if match is None:
        return {}

    brace_index = page_html.find("{", match.end() - 1)
    if brace_index < 0:
        return {}

    raw = _scan_balanced_json(page_html, brace_index)
    if raw is None:
        return {}
    return _loads_lenient(raw)


def _deep_find_list(node: Any, key_names: frozenset[str], depth: int = 0) -> Optional[list[Any]]:
    """在嵌套结构中深度搜索某个键对应的列表值。

    Args:
        node: 待搜索的对象。
        key_names: 目标键名（小写）。
        depth: 当前递归深度。

    Returns:
        找到的列表；未找到返回 ``None``。
    """
    if depth > MAX_WALK_DEPTH:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in key_names and isinstance(value, list):
                return value
        for value in node.values():
            found = _deep_find_list(value, key_names, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _deep_find_list(value, key_names, depth + 1)
            if found is not None:
                return found
    return None


def extract_msg_list(state: dict[str, Any]) -> list[dict[str, Any]]:
    """从状态字典中取出精华消息原始数组。

    Args:
        state: :func:`extract_initial_state` 的返回值。

    Returns:
        原始消息对象的列表；无数据时返回 ``[]``。非 ``dict`` 但仍有内容（例如字符串）
        的条目也会保留，交给 :func:`normalize_message` 决定如何展示，避免丢数据。
    """
    if not isinstance(state, dict):
        return []

    candidates: list[Any] = []
    direct = state.get("msgList")
    if direct is None:
        direct = state.get("msglist")
    if direct is None:
        direct = state.get("messages")
    if isinstance(direct, list):
        candidates = direct
    else:
        found = _deep_find_list(state, frozenset({"msglist", "messages", "essencelist", "list"}))
        candidates = found or []

    messages: list[Any] = []
    for item in candidates:
        if isinstance(item, (dict, list)):
            messages.append(item)
        elif isinstance(item, str) and item.strip():
            messages.append(item)
    return messages


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """生成状态摘要，用于写入快照 ``meta.json``，便于事后诊断接口行为。

    Args:
        state: 页面状态字典。

    Returns:
        只包含基本类型字段的摘要字典。
    """
    if not isinstance(state, dict):
        return {}

    summary: dict[str, Any] = {}
    for key in (
        "gc",
        "uin",
        "pageStart",
        "isEnd",
        "hasServerError",
        "hasFoundLocation",
        "isAndroid",
    ):
        if key in state:
            summary[key] = state.get(key)

    location_id = state.get("locationID")
    summary["locationID"] = "" if location_id is None else str(location_id)
    summary["has_skey"] = bool(state.get("skey"))
    summary["has_bkn"] = bool(state.get("bkn"))
    summary["msg_count"] = len(extract_msg_list(state))
    summary["top_level_keys"] = sorted(str(key) for key in state.keys())
    return summary


# --------------------------------------------------------------------------- #
# 2. 基础工具
# --------------------------------------------------------------------------- #


def _ci_get(node: dict[Any, Any], key_names: frozenset[str]) -> Any:
    """大小写不敏感地在字典里取第一个命中的键值。

    Args:
        node: 源字典。
        key_names: 目标键名集合（小写）。

    Returns:
        命中的值；未命中返回 ``None``。
    """
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        if str(key).lower() in key_names:
            return value
    return None


def _looks_like_url(value: str) -> bool:
    """判断字符串是否是一个完整的 http(s) 链接。"""
    return isinstance(value, str) and _URL_RE.fullmatch(value.strip()) is not None


def is_image_url(url: str) -> bool:
    """判断 URL 是否指向图片资源。

    判定顺序：域名特征优先，其次看路径扩展名。

    Args:
        url: 待判断的链接。

    Returns:
        是图片则返回 ``True``。
    """
    if not isinstance(url, str) or not url.strip():
        return False
    lowered = url.strip().lower()
    for hint in _IMAGE_HOST_HINTS:
        if hint in lowered:
            return True
    path = lowered.split("?", 1)[0].split("#", 1)[0]
    return path.endswith(_IMAGE_EXTENSIONS)


def _clean_url(url: str) -> str:
    """清理 URL 首尾噪声字符，但**不改变** URL 本体（不截断、不改写参数）。"""
    if not isinstance(url, str):
        return ""
    cleaned = url.strip().strip(",;|")
    cleaned = cleaned.rstrip(".,;:)]}\u3002")
    return cleaned


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    """按出现顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _format_size(value: Any) -> str:
    """把文件大小的任意表示统一成人类可读字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if not text.isdigit():
            return text
        value = int(text)
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        size = float(value)
        if size < 0:
            return ""
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
    return str(value)


_TIME_PATTERNS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
    "%Y年%m月%d日 %H:%M:%S",
)


def _timestamp_to_datetime(value: float) -> Optional[datetime]:
    """把秒级或毫秒级时间戳转成中国标准时间的 ``datetime``。"""
    if value <= 0:
        return None
    seconds = value / 1000.0 if value > 1e11 else value
    try:
        return datetime.fromtimestamp(seconds, tz=CN_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def to_time_pair(value: Any) -> tuple[str, str]:
    """把任意时间表示转成 ``(time_raw, time_iso)``。

    支持秒/毫秒时间戳（int、float、纯数字字符串）与常见日期字符串。

    Args:
        value: 原始时间值。

    Returns:
        ``(显示用字符串, ISO 字符串)``。无法识别时两项均为空字符串。
    """
    if value is None or isinstance(value, bool):
        return "", ""

    moment: Optional[datetime] = None

    if isinstance(value, (int, float)):
        moment = _timestamp_to_datetime(float(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return "", ""
        if text.isdigit():
            moment = _timestamp_to_datetime(float(text))
        if moment is None:
            for pattern in _TIME_PATTERNS:
                try:
                    moment = datetime.strptime(text, pattern).replace(tzinfo=CN_TZ)
                    break
                except ValueError:
                    continue
        if moment is None:
            # 保留原始字符串，让前端至少能展示点什么。
            return text, ""

    if moment is None:
        return "", ""

    return moment.strftime("%Y-%m-%d %H:%M:%S"), moment.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- #
# 3. 递归抽取
# --------------------------------------------------------------------------- #


def _walk_collect(node: Any, result: WalkResult, depth: int = 0) -> None:
    """递归遍历消息对象，把命中的字段写入 ``result``。

    Args:
        node: 当前节点（dict / list / 标量）。
        result: 累积结果。
        depth: 当前递归深度。
    """
    if depth > MAX_WALK_DEPTH:
        return

    if isinstance(node, dict):
        # 文件条目是「整个字典」维度的判断，与具体键无关，放在循环外只算一次。
        file_entry = _detect_file_entry(node)
        if file_entry is not None and file_entry not in result.files:
            result.files.append(file_entry)

        for key, value in node.items():
            lowered = str(key).lower()

            # 头像属于消息**元信息**：消费后必须终止递归，否则同一个 URL 会走到下面的
            # ``isinstance(node, str)`` 分支被当成正文图片采集，导致纯文本消息凭空多出
            # 一张头像大图（并被计入 image_count、被下载器当成精华图片下载）。
            if lowered in _AVATAR_KEYS:
                if isinstance(value, str) and not result.avatar:
                    candidate = value.strip()
                    if candidate and _looks_like_url(candidate):
                        result.avatar = candidate
                continue

            if isinstance(value, str):
                text = value.strip()
                if not text:
                    continue
                if lowered in _NAME_KEYS and not result.sender_name and not _looks_like_url(text):
                    result.sender_name = text[:120]
                elif lowered in _UIN_KEYS and not result.sender_uin and text.isdigit():
                    result.sender_uin = text
                elif lowered in _TIME_KEYS and result.time_value is None:
                    raw, iso = to_time_pair(text)
                    if raw:
                        result.time_value = text
                elif lowered in _ID_KEYS and not result.msg_id:
                    result.msg_id = text[:64]
                elif lowered in _TEXT_KEYS and not _looks_like_url(text):
                    result.texts.append(text)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if lowered in _UIN_KEYS and not result.sender_uin and float(value).is_integer():
                    digits = str(int(value))
                    if 4 <= len(digits) <= 12:
                        result.sender_uin = digits
                elif lowered in _TIME_KEYS and result.time_value is None and value > 0:
                    result.time_value = value
                elif lowered in _ID_KEYS and not result.msg_id:
                    result.msg_id = str(int(value))

            _walk_collect(value, result, depth + 1)

    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk_collect(item, result, depth + 1)

    elif isinstance(node, str):
        for url in _URL_RE.findall(node):
            cleaned = _clean_url(url)
            if not cleaned:
                continue
            if is_image_url(cleaned):
                result.image_urls.append(cleaned)
            else:
                result.link_urls.append(cleaned)


def _detect_file_entry(node: dict[Any, Any]) -> Optional[dict[str, str]]:
    """把形如「文件名 + 大小/下载链接」的字典识别为一个文件条目。

    Args:
        node: 待检测的字典。

    Returns:
        文件条目 ``{"name", "size", "url"}``；不像文件则返回 ``None``。
    """
    if not isinstance(node, dict) or not node:
        return None

    name_value = _ci_get(node, _FILE_NAME_KEYS)
    if not isinstance(name_value, str):
        return None
    name = name_value.strip()
    if not name or len(name) > 300 or _looks_like_url(name):
        return None

    url_value = _ci_get(node, _FILE_URL_KEYS)
    url = _clean_url(url_value) if isinstance(url_value, str) else ""

    size_value = _ci_get(node, _FILE_SIZE_KEYS)
    size = _format_size(size_value)

    # 判定为文件的条件：有明确的大小字段，或带下载链接且不是图片。
    has_size = bool(size)
    has_download_url = bool(url) and _looks_like_url(url) and not is_image_url(url)
    if not has_size and not has_download_url:
        return None

    return {"name": name, "size": size, "url": url}


def _guess_link_title(node: Any) -> str:
    """从节点里猜一个标题，用于链接卡片。

    含有 URL 的文本不会被当作标题（否则整段正文都会变成链接标题）。
    """
    if not isinstance(node, dict):
        return ""

    def _acceptable(text: Any) -> str:
        if not isinstance(text, str):
            return ""
        candidate = text.strip()
        if not candidate or len(candidate) > 200:
            return ""
        if _looks_like_url(candidate) or "http" in candidate.lower():
            return ""
        return candidate

    for key in ("title", "text", "summary", "desc", "description", "name"):
        candidate = _acceptable(node.get(key))
        if candidate:
            return candidate

    for key, value in node.items():
        if str(key).lower() in _TEXT_KEYS:
            candidate = _acceptable(value)
            if candidate:
                return candidate
    return ""


def _collect_link_titles(node: Any, mapping: dict[str, str], depth: int = 0) -> None:
    """递归收集「链接 → 标题」的映射关系。"""
    if depth > MAX_WALK_DEPTH:
        return
    if isinstance(node, dict):
        title = _guess_link_title(node)
        if title:
            for value in node.values():
                if isinstance(value, str):
                    for url in _URL_RE.findall(value):
                        cleaned = _clean_url(url)
                        if cleaned and not is_image_url(cleaned) and cleaned not in mapping:
                            mapping[cleaned] = title
        for value in node.values():
            _collect_link_titles(value, mapping, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_link_titles(item, mapping, depth + 1)


def _contains_forward_hint(raw: Any) -> bool:
    """粗略判断原始对象里是否含有「合并转发」特征。"""
    for key, value in _iter_pairs(raw):
        lowered = key.lower()
        if "forward" in lowered and value:
            return True
        if lowered in {"resid", "ark", "arkdata"} and value:
            return True
    return False


def _iter_pairs(node: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
    """遍历嵌套结构下所有 ``(键, 值)`` 对。"""
    if depth > MAX_WALK_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _iter_pairs(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_pairs(item, depth + 1)


# --------------------------------------------------------------------------- #
# 4. 单条消息归一化
# --------------------------------------------------------------------------- #


def build_avatar_url(uin: str) -> str:
    """根据 QQ 号拼出兜底头像地址。"""
    digits = "".join(ch for ch in str(uin) if ch.isdigit())
    if not digits:
        return ""
    return f"https://q1.qlogo.cn/g?b=sdk&nk={digits}&s=140"


def normalize_message(raw: Any, index: int = 0) -> dict[str, Any]:
    """把一条原始消息归一化为统一消息模型。

    Args:
        raw: 原始消息对象（通常是 dict，也可能是任意标量）。
        index: 该消息在列表中的下标，用于生成兜底 ID。

    Returns:
        统一消息模型字典，包含 ``id/sender_uin/sender_name/avatar/time_raw/time_iso/
        text/images/links/files/type/raw`` 字段。解析异常时 ``type`` 为 ``unknown``。
    """
    base: dict[str, Any] = {
        "id": str(index),
        "sender_uin": "",
        "sender_name": "",
        "avatar": "",
        "time_raw": "",
        "time_iso": "",
        "text": "",
        "images": [],
        "links": [],
        "files": [],
        "type": "unknown",
        "raw": raw if isinstance(raw, (dict, list)) else {"value": raw},
    }

    try:
        if not isinstance(raw, (dict, list)):
            base["text"] = "" if raw is None else str(raw)
            base["type"] = "text" if base["text"] else "unknown"
            return base

        result = WalkResult()
        _walk_collect(raw, result)

        time_raw, time_iso = to_time_pair(result.time_value)

        # 正文：过滤掉纯链接与重复项，并剔除与文件名完全相同的噪声文本。
        file_names = {item["name"] for item in result.files}
        texts: list[str] = []
        for text in result.texts:
            cleaned = text.strip()
            if not cleaned or _looks_like_url(cleaned):
                continue
            if cleaned in file_names:
                continue
            if cleaned not in texts:
                texts.append(cleaned)
        text = "\n".join(texts)
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + "…"

        # 文件 URL 不应再重复出现在链接列表里。
        file_urls = {item["url"] for item in result.files if item["url"]}
        link_map: dict[str, str] = {}
        _collect_link_titles(raw, link_map)
        links: list[dict[str, str]] = []
        for url in _dedupe_keep_order(result.link_urls):
            if url in file_urls:
                continue
            links.append({"url": url, "title": link_map.get(url, "")})

        images = _dedupe_keep_order(result.image_urls)
        sender_uin = result.sender_uin
        avatar = result.avatar or build_avatar_url(sender_uin)

        base.update(
            {
                "id": result.msg_id or str(index),
                "sender_uin": sender_uin,
                "sender_name": result.sender_name or (f"QQ {sender_uin}" if sender_uin else "未知成员"),
                "avatar": avatar,
                "time_raw": time_raw,
                "time_iso": time_iso,
                "text": text,
                "images": images,
                "links": links,
                "files": result.files,
            }
        )
        base["type"] = _derive_type(text, images, links, result.files, raw)

    except Exception as exc:  # noqa: BLE001 - 任何异常都不能让整条消息丢失
        base["type"] = "unknown"
        base["text"] = base.get("text") or f"[解析异常] {exc.__class__.__name__}: {exc}"
        base["raw"] = raw if isinstance(raw, (dict, list)) else {"value": str(raw)}

    return base


def _derive_type(
    text: str,
    images: list[str],
    links: list[dict[str, str]],
    files: list[dict[str, str]],
    raw: Any,
) -> str:
    """根据内容要素推导消息类型。

    Args:
        text: 归一化后的正文。
        images: 图片 URL 列表。
        links: 链接列表。
        files: 文件列表。
        raw: 原始消息对象。

    Returns:
        消息类型字符串：``text|image|mix|file|link|forward|unknown``。
    """
    if files and not images:
        return "file"
    if images and text:
        return "mix"
    if images:
        return "image"
    if links and not text:
        return "link"
    if text:
        if _contains_forward_hint(raw):
            return "forward"
        return "text"
    return "unknown"


def normalize_messages(raw_list: Iterable[Any]) -> list[dict[str, Any]]:
    """批量归一化消息列表。

    Args:
        raw_list: 原始消息对象序列。

    Returns:
        归一化后的消息列表（顺序保持不变）。
    """
    messages: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_list):
        messages.append(normalize_message(raw, index))
    return messages


# --------------------------------------------------------------------------- #
# 5. DOM 兜底解析（旧版结构）
# --------------------------------------------------------------------------- #


def _parse_dom(page_html: str) -> list[dict[str, Any]]:
    """按旧版 DOM 结构解析页面，作为 ``__INITIAL_STATE__`` 缺失时的兜底。

    旧版结构为 ``#app > div[2] > div[i]``，每块依次为「头像 div / 昵称 div / 时间 div /
    内容 div」。这里只做尽力而为的抽取，任何异常都直接放弃并返回空列表。

    Args:
        page_html: 页面 HTML 文本。

    Returns:
        伪原始消息字典的列表，可直接交给 :func:`normalize_messages`。
    """
    if _lxml_html is None or not page_html:
        return []

    try:
        document = _lxml_html.fromstring(page_html)
    except Exception:  # noqa: BLE001 - 页面结构异常时直接放弃
        return []

    try:
        containers = document.xpath('//*[@id="app"]/div')
    except Exception:  # noqa: BLE001
        return []
    if len(containers) < 2:
        return []

    blocks: list[Any] = []
    try:
        blocks = containers[1].xpath("./div")
    except Exception:  # noqa: BLE001
        return []

    pseudo_messages: list[dict[str, Any]] = []
    for block in blocks:
        try:
            entry = _parse_dom_block(block)
        except Exception:  # noqa: BLE001 - 单块失败不影响其它块
            continue
        if entry:
            pseudo_messages.append(entry)
    return pseudo_messages


def _parse_dom_block(block: Any) -> Optional[dict[str, Any]]:
    """解析单个 DOM 消息块为伪原始消息字典。"""
    style_values = block.xpath("./div[1]/@style")
    avatar_match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style_values[0]) if style_values else None
    avatar = _clean_url(avatar_match.group(1)) if avatar_match else ""

    uin = ""
    if avatar:
        uin_match = re.search(r"/(\d{4,12})/", avatar) or re.search(r"nk=(\d{4,12})", avatar)
        if uin_match:
            uin = uin_match.group(1)

    name_nodes = block.xpath("./div[2]")
    name = name_nodes[0].text_content().strip() if name_nodes else ""

    time_nodes = block.xpath("./div[3]")
    time_text = time_nodes[0].text_content().strip() if time_nodes else ""

    content_nodes = block.xpath("./div[last()]")
    texts: list[str] = []
    images: list[str] = []
    for node in content_nodes:
        for span in node.xpath(".//span"):
            span_text = span.text_content().strip()
            if span_text:
                texts.append(span_text)
        for img in node.xpath(".//img"):
            src = _clean_url(img.get("src") or "")
            if src:
                images.append(src)
        for anchor in node.xpath(".//a"):
            href = _clean_url(anchor.get("href") or "")
            if href:
                texts.append(href)

    if not (texts or images or name):
        return None

    return {
        "senderUin": uin,
        "senderName": name,
        "msgTime": time_text,
        "avatar": avatar,
        "msgContent": "\n".join(_dedupe_keep_order(texts)),
        "domImages": images,
    }


# --------------------------------------------------------------------------- #
# 6. 顶层入口
# --------------------------------------------------------------------------- #


def parse_page(page_html: str) -> dict[str, Any]:
    """解析一次页面抓取结果。

    Args:
        page_html: 页面 HTML 文本。

    Returns:
        字典，包含：
        ``state``（状态字典）、``raw_messages``（原始消息列表）、
        ``messages``（归一化消息列表）、``source``（``initial_state`` 或 ``dom``）、
        ``summary``（状态摘要）。
    """
    state = extract_initial_state(page_html)
    raw_messages = extract_msg_list(state)

    if raw_messages:
        return {
            "state": state,
            "raw_messages": raw_messages,
            "messages": normalize_messages(raw_messages),
            "source": "initial_state",
            "summary": state_summary(state),
        }

    dom_messages = _parse_dom(page_html)
    return {
        "state": state,
        "raw_messages": dom_messages,
        "messages": normalize_messages(dom_messages),
        "source": "dom" if dom_messages else "none",
        "summary": state_summary(state),
    }
