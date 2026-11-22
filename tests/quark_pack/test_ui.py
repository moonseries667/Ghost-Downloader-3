from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import threading
from types import SimpleNamespace

import pytest
import qfluentwidgets
from PySide6.QtCore import QModelIndex, QTimer, Qt
from PySide6.QtWidgets import QDialog, QWidget
from qfluentwidgets import (
    FluentIcon, MessageBox, RangeSettingCard, SimpleCardWidget, SwitchSettingCard, Theme,
)

from app.models.task import TaskError, TaskOptions
from app.services.coroutine_runner import CoroutineRunner
from app.view.components.scroll_area import ScrollArea
from quark_pack.account import QR_LOGIN_SUCCESS, QuarkAccount
from quark_pack.config import quarkConfig
from quark_pack.page import QuarkPage
from quark_pack.pack import QuarkPack
from quark_pack.setting_cards import QuarkLoginSettingCard, QuarkScanLoginDialog
from quark_pack.share_tree import FILE_INDEX_ROLE, ShareLinkCard, ShareSelectionQueue
from quark_pack.task import QuarkFile, QuarkTask
from tests.quark_pack.conftest import buildQuarkClient

BATCH_TEXT = """https://pan.quark.cn/s/ok001 提取码：aaaa
https://pan.quark.cn/s/bad002 提取码：zzzz
https://pan.quark.cn/s/ok003 提取码：bbbb
"""

SHARE_FILES = {
    "ok001": [("docs/a.mp4", 100), ("docs/ad.txt", 10), ("b.mp4", 200), ("ad_banner.txt", 20)],
    "bad002": [("late.txt", 7), ("late2.txt", 8)],
    "ok003": [("movie/x.mkv", 500), ("movie/x.srt", 5), ("readme.txt", 1)],
}


def buildAccount(origin, *, pollInterval: float = 0.05) -> QuarkAccount:
    loop = asyncio.get_running_loop()
    runner = CoroutineRunner(loop.call_soon, loop=loop)
    client = buildQuarkClient(origin, readCookie=lambda: str(quarkConfig.cookie.value or ""))
    return QuarkAccount(runner, client, pollInterval=pollInterval)


def buildPack(origin) -> QuarkPack:
    loop = asyncio.get_running_loop()
    runner = CoroutineRunner(loop.call_soon, loop=loop)
    pack = QuarkPack(SimpleNamespace(coroutineRunner=runner))
    pack.submit = runner.submit
    client = buildQuarkClient(origin, readCookie=lambda: str(quarkConfig.cookie.value or ""))
    pack.client = client
    pack.account.client = client
    return pack


def buildFakePack() -> FakeQuarkPack:
    loop = asyncio.get_running_loop()
    return FakeQuarkPack(CoroutineRunner(loop.call_soon, loop=loop))


async def waitUi(predicate, timeout: float = 8.0):
    from PySide6.QtWidgets import QApplication
    for _ in range(int(timeout / 0.02)):
        QApplication.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


class FakeQuarkPack:

    def __init__(self, runner: CoroutineRunner | None = None) -> None:
        self.tasks: list[QuarkTask] = []
        self.inFlight = 0
        self.peakInFlight = 0
        self.parseThreads: list[int] = []
        self.addThreads: list[int] = []
        if runner is not None:
            self.submit = runner.submit

    async def parse(self, options: TaskOptions) -> QuarkTask:
        self.parseThreads.append(threading.get_ident())
        self.inFlight += 1
        self.peakInFlight = max(self.peakInFlight, self.inFlight)
        try:
            await asyncio.sleep(0.01)
            pwdId, passcode = splitShareUrl(options.url)
            if pwdId == "bad002" and passcode != "good":
                raise TaskError("提取码错误，请核对后再试")
            files = [
                QuarkFile(index=index, relativePath=path, size=size)
                for index, (path, size) in enumerate(SHARE_FILES[pwdId])
            ]
            return QuarkTask(
                name=pwdId,
                url=options.url,
                outputFolder=options.outputFolder,
                sourceKind="share",
                pwdId=pwdId,
                passcode=passcode,
                files=files,
            )
        finally:
            self.inFlight -= 1

    def addTask(self, task: QuarkTask) -> None:
        self.addThreads.append(threading.get_ident())
        self.tasks.append(task)


def splitShareUrl(url: str) -> tuple[str, str]:
    path, _, query = url.partition("?")
    passcode = query.split("pwd=", 1)[1] if "pwd=" in query else ""
    return path.rsplit("/", 1)[-1], passcode


