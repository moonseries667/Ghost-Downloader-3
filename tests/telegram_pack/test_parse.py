import json

import pytest

from telegram_pack.parse import (
    buildBundleName,
    buildExportPayload,
    groupExportedMessages,
    parseTelegramUrl,
    toCanonicalTelegramUrl,
)


def fileMessage(messageId, fileName, *, size=100, groupedId=None, text="", date=0):
    return {
        "id": messageId,
        "file": fileName,
        "size": size,
        "grouped_id": groupedId,
        "date": date,
        "text": text,
        "raw": {
            "ID": messageId,
            "Media": {"Document": {"Size": size, "Attributes": [{"FileName": fileName}]}},
        },
    }


def test_parse_public_message_link():
    link = parseTelegramUrl("https://t.me/example_channel/123")

    assert link.kind == "public"
    assert link.chat == "example_channel"
    assert link.messageId == 123
    assert link.topicId is None
    assert not link.isSingle
    assert link.url == "https://t.me/example_channel/123"


def test_parse_public_topic_link():
    link = parseTelegramUrl("https://t.me/example_channel/45/678")

    assert link.kind == "topic"
    assert link.chat == "example_channel"
    assert link.topicId == 45
    assert link.messageId == 678
    assert link.url == "https://t.me/example_channel/45/678"


def test_parse_private_message_link():
    link = parseTelegramUrl("https://t.me/c/123456789/42")

    assert link.kind == "private"
    assert link.chat == "123456789"
    assert link.topicId is None
    assert link.messageId == 42
    assert link.url == "https://t.me/c/123456789/42"


def test_parse_private_topic_link():
    link = parseTelegramUrl("t.me/c/123456789/7/42?single")

    assert link.kind == "private"
    assert link.chat == "123456789"
    assert link.topicId == 7
    assert link.messageId == 42
    assert link.isSingle
    assert link.url == "https://t.me/c/123456789/7/42?single"


@pytest.mark.parametrize(
    "url",
    [
        "http://telegram.me/example_channel/9?foo=bar",
        "www.t.me/example_channel/9",
        "t.me/example_channel/9",
    ],
)
def test_parse_accepts_host_and_scheme_variants(url):
    link = parseTelegramUrl(url)

    assert link.kind == "public"
    assert link.chat == "example_channel"
    assert link.messageId == 9
    assert not link.isSingle
    assert link.url == "https://t.me/example_channel/9"


def test_parse_single_query_sets_flag():
    link = parseTelegramUrl("https://t.me/example_channel/9?single")

    assert link.isSingle
    assert toCanonicalTelegramUrl(link) == "https://t.me/example_channel/9?single"


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/+abcdef",
        "https://t.me/joinchat/abcdef",
        "https://t.me/example_channel",
        "https://t.me/example_channel/abc",
        "https://t.me/example_channel/0",
        "https://t.me/s/example_channel/12",
        "https://t.me/c/abc/12",
        "https://t.me/c/123456789",
        "https://example.com/example_channel/1",
        "ftp://t.me/example_channel/1",
        "",
    ],
)
def test_parse_rejects_invalid_links(url):
    assert parseTelegramUrl(url) is None


def test_group_filters_text_only_messages():
    payload = {"id": 123, "messages": [{"id": 5, "text": "hello", "raw": {"Message": "hello"}}]}

    assert groupExportedMessages(payload) == []


def test_group_merges_grouped_id_videos_and_audios():
    messages = [
        fileMessage(12, "b.mp4", size=200, groupedId=900, date=120),
        fileMessage(10, "a.mp4", size=100, groupedId=900, text="Album caption\nline", date=100),
        fileMessage(11, "c.mp3", size=300, groupedId=900, date=0),
    ]

    bundles = groupExportedMessages({"id": "-1001234", "messages": messages})

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.bundleKey == "-1001234:g:900"
    assert bundle.chatId == "-1001234"
    assert [item.messageId for item in bundle.items] == [10, 11, 12]
    assert bundle.firstMessageId == 10
    assert bundle.lastMessageId == 12
    assert bundle.totalSize == 600
    assert bundle.date == 100
    assert bundle.caption == "Album caption\nline"


