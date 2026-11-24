from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.models.pack import PackServices
from app.models.task import RemoteFile, TaskError, TaskOptions, TaskStatus
from bili_pack.pack import BilibiliParser
from ffmpeg_pack.pack import MergeParser
from http_pack.pack import HttpParser
from http_pack.task import HttpTask
from quark_pack.pack import QuarkPack, QuarkParser
from quark_pack.task import QuarkTask
from telegram_pack import pack as packModule
from telegram_pack.config import telegramConfig
from telegram_pack.pack import TelegramPack, TelegramParser
from telegram_pack.task import TelegramTask
from tests.quark_pack.conftest import QuarkOrigin, buildQuarkClient


def fileMessage(messageId: int, fileName: str, *, size: int = 100, groupedId=None, text: str = "") -> dict:
    return {
        "id": messageId,
        "file": fileName,
        "size": size,
        "grouped_id": groupedId,
        "date": 0,
        "text": text,
        "raw": {
            "ID": messageId,
            "Media": {"Document": {"Size": size, "Attributes": [{"FileName": fileName}]}},
        },
    }


class FakeTdlRunner:
    """只响应探查的 tdl 替身，绝不启动真实进程。"""

    def __init__(self, payload: dict | None = None, *, isBusy: bool = False, error=None) -> None:
        self.payload = payload if payload is not None else {"id": "c1", "messages": []}
        self._isBusy = isBusy
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.runtime = SimpleNamespace(path=lambda: "/fake/tdl")

    @property
    def isBusy(self) -> bool:
        return self._isBusy

    async def exportMessages(self, chatId: str, **kwargs) -> dict:
        self.calls.append((chatId, kwargs))
        if self.error is not None:
            raise self.error
        return self.payload


def buildTelegramParser(payload: dict, **kwargs) -> TelegramParser:
    pack = TelegramPack(SimpleNamespace())
    pack.runner = FakeTdlRunner(payload, **kwargs)
    return TelegramParser(pack)


