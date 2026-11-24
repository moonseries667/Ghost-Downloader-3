from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import qfluentwidgets
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
    FluentIcon,
    MessageBox,
    RangeSettingCard,
    SwitchSettingCard,
    Theme,
)

from app.models.task import TaskError, TaskOptions
from app.services.coroutine_runner import CoroutineRunner
from app.view.components.scroll_area import ScrollArea
from app.view.components.setting_cards import LineEditSettingCard, SelectFolderSettingCard
from telegram_pack.account import TelegramAccount
from telegram_pack.config import telegramConfig
from telegram_pack.inbox import TelegramInbox
from telegram_pack.pack import TelegramPack
from telegram_pack.page import InboxBundleCard, TelegramChannelDialog, TelegramPage
from telegram_pack.parse import groupExportedMessages
from telegram_pack.runtime import TelegramChat
from telegram_pack.setting_cards import (
    TelegramDesktopLoginDialog,
    TelegramLoginSettingCard,
    TelegramScanLoginDialog,
    toQrMatrixPixmap,
)
from telegram_pack.task import TelegramTask


def sampleQrMatrix(size: int = 21) -> tuple[tuple[bool, ...], ...]:
    return tuple(
        tuple(
            False if (r < 2 or r >= size - 2 or c < 2 or c >= size - 2) else ((r + c) % 2 == 0)
            for c in range(size)
        )
        for r in range(size)
    )


def fileMessage(messageId: int, fileName: str, *, size: int = 100, groupedId=None, text: str = "") -> dict:
    return {
        "id": messageId,
        "file": fileName,
        "size": size,
        "grouped_id": groupedId,
        "date": 1700000000 + messageId,
        "text": text,
        "raw": {
            "ID": messageId,
            "Media": {"Document": {"Size": size, "Attributes": [{"FileName": fileName}]}},
        },
    }


async def waitUi(predicate, timeout: float = 8.0) -> None:
    from PySide6.QtWidgets import QApplication
    for _ in range(int(timeout / 0.02)):
        QApplication.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


class FakeUiRunner:
    def __init__(
        self,
        *,
        installed: bool = True,
        authorized: bool = False,
        chats: list[TelegramChat] | None = None,
        exportPayload: dict | None = None,
        isBusy: bool = False,
    ) -> None:
        self.installed = installed
        self.authorized = authorized
        self.chats = list(chats or [])
        self.exportPayload = exportPayload or {"id": "1001", "messages": []}
        self._isBusy = isBusy
        self.loginCalls: list[tuple[str, str, str]] = []
        self.qrPasswords: list[str] = []
        self.logoutCalls = 0
        self.exportCalls: list[tuple[str, int, int]] = []
        self.runtime = SimpleNamespace(path=lambda: "/fake/tdl.exe" if self.installed else "")

    @property
    def isBusy(self) -> bool:
        return self._isBusy

    async def probeAuth(self) -> bool:
        return self.authorized

    async def loginInteractive(self, mode: str = "qr", *, desktopPath: str = "", passcode: str = "") -> None:
        self.loginCalls.append((mode, desktopPath, passcode))
        self.authorized = True

    async def loginQrSession(self, *, onQrMatrix, onPasswordRequired, readPassword, onStatus=None) -> None:
        self.loginCalls.append(("qr", "", ""))
        onQrMatrix(sampleQrMatrix())
        await asyncio.sleep(0.03)
        onPasswordRequired()
        password = await readPassword()
        if password is None:
            raise asyncio.CancelledError
        self.qrPasswords.append(password)
        self.authorized = True

    async def logout(self) -> bool:
        self.logoutCalls += 1
        self.authorized = False
        return True

    async def listChats(self) -> list[TelegramChat]:
        return list(self.chats)

    async def exportMessages(self, chatId: str, *, sinceMsgId: int = 0, limit: int = 50, **_kwargs) -> dict:
        self.exportCalls.append((chatId, sinceMsgId, limit))
        return self.exportPayload


