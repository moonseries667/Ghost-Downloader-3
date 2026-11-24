from __future__ import annotations

import weakref
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from app.config.cfg import cfg
from app.i18n import N
from app.models.pack import BinaryRuntime, FeaturePack, PackPage, PackServices, TaskParser
from app.models.task import Task, TaskError, TaskOptions
from app.signal import Signal
from .account import TelegramAccount
from .config import telegramConfig
from .inbox import InboxBundle, TelegramInbox
from .parse import MediaBundle, groupExportedMessages, parseTelegramUrl
from .runtime import tdlRunner, tdlRuntime
from .task import TelegramTask, buildBundleTask

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from app.view.cards.draft_cards import DraftCard
    from app.view.cards.task_cards import TaskCard
    from app.view.components.option_cards import OptionCard


class TelegramParser(TaskParser):
    pack: TelegramPack
    priority = 55

    def match(self, options: TaskOptions) -> bool:
        return parseTelegramUrl(options.url) is not None

    def matchPassive(self, options: TaskOptions) -> bool:
        return parseTelegramUrl(options.url) is not None

    async def parse(self, options: TaskOptions) -> Task:
        link = parseTelegramUrl(options.url)
        if link is None:
            raise TaskError("无法解析 Telegram 消息链接")
        outputFolder = options.outputFolder
        runner = self.pack.runner
        try:
            if not runner.isBusy and runner.runtime.path():
                payload = await runner.exportMessages(
                    link.chat,
                    idRange=(max(1, link.messageId - 10), link.messageId + 10),
                )
                bundles = groupExportedMessages(payload)
                bundle = next(
                    (
                        bundle for bundle in bundles
                        if any(item.messageId == link.messageId for item in bundle.items)
                    ),
                    None,
                )
                if bundle is not None:
                    if link.isSingle:
                        item = next(
                            item for item in bundle.items if item.messageId == link.messageId
                        )
                        bundle = replace(
                            bundle,
                            bundleKey=f"{bundle.chatId}:m:{item.messageId}",
                            items=(item,),
                            groupedId=item.groupedId,
                            firstMessageId=item.messageId,
                            lastMessageId=item.messageId,
                            date=item.date,
                            totalSize=item.size,
                        )
                    return buildBundleTask(
                        bundle, outputFolder, chatId=bundle.chatId or link.chat, url=link.url, runner=runner,
                    )
        except Exception:
            # 锁忙、未装 tdl 或受保护频道导出失败时静默降级，不阻塞入队
            pass
        task = TelegramTask(
            name=f"tg_{link.chat}_{link.messageId}",
            url=link.url,
            outputFolder=outputFolder,
            packId="telegram",
            chatId=link.chat,
            sourceMode="url",
        )
        task.runner = runner
        return task


class TelegramPack(FeaturePack):
    packId = "telegram"
    capturedLinks = N("TelegramPack", "Telegram 消息链接")
    config = telegramConfig
    parsers = [TelegramParser]
    inboxChanged = Signal()

    def __init__(self, services: PackServices) -> None:
        super().__init__(services)
        coroutineRunner = getattr(services, "coroutineRunner", None)
        self.runner = tdlRunner
        self.account = TelegramAccount(coroutineRunner, self.runner)
        self.inbox = TelegramInbox()
        self.inbox.load()
        configChatId = str(telegramConfig.inboxChatId.value or "").strip()
        configChatTitle = str(telegramConfig.inboxChatTitle.value or "").strip()
        if configChatId:
            title = configChatTitle or (self.inbox.title if configChatId == self.inbox.chatId else "")
            self.inbox.setChannel(configChatId, title)
        if self.config is not None:
            self.config._account = self.account
        self._onTaskCompleted = None
        if coroutineRunner is not None:
            packRef = weakref.ref(self)

            def onTaskCompleted(task: TelegramTask) -> None:
                pack = packRef()
                if pack is not None:
                    coroutineRunner.post(pack.markBundleCompleted, task.bundleKey)

            self._onTaskCompleted = onTaskCompleted
            TelegramTask._onCompleted = onTaskCompleted

    def pages(self) -> list[type[PackPage]]:
        from .page import TelegramPage
        return [TelegramPage]

    def runtimes(self) -> list[BinaryRuntime]:
        return [tdlRuntime]

    def taskCardClass(self, task: Task) -> type[TaskCard] | None:
        from .cards import TelegramTaskCard
        return TelegramTaskCard

    def draftCardClass(self, task: Task) -> type[DraftCard] | None:
        from .cards import TelegramDraftCard
        return TelegramDraftCard

    def optionCards(self, task: Task, parent: QWidget | None = None) -> list[OptionCard]:
        from app.view.components.option_cards import OutputFolderCard
        return [OutputFolderCard(parent, initial=task.outputFolder)]

    async def activate(self) -> None:
        coroutineRunner = getattr(self._services, "coroutineRunner", None)
        if tdlRuntime.path() and coroutineRunner is not None:
            coroutineRunner.post(self.account.refreshAuth)

    async def deactivate(self) -> None:
        if self._onTaskCompleted is not None and TelegramTask._onCompleted is self._onTaskCompleted:
            TelegramTask._onCompleted = None

    def bindInboxChannel(self, chatId: str, title: str) -> None:
        chatId = str(chatId or "").strip()
        title = str(title or "").strip()
        self.inbox.setChannel(chatId, title)
        self.inbox.save()
        cfg.set(telegramConfig.inboxChatId, chatId)
        cfg.set(telegramConfig.inboxChatTitle, title)
        self.inboxChanged.emit()

    async def fetchInboxUpdates(
        self,
        chatId: str,
        sinceMsgId: int = 0,
        limit: int = 50,
    ) -> list[MediaBundle]:
        targetChat = str(chatId or "").strip()
        if not targetChat:
            raise TaskError("请先绑定监听频道")
        payload = await self.runner.exportMessages(targetChat, sinceMsgId=sinceMsgId, limit=limit)
        return groupExportedMessages(payload)

    def applyInboxUpdates(self, bundles: list[MediaBundle]) -> list[InboxBundle]:
        previousLastMsgId = self.inbox.lastMsgId
        added = self.inbox.mergeBundles(bundles)
        if added or self.inbox.lastMsgId != previousLastMsgId:
            self.inbox.save()
            self.inboxChanged.emit()
        return added

    def enqueueInboxBundle(self, bundleKey: str, outputFolder: Path) -> TelegramTask | None:
        bundle = self.inbox.bundleByKey(bundleKey)
        if bundle is None:
            return None
        keyChatId = bundleKey.partition(":")[0]
        chatId = bundle.chatId or keyChatId or self.inbox.chatId
        task = buildBundleTask(
            bundle,
            Path(outputFolder),
            chatId=chatId,
            runner=self.runner,
        )
        addTask = getattr(self, "addTask", None)
        if addTask is not None:
            if addTask(task) is False:
                return None
        self.inbox.markStatus(bundleKey, "queued")
        self.inbox.save()
        self.inboxChanged.emit()
        return task

    def setInboxBundleStatus(self, bundleKey: str, status: str) -> bool:
        if not self.inbox.markStatus(bundleKey, status):
            return False
        self.inbox.save()
        self.inboxChanged.emit()
        return True

    def markBundleCompleted(self, bundleKey: str) -> bool:
        return self.setInboxBundleStatus(bundleKey, "completed")
