from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import QFileInfo, QModelIndex, QSignalBlocker, Qt, Signal as QtSignal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QFileIconProvider, QHBoxLayout, QHeaderView, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, FluentIcon, LineEdit, PushButton, SearchLineEdit,
    SimpleCardWidget, StrongBodyLabel, TransparentToolButton,
)

from app.format import toReadableSize
from app.models.task import TaskOptions
from app.signal import Signal
from app.view.components.tree_view import AutoSizingTreeView
from .parse import QuarkShareRef, extractShareEntries, toCanonicalShareUrl

if TYPE_CHECKING:
    from app.models.task import Task
    from .pack import QuarkPack
    from .task import QuarkTask

FILE_INDEX_ROLE = Qt.ItemDataRole.UserRole


@dataclass
class ShareSelectionEntry:
    pwdId: str
    passcode: str = ""
    status: str = "pending"
    task: QuarkTask | None = None
    selectedIndexes: set[int] = field(default_factory=set)
    error: str = ""

    @property
    def label(self) -> str:
        return self.task.name if self.task is not None else self.pwdId

    @property
    def fileCount(self) -> int:
        return len(self.task.files) if self.task is not None else 0

    @property
    def selectedSize(self) -> int:
        if self.task is None:
            return 0
        return sum(file.size for file in self.task.files if file.index in self.selectedIndexes)


class ShareSelectionQueue:
    changed = Signal()

    def __init__(self, pack: QuarkPack, owner: QWidget | None = None) -> None:
        self._pack = pack
        self._owner = owner
        self._entries: list[ShareSelectionEntry] = []
        self._isBusy = False
        self._targets: list[ShareSelectionEntry] = []
        self._folder = Path()

    @property
    def entries(self) -> list[ShareSelectionEntry]:
        return list(self._entries)

    @property
    def isBusy(self) -> bool:
        return self._isBusy

    def entryById(self, pwdId: str) -> ShareSelectionEntry | None:
        for entry in self._entries:
            if entry.pwdId == pwdId:
                return entry
        return None

    def setEntries(self, text: str) -> bool:
        if self._isBusy:
            return False
        self._entries = [
            ShareSelectionEntry(pwdId=ref.pwdId, passcode=ref.passcode)
            for ref in extractShareEntries(text)
        ]
        self.changed.emit()
        return True

    def setPasscode(self, pwdId: str, passcode: str) -> None:
        entry = self.entryById(pwdId)
        if entry is not None:
            entry.passcode = passcode

    def startParse(self, folder: Path) -> None:
        self._start("pending", folder)

    def retryFailed(self, folder: Path) -> None:
        self._start("failed", folder)

    def retry(self, pwdId: str, folder: Path) -> None:
        if self._isBusy:
            return
        entry = self.entryById(pwdId)
        if entry is None or entry.status != "failed":
            return
        self._isBusy = True
        self._targets = [entry]
        self._folder = folder
        self._runNext()

    def setSelectedIndexes(self, pwdId: str, selected: set[int]) -> None:
        entry = self.entryById(pwdId)
        if entry is None:
            return
        valid = {file.index for file in entry.task.files} if entry.task is not None else set()
        entry.selectedIndexes = set(selected) & valid
        self.changed.emit()

    def clearEntries(self) -> bool:
        if self._isBusy:
            return False
        self._entries = []
        self.changed.emit()
        return True

    def submitSelected(self, folder: Path) -> int:
        if self._isBusy:
            return 0
        count = 0
        for entry in self._entries:
            if entry.status != "ready" or entry.task is None or not entry.selectedIndexes:
                continue
            entry.task.outputFolder = Path(folder)
            entry.task.setSelection(entry.selectedIndexes)
            try:
                self._pack.addTask(entry.task)
            except Exception as error:
                logger.warning("夸克任务加入下载队列失败 {}: {}", entry.pwdId, error)
                continue
            entry.status = "queued"
            count += 1
        self.changed.emit()
        return count

    def _start(self, status: str, folder: Path) -> None:
        if self._isBusy:
            return
        self._isBusy = True
        self._targets = [entry for entry in self._entries if entry.status == status]
        self._folder = folder
        self._runNext()

    def _runNext(self) -> None:
        if not self._targets:
            self._isBusy = False
            self.changed.emit()
            return
        entry = self._targets.pop(0)
        entry.status = "running"
        entry.error = ""
        self.changed.emit()
        self._pack.submit(
            self._pack.parse(TaskOptions(
                url=toCanonicalShareUrl(QuarkShareRef(entry.pwdId, entry.passcode)),
                outputFolder=self._folder,
            )),
            done=partial(self._onParsed, entry),
            failed=partial(self._onFailed, entry),
            owner=self._owner,
        )

    def _onParsed(self, entry: ShareSelectionEntry, task: Task) -> None:
        entry.task = task
        entry.selectedIndexes = {file.index for file in task.files or []}
        entry.status = "ready"
        self.changed.emit()
        self._runNext()

    def _onFailed(self, entry: ShareSelectionEntry, error: Exception) -> None:
        entry.status = "failed"
        entry.error = str(error)
        self.changed.emit()
        self._runNext()