def test_group_merges_raw_grouped_id():
    first = fileMessage(2, "one.mp4", groupedId=None)
    first["raw"]["GroupedID"] = 555
    second = fileMessage(1, "two.mp4", groupedId=None)
    second["raw"]["GroupedID"] = "555"

    bundles = groupExportedMessages({"id": 7, "messages": [first, second]})

    assert len(bundles) == 1
    assert bundles[0].bundleKey == "7:g:555"
    assert [item.messageId for item in bundles[0].items] == [1, 2]


def test_group_renames_duplicate_files():
    messages = [
        fileMessage(1, "video.mp4", groupedId=7),
        fileMessage(2, "video.mp4", groupedId=7),
        fileMessage(3, "video.mp4", groupedId=7),
    ]

    bundle = groupExportedMessages(messages)[0]

    assert [item.fileName for item in bundle.items] == [
        "video.mp4",
        "video (2).mp4",
        "video (3).mp4",
    ]


def test_group_cleans_invalid_filename_characters():
    bundle = groupExportedMessages([fileMessage(1, 'a/b:c?.mp4', groupedId=7)])[0]

    assert bundle.items[0].fileName == "a_b_c_.mp4"


def test_group_splits_zero_and_missing_grouped_id():
    messages = [fileMessage(5, "solo.txt", groupedId=0), fileMessage(6, "none.txt")]

    bundles = groupExportedMessages(messages)

    assert [bundle.bundleKey for bundle in bundles] == [":m:5", ":m:6"]


def test_group_orders_bundles_by_first_message_id():
    messages = [fileMessage(9, "later.txt"), fileMessage(3, "earlier.txt")]

    bundles = groupExportedMessages({"id": 1, "messages": messages})

    assert [bundle.firstMessageId for bundle in bundles] == [3, 9]


def test_group_skips_bad_items():
    payload = [None, "not a message", {"id": "abc"}, {"id": 3, "text": "plain"}]

    assert groupExportedMessages(payload) == []


def test_group_falls_back_to_message_id_for_unnamed_media():
    payload = [
        {
            "id": 42,
            "raw": {"Media": {"Document": {"Size": 5, "Attributes": [{"_": "DocumentAttributeVideo"}]}}},
        }
    ]

    bundle = groupExportedMessages(payload)[0]

    assert bundle.items[0].fileName == "42.mp4"
    assert bundle.items[0].size == 5


def test_bundle_name_returns_single_file_name():
    bundle = groupExportedMessages([fileMessage(1, "movie.mkv")])[0]

    assert buildBundleName(bundle) == "movie.mkv"


def test_bundle_name_prefers_caption():
    bundle = groupExportedMessages(
        [
            fileMessage(10, "a.mp4", groupedId=1, text="  My\n  Album  "),
            fileMessage(11, "b.mp4", groupedId=1, text="other"),
        ]
    )[0]

    assert buildBundleName(bundle) == "My Album"


def test_bundle_name_truncates_caption():
    caption = "word " * 20
    bundle = groupExportedMessages(
        [
            fileMessage(10, "a.mp4", groupedId=1, text=caption),
            fileMessage(11, "b.mp4", groupedId=1),
        ]
    )[0]

    name = buildBundleName(bundle)

    assert len(name) <= 48
    assert name == name.rstrip()


def test_bundle_name_falls_back_to_message_id():
    withChat = groupExportedMessages(
        {"id": 123, "messages": [fileMessage(10, "a.mp4", groupedId=1), fileMessage(11, "b.mp4", groupedId=1)]}
    )[0]
    withoutChat = groupExportedMessages(
        [fileMessage(10, "a.mp4", groupedId=1), fileMessage(11, "b.mp4", groupedId=1)]
    )[0]

    assert buildBundleName(withChat) == "tg_123_10"
    assert buildBundleName(withoutChat) == "tg_10"


def test_export_payload_round_trips_json():
    items = [item for bundle in groupExportedMessages([fileMessage(10, "a.mp4", groupedId=1)]) for item in bundle.items]

    payload = buildExportPayload("123456", items)
    restored = json.loads(json.dumps(payload))

    assert payload["id"] == 123456
    assert [message["id"] for message in restored["messages"]] == [10]
    assert restored["messages"][0]["file"] == "a.mp4"


def test_export_payload_keeps_text_chat_id():
    payload = buildExportPayload("me", [])

    assert payload == {"id": "me", "messages": []}
