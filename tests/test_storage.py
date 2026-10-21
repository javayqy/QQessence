"""存储层测试：快照创建、索引维护、读取、删除与路径穿越防护。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import storage


def _sample_messages() -> list[dict[str, Any]]:
    """构造两条典型消息。"""
    return [
        {
            "id": "1",
            "sender_uin": "10001",
            "sender_name": "张三",
            "text": "文字消息",
            "images": [],
            "links": [],
            "files": [],
            "type": "text",
        },
        {
            "id": "2",
            "sender_uin": "10002",
            "sender_name": "李四",
            "text": "",
            "images": ["https://gchat.qpic.cn/a.jpg", "https://gchat.qpic.cn/b.jpg"],
            "links": [],
            "files": [],
            "type": "image",
        },
    ]


# --------------------------------------------------------------------------- #
# 快照创建
# --------------------------------------------------------------------------- #


class TestCreateSnapshot:
    """快照落盘与 meta 字段。"""

    def test_creates_directory_and_files(self, isolated_data_dir: Path) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        snapshot_dir = storage.SNAPSHOTS_DIR / meta["id"]
        assert snapshot_dir.is_dir()
        assert (snapshot_dir / "messages.json").is_file()
        assert (snapshot_dir / "meta.json").is_file()
        assert (snapshot_dir / "images").is_dir()

    def test_meta_fields(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages(), {"pages": 2})
        assert storage.is_valid_snapshot_id(meta["id"])
        assert meta["id"].startswith("123456789_")
        assert meta["gc"] == "123456789"
        assert meta["count"] == 2
        assert meta["image_count"] == 2
        assert meta["has_images"] is True
        assert meta["downloaded_images"] == 0
        assert meta["pages"] == 2
        assert meta["created_at_display"]

    def test_messages_roundtrip(self) -> None:
        messages = _sample_messages()
        meta = storage.create_snapshot("123456789", messages)
        loaded = storage.load_snapshot(meta["id"])
        assert loaded["messages"] == messages

    def test_empty_messages_allowed(self) -> None:
        meta = storage.create_snapshot("123456789", [])
        assert meta["count"] == 0
        assert meta["has_images"] is False

    def test_snapshot_ids_are_unique(self) -> None:
        first = storage.create_snapshot("123456789", [])
        second = storage.create_snapshot("123456789", [])
        assert first["id"] != second["id"]

    def test_index_written_with_entry(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        entries = storage.list_snapshots()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == meta["id"]
        assert entry["gc"] == "123456789"
        assert entry["count"] == 2
        assert entry["image_count"] == 2
        assert entry["has_images"] is True


class TestListSnapshots:
    """索引读取与排序。"""

    def test_sorted_by_created_at_desc(self, isolated_data_dir: Path) -> None:
        entries = [
            {"id": "111_20240101-000000", "gc": "111", "created_at": "2024-01-01T00:00:00"},
            {"id": "333_20240103-000000", "gc": "333", "created_at": "2024-01-03T00:00:00"},
            {"id": "222_20240102-000000", "gc": "222", "created_at": "2024-01-02T00:00:00"},
        ]
        storage.INDEX_PATH.write_text(json.dumps(entries), encoding="utf-8")
        ordered = [item["id"] for item in storage.list_snapshots()]
        assert ordered == [
            "333_20240103-000000",
            "222_20240102-000000",
            "111_20240101-000000",
        ]

    def test_entries_without_id_are_ignored(self) -> None:
        storage.INDEX_PATH.write_text(
            json.dumps([{"id": "111_20240101-000000"}, {"gc": "no-id"}, "junk"]),
            encoding="utf-8",
        )
        assert [item["id"] for item in storage.list_snapshots()] == ["111_20240101-000000"]

    @pytest.mark.parametrize("payload", ["not-json", "{}", "[]"])
    def test_broken_index_does_not_raise(self, payload: str) -> None:
        storage.INDEX_PATH.write_text(payload, encoding="utf-8")
        assert storage.list_snapshots() == []


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


class TestLoadSnapshot:
    """快照读取与兜底。"""

    def test_missing_snapshot_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            storage.load_snapshot("999999999_20240101-000000")

    @pytest.mark.parametrize("bad_id", ["../config", "..", "abc", "", "123/456", "1_2/3"])
    def test_invalid_id_raises_value_error(self, bad_id: str) -> None:
        with pytest.raises(ValueError):
            storage.load_snapshot(bad_id)

    def test_defaults_filled_for_handmade_directory(self) -> None:
        snapshot_dir = storage.SNAPSHOTS_DIR / "123456789_20240101-000000"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "messages.json").write_text(json.dumps([{"text": "x"}]), encoding="utf-8")

        detail = storage.load_snapshot("123456789_20240101-000000")
        assert detail["meta"]["id"] == "123456789_20240101-000000"
        assert detail["meta"]["gc"] == "123456789"
        assert detail["meta"]["count"] == 0
        assert detail["messages"] == [{"text": "x"}]

    def test_broken_messages_file_yields_empty_list(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        (storage.SNAPSHOTS_DIR / meta["id"] / "messages.json").write_text("{oops", encoding="utf-8")
        assert storage.load_snapshot(meta["id"])["messages"] == []


# --------------------------------------------------------------------------- #
# ID 校验与路径穿越
# --------------------------------------------------------------------------- #


class TestSnapshotIdValidation:
    """快照 ID 白名单与目录穿越防护。"""

    @pytest.mark.parametrize(
        "snapshot_id",
        ["123456789_20240101-000000", "1_1", "123456789_20240101-000000-1"],
    )
    def test_valid_ids(self, snapshot_id: str) -> None:
        assert storage.is_valid_snapshot_id(snapshot_id) is True

    @pytest.mark.parametrize(
        "snapshot_id",
        [
            "",
            "..",
            "../config",
            "..\\config",
            "123456789_20240101-000000/../../etc",
            "abc_def",
            "123456789_20240101-000000\x00",
            "123456789_20240101-000000\n",
            "/abs/path_20240101-000000",
            None,
            12345,
        ],
    )
    def test_invalid_ids(self, snapshot_id: Any) -> None:
        assert storage.is_valid_snapshot_id(snapshot_id) is False

    @pytest.mark.parametrize("bad_id", ["../config", "..\\config", "abc"])
    def test_snapshot_dir_rejects_traversal(self, bad_id: str) -> None:
        with pytest.raises(ValueError):
            storage.snapshot_dir(bad_id)

    def test_images_dir_is_created(self) -> None:
        meta = storage.create_snapshot("123456789", [])
        directory = storage.images_dir(meta["id"])
        assert directory.is_dir()
        assert directory.parent == storage.snapshot_dir(meta["id"])

    def test_images_dir_rejects_traversal(self) -> None:
        with pytest.raises(ValueError):
            storage.images_dir("../../etc")


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


class TestDeleteSnapshot:
    """删除快照（含图片目录）与索引同步。"""

    def test_removes_directory_images_and_index(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        images = storage.images_dir(meta["id"])
        (images / "abc.jpg").write_bytes(b"fake-image")

        assert storage.delete_snapshot(meta["id"]) is True
        assert not (storage.SNAPSHOTS_DIR / meta["id"]).exists()
        assert storage.list_snapshots() == []

    def test_delete_twice_returns_false(self) -> None:
        meta = storage.create_snapshot("123456789", [])
        assert storage.delete_snapshot(meta["id"]) is True
        assert storage.delete_snapshot(meta["id"]) is False

    def test_delete_keeps_other_snapshots(self) -> None:
        first = storage.create_snapshot("123456789", [])
        second = storage.create_snapshot("987654321", [])
        storage.delete_snapshot(first["id"])
        assert [item["id"] for item in storage.list_snapshots()] == [second["id"]]

    @pytest.mark.parametrize("bad_id", ["../config", "abc"])
    def test_delete_rejects_bad_id(self, bad_id: str) -> None:
        with pytest.raises(ValueError):
            storage.delete_snapshot(bad_id)


# --------------------------------------------------------------------------- #
# 图片映射与 meta 更新
# --------------------------------------------------------------------------- #


class TestImageMapAndMeta:
    """图片映射表与 meta 更新。"""

    def test_merge_image_map_exposed_as_media_urls(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        storage.merge_image_map(meta["id"], {"https://gchat.qpic.cn/a.jpg": "deadbeef.jpg"})
        detail = storage.load_snapshot(meta["id"])
        assert detail["meta"]["local_images"] == {
            "https://gchat.qpic.cn/a.jpg": f"/media/{meta['id']}/deadbeef.jpg"
        }

    def test_merge_image_map_is_incremental(self) -> None:
        meta = storage.create_snapshot("123456789", [])
        storage.save_image_map(meta["id"], {"u1": "a.jpg"})
        merged = storage.merge_image_map(meta["id"], {"u2": "b.jpg"})
        assert merged == {"u1": "a.jpg", "u2": "b.jpg"}

    def test_update_meta_syncs_index(self) -> None:
        meta = storage.create_snapshot("123456789", _sample_messages())
        storage.update_meta(meta["id"], {"downloaded_images": 2})
        entry = storage.list_snapshots()[0]
        assert entry["downloaded_images"] == 2
        assert storage.load_snapshot(meta["id"])["meta"]["downloaded_images"] == 2

    def test_update_meta_missing_snapshot_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            storage.update_meta("999999999_20240101-000000", {"downloaded_images": 1})

    def test_directory_size_counts_files(self) -> None:
        meta = storage.create_snapshot("123456789", [])
        images = storage.images_dir(meta["id"])
        (images / "x.bin").write_bytes(b"0" * 100)
        assert storage.directory_size(images) == 100
        assert storage.directory_size(storage.SNAPSHOTS_DIR / "does-not-exist") == 0


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestConfig:
    """Cookie 配置的读写与清理。"""

    def test_default_config_when_missing(self) -> None:
        config = storage.load_config()
        assert config == {"cookie": "", "uin": "", "updated_at": ""}

    def test_save_and_load(self) -> None:
        storage.save_config("skey=abc; uin=o123", "123")
        config = storage.load_config()
        assert config["cookie"] == "skey=abc; uin=o123"
        assert config["uin"] == "123"
        assert config["updated_at"]

    def test_clear_config(self) -> None:
        storage.save_config("skey=abc", "123")
        storage.clear_config()
        config = storage.load_config()
        assert config["cookie"] == ""
        assert config["uin"] == ""

    def test_broken_config_file_falls_back(self, isolated_data_dir: Path) -> None:
        storage.CONFIG_PATH.write_text("{broken", encoding="utf-8")
        assert storage.load_config()["cookie"] == ""

    def test_config_file_is_utf8_json(self) -> None:
        storage.save_config("skey=中文; uin=o123", "123")
        raw = storage.CONFIG_PATH.read_text(encoding="utf-8")
        assert "中文" in raw
        assert json.loads(raw)["uin"] == "123"
