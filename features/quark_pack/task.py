from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from loguru import logger

from app.models.task import Task, TaskError, TaskFile, TaskStatus, toTaskError
from http_pack.task import HttpTaskStep
from .client import (
    NON_RETRYABLE_SAVE_KINDS, QUARK_PC_UA, DriveEntry, QuarkClient, ShareEntry, readConfigCookie,
)
from .config import quarkConfig

PAN_REFERER = "https://pan.quark.cn/"
PAN_ORIGIN = "https://pan.quark.cn"
LINK_RETRY_LIMIT = 2


@dataclass(kw_only=True)
class QuarkFile(TaskFile):
    fid: str = ""
    pdirFid: str = "0"
    shareFidToken: str = ""
    savedFid: str = ""


def shareEntryFromFile(file: QuarkFile) -> ShareEntry:
    return ShareEntry(
        fid=file.fid,
        name=Path(file.relativePath).name,
        size=file.size,
        isDir=False,
        shareFidToken=file.shareFidToken,
        relativePath=file.relativePath,
        pdirFid=file.pdirFid or "0",
    )


def buildDriveTask(
    entries: list[DriveEntry] | list[tuple[DriveEntry, str]], outputFolder: Path, name: str = "",
) -> QuarkTask:
    files: list[QuarkFile] = []
    for item in entries:
        entry, relativePath = item if isinstance(item, tuple) else (item, item.name)
        if entry.isDir:
            continue
        files.append(QuarkFile(
            index=len(files),
            relativePath=relativePath,
            size=int(entry.size or 0),
            fid=str(entry.fid),
        ))
    if not files:
        raise TaskError("没有可下载的文件")
    taskName = Path(files[0].relativePath).name if len(files) == 1 else (name or "我的网盘")
    return QuarkTask(
        name=taskName,
        url="https://pan.quark.cn/list",
        outputFolder=outputFolder,
        fileSize=sum(item.size for item in files),
        sourceKind="drive",
        files=files,
    )


@dataclass(kw_only=True)
class QuarkStep(HttpTaskStep):
    _task: QuarkTask = field(repr=False, init=False)

    @property
    def task(self) -> QuarkTask:
        return self._task

    @property
    def outputPath(self) -> str:
        return str(self.task.toFilePath(self.fileIndex))

    @property
    def file(self) -> QuarkFile:
        for item in self.task.files:
            if item.index == self.fileIndex:
                return item
        raise TaskError("找不到对应的文件")

    def targetFid(self) -> str:
        if self.task.sourceKind == "drive":
            return self.file.fid
        return self.file.savedFid

    async def refreshLink(self) -> None:
        fid = self.targetFid()
        if not fid:
            raise TaskError("文件尚未转存，无法获取下载链接")
        client = self.task.client or QuarkClient()
        links = await client.fetchDownloadUrls([fid])
        link = links.get(fid)
        if link is None:
            raise TaskError("未能获取下载链接")
        self.url = link.url
        self.subworkerCount = quarkConfig.subworkerCount.value
        self.userAgent = QUARK_PC_UA
        headers = {"referer": PAN_REFERER, "origin": PAN_ORIGIN}
        cookie = client.currentCookie()
        if cookie:
            headers["cookie"] = cookie
        self.headers = headers

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            for attempt in range(LINK_RETRY_LIMIT + 1):
                await self.refreshLink()
                try:
                    await super().run(reportSpeed, waitForSpeedLimit)
                    return
                except TaskError as error:
                    status = error.params.get("status")
                    if status not in {401, 403} or attempt >= LINK_RETRY_LIMIT:
                        if status in {401, 403}:
                            raise TaskError(
                                "下载链接已失效，刷新后仍无法下载（{status}）", status=status,
                            ) from error
                        raise
        finally:
            self.headers.pop("cookie", None)
            self.headers.pop("Cookie", None)

    @classmethod
    def fromFile(cls, file: TaskFile, task: Task) -> QuarkStep:
        quarkFile: QuarkFile = file  # type: ignore[assignment]
        return cls(
            stepIndex=file.index + 1,
            url="",
            fileSize=quarkFile.size,
            subworkerCount=quarkConfig.subworkerCount.value,
            canUseRangeRequests=quarkFile.size > 0,
            fileIndex=file.index,
        )


