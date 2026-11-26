from __future__ import annotations

from app.view.cards.draft_cards import MultiFileDraftCard
from app.view.cards.task_cards import MultiFileTaskCard
from app.view.dialogs.file_select import FileSelectDialog
from .task import BaiduTask


class BaiduDraftCard(MultiFileDraftCard):
    fileSelectDialog = FileSelectDialog
    _task: BaiduTask

    @property
    def task(self) -> BaiduTask:
        return self._task


class BaiduTaskCard(MultiFileTaskCard):
    fileSelectDialog = FileSelectDialog
    _task: BaiduTask

    @property
    def task(self) -> BaiduTask:
        return self._task