def openPage(quarkOrigin, qtbot) -> tuple[QuarkPage, FakeQuarkPack]:
    pack = buildPack(quarkOrigin)
    fake = buildFakePack()
    pack.parse = fake.parse
    pack.addTask = fake.addTask
    page = QuarkPage(pack, None)
    qtbot.addWidget(page)
    page.show()
    return page, fake


async def parseShares(page: QuarkPage) -> None:
    page.shareInput.setPlainText(BATCH_TEXT)
    page.parseShareButton.click()
    await waitUi(
        lambda: page.parseShareButton.isEnabled() and len(page.findChildren(ShareLinkCard)) == 3
    )


def cardById(page: QuarkPage, pwdId: str) -> ShareLinkCard:
    return next(card for card in page.findChildren(ShareLinkCard) if card.pwdId == pwdId)


def fileItem(card: ShareLinkCard, name: str):
    stack = [card.treeModel.item(row) for row in range(card.treeModel.rowCount())]
    while stack:
        item = stack.pop()
        if item.data(FILE_INDEX_ROLE) is not None and item.text() == name:
            return item
        stack.extend(item.child(row) for row in range(item.rowCount()))
    raise AssertionError(f"没有找到文件行 {name}")


def isRowVisible(card: ShareLinkCard, name: str) -> bool:
    item = fileItem(card, name)
    parent = item.parent()
    parentIndex = parent.index() if parent is not None else QModelIndex()
    return not card.treeView.isRowHidden(item.row(), parentIndex)


async def test_parse_share_summarizes_links_and_collapses_cards(config, quarkOrigin, qtbot):
    page, fake = openPage(quarkOrigin, qtbot)

    page.shareInput.setPlainText(BATCH_TEXT)
    page.parseShareButton.click()

    assert "共 3 条链接" in page.shareSummaryLabel.text()
    assert page.parseShareButton.isEnabled() is False

    await waitUi(lambda: page.parseShareButton.isEnabled())

    assert fake.peakInFlight == 1
    assert fake.tasks == []
    cards = page.findChildren(ShareLinkCard)
    assert len(cards) == 3
    assert all(not card.isExpanded for card in cards)

    assert cardById(page, "ok001").checkBox.checkState() == Qt.CheckState.Checked
    assert cardById(page, "ok003").checkBox.checkState() == Qt.CheckState.Checked

    failed = cardById(page, "bad002")
    assert "提取码" in failed.errorLabel.text()
    assert failed.errorLabel.isVisibleTo(failed)
    assert failed.passcodeEdit.isVisibleTo(failed)
    assert failed.retryButton.isVisibleTo(failed)


async def test_retry_failed_link_recovers_card_in_place(config, quarkOrigin, qtbot):
    page, fake = openPage(quarkOrigin, qtbot)
    await parseShares(page)

    failed = cardById(page, "bad002")
    assert failed.statusLabel.text().startswith("失败")

    failed.passcodeEdit.setText("good")
    failed.retryButton.click()
    await waitUi(lambda: not failed.errorLabel.isVisibleTo(failed) and page.parseShareButton.isEnabled())

    assert failed.checkBox.checkState() == Qt.CheckState.Checked
    assert "已选 2/2" in failed.statusLabel.text()
    assert failed.isExpanded is False
    assert len(page.findChildren(ShareLinkCard)) == 3
    assert [task.pwdId for task in fake.tasks] == []