def buildUiPack(tmpPath: Path, runner: FakeUiRunner) -> tuple[TelegramPack, list[TelegramTask], list[int]]:
    loop = asyncio.get_running_loop()
    coroutineRunner = CoroutineRunner(loop.call_soon, loop=loop)
    pack = TelegramPack(SimpleNamespace(coroutineRunner=coroutineRunner))
    pack.runner = runner
    pack.account = TelegramAccount(coroutineRunner, runner)
    pack.config._account = pack.account
    pack.inbox = TelegramInbox(tmpPath / "telegram_inbox.json")
    pack.submit = coroutineRunner.submit

    added: list[TelegramTask] = []
    addThreads: list[int] = []

    def addTask(task: TelegramTask) -> bool:
        addThreads.append(threading.get_ident())
        added.append(task)
        return True

    async def parse(options: TaskOptions) -> TelegramTask:
        if "999" in options.url:
            raise TaskError("模拟解析失败")
        return TelegramTask(
            name=f"parsed_{options.url.rsplit('/', 1)[-1]}",
            url=options.url,
            outputFolder=options.outputFolder,
            chatId="1001",
            sourceMode="url",
        )

    pack.addTask = addTask
    pack.parse = parse
    return pack, added, addThreads


def test_setting_groups_build_cards_and_bind_runtime_folder(config, tmp_path, qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    runner = FakeUiRunner(installed=True, authorized=True)
    account = TelegramAccount(None, runner)

    refreshes = 0
    folderChanges = 0

    class FakeRuntimeCard(QWidget):
        def refreshStatus(self) -> None:
            nonlocal refreshes
            refreshes += 1

        def _onInstallFolderChanged(self, *_args) -> None:
            nonlocal folderChanges
            folderChanges += 1

    previousAccount = telegramConfig._account
    previousFactory = telegramConfig.createRuntimeCard
    telegramConfig._account = account
    telegramConfig.createRuntimeCard = lambda _runtime, p: FakeRuntimeCard(p)
    try:
        groups = telegramConfig.settingGroups(parent)
        assert len(groups) == 1
        group = groups[0]

        folderCard = group.findChild(SelectFolderSettingCard)
        loginCard = group.findChild(TelegramLoginSettingCard)
        rangeCards = group.findChildren(RangeSettingCard)
        switchCards = group.findChildren(SwitchSettingCard)
        lineCards = group.findChildren(LineEditSettingCard)

        assert folderCard is not None
        assert loginCard is not None
        assert len(rangeCards) == 2
        assert len(switchCards) == 1
        assert len(lineCards) == 2
        assert refreshes == 1

        folderCard.pathChanged.emit(str(tmp_path / "TDL"))
        assert folderChanges == 1
    finally:
        telegramConfig._account = previousAccount
        telegramConfig.createRuntimeCard = previousFactory


async def test_login_setting_card_flows(config, qtbot):
    loop = asyncio.get_running_loop()
    coroutineRunner = CoroutineRunner(loop.call_soon, loop=loop)
    runner = FakeUiRunner(installed=False, authorized=False)
    account = TelegramAccount(coroutineRunner, runner)
    card = TelegramLoginSettingCard(account)
    qtbot.addWidget(card)
    card.show()

    assert "未安装 tdl" in card.contentLabel.text()
    assert not card.qrButton.isEnabled()
    assert not card.logoutButton.isEnabled()

    runner.installed = True
    account.refreshAuth()
    await waitUi(lambda: not account.isBusy and "未登录" in card.contentLabel.text())
    assert card.qrButton.isEnabled()

    pixmap = toQrMatrixPixmap(sampleQrMatrix(45), size=240)
    assert not pixmap.isNull()
    assert pixmap.width() == 240 and pixmap.height() == 240

    scanDialog = TelegramScanLoginDialog(account, card.window())
    qtbot.addWidget(scanDialog)
    scanDialog.show()
    await waitUi(lambda: not scanDialog.qrLabel.pixmap().isNull())

    qrRect = scanDialog.qrLabel.geometry()
    for sibling in (
        scanDialog.titleLabel,
        scanDialog.descriptionLabel,
        scanDialog.statusLabel,
        scanDialog.tipLabel,
        scanDialog.refreshButton,
    ):
        assert not qrRect.intersects(sibling.geometry()), f"QR 被 {sibling.objectName() or type(sibling).__name__} 遮挡"

    await waitUi(lambda: scanDialog.twoFaWidget.isVisible())
    assert scanDialog.qrLabel.isHidden()
    scanDialog.passwordEdit.setText("cloud-2fa-pwd")
    scanDialog.submitPasswordButton.click()
    await waitUi(lambda: account.isLoggedIn and "已登录" in card.contentLabel.text())
    assert runner.qrPasswords == ["cloud-2fa-pwd"]
    assert runner.loginCalls[-1] == ("qr", "", "")
    assert card.logoutButton.isEnabled()

    openedScan = False

    def closeQrDialog() -> None:
        nonlocal openedScan
        dialogs = [d for d in card.window().findChildren(TelegramScanLoginDialog) if d.isVisible()]
        if not dialogs:
            return
        openedScan = True
        dialogs[-1].cancelButton.click()
        openTimer.stop()

    openTimer = QTimer()
    openTimer.setInterval(20)
    openTimer.timeout.connect(closeQrDialog)
    openTimer.start()
    try:
        card.qrButton.click()
    finally:
        openTimer.stop()
    assert openedScan is True

    def submitDesktopDialog() -> None:
        dialogs = [d for d in card.window().findChildren(TelegramDesktopLoginDialog) if d.isVisible()]
        if not dialogs:
            return
        dialog = dialogs[-1]
        dialog.pathEdit.setText("D:/TelegramPortable/tdata")
        dialog.passcodeEdit.setText("local-pass")
        dialog.yesButton.click()
        timer.stop()

    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(submitDesktopDialog)
    timer.start()
    try:
        card.desktopButton.click()
        await waitUi(lambda: len(runner.loginCalls) == 2 and not account.isBusy)
    finally:
        timer.stop()
    assert runner.loginCalls[-1] == ("desktop", "D:/TelegramPortable/tdata", "local-pass")

    def confirmLogout() -> None:
        dialogs = [d for d in card.window().findChildren(MessageBox) if d.isVisible()]
        if not dialogs:
            return
        dialogs[-1].yesButton.click()
        logoutTimer.stop()

    logoutTimer = QTimer()
    logoutTimer.setInterval(20)
    logoutTimer.timeout.connect(confirmLogout)
    logoutTimer.start()
    try:
        card.logoutButton.click()
        await waitUi(lambda: runner.logoutCalls == 1 and not account.isLoggedIn)
    finally:
        logoutTimer.stop()
    assert not card.logoutButton.isEnabled()


async def test_page_direct_links_add_tasks_on_main_thread(config, tmp_path, qtbot):
    runner = FakeUiRunner(installed=True, authorized=True)
    pack, added, addThreads = buildUiPack(tmp_path, runner)
    assert pack.pages() == [TelegramPage]
    assert TelegramPage.icon is FluentIcon.SEND
    assert "Telegram" in TelegramPage.title

    page = TelegramPage(pack)
    qtbot.addWidget(page)
    page.show()

    page.directInput.setPlainText("not-a-telegram-link")
    page.directDownloadButton.click()
    assert "未识别" in page.directStatus.text()

    page.directInput.setPlainText(
        "https://t.me/c/123456/10\nhttps://t.me/c/123456/10\nhttps://t.me/c/123456/999\nhttps://t.me/chan/20?single"
    )
    mainThread = threading.get_ident()
    page.directDownloadButton.click()
    await waitUi(lambda: page.directDownloadButton.isEnabled() and len(added) == 2)

    assert [task.url for task in added] == [
        "https://t.me/c/123456/10",
        "https://t.me/chan/20?single",
    ]
    assert addThreads == [mainThread, mainThread]
    assert "已加入 2 个任务" in page.directStatus.text()
    assert "失败 1 条" in page.directStatus.text()


async def test_page_bind_sync_filter_skip_and_enqueue_inbox(config, tmp_path, qtbot):
    chats = [
        TelegramChat(chatId="2002", type="group", title="闲聊群", username=""),
        TelegramChat(chatId="1001", type="channel", title="我的私人收件箱", username=""),
    ]
    payload = {
        "id": 1001,
        "messages": [
            fileMessage(10, "video_a.mp4", size=300, groupedId=77, text="精选相册"),
            fileMessage(11, "audio_b.mp3", size=200, groupedId=77),
            fileMessage(15, "solo.mkv", size=500, text="单独电影"),
        ],
    }
    runner = FakeUiRunner(installed=True, authorized=True, chats=chats, exportPayload=payload)
    pack, added, _threads = buildUiPack(tmp_path, runner)
    pack.account.refreshAuth()
    await waitUi(lambda: pack.account.isLoggedIn)

    page = TelegramPage(pack)
    qtbot.addWidget(page)
    page.show()

    assert page.loginHint.isHidden()
    assert not page.syncButton.isEnabled()

    def chooseChannel() -> None:
        dialogs = [d for d in page.window().findChildren(TelegramChannelDialog) if d.isVisible()]
        if not dialogs:
            return
        dialog = dialogs[-1]
        assert "我的私人收件箱" in dialog.chatList.item(0).text()
        dialog.chatList.setCurrentRow(0)
        dialog.yesButton.click()
        bindTimer.stop()

    bindTimer = QTimer()
    bindTimer.setInterval(20)
    bindTimer.timeout.connect(chooseChannel)
    bindTimer.start()
    try:
        page.bindChannelButton.click()
        await waitUi(lambda: pack.inbox.chatId == "1001")
    finally:
        bindTimer.stop()

    assert "我的私人收件箱" in page.channelLabel.text()
    assert page.syncButton.isEnabled()

    page.syncButton.click()
    await waitUi(lambda: len(page.findChildren(InboxBundleCard)) == 2)
    assert "新增 2 条媒体组" in page.inboxStatus.text()
    assert "#15" in page.watermarkLabel.text()

    page.setSearchText("精选相册")
    cards = page.findChildren(InboxBundleCard)
    visible = [card for card in cards if not card.isHidden()]
    assert len(visible) == 1
    assert visible[0].bundle.bundleKey == "1001:g:77"

    page.setSearchText("")
    soloCard = next(card for card in cards if card.bundle.bundleKey == "1001:m:15")
    soloCard.skipButton.click()
    await waitUi(lambda: pack.inbox.bundleByKey("1001:m:15").status == "skipped")

    page.filterCombo.setCurrentIndex(4)
    visibleSkipped = [card for card in page.findChildren(InboxBundleCard) if not card.isHidden()]
    assert [card.bundle.bundleKey for card in visibleSkipped] == ["1001:m:15"]

    visibleSkipped[0].skipButton.click()
    await waitUi(lambda: pack.inbox.bundleByKey("1001:m:15").status == "new")

    page.filterCombo.setCurrentIndex(0)
    page.downloadAllPendingButton.click()
    await waitUi(lambda: len(added) == 2)
    assert all(bundle.status == "queued" for bundle in pack.inbox.bundles)
    groupTask = next(task for task in added if task.bundleKey == "1001:g:77")
    assert [file.size for file in groupTask.files] == [300, 200]


async def test_page_supports_dark_and_light_transparent_containers(config, tmp_path, qtbot):
    runner = FakeUiRunner(installed=True, authorized=True)
    pack, _added, _threads = buildUiPack(tmp_path, runner)
    bundles = groupExportedMessages({
        "id": 1001,
        "messages": [fileMessage(10, "clip.mp4", size=128, text="测试卡片")],
    })
    pack.bindInboxChannel("1001", "我的频道")
    pack.applyInboxUpdates(bundles)

    page = TelegramPage(pack)
    qtbot.addWidget(page)
    page.show()

    try:
        for theme in (Theme.DARK, Theme.LIGHT):
            qfluentwidgets.setTheme(theme)
            scrolls = page.findChildren(ScrollArea)
            assert scrolls
            for scroll in scrolls:
                assert "transparent" in scroll.styleSheet().lower()
                assert "transparent" in scroll.viewport().styleSheet().lower()
            assert "transparent" in page.inboxList.styleSheet().lower()
            assert page.directCard.isVisible()
            assert page.inboxHeaderCard.isVisible()
            assert len(page.findChildren(InboxBundleCard)) == 1
    finally:
        qfluentwidgets.setTheme(Theme.AUTO)
