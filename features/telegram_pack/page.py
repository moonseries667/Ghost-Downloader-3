from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    ListWidget,
    MessageBoxBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
)

from app.config.cfg import cfg
from app.format import toReadableSize
from app.i18n import N
from app.models.pack import PackPage
from app.models.task import TaskOptions
from app.view.components.scroll_area import ScrollArea
from .parse import TelegramLink, parseTelegramUrl

if TYPE_CHECKING:
    from app.models.task import Task
    from .inbox import InboxBundle
    from .pack import TelegramPack
    from .parse import MediaBundle
    from .runtime import TelegramChat

STATUS_LABELS = {
    "new": "待下载",
    "queued": "已入队",
    "completed": "已完成",
    "skipped": "已跳过",
}

FILTER_OPTIONS = (
    ("all", "全部状态"),
    ("new", "待下载"),
    ("queued", "已入队"),
    ("completed", "已完成"),
    ("skipped", "已跳过"),
)

CHAT_TYPE_LABELS = {
    "channel": "频道",
    "group": "群组",
    "private": "私聊",
}

TITLE_LIMIT = 60
PREVIEW_LIMIT = 3
TOKEN_SPLIT_PATTERN = re.compile(r"\s+")


def extractTelegramLinks(text: str) -> list[TelegramLink]:
    seen: set[str] = set()
    links: list[TelegramLink] = []
    for token in TOKEN_SPLIT_PATTERN.split(text or ""):
        cleaned = token.strip("<>()[]\"',;")
        link = parseTelegramUrl(cleaned)
        if link is None or link.url in seen:
            continue
        seen.add(link.url)
        links.append(link)
    return links


def formatBundleTitle(bundle: InboxBundle) -> str:
    for line in (bundle.caption or "").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned[:TITLE_LIMIT] + ("..." if len(cleaned) > TITLE_LIMIT else "")
    if bundle.fileNames:
        return bundle.fileNames[0]
    return f"消息 #{bundle.firstMessageId}"


def formatBundleMeta(bundle: InboxBundle) -> str:
    fileCount = len(bundle.fileNames) or len(bundle.messageIds)
    messageRange = (
        f"#{bundle.firstMessageId}"
        if bundle.firstMessageId == bundle.lastMessageId
        else f"#{bundle.firstMessageId}~#{bundle.lastMessageId}"
    )
    parts = [f"{fileCount} 个文件", toReadableSize(bundle.totalSize), messageRange]
    if bundle.fileNames:
        preview = ", ".join(bundle.fileNames[:PREVIEW_LIMIT])
        if len(bundle.fileNames) > PREVIEW_LIMIT:
            preview += f" 等 {len(bundle.fileNames)} 项"
        parts.append(preview)
    return " | ".join(parts)