@pytest.fixture
async def quarkOrigin():
    origin = QuarkOrigin()
    app = web.Application()
    app.router.add_get("/{path:.*}", origin.handle)
    app.router.add_post("/{path:.*}", origin.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    origin.driveBase = f"{base}/drive"
    origin.panBase = f"{base}/pan"
    origin.authBase = f"{base}/uop"
    try:
        yield origin
    finally:
        await runner.cleanup()


def test_parser_priority_orders_between_bili_and_quark():
    priority = TelegramParser.priority

    assert priority == 55
    assert BilibiliParser.priority < priority < MergeParser.priority < QuarkParser.priority < HttpParser.priority


def test_match_accepts_telegram_links_and_rejects_others():
    parser = TelegramParser()

    assert parser.match(TaskOptions(url="https://t.me/example_channel/123"))
    assert parser.match(TaskOptions(url="https://t.me/c/123456789/42"))
    assert parser.match(TaskOptions(url="https://t.me/example_channel/45/678"))
    assert not parser.match(TaskOptions(url="https://pan.quark.cn/s/shareid01"))
    assert not parser.match(TaskOptions(url="https://example.com/file.zip"))


def test_match_passive_uses_same_telegram_links():
    parser = TelegramParser()

    assert parser.matchPassive(TaskOptions(url="https://t.me/example_channel/123"))
    assert parser.matchPassive(TaskOptions(url="t.me/c/123456789/42?single"))
    assert not parser.matchPassive(TaskOptions(url="https://pan.quark.cn/s/shareid01"))
    assert not parser.matchPassive(TaskOptions(url="https://example.com/file.zip"))


async def test_parse_exports_group_files_for_matched_message(downloadFolder):
    payload = {
        "id": "c1",
        "messages": [
            fileMessage(10, "a.mp4", groupedId=900),
            fileMessage(11, "b.mp4", groupedId=900),
            fileMessage(12, "c.mp4", groupedId=900),
        ],
    }
    parser = buildTelegramParser(payload)

    task = await parser.parse(TaskOptions(url="https://t.me/example_channel/11", outputFolder=downloadFolder))

    assert isinstance(task, TelegramTask)
    assert task.sourceMode == "export"
    assert task.chatId == "c1"
    assert [file.messageId for file in task.files] == [10, 11, 12]
    assert parser.pack.runner.calls == [("example_channel", {"idRange": (1, 21)})]


async def test_parse_preserves_numeric_chat_id_and_falls_back_to_link_chat(downloadFolder):
    numericParser = buildTelegramParser({
        "id": 1234567890,
        "messages": [fileMessage(11, "clip.mp4")],
    })
    numericTask = await numericParser.parse(
        TaskOptions(url="https://t.me/example_channel/11", outputFolder=downloadFolder)
    )
    assert numericTask.chatId == "1234567890"

    emptyIdParser = buildTelegramParser({
        "messages": [fileMessage(11, "clip.mp4")],
    })
    fallbackTask = await emptyIdParser.parse(
        TaskOptions(url="https://t.me/example_channel/11", outputFolder=downloadFolder)
    )
    assert fallbackTask.chatId == "example_channel"


async def test_parse_single_query_keeps_only_the_linked_message(downloadFolder):
    payload = {
        "id": "c1",
        "messages": [
            fileMessage(10, "a.mp4", groupedId=900),
            fileMessage(11, "b.mp4", groupedId=900),
            fileMessage(12, "c.mp4", groupedId=900),
        ],
    }
    parser = buildTelegramParser(payload)

    task = await parser.parse(
        TaskOptions(url="https://t.me/example_channel/11?single", outputFolder=downloadFolder)
    )

    assert task.sourceMode == "export"
    assert [file.messageId for file in task.files] == [11]


async def test_parse_falls_back_to_url_when_runner_is_busy(downloadFolder):
    parser = buildTelegramParser({"id": "c1", "messages": []}, isBusy=True)

    task = await parser.parse(TaskOptions(url="https://t.me/example_channel/11", outputFolder=downloadFolder))

    assert task.sourceMode == "url"
    assert task.name == "tg_example_channel_11"
    assert task.url == "https://t.me/example_channel/11"
    assert task.runner is parser.pack.runner
    assert parser.pack.runner.calls == []


async def test_parse_falls_back_to_url_when_export_fails(downloadFolder):
    parser = buildTelegramParser({"id": "c1", "messages": []}, error=TaskError("锁忙"))

    task = await parser.parse(TaskOptions(url="https://t.me/example_channel/11", outputFolder=downloadFolder))

    assert task.sourceMode == "url"
    assert task.name == "tg_example_channel_11"
    assert parser.pack.runner.calls


async def test_feature_service_dispatches_telegram_quark_and_http(
    engine, httpPack, quarkOrigin, downloadFolder,
):
    payload = {
        "id": "c1",
        "messages": [
            fileMessage(10, "a.mp4", groupedId=900),
            fileMessage(11, "b.mp4", groupedId=900),
            fileMessage(12, "c.mp4", groupedId=900),
        ],
    }
    telegramPack = TelegramPack(PackServices(engine.coroutineRunner, engine.speedMeter))
    telegramPack.runner = FakeTdlRunner(payload)
    quarkPack = QuarkPack(PackServices(engine.coroutineRunner, engine.speedMeter))
    quarkPack.client = buildQuarkClient(quarkOrigin)
    engine.featureService.register(telegramPack)
    engine.featureService.register(quarkPack)

    telegramTask = await engine.featureService.parse(TaskOptions(
        url="https://t.me/example_channel/11",
        outputFolder=downloadFolder,
    ))
    quarkTask = await engine.featureService.parse(TaskOptions(
        url="https://pan.quark.cn/s/pwd00001?pwd=ab12",
        outputFolder=downloadFolder,
    ))
    httpTask = await engine.featureService.parse(TaskOptions(
        url="https://example.com/file.zip",
        outputFolder=downloadFolder,
        file=RemoteFile(name="file.zip", fileSize=3, canUseRangeRequests=True),
    ))

    assert isinstance(telegramTask, TelegramTask)
    assert telegramTask.sourceMode == "export"
    assert isinstance(quarkTask, QuarkTask)
    assert quarkTask.pwdId == "pwd00001"
    assert isinstance(httpTask, HttpTask)
    assert httpTask.packId == "http"
    assert (
        telegramPack.parsers[0].priority
        < quarkPack.parsers[0].priority
        < httpPack.parsers[0].priority
    )


async def test_pack_inbox_workflow_and_completion_hook(config, downloadFolder, monkeypatch):
    posted: list[tuple] = []

    class FakeCoroutineRunner:
        def post(self, callback, *args, **kwargs) -> None:
            posted.append((callback, args, kwargs))
            callback(*args, **kwargs)

        def submit(self, work, done=None, failed=None, *args, owner=None, **kwargs) -> str:
            return "w1"

        def cancel(self, workId: str, finished=None) -> bool:
            return False

    services = SimpleNamespace(coroutineRunner=FakeCoroutineRunner())
    pack = TelegramPack(services)
    assert telegramConfig._account is pack.account

    payload = {
        "id": -1001,
        "messages": [
            fileMessage(10, "a.mp4", size=100, groupedId=900),
            fileMessage(11, "b.mp4", size=200, groupedId=900),
        ],
    }
    pack.runner = FakeTdlRunner(payload)
    addedTasks: list[TelegramTask] = []
    pack.addTask = lambda task: addedTasks.append(task) or True
    changes: list[int] = []
    pack.inboxChanged.connect(lambda: changes.append(len(pack.inbox.bundles)))

    pack.bindInboxChannel("-1001", "监听频道")
    assert telegramConfig.inboxChatId.value == "-1001"
    assert telegramConfig.inboxChatTitle.value == "监听频道"

    bundles = await pack.fetchInboxUpdates("-1001", sinceMsgId=0, limit=20)
    assert len(bundles) == 1
    assert pack.runner.calls == [("-1001", {"sinceMsgId": 0, "limit": 20})]

    added = pack.applyInboxUpdates(bundles)
    assert len(added) == 1
    key = added[0].bundleKey
    assert pack.inbox.bundleByKey(key).fileSizes == [100, 200]

    task = pack.enqueueInboxBundle(key, downloadFolder)
    assert task is not None
    assert len(addedTasks) == 1
    assert task.chatId == "-1001"
    assert task.groupedId == "900"
    assert [file.size for file in task.files] == [100, 200]
    assert pack.inbox.bundleByKey(key).status == "queued"

    # 模拟下载完成触发钩子回写收件箱状态
    async def fakeDownload(spec, onProgress=None):
        (spec.outputFolder / "10_a.mp4").write_bytes(b"a" * 100)
        (spec.outputFolder / "11_b.mp4").write_bytes(b"b" * 200)

    task.runner = SimpleNamespace(download=fakeDownload)
    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, lambda: asyncio.sleep(0))

    assert task.status == TaskStatus.COMPLETED
    assert pack.inbox.bundleByKey(key).status == "completed"
    assert pack.setInboxBundleStatus(key, "skipped") is True
    assert pack.inbox.bundleByKey(key).status == "skipped"
    assert pack.enqueueInboxBundle("missing", downloadFolder) is None

    # 重建 pack 时自动从磁盘与配置恢复频道与列表
    reloaded = TelegramPack(SimpleNamespace())
    assert reloaded.inbox.chatId == "-1001"
    assert reloaded.inbox.title == "监听频道"
    assert reloaded.inbox.bundleByKey(key).status == "skipped"

    # activate 仅在 tdl 已安装时触发 refreshAuth
    refreshed: list[bool] = []
    monkeypatch.setattr(pack.account, "refreshAuth", lambda: refreshed.append(True))
    monkeypatch.setattr(packModule.tdlRuntime, "path", lambda: "")
    await pack.activate()
    assert refreshed == []

    monkeypatch.setattr(packModule.tdlRuntime, "path", lambda: "/fake/tdl")
    await pack.activate()
    assert refreshed == [True]
    await pack.deactivate()
    assert TelegramTask._onCompleted is None
