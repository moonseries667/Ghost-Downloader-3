from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
from pathlib import Path
from types import SimpleNamespace

import qfluentwidgets
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import ComboBoxSettingCard, RangeSettingCard, SwitchSettingCard

from app.models.task import TaskError, TaskOptions
from app.services.coroutine_runner import CoroutineRunner
from app.view.components.setting_cards import SelectFolderSettingCard
from baidu_pack.account import BaiduAccount
from baidu_pack.client import BaiduQrPollResult, BaiduQrSession
from baidu_pack.config import baiduConfig
from baidu_pack.pack import BaiduPack
from baidu_pack.page import BaiduPage, DriveRow
from baidu_pack.runtime import BaiduDriveEntry, BaiduUserInfo
from baidu_pack.setting_cards import BaiduCookieDialog, BaiduLoginSettingCard, BaiduScanLoginDialog
from baidu_pack.task import BaiduFile, BaiduTask


async def waitUi(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        QApplication.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for UI condition")


class FakeUiRunner:
    def __init__(self, *, installed: bool = True, loggedIn: bool = True) -> None:
        self.installed = installed
        self.loggedIn = loggedIn
        self.runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go" if self.installed else "")
        self.cookieLogins: list[str] = []
        self.credLogins: list[tuple[str, str]] = []
        self.dirs: dict[str, list[BaiduDriveEntry]] = {
            "/": [
                BaiduDriveEntry("10", "课程", "/课程", 0, True),
                BaiduDriveEntry("11", "notes.txt", "/notes.txt", 256, False),
            ],
            "/课程": [
                BaiduDriveEntry("20", "lec1.mp4", "/课程/lec1.mp4", 4096, False),
            ],
        }

    def resolveProxy(self) -> str | None:
        return None

    async def probeUser(self) -> BaiduUserInfo:
        if not self.loggedIn:
            return BaiduUserInfo(isLoggedIn=False)
        return BaiduUserInfo(isLoggedIn=True, uid="1", username="pan_user", totalQuota="2TB", usedQuota="128GB")

    async def loginWithCookies(self, cookies: str) -> BaiduUserInfo:
        self.cookieLogins.append(cookies)
        self.loggedIn = True
        return await self.probeUser()

    async def loginWithCredentials(self, bduss: str, stoken: str = "") -> BaiduUserInfo:
        self.credLogins.append((bduss, stoken))
        self.loggedIn = True
        return await self.probeUser()

    async def logout(self) -> bool:
        self.loggedIn = False
        return True

    async def listDirectory(self, remotePath: str = "/") -> list[BaiduDriveEntry]:
        return list(self.dirs.get(remotePath, []))

    async def cleanStaleTempDirs(self, _keep: set[str]) -> list[str]:
        return []

    async def stopAll(self) -> None:
        pass


async def test_setting_group_is_concise_and_has_no_temp_switches(qapp, monkeypatch):
    qfluentwidgets.load(qapp)
    loop = asyncio.get_running_loop()
    runner = FakeUiRunner(installed=True, loggedIn=True)
    account = BaiduAccount(CoroutineRunner(loop.call_soon, loop=loop), runner)
    account.applyUserInfo(await runner.probeUser())

    parent = QWidget()
    baiduConfig._account = account
    fakeRuntimeCard = SimpleNamespace(
        _onInstallFolderChanged=lambda *_: None,
        refreshStatus=lambda: None,
        _runtimeStatusService=None,
    )
    createdRuntimeWidget = QWidget(parent)
    createdRuntimeWidget._onInstallFolderChanged = fakeRuntimeCard._onInstallFolderChanged
    createdRuntimeWidget.refreshStatus = fakeRuntimeCard.refreshStatus
    monkeypatch.setattr(baiduConfig, "createRuntimeCard", lambda _rt, _grp: createdRuntimeWidget)

    groups = baiduConfig.settingGroups(parent)
    assert len(groups) == 1
    group = groups[0]
    layout = group.cardContainer.layout()
    cards = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]
    assert len(cards) == 5
    assert isinstance(cards[0], SelectFolderSettingCard)
    assert cards[1] is createdRuntimeWidget
    assert isinstance(cards[2], BaiduLoginSettingCard)
    assert isinstance(cards[3], ComboBoxSettingCard)
    assert isinstance(cards[4], RangeSettingCard)
    # 确认设置组没有暴露任何"临时目录"开关
    assert not any(isinstance(card, SwitchSettingCard) for card in cards)
    assert "pan_user" in cards[2].contentLabel.text()
    assert "128GB" in cards[2].contentLabel.text()
    parent.deleteLater()


