from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_pack.inbox import TelegramInbox
from telegram_pack.parse import ExportedMediaItem, MediaBundle


def buildItem(messageId: int, fileName: str = "video.mp4", size: int = 1024) -> ExportedMediaItem:
    return ExportedMediaItem(
        messageId=messageId,
        fileName=fileName,
        size=size,
        groupedId="",
        date=1_700_000_000 + messageId,
        text="",
        rawMessage={
            "id": messageId,
            "message": "",
            "media": {"document": {"attributes": [{"file_name": fileName}]}},
        },
    )


def buildBundle(
    firstMessageId: int,
    lastMessageId: int,
    fileName: str = "video.mp4",
    *,
    groupedId: str = "",
) -> MediaBundle:
    items = tuple(
        buildItem(messageId, fileName, size=1000 + messageId)
        for messageId in range(firstMessageId, lastMessageId + 1)
    )
    key = f"100:g:{groupedId}" if groupedId else f"100:m:{firstMessageId}"
    return MediaBundle(
        bundleKey=key,
        chatId="100",
        groupedId=groupedId,
        firstMessageId=firstMessageId,
        lastMessageId=lastMessageId,
        date=1_700_000_000 + firstMessageId,
        caption="",
        items=items,
        totalSize=sum(item.size for item in items),
    )


def test_save_then_load_keeps_state_and_raw_messages(tmp_path: Path):
    path = tmp_path / "telegram_inbox.json"
    inbox = TelegramInbox(path)
    inbox.setChannel("-1001", "私密频道")
    inbox.mergeBundles([buildBundle(10, 12, groupedId="900"), buildBundle(13, 15)])
    inbox.markStatus("100:g:900", "queued")
    inbox.save()

    assert sorted(item.name for item in tmp_path.iterdir()) == ["telegram_inbox.json"]

    loaded = TelegramInbox(path)
    loaded.load()

    assert loaded.chatId == "-1001"
    assert loaded.title == "私密频道"
    assert loaded.lastMsgId == 15
    assert [bundle.bundleKey for bundle in loaded.bundles] == ["100:g:900", "100:m:13"]
    assert [bundle.status for bundle in loaded.bundles] == ["queued", "new"]
    first = loaded.bundleByKey("100:g:900")
    assert first.messageIds == [10, 11, 12]
    assert first.fileSizes == [1010, 1011, 1012]
    assert first.groupedId == "900"
    assert first.rawMessages[0]["id"] == 10


def test_load_legacy_json_without_file_sizes_and_grouped_id(tmp_path: Path):
    path = tmp_path / "telegram_inbox.json"
    path.write_text(json.dumps({
        "chatId": "-1001",
        "title": "旧版频道",
        "lastMsgId": 10,
        "bundles": [{
            "bundleKey": "100:m:10",
            "firstMessageId": 10,
            "lastMessageId": 10,
            "date": 1_700_000_010,
            "caption": "旧数据",
            "fileNames": ["clip.mp4"],
            "totalSize": 4096,
            "status": "new",
            "messageIds": [10],
            "rawMessages": [{"id": 10}],
        }],
    }), encoding="utf-8")

    inbox = TelegramInbox(path)
    inbox.load()

    bundle = inbox.bundleByKey("100:m:10")
    assert bundle is not None
    assert bundle.fileSizes == []
    assert bundle.groupedId == ""
    assert bundle.totalSize == 4096


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"bundles": 3}), json.dumps([1, 2])],
)
def test_load_broken_json_clears_state(tmp_path: Path, content: str):
    path = tmp_path / "telegram_inbox.json"
    path.write_text(content, encoding="utf-8")
    inbox = TelegramInbox(path)
    inbox.setChannel("-1001", "旧频道")

    inbox.load()

    assert inbox.chatId == ""
    assert inbox.lastMsgId == 0
    assert inbox.bundles == []


def test_merge_sets_high_watermark(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")

    added = inbox.mergeBundles([buildBundle(10, 20)])

    assert inbox.lastMsgId == 20
    assert [bundle.bundleKey for bundle in added] == ["100:m:10"]
    assert [bundle.status for bundle in added] == ["new"]
    assert inbox.pendingBundles() == added


def test_merge_keeps_marked_status_and_adds_new(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")
    inbox.mergeBundles([buildBundle(10, 14), buildBundle(15, 20)])
    inbox.markStatus("100:m:10", "queued")
    inbox.markStatus("100:m:15", "completed")

    added = inbox.mergeBundles([buildBundle(15, 20), buildBundle(21, 25), buildBundle(10, 14)])

    assert inbox.lastMsgId == 25
    assert [bundle.bundleKey for bundle in added] == ["100:m:21"]
    assert inbox.bundleByKey("100:m:10").status == "queued"
    assert inbox.bundleByKey("100:m:15").status == "completed"
    assert [bundle.bundleKey for bundle in inbox.pendingBundles()] == ["100:m:21"]


def test_merge_lower_watermark_keeps_last_msg_id(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")
    inbox.mergeBundles([buildBundle(20, 30)])

    added = inbox.mergeBundles([buildBundle(10, 15)])

    assert inbox.lastMsgId == 30
    assert [bundle.bundleKey for bundle in added] == ["100:m:10"]
    assert [bundle.bundleKey for bundle in inbox.bundles] == ["100:m:10", "100:m:20"]


def test_set_channel_resets_state_on_channel_change(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")
    inbox.setChannel("-1001", "旧频道")
    inbox.mergeBundles([buildBundle(10, 20)])

    inbox.setChannel("-1002", "新频道")

    assert inbox.chatId == "-1002"
    assert inbox.title == "新频道"
    assert inbox.lastMsgId == 0
    assert inbox.bundles == []


def test_set_channel_keeps_state_on_same_channel(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")
    inbox.setChannel("-1001", "旧标题")
    inbox.mergeBundles([buildBundle(10, 20)])

    inbox.setChannel("-1001", "新标题")

    assert inbox.title == "新标题"
    assert inbox.lastMsgId == 20
    assert [bundle.bundleKey for bundle in inbox.bundles] == ["100:m:10"]


def test_mark_status_rejects_unknown_key_or_status(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "telegram_inbox.json")
    inbox.mergeBundles([buildBundle(10, 12)])

    assert inbox.markStatus("100:m:10", "unknown") is False
    assert inbox.markStatus("missing", "new") is False
    assert inbox.bundleByKey("100:m:10").status == "new"
