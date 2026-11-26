from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from app.i18n import N
from app.models.pack import BinaryRuntime, FeaturePack, PackPage, PackServices, TaskParser
from app.models.task import Task, TaskError, TaskOptions
from .account import BaiduAccount
from .client import BaiduWebClient
from .config import baiduConfig
from .parse import extractShareEntries, parseShareUrl, toCanonicalShareUrl
from .runtime import baiduPcsRunner, baiduPcsRuntime
from .task import BaiduFile, BaiduTask

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from app.view.cards.draft_cards import DraftCard
    from app.view.cards.task_cards import TaskCard
    from app.view.components.option_cards import OptionCard


def shareRefFrom(text: str):
    ref = parseShareUrl(text)
    if ref is not None:
        return ref
    entries = extractShareEntries(text)
    if len(entries) == 1:
        return entries[0]
    return None


class BaiduParser(TaskParser):
    pack: BaiduPack
    priority = 66

    def match(self, options: TaskOptions) -> bool:
        if parseShareUrl(options.url) is not None:
            return True
        return len(extractShareEntries(options.url)) == 1

    def matchPassive(self, options: TaskOptions) -> bool:
        return parseShareUrl(options.url) is not None

    async def parse(self, options: TaskOptions) -> Task:
        ref = shareRefFrom(options.url)
        if ref is None:
            raise TaskError("无法解析百度网盘分享链接")
        tree = await self.pack.client.fetchShareTree(ref.shareKey, ref.passcode)
        if not tree.files:
            raise TaskError("分享中没有可下载的文件")
        name = tree.files[0].name if len(tree.files) == 1 else (tree.title or f"baidu_{ref.shareKey}")
        files = [
            BaiduFile(
                index=index,
                relativePath=entry.relativePath,
                size=entry.size,
                fsId=entry.fsId,
            )
            for index, entry in enumerate(tree.files)
        ]
        task = BaiduTask(
            name=name,
            url=toCanonicalShareUrl(ref),
            outputFolder=options.outputFolder,
            fileSize=sum(item.size for item in files),
            sourceKind="share",
            shareKey=ref.shareKey,
            passcode=ref.passcode,
            files=files,
        )
        task.runner = self.pack.runner
        return task


class BaiduPack(FeaturePack):
    packId = "baidu"
    capturedLinks = N("BaiduPack", "百度网盘分享链接")
    config = baiduConfig
    parsers = [BaiduParser]

    def __init__(self, services: PackServices) -> None:
        super().__init__(services)
        coroutineRunner = getattr(services, "coroutineRunner", None)
        self.runner = baiduPcsRunner
        self.client = BaiduWebClient()
        self.account = BaiduAccount(coroutineRunner, self.runner, self.client)
        if self.config is not None:
            self.config._account = self.account

    def pages(self) -> list[type[PackPage]]:
        from .page import BaiduPage
        return [BaiduPage]

    def runtimes(self) -> list[BinaryRuntime]:
        return [baiduPcsRuntime]

    def taskCardClass(self, task: Task) -> type[TaskCard] | None:
        from .cards import BaiduTaskCard
        return BaiduTaskCard

    def draftCardClass(self, task: Task) -> type[DraftCard] | None:
        from .cards import BaiduDraftCard
        return BaiduDraftCard

    def optionCards(self, task: Task, parent: QWidget | None = None) -> list[OptionCard]:
        from app.view.components.option_cards import OutputFolderCard
        return [OutputFolderCard(parent, initial=task.outputFolder)]

    def protectedTempNames(self) -> set[str]:
        return BaiduTask.protectedTempNames()

    async def cleanStaleTempDirs(self) -> list[str]:
        if not self.runner.runtime.path():
            return []
        return await self.runner.cleanStaleTempDirs(self.protectedTempNames())

    async def _activateBackground(self) -> None:
        if not self.runner.runtime.path():
            return
        try:
            user = await self.runner.probeUser()
        except Exception:
            return
        coroutineRunner = getattr(self._services, "coroutineRunner", None)
        if coroutineRunner is not None:
            coroutineRunner.post(self.account.applyUserInfo, user)
        if user.isLoggedIn:
            await self.cleanStaleTempDirs()

    async def activate(self) -> None:
        coroutineRunner = getattr(self._services, "coroutineRunner", None)
        if self.runner.runtime.path() and coroutineRunner is not None:
            coroutineRunner.submit(self._activateBackground())

    async def deactivate(self) -> None:
        self.account.cancelQrLogin()
        if self.runner.runtime.path() and self.account.isLoggedIn:
            with suppress(Exception):
                await asyncio.wait_for(self.cleanStaleTempDirs(), timeout=5.0)
        with suppress(Exception):
            await self.runner.stopAll()