async def test_cookie_and_scan_dialogs(qapp):
    qfluentwidgets.load(qapp)
    loop = asyncio.get_running_loop()
    runner = FakeUiRunner(installed=True, loggedIn=False)

    class FakeScanClient:
        async def fetchQrSession(self) -> BaiduQrSession:
            # 1x1 合法 PNG 字节，验证真实图片渲染路径
            pngBytes = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
                b"\x00\x05\xfe\x02\xfeA\xe2\x1b\x85\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            return BaiduQrSession("sign-1", "gid-1", "https://passport.baidu.com/qr?s=1", pngBytes)

        async def pollQrStatus(self, _session: BaiduQrSession) -> BaiduQrPollResult:
            return BaiduQrPollResult("confirmed", token="ok-token")

        async def exchangeQrCookie(self, _token: str, _gid: str = "") -> str:
            return "BDUSS=scan-bduss; STOKEN=scan-stoken"

    host = QWidget()
    host.resize(800, 600)
    account = BaiduAccount(
        CoroutineRunner(loop.call_soon, loop=loop), runner, FakeScanClient(), pollInterval=0.01,
    )
    scanDialog = BaiduScanLoginDialog(account, host)
    await waitUi(lambda: account.isLoggedIn)
    assert runner.cookieLogins == ["BDUSS=scan-bduss; STOKEN=scan-stoken"]
    scanDialog.close()
    scanDialog.deleteLater()

    cookieDialog = BaiduCookieDialog(host)
    cookieDialog.bdussEdit.setText("manual-bduss")
    cookieDialog.stokenEdit.setText("manual-stoken")
    assert cookieDialog.rawCookie == ""
    assert cookieDialog.bduss == "manual-bduss"
    assert cookieDialog.stoken == "manual-stoken"
    cookieDialog.close()
    cookieDialog.deleteLater()
    host.deleteLater()


async def test_baidu_page_share_tree_and_drive_tabs(qapp, downloadFolder):
    qfluentwidgets.load(qapp)
    loop = asyncio.get_running_loop()
    coroutineRunner = CoroutineRunner(loop.call_soon, loop=loop)
    runner = FakeUiRunner(installed=True, loggedIn=True)

    pack = BaiduPack(SimpleNamespace(coroutineRunner=coroutineRunner))
    pack.runner = runner
    pack.account = BaiduAccount(coroutineRunner, runner)
    pack.account.applyUserInfo(await runner.probeUser())

    addedTasks: list[BaiduTask] = []
    pack.submit = coroutineRunner.submit
    pack.addTask = addedTasks.append

    async def fakeParse(options: TaskOptions) -> BaiduTask:
        if "1FailShare" in options.url and "pwd=9999" not in options.url:
            raise TaskError("提取码错误或需要输入提取码", kind="passcode")
        shareKey = "1FailShare" if "1FailShare" in options.url else "1GoodShare"
        return BaiduTask(
            name="演示合集" if shareKey == "1GoodShare" else "重试成功包",
            url=options.url,
            outputFolder=options.outputFolder,
            sourceKind="share",
            shareKey=shareKey,
            passcode="ab12",
            files=[
                BaiduFile(index=0, relativePath="sub/a.mp4", size=1000, fsId="1"),
                BaiduFile(index=1, relativePath="sub/b.pdf", size=500, fsId="2"),
            ],
        )

    pack.parse = fakeParse

    page = BaiduPage(pack)
    page._folder = Path(downloadFolder)
    # 确认主页面没有暴露任何"清理临时目录"按钮
    assert not hasattr(page, "cleanTempButton")

    # 1. 分享下载 Tab：批量解析、展开搜索、失败项输入提取码重试、加入下载队列
    page.shareInput.setPlainText(
        "链接: https://pan.baidu.com/s/1GoodShare 提取码: ab12\n"
        "链接: https://pan.baidu.com/s/1FailShare\n"
    )
    page.parseShareButton.click()
    await waitUi(lambda: not page._queue.isBusy and len(page.cards) == 2)

    goodCard = next(card for card in page.cards if card.shareKey == "1GoodShare")
    failCard = next(card for card in page.cards if card.shareKey == "1FailShare")

    goodCard.setExpanded(True)
    assert goodCard.isExpanded is True
    goodCard.setSearchText("a.mp4")
    assert goodCard.shownIndexes == {0}
    goodCard.clearButton.click()
    assert page._queue.entryById("1GoodShare").selectedIndexes == {1}
    assert goodCard.checkBox.checkState() == Qt.CheckState.PartiallyChecked

    page.collapseAllButton.click()
    assert goodCard.isExpanded is False

    # 失败卡片内嵌提取码输入并重试
    assert not failCard.passcodeEdit.isHidden()
    failCard.passcodeEdit.setText("9999")
    failCard.retryButton.click()
    await waitUi(lambda: page._queue.entryById("1FailShare").status == "ready")

    page.startSelectedButton.click()
    assert len(addedTasks) == 2
    assert addedTasks[0].selectedIndexes == {1}

    # 2. 我的网盘 Tab：面包屑浏览、双击进目录、返回上级、勾选文件夹加入下载队列
    page.segment.setCurrentItem("drive")
    await waitUi(lambda: any(isinstance(row, DriveRow) for row in page._driveRows))
    folderRow = next(row for row in page._driveRows if isinstance(row, DriveRow) and row.entry.name == "课程")
    page.enterFolder(folderRow.entry)
    await waitUi(lambda: "课程" in page.driveBreadcrumb.text() and any(
        isinstance(row, DriveRow) and row.entry.name == "lec1.mp4" for row in page._driveRows
    ))

    page.goUp()
    await waitUi(lambda: page.driveBreadcrumb.text() == "根目录" and any(
        isinstance(row, DriveRow) and row.entry.name == "课程" for row in page._driveRows
    ))

    folderRow = next(row for row in page._driveRows if isinstance(row, DriveRow) and row.entry.name == "课程")
    folderRow.checkBox.setChecked(True)
    page.downloadSelected()
    await waitUi(lambda: len(addedTasks) == 3)
    driveTask = addedTasks[2]
    assert driveTask.sourceKind == "drive"
    assert [file.relativePath for file in driveTask.files] == ["课程/lec1.mp4"]

    page.close()
    page.deleteLater()