@dataclass(kw_only=True, eq=False)
class QuarkTask(Task):
    packId: str = "quark"
    canEdit = True
    fileType = QuarkFile
    activeTempFolderNames: ClassVar[set[str]] = set()
    sourceKind: str = "share"
    pwdId: str = ""
    passcode: str = ""
    stoken: str = ""
    tempFolderFid: str = ""
    files: list[QuarkFile] = field(default_factory=list)
    client: QuarkClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.files:
            existing = {step.fileIndex for step in self.steps}
            for file in self.files:
                if file.index not in existing:
                    self.addStep(QuarkStep.fromFile(file, self))

    def reset(self) -> TaskStatus:
        if self.sourceKind == "share":
            self.tempFolderFid = ""
            for file in self.files:
                file.savedFid = ""
        return super().reset()

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        if self.client is None:
            # 恢复的历史任务没有 pack 注入的 client，本次运行内复用同一实例以合并响应 Set-Cookie
            self.client = QuarkClient()
        client = self.client
        isShare = self.sourceKind == "share"
        if isShare:
            self.activeTempFolderNames.add(self.taskId)
        try:
            if isShare:
                try:
                    await self.savePending(client)
                except asyncio.CancelledError:
                    self.setStatus(TaskStatus.PAUSED)
                    raise
                except Exception as error:
                    step = next((
                        item for item in self.steps
                        if self.isStepSelected(item, self.selectedIndexes)
                        and item.status != TaskStatus.COMPLETED
                    ), None)
                    if step is not None:
                        step.setError(toTaskError(error))
                    raise
            await super().run(reportSpeed, waitForSpeedLimit)
            await self.cleanTemp(client)
        finally:
            if isShare:
                self.activeTempFolderNames.discard(self.taskId)

    async def savePending(self, client: QuarkClient) -> None:
        pending = [file for file in self.files if file.selected and not file.completed and not file.savedFid]
        if not pending:
            return
        if not readConfigCookie():
            raise TaskError("请先在设置中登录夸克账号", kind="auth")
        hadTempFolder = bool(self.tempFolderFid)
        byFid = {file.fid: file for file in pending}

        def onFolderReady(folderFid: str) -> None:
            self.tempFolderFid = folderFid

        def onBatchSaved(mapped: dict[str, str]) -> None:
            for sourceFid, savedFid in mapped.items():
                file = byFid.get(sourceFid)
                if file is not None and savedFid:
                    file.savedFid = savedFid

        async def runSave() -> None:
            remaining = [file for file in pending if not file.savedFid]
            if not remaining:
                return
            result = await client.saveShareFiles(
                self.pwdId,
                self.stoken,
                [shareEntryFromFile(file) for file in remaining],
                self.taskId,
                folderFid=self.tempFolderFid,
                onFolderReady=onFolderReady,
                onBatchSaved=onBatchSaved,
                usedFids={file.savedFid for file in self.files if file.savedFid},
            )
            self.tempFolderFid = result.folderFid
            for file in remaining:
                savedFid = result.savedFidBySource.get(file.fid, "")
                if not savedFid:
                    raise TaskError("转存完成，但未能对应文件 {name}", name=file.relativePath)
                file.savedFid = savedFid

        try:
            try:
                await runSave()
            except TaskError as error:
                if error.params.get("kind") in NON_RETRYABLE_SAVE_KINDS:
                    raise
                await self.refreshShare(client)
                await runSave()
        except Exception:
            if not hadTempFolder and self.tempFolderFid and not any(file.savedFid for file in self.files):
                await self._cleanEmptyTemp(client)
            raise

    async def _cleanEmptyTemp(self, client: QuarkClient) -> None:
        folderFid = self.tempFolderFid
        self.tempFolderFid = ""
        try:
            await client.deleteRemote([folderFid])
        except Exception as error:
            logger.warning("清理空夸克临时目录失败 {}", type(error).__name__)

    async def refreshShare(self, client: QuarkClient) -> None:
        tree = await client.fetchShareTree(self.pwdId, self.passcode)
        self.stoken = tree.stoken
        byFid = {entry.fid: entry for entry in tree.files}
        for file in self.files:
            entry = byFid.get(file.fid)
            if entry is not None:
                file.shareFidToken = entry.shareFidToken

    async def cleanTemp(self, client: QuarkClient) -> None:
        if self.status != TaskStatus.COMPLETED or self.sourceKind != "share":
            return
        if not quarkConfig.shouldCleanTempAfterDone.value or not self.tempFolderFid:
            return
        folderFid = self.tempFolderFid
        try:
            await client.deleteRemote([folderFid])
        except Exception as error:
            logger.warning("清理夸克临时目录失败 {}", type(error).__name__)
            return
        self.tempFolderFid = ""
        for file in self.files:
            file.savedFid = ""