async def test_invert_with_search_keeps_unmatched_files_selected(config, tmp_path, qtbot):
    queue = ShareSelectionQueue(buildFakePack())
    assert queue.setEntries(
        "https://pan.quark.cn/s/ok001 提取码：aaaa\nhttps://pan.quark.cn/s/ok003 提取码：bbbb"
    )
    queue.startParse(tmp_path)
    await waitUi(lambda: not queue.isBusy)

    first = ShareLinkCard(queue, "ok001")
    second = ShareLinkCard(queue, "ok003")
    qtbot.addWidget(first)
    qtbot.addWidget(second)

    firstEntry = queue.entryById("ok001")
    assert firstEntry.selectedIndexes == {0, 1, 2, 3}
    assert first.checkBox.checkState() == Qt.CheckState.Checked
    first.toggleButton.click()
    assert first.isExpanded is True

    first.searchEdit.setText("ad")
    assert first.shownIndexes == {1, 3}
    assert isRowVisible(first, "ad.txt")
    assert isRowVisible(first, "ad_banner.txt")
    assert not isRowVisible(first, "a.mp4")
    assert not isRowVisible(first, "b.mp4")

    first.invertButton.click()
    assert firstEntry.selectedIndexes == {0, 2}
    assert "已选 2/4" in first.statusLabel.text()
    assert fileItem(first, "ad.txt").checkState() == Qt.CheckState.Unchecked
    assert fileItem(first, "ad_banner.txt").checkState() == Qt.CheckState.Unchecked

    second.toggleButton.click()
    second.searchEdit.setText("movie")
    assert second.shownIndexes == {0, 1}
    second.clearButton.click()
    assert queue.entryById("ok003").selectedIndexes == {2}
    assert "已选 1/3" in second.statusLabel.text()
    assert not isRowVisible(second, "readme.txt")

    assert first.searchEdit.text() == "ad"
    assert firstEntry.selectedIndexes == {0, 2}
    assert "已选 2/4" in first.statusLabel.text()

    first.searchEdit.setText("")
    assert first.shownIndexes == {0, 1, 2, 3}
    for name in ("a.mp4", "ad.txt", "b.mp4", "ad_banner.txt"):
        assert isRowVisible(first, name)
    assert fileItem(first, "a.mp4").checkState() == Qt.CheckState.Checked
    assert fileItem(first, "ad.txt").checkState() == Qt.CheckState.Unchecked
    assert fileItem(first, "b.mp4").checkState() == Qt.CheckState.Checked
    assert fileItem(first, "ad_banner.txt").checkState() == Qt.CheckState.Unchecked


async def test_start_selected_queues_only_selected_files(config, quarkOrigin, qtbot):
    page, fake = openPage(quarkOrigin, qtbot)
    await parseShares(page)

    first = cardById(page, "ok001")
    first.toggleButton.click()
    first.clearButton.click()
    assert "已选 0/4" in first.statusLabel.text()

    second = cardById(page, "ok003")
    second.toggleButton.click()
    second.searchEdit.setText("movie")
    second.clearButton.click()
    assert "已选 1/3" in second.statusLabel.text()

    page.startSelectedButton.click()

    assert [task.pwdId for task in fake.tasks] == ["ok003"]
    assert fake.tasks[0].selectedIndexes == {2}
    assert "已加入下载队列" in second.statusLabel.text()
    assert "已选 0/4" in first.statusLabel.text()
    assert "已加入下载队列" not in first.statusLabel.text()
    assert cardById(page, "bad002").statusLabel.text().startswith("失败")
    assert "1" in page.shareStatus.text()


async def test_parse_shares_with_independent_runner_keeps_main_thread_callbacks(config, tmp_path, qapp):
    mainId = threading.get_ident()
    runner = CoroutineRunner(
        lambda fn: QTimer.singleShot(0, qapp, fn), loop=asyncio.new_event_loop(),
    )
    runner.start()
    try:
        pack = FakeQuarkPack(runner)
        queue = ShareSelectionQueue(pack)
        changedThreads: list[int] = []
        queue.changed.connect(lambda: changedThreads.append(threading.get_ident()))

        assert queue.setEntries(BATCH_TEXT) is True
        queue.startParse(tmp_path)
        assert queue.isBusy is True
        assert queue.setEntries("https://pan.quark.cn/s/other9") is False
        assert queue.clearEntries() is False
        assert queue.submitSelected(tmp_path) == 0
        assert [entry.pwdId for entry in queue.entries] == ["ok001", "bad002", "ok003"]

        await waitUi(lambda: not queue.isBusy)
        assert queue.submitSelected(tmp_path) == 2
    finally:
        runner.stop()

    assert pack.parseThreads
    assert all(threadId != mainId for threadId in pack.parseThreads)
    assert pack.addThreads
    assert all(threadId == mainId for threadId in pack.addThreads)
    assert changedThreads
    assert all(threadId == mainId for threadId in changedThreads)
    assert pack.peakInFlight == 1
    assert [task.pwdId for task in pack.tasks] == ["ok001", "ok003"]


