from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QFileInfo, Qt, Signal as QtSignal
from PySide6.QtWidgets import (
    QFileDialog, QFileIconProvider, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    IconWidget,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
)

from app.config.cfg import cfg
from app.format import toReadableSize
from app.i18n import N
from app.models.pack import PackPage
from app.view.components.scroll_area import ScrollArea
from .share_tree import ShareLinkCard, ShareSelectionQueue
from .task import QuarkTask, buildDriveTask

if TYPE_CHECKING:
    from app.models.task import Task
    from .client import DriveEntry
    from .pack import QuarkPack

ROOT_FID = "0"
ROOT_TITLE = "根目录"
BREADCRUMB_SEPARATOR = " > "


class DriveRow(SimpleCardWidget):
    doubleClicked = QtSignal()

    def __init__(self, entry: DriveEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self._initWidget()
        self._initLayout()
        self._bind()

    def _initWidget(self) -> None:
        self.setObjectName(f"drive-{self.entry.fid}")
        provider = QFileIconProvider()
        icon = provider.icon(
            QFileIconProvider.IconType.Folder if self.entry.isDir else QFileInfo(self.entry.name)
        )
        self.icon = IconWidget(icon, self)
        self.icon.setFixedSize(20, 20)
        self.checkBox = CheckBox(self)
        self.nameLabel = BodyLabel(self.entry.name, self)
        size = self.tr("文件夹") if self.entry.isDir else toReadableSize(self.entry.size)
        self.sizeLabel = CaptionLabel(size, self)
        self.sizeLabel.setMinimumWidth(80)
        self.sizeLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _initLayout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(self.icon)
        layout.addWidget(self.checkBox)
        layout.addWidget(self.nameLabel, 1)
        layout.addWidget(self.sizeLabel)

    def _bind(self) -> None:
        pass

    def mouseDoubleClickEvent(self, event) -> None:
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class QuarkPage(PackPage, QWidget):
    icon = FluentIcon.CLOUD_DOWNLOAD
    title = N("PackPage", "夸克网盘")
    searchPlaceholder = "搜索网盘文件或批量链接"

    def __init__(self, pack: QuarkPack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pack = pack
        self._folder = Path(cfg.downloadFolder.value)
        self._searchText = ""
        self._driveNotice = ""
        self._driveGeneration = 0
        self._isLoggedIn = pack.account.isLoggedIn
        self._driveTrail: list[tuple[str, str]] = [(ROOT_FID, ROOT_TITLE)]
        self._driveEntries: list[DriveEntry] = []
        self._driveRows: list[QWidget] = []
        self._cards: dict[str, ShareLinkCard] = {}
        self._queue = ShareSelectionQueue(pack, owner=self)

        self._initWidget()
        self._initLayout()
        self._bind()
        self._updateShareSummary()
        self._applyLoginState()
        if pack.account.isLoggedIn:
            self.refreshDrive()

    def _initWidget(self) -> None:
        self.segment = SegmentedWidget(self)
        self.segment.addItem("share", self.tr("分享下载"))
        self.segment.addItem("drive", self.tr("我的网盘"))

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("quarkStack")

        self.shareTab = QWidget(self.stack)
        self.shareTab.setObjectName("shareTab")
        self.shareInput = PlainTextEdit(self.shareTab)
        self.shareInput.setObjectName("shareInput")
        self.shareInput.setPlaceholderText(self.tr("粘贴夸克分享链接，一行一条；支持带提取码的完整文案"))
        self.shareInput.setFixedHeight(120)

        self.outputFolderLabel = CaptionLabel(str(self._folder), self.shareTab)
        self.outputFolderLabel.setObjectName("outputFolderLabel")

        self.chooseFolderButton = PushButton(FluentIcon.FOLDER, self.tr("选择目录"), self.shareTab)
        self.chooseFolderButton.setObjectName("chooseFolderButton")
        self.parseShareButton = PrimaryPushButton(FluentIcon.SYNC, self.tr("解析分享"), self.shareTab)
        self.parseShareButton.setObjectName("parseShareButton")
        self.startSelectedButton = PrimaryPushButton(
            FluentIcon.DOWNLOAD, self.tr("开始下载已选"), self.shareTab,
        )
        self.startSelectedButton.setObjectName("startSelectedButton")

        self.selectAllLinksButton = PushButton(self.tr("全部全选"), self.shareTab)
        self.selectAllLinksButton.setObjectName("selectAllLinksButton")
        self.clearAllLinksButton = PushButton(self.tr("全部全不选"), self.shareTab)
        self.clearAllLinksButton.setObjectName("clearAllLinksButton")
        self.invertAllLinksButton = PushButton(self.tr("全部反选"), self.shareTab)
        self.invertAllLinksButton.setObjectName("invertAllLinksButton")
        self.collapseAllButton = PushButton(self.tr("全部折叠"), self.shareTab)
        self.collapseAllButton.setObjectName("collapseAllButton")
        self.retryFailedButton = PushButton(FluentIcon.SYNC, self.tr("重试失败项"), self.shareTab)
        self.retryFailedButton.setObjectName("retryFailedButton")
        self.clearListButton = PushButton(FluentIcon.DELETE, self.tr("清空列表"), self.shareTab)
        self.clearListButton.setObjectName("clearListButton")
        self.cleanTempButton = PushButton(FluentIcon.BROOM, self.tr("清理临时目录残留"), self.shareTab)
        self.cleanTempButton.setObjectName("cleanTempButton")

        self.shareSummaryLabel = CaptionLabel("", self.shareTab)
        self.shareSummaryLabel.setObjectName("shareSummaryLabel")
        self.shareStatus = CaptionLabel("", self.shareTab)
        self.shareStatus.setObjectName("shareStatus")

        self.shareList = QWidget()
        self.shareList.setObjectName("shareList")
        self.shareLayout = QVBoxLayout(self.shareList)
        self.shareScroll = ScrollArea(self.shareTab)
        self.shareScroll.setObjectName("shareScroll")
        self.shareScroll.setWidget(self.shareList)
        self.shareScroll.setWidgetResizable(True)
        self.shareScroll.enableTransparentBackground()
        self.shareScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shareList.setStyleSheet("QWidget#shareList{background: transparent;}")

        self.driveTab = QWidget(self.stack)
        self.driveTab.setObjectName("driveTab")
        self.driveBreadcrumb = BodyLabel(ROOT_TITLE, self.driveTab)
        self.driveBreadcrumb.setObjectName("driveBreadcrumb")
        self.driveUpButton = PushButton(FluentIcon.UP, self.tr("返回上级"), self.driveTab)
        self.driveUpButton.setObjectName("driveUpButton")
        self.driveRefreshButton = PushButton(FluentIcon.SYNC, self.tr("刷新"), self.driveTab)
        self.driveRefreshButton.setObjectName("driveRefreshButton")
        self.downloadSelectedButton = PrimaryPushButton(FluentIcon.DOWNLOAD, self.tr("下载所选"), self.driveTab)
        self.downloadSelectedButton.setObjectName("downloadSelectedButton")
        self.driveLoginHint = CaptionLabel(self.tr("请先在设置页登录夸克账号，再浏览网盘文件"), self.driveTab)
        self.driveLoginHint.setObjectName("driveLoginHint")
        self.driveStatus = CaptionLabel("", self.driveTab)
        self.driveStatus.setObjectName("driveStatus")

        self.driveList = QWidget()
        self.driveList.setObjectName("driveList")
        self.driveLayout = QVBoxLayout(self.driveList)
        self.driveScroll = ScrollArea(self.driveTab)
        self.driveScroll.setObjectName("driveScroll")
        self.driveScroll.setWidget(self.driveList)
        self.driveScroll.setWidgetResizable(True)
        self.driveScroll.enableTransparentBackground()
        self.driveScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.driveList.setStyleSheet("QWidget#driveList{background: transparent;}")

        for container in (self.stack, self.shareTab, self.driveTab):
            container.setStyleSheet(f"QWidget#{container.objectName()}{{background: transparent;}}")

    def _initLayout(self) -> None:
        folderToolbar = QHBoxLayout()
        folderToolbar.setSpacing(8)
        folderToolbar.addWidget(self.outputFolderLabel, 1)
        folderToolbar.addWidget(self.chooseFolderButton)
        folderToolbar.addWidget(self.parseShareButton)
        folderToolbar.addWidget(self.startSelectedButton)

        actionToolbar = QHBoxLayout()
        actionToolbar.setSpacing(8)
        actionToolbar.addWidget(self.selectAllLinksButton)
        actionToolbar.addWidget(self.clearAllLinksButton)
        actionToolbar.addWidget(self.invertAllLinksButton)
        actionToolbar.addWidget(self.collapseAllButton)
        actionToolbar.addWidget(self.retryFailedButton)
        actionToolbar.addWidget(self.clearListButton)
        actionToolbar.addWidget(self.cleanTempButton)
        actionToolbar.addStretch(1)

        shareTabLayout = QVBoxLayout(self.shareTab)
        shareTabLayout.setSpacing(8)
        shareTabLayout.addWidget(self.shareInput)
        shareTabLayout.addLayout(folderToolbar)
        shareTabLayout.addLayout(actionToolbar)
        shareTabLayout.addWidget(self.shareSummaryLabel)
        shareTabLayout.addWidget(self.shareScroll, 1)
        shareTabLayout.addWidget(self.shareStatus)

        driveToolbar = QHBoxLayout()
        driveToolbar.setSpacing(8)
        driveToolbar.addWidget(self.driveBreadcrumb, 1)
        for button in (self.driveUpButton, self.driveRefreshButton, self.downloadSelectedButton):
            driveToolbar.addWidget(button)

        driveTabLayout = QVBoxLayout(self.driveTab)
        driveTabLayout.setSpacing(8)
        driveTabLayout.addLayout(driveToolbar)
        driveTabLayout.addWidget(self.driveLoginHint)
        driveTabLayout.addWidget(self.driveScroll, 1)
        driveTabLayout.addWidget(self.driveStatus)

        self.stack.addWidget(self.shareTab)
        self.stack.addWidget(self.driveTab)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.segment)
        layout.addWidget(self.stack, 1)

        self.shareLayout.setContentsMargins(0, 0, 0, 0)
        self.shareLayout.addStretch(1)
        self.driveLayout.setContentsMargins(0, 0, 0, 0)
        self.driveLayout.addStretch(1)

        self.stack.setCurrentWidget(self.shareTab)
        self.segment.setCurrentItem("share")

    def _bind(self) -> None:
        self.segment.currentItemChanged.connect(self._onTabChanged)
        self.chooseFolderButton.clicked.connect(self._onChooseFolder)
        self.parseShareButton.clicked.connect(self._onParseShare)
        self.startSelectedButton.clicked.connect(self._onStartSelected)
        self.selectAllLinksButton.clicked.connect(lambda: self._setAllLinksSelection("all"))
        self.clearAllLinksButton.clicked.connect(lambda: self._setAllLinksSelection("none"))
        self.invertAllLinksButton.clicked.connect(lambda: self._setAllLinksSelection("invert"))
        self.collapseAllButton.clicked.connect(self._onCollapseAll)
        self.retryFailedButton.clicked.connect(self._onRetryFailed)
        self.clearListButton.clicked.connect(self._onClearList)
        self.cleanTempButton.clicked.connect(self._onCleanTemp)
        self.driveUpButton.clicked.connect(self.goUp)
        self.driveRefreshButton.clicked.connect(self.refreshDrive)
        self.downloadSelectedButton.clicked.connect(self.downloadSelected)
        self._queue.changed.connect(self._onQueueChanged, owner=self)
        self._pack.account.accountChanged.connect(self._onAccountChanged, owner=self)

    @property
    def _driveFid(self) -> str:
        return self._driveTrail[-1][0]

    @property
    def cards(self) -> list[ShareLinkCard]:
        return list(self._cards.values())

    def setSearchText(self, text: str) -> None:
        self._searchText = str(text or "").strip().lower()
        self._applySearch()

    def refreshDrive(self) -> None:
        if not self._pack.account.isLoggedIn:
            self._applyLoginState()
            return
        self._applyLoginState()
        self._driveNotice = ""
        self._driveGeneration += 1
        generation = self._driveGeneration
        self._pack.submit(
            self._pack.client.fetchDriveFiles(self._driveFid),
            done=partial(self._onDriveLoaded, generation),
            failed=partial(self._onDriveFailed, generation),
            owner=self,
        )

    def enterFolder(self, entry: DriveEntry) -> None:
        if not entry.isDir:
            return
        self._driveTrail.append((entry.fid, entry.name))
        self._applyBreadcrumb()
        self.refreshDrive()

    def goUp(self) -> None:
        if len(self._driveTrail) <= 1:
            return
        self._driveTrail.pop()
        self._applyBreadcrumb()
        self.refreshDrive()

    def downloadSelected(self) -> None:
        entries = [
            row.entry for row in self._driveRows
            if isinstance(row, DriveRow) and row.checkBox.isChecked()
        ]
        if not entries:
            self.driveStatus.setText(self.tr("请先勾选要下载的文件或文件夹"))
            return
        self.driveStatus.setText(self.tr("正在展开所选目录..."))
        self._pack.submit(
            self._buildSelectedTask(entries),
            done=self._onDriveTaskBuilt,
            failed=self._onDriveTaskFailed,
            owner=self,
        )

    async def _buildSelectedTask(self, entries: list[DriveEntry]) -> Task:
        client = self._pack.client
        tree = await client.fetchDriveTree(entries)
        task = buildDriveTask(tree, self._folder, name="我的网盘")
        task.client = client
        return task

    async def _fetchIdleTempFolders(self) -> list[str]:
        folders = await self._pack.client.fetchTempFolders()
        activeNames = QuarkTask.activeTempFolderNames
        return [
            entry.fid for entry in folders
            if entry.fid and entry.fid != ROOT_FID and entry.name not in activeNames
        ]

    async def _deleteTempFolders(self, fids: list[str]) -> list[str]:
        targets = [fid for fid in fids if fid and fid != ROOT_FID]
        if not targets:
            return []
        await self._pack.client.deleteRemote(targets)
        return targets

    def _onTabChanged(self, routeKey: str) -> None:
        self.stack.setCurrentWidget(self.driveTab if routeKey == "drive" else self.shareTab)
        self._applySearch()

    def _onChooseFolder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self.tr("选择下载目录"), str(self._folder))
        if not folder:
            return
        self._folder = Path(folder)
        self.outputFolderLabel.setText(str(self._folder))

    def _onParseShare(self) -> None:
        if not self._queue.setEntries(self.shareInput.toPlainText()):
            return
        self._queue.startParse(self._folder)

    def _onStartSelected(self) -> None:
        count = self._queue.submitSelected(self._folder)
        if count:
            self.shareStatus.setText(self.tr("已加入 {count} 个下载任务").format(count=count))
        else:
            self.shareStatus.setText(self.tr("没有勾选任何文件"))

    def _onRetryFailed(self) -> None:
        for card in self._cards.values():
            entry = self._queue.entryById(card.pwdId)
            if entry is not None and entry.status == "failed":
                self._queue.setPasscode(card.pwdId, card.passcodeEdit.text().strip())
        self._queue.retryFailed(self._folder)

    def _onClearList(self) -> None:
        self._queue.clearEntries()

    def _onCollapseAll(self) -> None:
        for card in self._cards.values():
            card.setExpanded(False)

    def _setAllLinksSelection(self, mode: str) -> None:
        for entry in self._queue.entries:
            if entry.status != "ready" or entry.task is None:
                continue
            allIndexes = {file.index for file in entry.task.files}
            if mode == "all":
                selected = allIndexes
            elif mode == "none":
                selected = set()
            else:
                selected = allIndexes - entry.selectedIndexes
            self._queue.setSelectedIndexes(entry.pwdId, selected)

    def _onCleanTemp(self) -> None:
        self.cleanTempButton.setEnabled(False)
        self.shareStatus.setText(self.tr("正在查询临时目录..."))
        self._pack.submit(
            self._fetchIdleTempFolders(),
            done=self._onIdleTempFoldersFetched,
            failed=self._onTempFoldersFailed,
            owner=self,
        )

    def _onIdleTempFoldersFetched(self, fids: list[str]) -> None:
        if not fids:
            self.cleanTempButton.setEnabled(True)
            self.shareStatus.setText(self.tr("没有需要清理的临时目录"))
            return
        dialog = MessageBox(
            self.tr("清理临时目录"),
            self.tr("将删除 {count} 个已结束任务留下的临时目录，正在下载的临时目录会保留。").format(
                count=len(fids),
            ),
            self,
        )
        isConfirmed = bool(dialog.exec())
        dialog.deleteLater()
        if not isConfirmed:
            self.cleanTempButton.setEnabled(True)
            self.shareStatus.setText("")
            return
        self._pack.submit(
            self._deleteTempFolders(fids),
            done=self._onTempFoldersDeleted,
            failed=self._onTempFoldersFailed,
            owner=self,
        )

    def _onQueueChanged(self) -> None:
        self._syncCards()
        self._updateShareSummary()
        isIdle = not self._queue.isBusy
        self.parseShareButton.setEnabled(isIdle)
        self.startSelectedButton.setEnabled(isIdle)
        self.retryFailedButton.setEnabled(isIdle)
        self.clearListButton.setEnabled(isIdle)

    def _syncCards(self) -> None:
        entries = self._queue.entries
        pwdIds = [entry.pwdId for entry in entries]
        for pwdId, card in list(self._cards.items()):
            if pwdId in pwdIds:
                continue
            self.shareLayout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
            del self._cards[pwdId]
        for index, entry in enumerate(entries):
            card = self._cards.get(entry.pwdId)
            if card is None:
                card = ShareLinkCard(self._queue, entry.pwdId, self.shareList)
                card.retryRequested.connect(self._onCardRetryRequested)
                self.shareLayout.insertWidget(index, card)
                self._cards[entry.pwdId] = card
            card.refresh()
        self._applySearch()

    def _onCardRetryRequested(self, pwdId: str) -> None:
        self._queue.retry(pwdId, self._folder)

    def _updateShareSummary(self) -> None:
        entries = self._queue.entries
        ready = sum(1 for entry in entries if entry.status in ("ready", "queued"))
        parsing = sum(1 for entry in entries if entry.status in ("pending", "running"))
        failed = sum(1 for entry in entries if entry.status == "failed")
        selected = sum(len(entry.selectedIndexes) for entry in entries)
        size = sum(entry.selectedSize for entry in entries)
        self.shareSummaryLabel.setText(
            self.tr("共 {count} 条链接（就绪 {ready} / 解析中 {parsing} / 失败 {failed}），"
                    "已选 {selected} 个文件，总大小 {size}").format(
                count=len(entries), ready=ready, parsing=parsing, failed=failed,
                selected=selected, size=toReadableSize(size),
            )
        )

    def _onAccountChanged(self) -> None:
        isLoggedIn = self._pack.account.isLoggedIn
        self._applyLoginState()
        if isLoggedIn == self._isLoggedIn:
            return
        self._isLoggedIn = isLoggedIn
        if isLoggedIn:
            self.refreshDrive()
            return
        self._driveGeneration += 1
        self._driveTrail = [(ROOT_FID, ROOT_TITLE)]
        self._applyBreadcrumb()
        self._driveEntries = []
        self._buildDriveRows()

    def _onDriveLoaded(self, generation: int, entries: list[DriveEntry]) -> None:
        if generation != self._driveGeneration:
            return
        self._driveEntries = list(entries)
        self._buildDriveRows()

    def _onDriveFailed(self, generation: int, error: Exception) -> None:
        if generation != self._driveGeneration:
            return
        self._driveEntries = []
        self._driveNotice = str(error)
        self._buildDriveRows()

    def _onDriveTaskBuilt(self, task: Task) -> None:
        self._pack.addTask(task)
        self.driveStatus.setText(self.tr("已加入下载队列"))

    def _onDriveTaskFailed(self, error: Exception) -> None:
        self.driveStatus.setText(str(error))

    def _onTempFoldersDeleted(self, fids: list[str]) -> None:
        self.cleanTempButton.setEnabled(True)
        self.shareStatus.setText(self.tr("已清理 {count} 个临时目录").format(count=len(fids)))

    def _onTempFoldersFailed(self, error: Exception) -> None:
        self.cleanTempButton.setEnabled(True)
        self.shareStatus.setText(str(error))

    def _applyLoginState(self) -> None:
        isLoggedIn = self._pack.account.isLoggedIn
        self.driveLoginHint.setVisible(not isLoggedIn)
        self.driveBreadcrumb.setVisible(isLoggedIn)
        self.driveUpButton.setVisible(isLoggedIn)
        self.driveRefreshButton.setVisible(isLoggedIn)
        self.downloadSelectedButton.setVisible(isLoggedIn)
        self.driveScroll.setVisible(isLoggedIn)
        self.driveStatus.setVisible(isLoggedIn)
        self.driveUpButton.setEnabled(len(self._driveTrail) > 1)

    def _applyBreadcrumb(self) -> None:
        self.driveBreadcrumb.setText(BREADCRUMB_SEPARATOR.join(title for _fid, title in self._driveTrail))
        self.driveUpButton.setEnabled(len(self._driveTrail) > 1)

    def _applySearch(self) -> None:
        for card in self._cards.values():
            card.setVisible(card.isMatch(self._searchText))
        for row in self._driveRows:
            if isinstance(row, DriveRow):
                row.setVisible(self._isMatch(row.entry.name))

    def _isMatch(self, text: str) -> bool:
        return not self._searchText or self._searchText in text.lower()

    def _buildDriveRows(self) -> None:
        self._clearRows(self._driveRows, self.driveLayout)
        if not self._driveEntries:
            notice = CaptionLabel(self._driveNotice or self.tr("这个目录是空的"), self.driveList)
            self.driveLayout.insertWidget(0, notice)
            self._driveRows.append(notice)
            return
        for index, entry in enumerate(self._driveEntries):
            row = DriveRow(entry, self.driveList)
            row.doubleClicked.connect(lambda row=row: self.enterFolder(row.entry))
            self.driveLayout.insertWidget(index, row)
            self._driveRows.append(row)
        self._applySearch()

    def _clearRows(self, rows: list[QWidget], layout: QVBoxLayout) -> None:
        for row in rows:
            layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        rows.clear()
