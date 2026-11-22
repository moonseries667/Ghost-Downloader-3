from __future__ import annotations

from app.view.cards.draft_cards import MultiFileDraftCard
from app.view.cards.task_cards import MultiFileTaskCard
from app.view.dialogs.file_select import FileSelectDialog
from .task import QuarkTask


class QuarkDraftCard(MultiFileDraftCard):
    fileSelectDialog = FileSelectDialog
    _task: QuarkTask

    @property
    def task(self) -> QuarkTask:
        return self._task


class QuarkTaskCard(MultiFileTaskCard):
    fileSelectDialog = FileSelectDialog
    _task: QuarkTask

    @property
    def task(self) -> QuarkTask:
        return self._task