@pytest.mark.parametrize("theme", [Theme.DARK, Theme.LIGHT], ids=["dark", "light"])
async def test_page_paints_transparent_in_both_themes(config, quarkOrigin, qtbot, theme):
    original = qfluentwidgets.qconfig.theme
    qfluentwidgets.setTheme(theme)
    try:
        page, _fake = openPage(quarkOrigin, qtbot)

        scrolls = page.findChildren(ScrollArea)
        assert scrolls
        for scroll in scrolls:
            assert "transparent" in scroll.styleSheet()
            assert "transparent" in scroll.viewport().styleSheet()
            assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

        for widget in (page.shareList, page.driveList, page.stack, page.shareTab, page.driveTab):
            assert "transparent" in widget.styleSheet()

        page.shareInput.setPlainText("https://pan.quark.cn/s/ok001 提取码：aaaa")
        page.parseShareButton.click()
        await waitUi(
            lambda: page.parseShareButton.isEnabled() and page.findChildren(ShareLinkCard)
        )

        assert isinstance(page.findChildren(ShareLinkCard)[0], SimpleCardWidget)
    finally:
        qfluentwidgets.setTheme(original)


async def test_search_text_hides_unmatched_link_cards(config, quarkOrigin, qtbot):
    page, _fake = openPage(quarkOrigin, qtbot)
    await parseShares(page)

    page.setSearchText("movie")
    assert cardById(page, "ok003").isHidden() is False
    assert cardById(page, "ok001").isHidden() is True
    assert cardById(page, "bad002").isHidden() is True

    page.setSearchText("ok001")
    assert cardById(page, "ok001").isHidden() is False
    assert cardById(page, "ok003").isHidden() is True

    page.setSearchText("ad_banner")
    assert cardById(page, "ok001").isHidden() is False
    assert cardById(page, "ok003").isHidden() is True

    page.setSearchText("")
    for pwdId in ("ok001", "bad002", "ok003"):
        assert cardById(page, pwdId).isHidden() is False


async def test_setting_group_has_login_concurrency_and_cleanup(config, quarkOrigin, qtbot):
    account = buildAccount(quarkOrigin)
    quarkConfig._account = account

    groups = quarkConfig.settingGroups(None)

    assert len(groups) == 1
    group = groups[0]
    qtbot.addWidget(group)
    cards = [group.cardLayout.itemAt(index).widget() for index in range(group.cardLayout.count())]
    assert isinstance(cards[0], QuarkLoginSettingCard)
    assert isinstance(cards[1], RangeSettingCard)
    assert isinstance(cards[2], SwitchSettingCard)


async def test_login_card_shows_profile_then_logout(config, quarkOrigin, qtbot):
    account = buildAccount(quarkOrigin)
    card = QuarkLoginSettingCard(account, None)
    qtbot.addWidget(card)
    assert "状态：未登录" in card.contentLabel.text()

    account.setCookie("__pus=fake-pus-value; __puus=fake-puus-value")
    await waitUi(lambda: "测试用户" in card.contentLabel.text() and "超级会员" in card.contentLabel.text())

    account.logout()
    assert "状态：未登录" in card.contentLabel.text()


async def test_scan_dialog_accepts_waiting_then_success(config, quarkOrigin, qtbot):
    quarkOrigin.qrScript = ["waiting", "success"]
    account = buildAccount(quarkOrigin)
    states: list[int] = []
    account.qrStateChanged.connect(lambda code, message: states.append(code))
    host = QWidget()
    qtbot.addWidget(host)
    dialog = QuarkScanLoginDialog(account, host)
    qtbot.addWidget(dialog)
    dialog.show()

    await waitUi(lambda: dialog.result() == QDialog.DialogCode.Accepted)

    assert "qr-pus-value" in quarkConfig.cookie.value
    assert QR_LOGIN_SUCCESS in states


