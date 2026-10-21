"""解析层测试：``__INITIAL_STATE__`` 提取、消息归一化、DOM 兜底。

重点验证「不丢消息、不串条、不崩溃」三项硬要求。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core import parser

# --------------------------------------------------------------------------- #
# 1. __INITIAL_STATE__ 提取
# --------------------------------------------------------------------------- #


class TestExtractInitialState:
    """状态提取与容错。"""

    def test_extracts_plain_state(self, state_builder: Any, page_builder: Any) -> None:
        state = state_builder([{"msgSeq": "1", "msgContent": "hi"}])
        result = parser.extract_initial_state(page_builder(state))
        assert isinstance(result, dict)
        assert result["gc"] == "123456789"
        assert result["msgList"] == [{"msgSeq": "1", "msgContent": "hi"}]

    def test_tolerates_no_space_around_assign(self) -> None:
        page = '<html><script>window.__INITIAL_STATE__={"gc":"1"};</script></html>'
        assert parser.extract_initial_state(page) == {"gc": "1"}

    def test_returns_empty_when_marker_missing(self) -> None:
        page = "<html><body><div id='app'></div></body></html>"
        assert parser.extract_initial_state(page) == {}

    @pytest.mark.parametrize("bad_input", ["", None, 123, b"bytes"])
    def test_returns_empty_for_non_text_input(self, bad_input: Any) -> None:
        assert parser.extract_initial_state(bad_input) == {}

    def test_lenient_parsing_fixes_trailing_comma(self, page_builder: Any) -> None:
        page = page_builder('{"gc":"1","msgList":[{"content":"a",},]}')
        state = parser.extract_initial_state(page)
        assert state["msgList"] == [{"content": "a"}]

    def test_lenient_parsing_fixes_undefined(self, page_builder: Any) -> None:
        page = page_builder('{"gc":"1","msgList":[{"content":"a","extra":undefined}]}')
        state = parser.extract_initial_state(page)
        assert state["gc"] == "1"
        assert state["msgList"][0]["extra"] is None

    def test_returns_empty_for_unclosed_json(self, page_builder: Any) -> None:
        page = page_builder('{"gc":"1","msgList":[{"content":"a"')
        assert parser.extract_initial_state(page) == {}

    def test_returns_empty_when_json_is_not_object(self, page_builder: Any) -> None:
        assert parser.extract_initial_state(page_builder('"just a string"')) == {}

    def test_braces_and_quotes_inside_string_do_not_break_scan(self, page_builder: Any) -> None:
        text = 'a {b} c } d \\ "escaped" {'
        page = page_builder(json.dumps({"msgList": [{"msgContent": text}]}, ensure_ascii=False))
        state = parser.extract_initial_state(page)
        assert state["msgList"][0]["msgContent"] == text


class TestExtractMsgList:
    """原始消息列表抽取。"""

    def test_reads_msg_list(self) -> None:
        state = {"msgList": [{"a": 1}, {"b": 2}]}
        assert parser.extract_msg_list(state) == [{"a": 1}, {"b": 2}]

    def test_reads_lowercase_msglist(self) -> None:
        assert parser.extract_msg_list({"msglist": [{"a": 1}]}) == [{"a": 1}]

    def test_finds_nested_list(self) -> None:
        state = {"data": {"essenceList": [{"msg": {"content": "深"}}]}}
        assert parser.extract_msg_list(state) == [{"msg": {"content": "深"}}]

    @pytest.mark.parametrize("state", [{}, {"msgList": []}, {"msgList": "oops"}, None, []])
    def test_returns_empty_for_no_messages(self, state: Any) -> None:
        assert parser.extract_msg_list(state) == []

    def test_keeps_scalar_string_entries(self) -> None:
        assert parser.extract_msg_list({"msgList": ["纯文本", 123, None]}) == ["纯文本"]

    def test_stops_at_depth_limit_without_crash(self) -> None:
        node: Any = {"msgList": [{"content": "too deep"}]}
        for _ in range(parser.MAX_WALK_DEPTH + 3):
            node = {"wrap": node}
        assert parser.extract_msg_list(node) == []


class TestStateSummary:
    """状态摘要。"""

    def test_summary_fields(self) -> None:
        state = {
            "gc": "123456789",
            "uin": 10001,
            "pageStart": 20,
            "isEnd": False,
            "hasServerError": True,
            "hasFoundLocation": True,
            "skey": "secret",
            "bkn": 999,
            "locationID": 5,
            "msgList": [{"a": 1}],
        }
        summary = parser.state_summary(state)
        assert summary["gc"] == "123456789"
        assert summary["pageStart"] == 20
        assert summary["hasServerError"] is True
        assert summary["locationID"] == "5"
        assert summary["has_skey"] is True
        assert summary["has_bkn"] is True
        assert summary["msg_count"] == 1
        # 只暴露键名，不暴露 skey 的值
        assert "skey" in summary["top_level_keys"]

    def test_summary_never_contains_skey_value(self) -> None:
        summary = parser.state_summary({"skey": "SUPER_SECRET_SKEY", "msgList": []})
        assert "SUPER_SECRET_SKEY" not in json.dumps(summary, ensure_ascii=False)

    def test_summary_of_non_dict(self) -> None:
        assert parser.state_summary(None) == {}


# --------------------------------------------------------------------------- #
# 2. 时间与工具函数
# --------------------------------------------------------------------------- #


class TestTimeParsing:
    """时间归一化。"""

    @pytest.mark.parametrize(
        ("raw", "expected_iso"),
        [
            (1700000000, "2023-11-15T06:13:20"),
            (1700000000123, "2023-11-15T06:13:20"),
            ("1700000000", "2023-11-15T06:13:20"),
            ("2023-10-21 13:15:00", "2023-10-21T13:15:00"),
            ("2023/10/21 13:15", "2023-10-21T13:15:00"),
            ("2023-10-21T13:15:00", "2023-10-21T13:15:00"),
            ("2023-10-21", "2023-10-21T00:00:00"),
        ],
    )
    def test_to_time_pair_known_formats(self, raw: Any, expected_iso: str) -> None:
        raw_text, iso_text = parser.to_time_pair(raw)
        assert iso_text == expected_iso
        assert raw_text.startswith("2023-")

    @pytest.mark.parametrize("raw", [None, "", "   ", True, False, 0, -100, {}])
    def test_to_time_pair_returns_empty_for_unusable_input(self, raw: Any) -> None:
        assert parser.to_time_pair(raw) == ("", "")

    def test_to_time_pair_keeps_unknown_text(self) -> None:
        raw_text, iso_text = parser.to_time_pair("刚刚")
        assert raw_text == "刚刚"
        assert iso_text == ""


class TestValueHelpers:
    """图片判定与头像兜底。"""

    @pytest.mark.parametrize(
        "url",
        [
            "https://gchat.qpic.cn/gchatpic_new/1/2-3/0?term=2",
            "https://p.qpic.cn/x/a.jpg",
            "https://example.com/pic.webp",
            "https://example.com/pic.PNG?x=1",
        ],
    )
    def test_is_image_url_true(self, url: str) -> None:
        assert parser.is_image_url(url) is True

    @pytest.mark.parametrize(
        "url",
        ["https://example.com/page", "https://qfile.qq.com/download/abc", "", None, 123],
    )
    def test_is_image_url_false(self, url: Any) -> None:
        assert parser.is_image_url(url) is False

    def test_build_avatar_url_uses_digits_only(self) -> None:
        assert parser.build_avatar_url("o12345") == "https://q1.qlogo.cn/g?b=sdk&nk=12345&s=140"
        assert parser.build_avatar_url("") == ""


# --------------------------------------------------------------------------- #
# 3. 消息归一化
# --------------------------------------------------------------------------- #


class TestNormalizeMessage:
    """单条消息归一化。"""

    def test_text_message_fields(self) -> None:
        raw = {
            "msgSeq": "77",
            "msgTime": 1700000000,
            "senderUin": 10001,
            "senderName": "张三",
            "msgContent": "你好世界",
        }
        message = parser.normalize_message(raw, 3)
        assert message["id"] == "77"
        assert message["sender_uin"] == "10001"
        assert message["sender_name"] == "张三"
        assert message["time_iso"] == "2023-11-15T06:13:20"
        assert message["text"] == "你好世界"
        assert message["type"] == "text"
        assert message["raw"] is raw

    def test_avatar_fallback_generated_from_uin(self) -> None:
        message = parser.normalize_message({"senderUin": 10002, "msgContent": "x"})
        assert message["avatar"] == "https://q1.qlogo.cn/g?b=sdk&nk=10002&s=140"

    def test_sender_name_fallback_uses_uin(self) -> None:
        message = parser.normalize_message({"senderUin": "10003", "msgContent": "x"})
        assert message["sender_name"] == "QQ 10003"

    def test_sender_name_fallback_unknown(self) -> None:
        message = parser.normalize_message({"msgContent": "x"})
        assert message["sender_name"] == "未知成员"

    @pytest.mark.parametrize(
        ("raw", "expected_type"),
        [
            ({"msgContent": "只有文字"}, "text"),
            ({"picUrl": "https://gchat.qpic.cn/a.jpg"}, "image"),
            (
                {"msgContent": "看图", "picUrl": "https://gchat.qpic.cn/a.jpg"},
                "mix",
            ),
            ({"fileName": "资料.docx", "fileSize": 2048}, "file"),
            ({"msgContent": "https://example.com/only"}, "link"),
            ({"msgContent": "合并转发内容", "forwardId": "f1"}, "forward"),
            ({}, "unknown"),
        ],
    )
    def test_derive_type(self, raw: dict[str, Any], expected_type: str) -> None:
        assert parser.normalize_message(raw)["type"] == expected_type

    def test_image_message_has_no_text_but_keeps_image(self) -> None:
        message = parser.normalize_message({"picUrl": "https://gchat.qpic.cn/a.jpg"})
        assert message["text"] == ""
        assert message["images"] == ["https://gchat.qpic.cn/a.jpg"]

    def test_link_card_keeps_url_and_title(self) -> None:
        raw = {"title": "原文标题", "url": "https://example.com/a?x=1", "desc": "描述"}
        message = parser.normalize_message(raw)
        assert message["links"] == [{"url": "https://example.com/a?x=1", "title": "原文标题"}]

    def test_file_card_formats_size(self) -> None:
        raw = {"fileName": "报表.xlsx", "fileSize": 2048, "url": "https://qfile.qq.com/x"}
        message = parser.normalize_message(raw)
        assert message["type"] == "file"
        assert message["files"] == [
            {"name": "报表.xlsx", "size": "2.0 KB", "url": "https://qfile.qq.com/x"}
        ]
        # 文件链接不应同时出现在 links 里
        assert message["links"] == []

    def test_scalar_raw_is_wrapped(self) -> None:
        message = parser.normalize_message("裸字符串")
        assert message["text"] == "裸字符串"
        assert message["type"] == "text"
        assert message["raw"] == {"value": "裸字符串"}

    def test_none_raw_does_not_crash(self) -> None:
        message = parser.normalize_message(None)
        assert message["type"] == "unknown"
        assert message["text"] == ""

    def test_text_truncated_at_limit(self) -> None:
        long_text = "a" * (parser.MAX_TEXT_LENGTH + 50)
        message = parser.normalize_message({"msgContent": long_text})
        assert len(message["text"]) == parser.MAX_TEXT_LENGTH + 1
        assert message["text"].endswith("…")

    @pytest.mark.parametrize(
        "key",
        [
            "senderName",
            "nick",
            "nickname",
            "uinName",
            "sendername",
            "sendMemberName",
            "send_member_name",
            "fromName",
            "memberName",
        ],
    )
    def test_sender_name_key_variants(self, key: str) -> None:
        assert parser.normalize_message({key: "昵称"})["sender_name"] == "昵称"

    @pytest.mark.parametrize("key", ["msgContent", "content", "text", "summary", "message"])
    def test_text_key_variants(self, key: str) -> None:
        assert parser.normalize_message({key: "正文"})["text"] == "正文"

    @pytest.mark.parametrize("key", ["msgTime", "time", "sendTime", "timestamp", "date"])
    def test_time_key_variants(self, key: str) -> None:
        message = parser.normalize_message({key: 1700000000})
        assert message["time_iso"] == "2023-11-15T06:13:20"

    @pytest.mark.parametrize("key", ["msgSeq", "msgId", "seq", "id"])
    def test_id_key_variants(self, key: str) -> None:
        assert parser.normalize_message({key: "42"})["id"] == "42"

    def test_nested_structure_is_walked(self) -> None:
        raw = {"msg": {"senderName": "嵌套", "content": {"text": "深层正文"}}}
        message = parser.normalize_message(raw)
        assert message["sender_name"] == "嵌套"
        assert message["text"] == "深层正文"

    @pytest.mark.parametrize("avatar_key", ["avatar", "avatarUrl", "face", "headImg", "portrait"])
    def test_avatar_url_is_not_treated_as_content_image(self, avatar_key: str) -> None:
        """头像地址属于消息元信息，不能算作正文图片。

        否则纯文本消息会被判成 ``mix``、``image_count`` 被虚增、
        「下载图片」会把一群头像拉进快照。
        """
        raw = {
            "senderName": "张三",
            "msgContent": "这是纯文本消息",
            avatar_key: "https://q1.qlogo.cn/g?b=sdk&nk=10001&s=140",
        }
        message = parser.normalize_message(raw)
        assert message["avatar"] == "https://q1.qlogo.cn/g?b=sdk&nk=10001&s=140"
        assert message["images"] == []
        assert message["type"] == "text"


class TestSpecialCharacters:
    """特殊字符不得丢失、不得串条。"""

    def test_quotes_newlines_html_and_emoji_survive(self) -> None:
        tricky = "第一行\n第二行 <b>粗</b> <script>alert(1)</script> '单' \"双\" 😀 & \\ /"
        message = parser.normalize_message({"msgContent": tricky})
        assert message["text"] == tricky

    def test_multiple_messages_do_not_mix(self) -> None:
        messages = [
            "甲的消息",
            "乙的消息",
            "丙的消息",
        ]
        result = parser.normalize_messages(
            [{"msgContent": text, "senderName": f"用户{index}"} for index, text in enumerate(messages)]
        )
        assert [item["text"] for item in result] == messages
        assert [item["sender_name"] for item in result] == ["用户0", "用户1", "用户2"]

    def test_single_quote_text_does_not_break_json(self, page_builder: Any) -> None:
        page = page_builder(
            json.dumps({"msgList": [{"msgContent": "it's fine, don't panic"}]}, ensure_ascii=False)
        )
        result = parser.parse_page(page)
        assert result["messages"][0]["text"] == "it's fine, don't panic"


# --------------------------------------------------------------------------- #
# 4. 页面级解析与兜底
# --------------------------------------------------------------------------- #


class TestParsePage:
    """``parse_page`` 主路径与兜底路径。"""

    def test_initial_state_source(self, state_builder: Any, page_builder: Any) -> None:
        page = page_builder(
            state_builder(
                [
                    {"msgSeq": "1", "msgContent": "你好", "senderName": "张三"},
                    {"msgSeq": "2", "picUrl": "https://gchat.qpic.cn/a.jpg"},
                ]
            )
        )
        result = parser.parse_page(page)
        assert result["source"] == "initial_state"
        assert len(result["messages"]) == 2
        assert result["summary"]["msg_count"] == 2

    def test_empty_list_with_server_error(self, page_builder: Any) -> None:
        page = page_builder(
            {"msgList": [], "hasServerError": True, "isEnd": True, "pageStart": 0}
        )
        result = parser.parse_page(page)
        assert result["messages"] == []
        assert result["source"] in {"none", "initial_state"}
        assert result["summary"]["hasServerError"] is True

    def test_dom_fallback_when_state_missing(self) -> None:
        page = (
            '<html><body><div id="app"><div>header</div><div>'
            "<div><div style=\"background-image:url('https://q1.qlogo.cn/g?b=sdk&nk=10001&s=140')\"></div>"
            "<div>张三</div><div>2023-10-21 13:15:00</div>"
            "<div><span>兜底正文</span><img src='https://gchat.qpic.cn/b.jpg'/></div>"
            "</div></div></div></body></html>"
        )
        result = parser.parse_page(page)
        assert result["source"] == "dom"
        assert len(result["messages"]) == 1
        message = result["messages"][0]
        assert message["sender_name"] == "张三"
        assert "兜底正文" in message["text"]
        assert "https://gchat.qpic.cn/b.jpg" in message["images"]

    def test_no_source_when_nothing_parsable(self) -> None:
        result = parser.parse_page("<html><body>nothing here</body></html>")
        assert result["source"] == "none"
        assert result["messages"] == []

    def test_garbage_html_does_not_raise(self) -> None:
        result = parser.parse_page("<<<>>> %% not html at all")
        assert result["messages"] == []

    def test_empty_page_does_not_raise(self) -> None:
        result = parser.parse_page("")
        assert result["source"] == "none"
