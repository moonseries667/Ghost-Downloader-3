from __future__ import annotations

from typing import TYPE_CHECKING

from app.i18n import N
from app.models.pack import FeaturePack, PackPage, PackServices, TaskParser
from app.models.task import Task, TaskError, TaskOptions
from .account import QuarkAccount
from .config import quarkConfig
from .parse import extractShareEntries, parseShareUrl, toCanonicalShareUrl
from .task import QuarkFile, QuarkTask

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


class QuarkParser(TaskParser):
    pack: QuarkPack
    priority = 65

    def match(self, options: TaskOptions) -> bool:
        if parseShareUrl(options.url) is not None:
            return True
        return len(extractShareEntries(options.url)) == 1

    def matchPassive(self, options: TaskOptions) -> bool:
        return parseShareUrl(options.url) is not None

    async def parse(self, options: TaskOptions) -> Task:
        ref = shareRefFrom(options.url)
        if ref is None:
            raise TaskError("无法解析夸克分享链接")
        tree = await self.pack.client.fetchShareTree(ref.pwdId, ref.passcode)
        if not tree.files:
            raise TaskError("分享中没有可下载的文件")
        if len(tree.files) == 1:
            name = tree.files[0].name
        else:
            name = tree.title or f"quark_{ref.pwdId}"
        files = [
            QuarkFile(
                index=index,
                relativePath=entry.relativePath,
                size=entry.size,
                fid=entry.fid,
                pdirFid=entry.pdirFid,
                shareFidToken=entry.shareFidToken,
            )
            for index, entry in enumerate(tree.files)
        ]
        task = QuarkTask(
            name=name,
            url=toCanonicalShareUrl(ref),
            outputFolder=options.outputFolder,
            fileSize=sum(item.size for item in files),
            sourceKind="share",
            pwdId=ref.pwdId,
            passcode=ref.passcode,
            stoken=tree.stoken,
            files=files,
        )
        task.client = self.pack.client
        return task


class QuarkPack(FeaturePack):
    packId = "quark"
    capturedLinks = N("QuarkPack", "夸克网盘分享链接")
    config = quarkConfig
    parsers = [QuarkParser]

    def __init__(self, services: PackServices) -> None:
        super().__init__(services)
        self.account = QuarkAccount(services.coroutineRunner)
        self.client = self.account.client
        self.config._account = self.account

    def pages(self) -> list[type[PackPage]]:
        from .page import QuarkPage
        return [QuarkPage]

    def taskCardClass(self, task: Task) -> type[TaskCard] | None:
        from .cards import QuarkTaskCard
        return QuarkTaskCard

    def draftCardClass(self, task: Task) -> type[DraftCard] | None:
        from .cards import QuarkDraftCard
        return QuarkDraftCard

    def optionCards(self, task: Task, parent: QWidget | None = None) -> list[OptionCard]:
        from app.view.components.option_cards import OutputFolderCard
        return [OutputFolderCard(parent, initial=task.outputFolder)]

    async def activate(self) -> None:
        if self.account.isLoggedIn:
            self.account.fetchAccountInfo()