class ShareLinkCard(SimpleCardWidget):
    retryRequested = QtSignal(str)

    def __init__(self, queue: ShareSelectionQueue, pwdId: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue = queue
        self._pwdId = pwdId
        self._isExpanded = False
        self._isTreeBuilt = False
        self._searchText = ""
        self._lastQuery = ""
        self._lastStatus = ""
        self._initWidget()
        self._initLayout()
        self._bind()
        self.refresh()

    @property
    def pwdId(self) -> str:
        return self._pwdId

    @property
    def isExpanded(self) -> bool:
        return self._isExpanded

    @property
    def shownIndexes(self) -> set[int]:
        entry = self._queue.entryById(self._pwdId)
        if entry is None or entry.task is None:
            return set()
        return {
            file.index for file in entry.task.files
            if self._searchText in file.relativePath.lower()
        }

    def _initWidget(self) -> None:
        self.setObjectName(f"share-{self._pwdId}")
        self.toggleButton = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
        self.toggleButton.setFixedSize(28, 28)
        self.checkBox = CheckBox(self)
        self.checkBox.setTristate(True)
        self.titleLabel = StrongBodyLabel(self._pwdId, self)
        self.titleLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.statusLabel = CaptionLabel("", self)
        self.errorLabel = CaptionLabel("", self)
        self.passcodeEdit = LineEdit(self)
        self.passcodeEdit.setObjectName("passcodeEdit")
        self.passcodeEdit.setPlaceholderText(self.tr("提取码"))
        self.passcodeEdit.setFixedWidth(110)
        self.retryButton = PushButton(self.tr("重试"), self)
        self.retryButton.setObjectName("retryButton")

        self.body = QWidget(self)
        self.body.setObjectName("shareBody")
        self.searchEdit = SearchLineEdit(self.body)
        self.searchEdit.setObjectName("shareSearchEdit")
        self.searchEdit.setPlaceholderText(self.tr("搜索当前链接内的文件名"))
        self.selectAllButton = PushButton(self.tr("全选"), self.body)
        self.selectAllButton.setObjectName("shareSelectAllButton")
        self.clearButton = PushButton(self.tr("全不选"), self.body)
        self.clearButton.setObjectName("shareClearButton")
        self.invertButton = PushButton(self.tr("反选"), self.body)
        self.invertButton.setObjectName("shareInvertButton")
        self.treeView = AutoSizingTreeView(self.body, minimumVisibleRows=3, maximumVisibleRows=12)
        self.treeModel = QStandardItemModel(0, 2, self.treeView)
        self.treeModel.setHorizontalHeaderLabels([self.tr("文件"), self.tr("大小")])
        self.treeView.setModel(self.treeModel)
        self.treeView.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.treeView.setUniformRowHeights(True)
        self.treeView.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.treeView.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)

        self.body.hide()
        self.errorLabel.hide()
        self.passcodeEdit.hide()
        self.retryButton.hide()

    def _initLayout(self) -> None:
        titleLayout = QVBoxLayout()
        titleLayout.setContentsMargins(0, 0, 0, 0)
        titleLayout.setSpacing(2)
        titleLayout.addWidget(self.titleLabel)
        titleLayout.addWidget(self.statusLabel)

        headerLayout = QHBoxLayout()
        headerLayout.setContentsMargins(0, 0, 0, 0)
        headerLayout.setSpacing(8)
        headerLayout.addWidget(self.toggleButton)
        headerLayout.addWidget(self.checkBox)
        headerLayout.addLayout(titleLayout, 1)

        failedLayout = QHBoxLayout()
        failedLayout.setContentsMargins(36, 0, 0, 0)
        failedLayout.setSpacing(8)
        failedLayout.addWidget(self.errorLabel, 1)
        failedLayout.addWidget(self.passcodeEdit)
        failedLayout.addWidget(self.retryButton)

        actionsLayout = QHBoxLayout()
        actionsLayout.setContentsMargins(0, 0, 0, 0)
        actionsLayout.setSpacing(8)
        actionsLayout.addWidget(self.searchEdit, 1)
        actionsLayout.addWidget(self.selectAllButton)
        actionsLayout.addWidget(self.clearButton)
        actionsLayout.addWidget(self.invertButton)

        bodyLayout = QVBoxLayout(self.body)
        bodyLayout.setContentsMargins(36, 8, 0, 0)
        bodyLayout.setSpacing(8)
        bodyLayout.addLayout(actionsLayout)
        bodyLayout.addWidget(self.treeView)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addLayout(headerLayout)
        layout.addLayout(failedLayout)
        layout.addWidget(self.body)

    def _bind(self) -> None:
        self.toggleButton.clicked.connect(self._onToggleClicked)
        self.checkBox.clicked.connect(self._onHeaderClicked)
        self.searchEdit.textChanged.connect(self._onSearchChanged)
        self.selectAllButton.clicked.connect(self._onSelectAllClicked)
        self.clearButton.clicked.connect(self._onClearClicked)
        self.invertButton.clicked.connect(self._onInvertClicked)
        self.retryButton.clicked.connect(self._onRetryClicked)
        self.treeModel.itemChanged.connect(self._onItemChanged)
        self._queue.changed.connect(self.refresh, owner=self)

    def setExpanded(self, isExpanded: bool) -> None:
        self._isExpanded = bool(isExpanded)
        self.body.setVisible(self._isExpanded)
        self.toggleButton.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if self._isExpanded else FluentIcon.CHEVRON_RIGHT_MED
        )

    def setSearchText(self, text: str) -> None:
        self.searchEdit.setText(str(text or ""))

    def isMatch(self, text: str) -> bool:
        query = str(text or "").strip().lower()
        if not query:
            return True
        entry = self._queue.entryById(self._pwdId)
        if entry is None:
            return False
        if query in self._pwdId.lower():
            return True
        if entry.task is None:
            return False
        if query in entry.task.name.lower():
            return True
        return any(query in file.relativePath.lower() for file in entry.task.files)

    def refresh(self) -> None:
        entry = self._queue.entryById(self._pwdId)
        if entry is None:
            return
        if entry.task is not None and not self._isTreeBuilt:
            self._buildTree(entry.task)
            self._isTreeBuilt = True
        self.titleLabel.setText(entry.label)
        self.statusLabel.setText(self._statusText(entry))
        self._syncCheckBox(entry)
        self._syncFailedArea(entry)
        if self._isTreeBuilt:
            self._updateRows(entry)

    def _statusText(self, entry: ShareSelectionEntry) -> str:
        if entry.status == "pending":
            return self.tr("等待")
        if entry.status == "running":
            return self.tr("解析中...")
        if entry.status == "failed":
            return self.tr("失败：{0}").format(entry.error)
        if entry.status == "queued":
            return self.tr("已加入下载队列")
        return self.tr("已选 {selected}/{total} 个文件，共 {size}").format(
            selected=len(entry.selectedIndexes),
            total=entry.fileCount,
            size=toReadableSize(entry.selectedSize),
        )

    def _syncCheckBox(self, entry: ShareSelectionEntry) -> None:
        total = {file.index for file in entry.task.files} if entry.task is not None else set()
        selected = entry.selectedIndexes & total
        with QSignalBlocker(self.checkBox):
            if total and selected == total:
                self.checkBox.setCheckState(Qt.CheckState.Checked)
            elif selected:
                self.checkBox.setCheckState(Qt.CheckState.PartiallyChecked)
            else:
                self.checkBox.setCheckState(Qt.CheckState.Unchecked)
        self.checkBox.setEnabled(bool(total))

    def _syncFailedArea(self, entry: ShareSelectionEntry) -> None:
        isFailed = entry.status == "failed"
        if isFailed and self._lastStatus != "failed":
            self.passcodeEdit.setText(entry.passcode)
        self._lastStatus = entry.status
        self.errorLabel.setText(entry.error)
        self.errorLabel.setVisible(isFailed)
        self.passcodeEdit.setVisible(isFailed)
        self.retryButton.setVisible(isFailed)

    def _buildTree(self, task: QuarkTask) -> None:
        provider = QFileIconProvider()
        folderItems: dict[tuple[str, ...], QStandardItem] = {}
        root = self.treeModel.invisibleRootItem()

        with QSignalBlocker(self.treeModel):
            for file in task.files:
                parts = file.relativePath.split("/")
                parent = root
                prefix: list[str] = []

                for part in parts[:-1]:
                    prefix.append(part)
                    key = tuple(prefix)
                    item = folderItems.get(key)
                    if item is None:
                        item = QStandardItem(part)
                        item.setEditable(False)
                        item.setCheckable(True)
                        item.setIcon(provider.icon(QFileIconProvider.IconType.Folder))
                        sizeItem = QStandardItem("")
                        sizeItem.setEditable(False)
                        parent.appendRow([item, sizeItem])
                        folderItems[key] = item
                    parent = item

                name = parts[-1] if parts else file.relativePath
                item = QStandardItem(name)
                item.setEditable(False)
                item.setCheckable(True)
                item.setData(file.index, FILE_INDEX_ROLE)
                item.setIcon(provider.icon(QFileInfo(name)))
                sizeItem = QStandardItem(toReadableSize(file.size))
                sizeItem.setEditable(False)
                parent.appendRow([item, sizeItem])

        self.treeView.expandAll()
        self.treeView.resizeColumnToContents(0)

    def _updateRows(self, entry: ShareSelectionEntry) -> None:
        shown = self.shownIndexes
        selected = entry.selectedIndexes
        isQueryChanged = self._searchText != self._lastQuery
        self._lastQuery = self._searchText

        root = self.treeModel.invisibleRootItem()
        with QSignalBlocker(self.treeModel):
            for row in range(root.rowCount()):
                self._updateRow(root.child(row), shown, selected)
        self.treeView.viewport().update()

        if isQueryChanged and self._searchText:
            self.treeView.expandAll()

    def _updateRow(
        self, item: QStandardItem, shown: set[int], selected: set[int],
    ) -> Qt.CheckState | None:
        fileIndex = item.data(FILE_INDEX_ROLE)
        if fileIndex is not None:
            if fileIndex not in shown:
                state = None
            else:
                state = Qt.CheckState.Checked if fileIndex in selected else Qt.CheckState.Unchecked
        else:
            states = {
                self._updateRow(item.child(row), shown, selected)
                for row in range(item.rowCount())
            } - {None}
            if len(states) == 1:
                state = states.pop()
            else:
                state = Qt.CheckState.PartiallyChecked if states else None
        parent = item.parent()
        parentIndex = parent.index() if parent else QModelIndex()
        if self.treeView.isRowHidden(item.row(), parentIndex) != (state is None):
            self.treeView.setRowHidden(item.row(), parentIndex, state is None)
        if state is not None and item.checkState() != state:
            item.setCheckState(state)
        return state

    def _onToggleClicked(self) -> None:
        self.setExpanded(not self._isExpanded)

    def _onHeaderClicked(self) -> None:
        entry = self._queue.entryById(self._pwdId)
        if entry is None or entry.task is None:
            return
        allIndexes = {file.index for file in entry.task.files}
        self._queue.setSelectedIndexes(
            self._pwdId, set() if entry.selectedIndexes == allIndexes else allIndexes,
        )

    def _onSearchChanged(self) -> None:
        self._searchText = self.searchEdit.text().strip().lower()
        self.refresh()

    def _onSelectAllClicked(self) -> None:
        self._queue.setSelectedIndexes(
            self._pwdId, self._selectedIndexes() | self.shownIndexes,
        )

    def _onClearClicked(self) -> None:
        self._queue.setSelectedIndexes(
            self._pwdId, self._selectedIndexes() - self.shownIndexes,
        )

    def _onInvertClicked(self) -> None:
        selected = self._selectedIndexes()
        shown = self.shownIndexes
        self._queue.setSelectedIndexes(
            self._pwdId, (selected - shown) | (shown - selected),
        )

    def _onRetryClicked(self) -> None:
        self._queue.setPasscode(self._pwdId, self.passcodeEdit.text().strip())
        self.retryRequested.emit(self._pwdId)

    def _onItemChanged(self, item: QStandardItem) -> None:
        if item.column() != 0:
            return
        fileIndex = item.data(FILE_INDEX_ROLE)
        indexes = {fileIndex} if fileIndex is not None else self._shownIndexesUnder(item)
        selected = self._selectedIndexes()
        if item.checkState() == Qt.CheckState.Checked:
            selected |= indexes
        else:
            selected -= indexes
        self._queue.setSelectedIndexes(self._pwdId, selected)

    def _shownIndexesUnder(self, item: QStandardItem) -> set[int]:
        fileIndex = item.data(FILE_INDEX_ROLE)
        if fileIndex is not None:
            return {fileIndex} & self.shownIndexes
        return set().union(*(
            self._shownIndexesUnder(item.child(row)) for row in range(item.rowCount())
        ))

    def _selectedIndexes(self) -> set[int]:
        entry = self._queue.entryById(self._pwdId)
        return set(entry.selectedIndexes) if entry is not None else set()