async def test_quark_page_lists_drive_and_queues_selected_download(config, quarkOrigin, qtbot):
    pack = buildPack(quarkOrigin)
    added: list[QuarkTask] = []
    pack.addTask = added.append

    assert QuarkPage.icon is FluentIcon.CLOUD_DOWNLOAD
    assert "夸克网盘" in QuarkPage.title
    assert QuarkPage.searchPlaceholder == "搜索网盘文件或批量链接"
    assert pack.pages() == [QuarkPage]

    page = QuarkPage(pack, None)
    qtbot.addWidget(page)
    page.show()

    hint = page.findChild(QWidget, "driveLoginHint")
    assert hint is not None
    assert "设置页" in hint.text()

    quarkOrigin.drive["0"] = [
        {"fid": "dir-docs", "file_name": "docs", "size": 0, "dir": True},
        {"fid": "file-top", "file_name": "top.txt", "size": 12, "dir": False},
    ]
    quarkOrigin.drive["dir-docs"] = [
        {"fid": "file-inner", "file_name": "inner.txt", "size": 5, "dir": False},
    ]
    pack.account.setCookie("__pus=fake-pus-value; __puus=fake-puus-value")
    page.refreshDrive()
    await waitUi(lambda: page.findChild(QWidget, "drive-file-top") is not None)

    page.setSearchText("top")
    topRow = page.findChild(QWidget, "drive-file-top")
    docsRow = page.findChild(QWidget, "drive-dir-docs")
    assert not topRow.isHidden()
    assert docsRow.isHidden()
    page.setSearchText("")
    assert not topRow.isHidden()
    assert not docsRow.isHidden()

    docsRow.doubleClicked.emit()
    await waitUi(lambda: page.findChild(QWidget, "drive-file-inner") is not None)
    breadcrumb = page.findChild(QWidget, "driveBreadcrumb")
    assert "docs" in breadcrumb.text()

    page.findChild(QWidget, "driveUpButton").click()
    await waitUi(lambda: page.findChild(QWidget, "drive-file-top") is not None)
    assert "根目录" in breadcrumb.text()

    page.findChild(QWidget, "drive-dir-docs").checkBox.setChecked(True)
    page.findChild(QWidget, "drive-file-top").checkBox.setChecked(True)
    page.findChild(QWidget, "downloadSelectedButton").click()
    await waitUi(lambda: bool(added))

    task = added[0]
    assert task.sourceKind == "drive"
    assert task.tempFolderFid == ""
    assert {file.relativePath for file in task.files} == {"docs/inner.txt", "top.txt"}
    assert quarkOrigin.deleteBodies == []


async def test_clean_temp_deletes_only_idle_folder(config, quarkOrigin, qtbot):
    quarkOrigin.drive["0"] = [{"fid": "tmp-root", "file_name": "_ghd_tmp", "size": 0, "dir": True}]
    quarkOrigin.drive["tmp-root"] = [
        {"fid": "idle-fid", "file_name": "idle-task", "size": 0, "dir": True},
        {"fid": "busy-fid", "file_name": "busy-task", "size": 0, "dir": True},
    ]
    pack = buildPack(quarkOrigin)
    pack.account.setCookie("__pus=fake-pus-value; __puus=fake-puus-value")
    page = QuarkPage(pack, None)
    qtbot.addWidget(page)
    page.show()

    contents: list[str] = []

    def answerDialog() -> None:
        dialogs = [dialog for dialog in page.findChildren(MessageBox) if dialog.isVisible()]
        if not dialogs:
            return
        contents.append(dialogs[-1].contentLabel.text())
        dialogs[-1].yesButton.click()
        timer.stop()

    QuarkTask.activeTempFolderNames.add("busy-task")
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(answerDialog)
    try:
        timer.start()
        page.findChild(QWidget, "cleanTempButton").click()
        await waitUi(lambda: bool(quarkOrigin.deleteBodies))
    finally:
        timer.stop()
        QuarkTask.activeTempFolderNames.discard("busy-task")

    assert contents
    assert "1" in contents[0]
    filelist = quarkOrigin.deleteBodies[0]["filelist"]
    assert filelist == ["idle-fid"]
    assert "0" not in filelist
    assert "busy-fid" not in filelist
    assert "tmp-root" not in filelist


async def test_clean_temp_without_idle_folder_skips_confirm(config, quarkOrigin, qtbot):
    quarkOrigin.drive["0"] = [{"fid": "tmp-root", "file_name": "_ghd_tmp", "size": 0, "dir": True}]
    quarkOrigin.drive["tmp-root"] = [
        {"fid": "busy-fid", "file_name": "busy-task", "size": 0, "dir": True},
    ]
    pack = buildPack(quarkOrigin)
    pack.account.setCookie("__pus=fake-pus-value; __puus=fake-puus-value")
    page = QuarkPage(pack, None)
    qtbot.addWidget(page)
    page.show()

    contents: list[str] = []

    def recordDialog() -> None:
        for dialog in page.findChildren(MessageBox):
            if dialog.isVisible():
                contents.append(dialog.contentLabel.text())

    QuarkTask.activeTempFolderNames.add("busy-task")
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(recordDialog)
    try:
        timer.start()
        page.findChild(QWidget, "cleanTempButton").click()
        await waitUi(lambda: "没有需要清理" in page.shareStatus.text())
    finally:
        timer.stop()
        QuarkTask.activeTempFolderNames.discard("busy-task")

    assert contents == []
    assert quarkOrigin.deleteBodies == []
    assert page.cleanTempButton.isEnabled()
