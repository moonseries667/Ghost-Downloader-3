from __future__ import annotations

from app.view.cards.draft_cards import MultiFileDraftCard
from app.view.cards.task_cards import MultiFileTaskCard
from app.view.dialogs.file_select import FileSelectDialog
from .task import TelegramTask


class TelegramDraftCard(MultiFileDraftCard):
    fileSelectDialog = FileSelectDialog
    _task: TelegramTask

    @property
    def task(self) -> TelegramTask:
        return self._task


class TelegramTaskCard(MultiFileTaskCard):
    fileSelectDialog = FileSelectDialog
    _task: TelegramTask

    @property
    def task(self) -> TelegramTask:
        return self._task