class InboxBundleCard(SimpleCardWidget):
    downloadRequested = QtSignal(str)
    skipToggled = QtSignal(str, str)
    selectionChanged = QtSignal()

    def __init__(self, bundle: InboxBundle, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bundle = bundle
        self._initWidget()
        self._initLayout()
        self._bind()

    def _initWidget(self) -> None:
        self.setObjectName(f"bundle-{self.bundle.bundleKey}")
        self.checkBox = CheckBox(self)
        self.checkBox.setObjectName(f"check-{self.bundle.bundleKey}")
        self.checkBox.setChecked(self.bundle.status == "new")

        self.titleLabel = BodyLabel(formatBundleTitle(self.bundle), self)
        self.titleLabel.setWordWrap(True)

        self.metaLabel = CaptionLabel(formatBundleMeta(self.bundle), self)
        self.metaLabel.setWordWrap(True)

        self.statusLabel = CaptionLabel(
            STATUS_LABELS.get(self.bundle.status, self.bundle.status),
            self,
        )
        self.statusLabel.setObjectName(f"status-{self.bundle.bundleKey}")
        self.statusLabel.setMinimumWidth(56)
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.downloadButton = PushButton(FluentIcon.DOWNLOAD, self.tr("下载"), self)
        self.downloadButton.setObjectName(f"download-{self.bundle.bundleKey}")

        skipText = self.tr("恢复待下载") if self.bundle.status == "skipped" else self.tr("跳过")
        self.skipButton = PushButton(skipText, self)
        self.skipButton.setObjectName(f"skip-{self.bundle.bundleKey}")

    def _initLayout(self) -> None:
        textLayout = QVBoxLayout()
        textLayout.setContentsMargins(0, 0, 0, 0)
        textLayout.setSpacing(2)
        textLayout.addWidget(self.titleLabel)
        textLayout.addWidget(self.metaLabel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(self.checkBox)
        layout.addLayout(textLayout, 1)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.downloadButton)
        layout.addWidget(self.skipButton)

    def _bind(self) -> None:
        self.checkBox.stateChanged.connect(lambda *_: self.selectionChanged.emit())
        self.downloadButton.clicked.connect(lambda: self.downloadRequested.emit(self.bundle.bundleKey))
        self.skipButton.clicked.connect(self._onSkipClicked)

    def _onSkipClicked(self) -> None:
        nextStatus = "new" if self.bundle.status == "skipped" else "skipped"
        self.skipToggled.emit(self.bundle.bundleKey, nextStatus)

    def isMatch(self, keyword: str) -> bool:
        needle = (keyword or "").strip().lower()
        if not needle:
            return True
        haystacks = [
            self.bundle.bundleKey,
            self.bundle.caption,
            *self.bundle.fileNames,
            *(str(msgId) for msgId in self.bundle.messageIds),
        ]
        return any(needle in item.lower() for item in haystacks if item)


class TelegramChannelDialog(MessageBoxBase):
    def __init__(
        self,
        chats: list[TelegramChat],
        currentChatId: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._chats = sorted(
            chats,
            key=lambda chat: (0 if chat.type.lower() == "channel" else 1, (chat.title or chat.chatId).casefold()),
        )
        self._currentChatId = str(currentChatId or "").strip()
        self._initWidget()
        self._initLayout()
        self._bind()
        self._populateChats("")

    def _initWidget(self) -> None:
        self.widget.setMinimumWidth(480)
        self.yesButton.setText(self.tr("绑定频道"))
        self.cancelButton.setText(self.tr("取消"))

        self.titleLabel = SubtitleLabel(self.tr("选择个人私密频道"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("优先展示频道列表；也可直接在下方手动填写频道 ID 或用户名"),
            self.widget,
        )
        self.descriptionLabel.setWordWrap(True)

        self.searchEdit = SearchLineEdit(self.widget)
        self.searchEdit.setObjectName("channelSearchEdit")
        self.searchEdit.setPlaceholderText(self.tr("搜索频道名称、用户名或 ID"))

        self.chatList = ListWidget(self.widget)
        self.chatList.setObjectName("channelListWidget")
        self.chatList.setMinimumHeight(200)

        self.manualIdEdit = LineEdit(self.widget)
        self.manualIdEdit.setObjectName("manualChannelIdEdit")
        self.manualIdEdit.setPlaceholderText(self.tr("频道 ID 或 @username"))
        self.manualIdEdit.setText(self._currentChatId)

        self.manualTitleEdit = LineEdit(self.widget)
        self.manualTitleEdit.setObjectName("manualChannelTitleEdit")
        self.manualTitleEdit.setPlaceholderText(self.tr("显示名称（可选）"))

    def _initLayout(self) -> None:
        manualRow = QHBoxLayout()
        manualRow.setSpacing(8)
        manualRow.addWidget(self.manualIdEdit, 1)
        manualRow.addWidget(self.manualTitleEdit, 1)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addWidget(self.searchEdit)
        self.viewLayout.addWidget(self.chatList)
        self.viewLayout.addLayout(manualRow)

    def _bind(self) -> None:
        self.searchEdit.textChanged.connect(self._populateChats)
        self.chatList.currentItemChanged.connect(self._onCurrentItemChanged)
        self.chatList.itemDoubleClicked.connect(lambda *_: self.accept())

    def _populateChats(self, keyword: str) -> None:
        needle = (keyword or "").strip().lower()
        self.chatList.clear()
        matchedCurrent = None
        for chat in self._chats:
            title = chat.title or chat.username or chat.chatId
            fields = (title, chat.chatId, chat.username, chat.type)
            if needle and not any(needle in value.lower() for value in fields if value):
                continue
            typeLabel = CHAT_TYPE_LABELS.get(chat.type.lower(), chat.type or "对话")
            usernamePart = f" @{chat.username}" if chat.username else ""
            item = QListWidgetItem(f"[{typeLabel}] {title}{usernamePart} ({chat.chatId})")
            item.setData(Qt.ItemDataRole.UserRole, (chat.chatId, title))
            self.chatList.addItem(item)
            if chat.chatId == self._currentChatId:
                matchedCurrent = item
        if matchedCurrent is not None:
            self.chatList.setCurrentItem(matchedCurrent)

    def _onCurrentItemChanged(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        chatId, title = data
        self.manualIdEdit.setText(str(chatId))
        self.manualTitleEdit.setText(str(title))

    def selectedChat(self) -> tuple[str, str]:
        chatId = self.manualIdEdit.text().strip()
        title = self.manualTitleEdit.text().strip()
        if not chatId:
            current = self.chatList.currentItem()
            if current is not None:
                data = current.data(Qt.ItemDataRole.UserRole)
                if data:
                    return str(data[0]), str(data[1] or data[0])
        return chatId, title or chatId


class TelegramPage(PackPage, QWidget):
    icon = FluentIcon.SEND
    title = N("PackPage", "Telegram")
    searchPlaceholder = "搜索收件箱消息或文件名"

    def __init__(self, pack: TelegramPack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pack = pack
        self._folder = Path(cfg.downloadFolder.value)
        self._searchText = ""
        self._statusFilter = "all"
        self._cards: list[InboxBundleCard] = []
        self._emptyLabel: CaptionLabel | None = None
        self._directLinks: list[TelegramLink] = []
        self._directSucceeded = 0
        self._directFailed = 0
        self._isDirectBusy = False
        self._isSyncBusy = False
        self._isChatListBusy = False
        self._syncGeneration = 0

        self._initWidget()
        self._initLayout()
        self._bind()
        self._onAccountStateChanged()
        self._reloadInbox()

    def _initWidget(self) -> None:
        self.directCard = SimpleCardWidget(self)
        self.directCard.setObjectName("directCard")
        self.directTitleLabel = StrongBodyLabel(self.tr("紧急链接直下"), self.directCard)
        self.outputFolderLabel = CaptionLabel(str(self._folder), self.directCard)
        self.outputFolderLabel.setObjectName("outputFolderLabel")
        self.chooseFolderButton = PushButton(FluentIcon.FOLDER, self.tr("选择目录"), self.directCard)
        self.chooseFolderButton.setObjectName("chooseFolderButton")

        self.directInput = PlainTextEdit(self.directCard)
        self.directInput.setObjectName("directInput")
        self.directInput.setPlaceholderText(
            self.tr("粘贴单条或多条 t.me/... 消息链接（支持受保护禁止转发频道、媒体组与 ?single）")
        )
        self.directInput.setFixedHeight(76)

        self.directDownloadButton = PrimaryPushButton(
            FluentIcon.DOWNLOAD, self.tr("解析并下载"), self.directCard,
        )
        self.directDownloadButton.setObjectName("directDownloadButton")
        self.directStatus = CaptionLabel("", self.directCard)
        self.directStatus.setObjectName("directStatus")
        self.directStatus.setWordWrap(True)

        self.inboxHeaderCard = SimpleCardWidget(self)
        self.inboxHeaderCard.setObjectName("inboxHeaderCard")
        self.inboxTitleLabel = StrongBodyLabel(self.tr("个人频道收件箱"), self.inboxHeaderCard)
        self.channelLabel = BodyLabel("", self.inboxHeaderCard)
        self.channelLabel.setObjectName("channelLabel")
        self.watermarkLabel = CaptionLabel("", self.inboxHeaderCard)
        self.watermarkLabel.setObjectName("watermarkLabel")

        self.bindChannelButton = PushButton(FluentIcon.SETTING, self.tr("绑定频道"), self.inboxHeaderCard)
        self.bindChannelButton.setObjectName("bindChannelButton")
        self.syncButton = PrimaryPushButton(FluentIcon.SYNC, self.tr("拉取新消息"), self.inboxHeaderCard)
        self.syncButton.setObjectName("syncButton")

        self.filterCombo = ComboBox(self.inboxHeaderCard)
        self.filterCombo.setObjectName("filterCombo")
        for key, label in FILTER_OPTIONS:
            self.filterCombo.addItem(self.tr(label), userData=key)

        self.selectPendingButton = PushButton(self.tr("全选待下载"), self.inboxHeaderCard)
        self.selectPendingButton.setObjectName("selectPendingButton")
        self.clearSelectButton = PushButton(self.tr("取消选择"), self.inboxHeaderCard)
        self.clearSelectButton.setObjectName("clearSelectButton")
        self.downloadSelectedButton = PrimaryPushButton(
            FluentIcon.DOWNLOAD, self.tr("下载已选"), self.inboxHeaderCard,
        )
        self.downloadSelectedButton.setObjectName("downloadSelectedButton")
        self.downloadAllPendingButton = PushButton(
            FluentIcon.PLAY, self.tr("一键下载待下载"), self.inboxHeaderCard,
        )
        self.downloadAllPendingButton.setObjectName("downloadAllPendingButton")

        self.inboxSummaryLabel = CaptionLabel("", self.inboxHeaderCard)
        self.inboxSummaryLabel.setObjectName("inboxSummaryLabel")
        self.inboxStatus = CaptionLabel("", self.inboxHeaderCard)
        self.inboxStatus.setObjectName("inboxStatus")
        self.inboxStatus.setWordWrap(True)
        self.loginHint = CaptionLabel(
            self.tr("请先在设置页 -> Telegram 完成扫码登录（支持 2FA）"),
            self.inboxHeaderCard,
        )
        self.loginHint.setObjectName("loginHint")

        self.inboxList = QWidget()
        self.inboxList.setObjectName("inboxList")
        self.inboxLayout = QVBoxLayout(self.inboxList)
        self.inboxLayout.setContentsMargins(0, 0, 0, 0)
        self.inboxLayout.setSpacing(8)
        self.inboxLayout.addStretch(1)

        self.inboxScroll = ScrollArea(self)
        self.inboxScroll.setObjectName("inboxScroll")
        self.inboxScroll.setWidget(self.inboxList)
        self.inboxScroll.setWidgetResizable(True)
        self.inboxScroll.enableTransparentBackground()
        self.inboxScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inboxList.setStyleSheet("QWidget#inboxList{background: transparent;}")

    def _initLayout(self) -> None:
        directTop = QHBoxLayout()
        directTop.setSpacing(8)
        directTop.addWidget(self.directTitleLabel)
        directTop.addStretch(1)
        directTop.addWidget(self.outputFolderLabel)
        directTop.addWidget(self.chooseFolderButton)

        directActions = QHBoxLayout()
        directActions.setSpacing(8)
        directActions.addWidget(self.directDownloadButton)
        directActions.addWidget(self.directStatus, 1)

        directLayout = QVBoxLayout(self.directCard)
        directLayout.setContentsMargins(16, 12, 16, 12)
        directLayout.setSpacing(8)
        directLayout.addLayout(directTop)
        directLayout.addWidget(self.directInput)
        directLayout.addLayout(directActions)

        channelRow = QHBoxLayout()
        channelRow.setSpacing(8)
        channelRow.addWidget(self.inboxTitleLabel)
        channelRow.addWidget(self.channelLabel, 1)
        channelRow.addWidget(self.watermarkLabel)
        channelRow.addWidget(self.bindChannelButton)
        channelRow.addWidget(self.syncButton)

        batchRow = QHBoxLayout()
        batchRow.setSpacing(8)
        batchRow.addWidget(self.filterCombo)
        batchRow.addWidget(self.selectPendingButton)
        batchRow.addWidget(self.clearSelectButton)
        batchRow.addStretch(1)
        batchRow.addWidget(self.downloadSelectedButton)
        batchRow.addWidget(self.downloadAllPendingButton)

        inboxHeaderLayout = QVBoxLayout(self.inboxHeaderCard)
        inboxHeaderLayout.setContentsMargins(16, 12, 16, 12)
        inboxHeaderLayout.setSpacing(8)
        inboxHeaderLayout.addLayout(channelRow)
        inboxHeaderLayout.addLayout(batchRow)
        inboxHeaderLayout.addWidget(self.inboxSummaryLabel)
        inboxHeaderLayout.addWidget(self.inboxStatus)
        inboxHeaderLayout.addWidget(self.loginHint)

        rootLayout = QVBoxLayout(self)
        rootLayout.setContentsMargins(20, 16, 20, 16)
        rootLayout.setSpacing(12)
        rootLayout.addWidget(self.directCard)
        rootLayout.addWidget(self.inboxHeaderCard)
        rootLayout.addWidget(self.inboxScroll, 1)

    def _bind(self) -> None:
        self.chooseFolderButton.clicked.connect(self._onChooseFolder)
        self.directDownloadButton.clicked.connect(self._onDirectDownload)
        self.bindChannelButton.clicked.connect(self._onBindChannelClicked)
        self.syncButton.clicked.connect(self._onSyncClicked)
        self.filterCombo.currentIndexChanged.connect(self._onFilterChanged)
        self.selectPendingButton.clicked.connect(self._onSelectPending)
        self.clearSelectButton.clicked.connect(self._onClearSelection)
        self.downloadSelectedButton.clicked.connect(self._onDownloadSelected)
        self.downloadAllPendingButton.clicked.connect(self._onDownloadAllPending)
        self._pack.account.stateChanged.connect(self._onAccountStateChanged, owner=self)
        self._pack.inboxChanged.connect(self._reloadInbox, owner=self)

    def setSearchText(self, text: str) -> None:
        self._searchText = (text or "").strip().lower()
        self._applyFilters()

    def _onChooseFolder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self.window(),
            self.tr("选择下载目录"),
            str(self._folder),
        )
        if selected:
            self._folder = Path(selected)
            self.outputFolderLabel.setText(str(self._folder))

    def _onDirectDownload(self) -> None:
        if self._isDirectBusy:
            return
        links = extractTelegramLinks(self.directInput.toPlainText())
        if not links:
            self.directStatus.setText(self.tr("未识别到有效的 Telegram 消息链接"))
            return
        self._directLinks = list(links)
        self._directSucceeded = 0
        self._directFailed = 0
        self._isDirectBusy = True
        self.directDownloadButton.setEnabled(False)
        busyTip = (
            self.tr("（当前有 Telegram 任务正在执行，已按排队模式解析）")
            if self._pack.runner.isBusy
            else ""
        )
        self.directStatus.setText(
            self.tr("正在解析 {count} 条链接...{tip}").format(count=len(links), tip=busyTip)
        )
        self._runNextDirectLink()

    def _runNextDirectLink(self) -> None:
        if not self._directLinks:
            self._isDirectBusy = False
            self.directDownloadButton.setEnabled(True)
            if self._directFailed:
                self.directStatus.setText(
                    self.tr("已加入 {ok} 个任务，失败 {fail} 条").format(
                        ok=self._directSucceeded, fail=self._directFailed,
                    )
                )
            else:
                self.directStatus.setText(
                    self.tr("已加入 {ok} 个下载任务").format(ok=self._directSucceeded)
                )
            return
        link = self._directLinks.pop(0)
        options = TaskOptions(url=link.url, outputFolder=self._folder)
        self._pack.submit(
            self._pack.parse(options),
            done=self._onDirectParsed,
            failed=self._onDirectFailed,
            owner=self,
        )

    def _onDirectParsed(self, task: Task) -> None:
        if self._pack.addTask(task) is not False:
            self._directSucceeded += 1
        else:
            self._directFailed += 1
        self._runNextDirectLink()

    def _onDirectFailed(self, _error: Exception) -> None:
        self._directFailed += 1
        self._runNextDirectLink()

    def _onBindChannelClicked(self) -> None:
        if self._isChatListBusy:
            return
        if not self._pack.account.isInstalled:
            self.inboxStatus.setText(self.tr("未安装 tdl，请先在设置页安装 Telegram 下载器"))
            return
        self._isChatListBusy = True
        self.bindChannelButton.setEnabled(False)
        busyTip = (
            self.tr("当前有 Telegram 任务正在执行，正在排队获取频道列表...")
            if self._pack.runner.isBusy
            else self.tr("正在获取频道列表...")
        )
        self.inboxStatus.setText(busyTip)
        self._pack.submit(
            self._pack.runner.listChats(),
            done=self._onChatsLoaded,
            failed=self._onChatsFailed,
            owner=self,
        )

    def _onChatsLoaded(self, chats: list[TelegramChat]) -> None:
        self._isChatListBusy = False
        self.bindChannelButton.setEnabled(True)
        self.inboxStatus.setText("")
        self._openChannelDialog(chats)

    def _onChatsFailed(self, error: Exception) -> None:
        from .account import readProxy, toSafeErrorText
        self._isChatListBusy = False
        self.bindChannelButton.setEnabled(True)
        detail = toSafeErrorText(error, proxyUrl=readProxy(self._pack.runner))
        self.inboxStatus.setText(self.tr("拉取频道列表失败：{detail}").format(detail=detail))

    def _openChannelDialog(self, chats: list[TelegramChat]) -> None:
        dialog = TelegramChannelDialog(chats, self._pack.inbox.chatId, self.window())
        try:
            isConfirmed = dialog.exec()
            chatId, title = dialog.selectedChat()
        finally:
            dialog.deleteLater()
        if not isConfirmed or not chatId:
            return
        self._syncGeneration += 1
        self._isSyncBusy = False
        self._pack.bindInboxChannel(chatId, title)
        self.inboxStatus.setText(self.tr("已绑定频道：{title}").format(title=title))

    def _onSyncClicked(self) -> None:
        if self._isSyncBusy:
            return
        chatId = self._pack.inbox.chatId
        if not chatId:
            self.inboxStatus.setText(self.tr("请先绑定个人频道"))
            return
        self._syncGeneration += 1
        generation = self._syncGeneration
        self._isSyncBusy = True
        self._updateActionStates()
        if self._pack.runner.isBusy:
            self.inboxStatus.setText(self.tr("当前有 Telegram 任务正在执行，正在排队拉取新消息..."))
        else:
            self.inboxStatus.setText(self.tr("正在拉取新消息..."))
        self._pack.submit(
            self._pack.fetchInboxUpdates(chatId, sinceMsgId=self._pack.inbox.lastMsgId),
            done=lambda bundles, gen=generation, cid=chatId: self._onSyncDone(gen, cid, bundles),
            failed=lambda error, gen=generation: self._onSyncFailed(gen, error),
            owner=self,
        )

    def _onSyncDone(self, generation: int, chatId: str, bundles: list[MediaBundle]) -> None:
        if generation != self._syncGeneration or chatId != self._pack.inbox.chatId:
            return
        self._isSyncBusy = False
        added = self._pack.applyInboxUpdates(bundles)
        self._updateActionStates()
        if added:
            self.inboxStatus.setText(self.tr("已同步，新增 {count} 条媒体组").format(count=len(added)))
        else:
            self.inboxStatus.setText(self.tr("已同步，暂无新消息"))

    def _onSyncFailed(self, generation: int, error: Exception) -> None:
        from .account import readProxy, toSafeErrorText
        if generation != self._syncGeneration:
            return
        self._isSyncBusy = False
        self._updateActionStates()
        detail = toSafeErrorText(error, proxyUrl=readProxy(self._pack.runner))
        self.inboxStatus.setText(self.tr("同步失败：{detail}").format(detail=detail))

    def _onFilterChanged(self, index: int) -> None:
        if 0 <= index < len(FILTER_OPTIONS):
            self._statusFilter = FILTER_OPTIONS[index][0]
        else:
            self._statusFilter = "all"
        self._applyFilters()

    def _onSelectPending(self) -> None:
        for card in self._cards:
            if not card.isHidden():
                card.checkBox.setChecked(card.bundle.status == "new")
        self._updateSummary()

    def _onClearSelection(self) -> None:
        for card in self._cards:
            card.checkBox.setChecked(False)
        self._updateSummary()

    def _onDownloadBundle(self, bundleKey: str) -> None:
        task = self._pack.enqueueInboxBundle(bundleKey, self._folder)
        if task is not None:
            self.inboxStatus.setText(self.tr("已加入下载队列：{name}").format(name=task.name))

    def _onSkipToggled(self, bundleKey: str, status: str) -> None:
        self._pack.setInboxBundleStatus(bundleKey, status)

    def _onDownloadSelected(self) -> None:
        keys = [
            card.bundle.bundleKey
            for card in self._cards
            if not card.isHidden() and card.checkBox.isChecked()
        ]
        self._enqueueKeys(keys)

    def _onDownloadAllPending(self) -> None:
        keys = [bundle.bundleKey for bundle in self._pack.inbox.pendingBundles()]
        self._enqueueKeys(keys)

    def _enqueueKeys(self, keys: list[str]) -> None:
        if not keys:
            self.inboxStatus.setText(self.tr("没有可入队的媒体组"))
            return
        added = 0
        for key in keys:
            if self._pack.enqueueInboxBundle(key, self._folder) is not None:
                added += 1
        self.inboxStatus.setText(self.tr("已加入 {count} 个下载任务").format(count=added))

    def _onAccountStateChanged(self) -> None:
        isLoggedIn = self._pack.account.isLoggedIn
        self.loginHint.setVisible(not isLoggedIn)
        self._updateActionStates()

    def _reloadInbox(self) -> None:
        inbox = self._pack.inbox
        if inbox.chatId:
            display = f"{inbox.title} ({inbox.chatId})" if inbox.title else inbox.chatId
            self.channelLabel.setText(display)
        else:
            self.channelLabel.setText(self.tr("尚未绑定个人频道"))
        self.watermarkLabel.setText(
            self.tr("已同步至消息 #{msgId}").format(msgId=inbox.lastMsgId)
            if inbox.lastMsgId > 0
            else self.tr("尚未同步")
        )

        self._clearCards()
        bundles = list(reversed(inbox.bundles))
        if not bundles:
            emptyText = (
                self.tr('还没有同步到的媒体消息，可点击"拉取新消息"')
                if inbox.chatId
                else self.tr('请先点击"绑定频道"选择你的个人私密频道')
            )
            self._emptyLabel = CaptionLabel(emptyText, self.inboxList)
            self._emptyLabel.setObjectName("inboxEmptyLabel")
            self.inboxLayout.insertWidget(0, self._emptyLabel)
        else:
            for index, bundle in enumerate(bundles):
                card = InboxBundleCard(bundle, self.inboxList)
                card.downloadRequested.connect(self._onDownloadBundle)
                card.skipToggled.connect(self._onSkipToggled)
                card.selectionChanged.connect(self._updateSummary)
                self.inboxLayout.insertWidget(index, card)
                self._cards.append(card)
        self._applyFilters()
        self._updateActionStates()

    def _clearCards(self) -> None:
        for card in self._cards:
            self.inboxLayout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        if self._emptyLabel is not None:
            self.inboxLayout.removeWidget(self._emptyLabel)
            self._emptyLabel.setParent(None)
            self._emptyLabel.deleteLater()
            self._emptyLabel = None

    def _applyFilters(self) -> None:
        for card in self._cards:
            statusMatched = self._statusFilter == "all" or card.bundle.status == self._statusFilter
            searchMatched = card.isMatch(self._searchText)
            card.setVisible(statusMatched and searchMatched)
        self._updateSummary()

    def _updateSummary(self) -> None:
        bundles = self._pack.inbox.bundles
        pending = sum(1 for bundle in bundles if bundle.status == "new")
        queued = sum(1 for bundle in bundles if bundle.status == "queued")
        completed = sum(1 for bundle in bundles if bundle.status == "completed")
        selectedCards = [card for card in self._cards if not card.isHidden() and card.checkBox.isChecked()]
        selectedSize = sum(card.bundle.totalSize for card in selectedCards)
        self.inboxSummaryLabel.setText(
            self.tr("共 {total} 组（待下载 {pending} / 已入队 {queued} / 已完成 {completed}），"
                    "当前已选 {selected} 组，大小 {size}").format(
                total=len(bundles),
                pending=pending,
                queued=queued,
                completed=completed,
                selected=len(selectedCards),
                size=toReadableSize(selectedSize),
            )
        )
        self.downloadSelectedButton.setEnabled(bool(selectedCards))
        self.downloadAllPendingButton.setEnabled(pending > 0)

    def _updateActionStates(self) -> None:
        hasChannel = bool(self._pack.inbox.chatId)
        self.syncButton.setEnabled(hasChannel and not self._isSyncBusy)
        self.bindChannelButton.setEnabled(not self._isChatListBusy)
