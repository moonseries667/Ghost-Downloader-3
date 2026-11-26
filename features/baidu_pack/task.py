from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from loguru import logger

from app.models.task import Task, TaskError, TaskFile, TaskStatus, TaskStep, toTaskError
from app.platform.filesystem import deletePath
from .config import baiduConfig
from .parse import toShareBaseUrl
from .runtime import (
    TEMP_ROOT_DIR,
    BaiduDownloadProgress,
    BaiduDriveEntry,
    BaiduPcsRunner,
    baiduPcsRunner,
    toRemotePath,
)

STAGING_FOLDER = "staging"
TEMP_DOWNLOAD_SUFFIX = ".baidupcs-go-downloading"


@dataclass(kw_only=True)
class BaiduFile(TaskFile):
    fsId: str = ""
    remotePath: str = ""


def buildDriveTask(
    entries: list[BaiduDriveEntry] | list[tuple[BaiduDriveEntry, str]],
    outputFolder: Path,
    *,
    name: str = "",
    runner: BaiduPcsRunner | None = None,
) -> BaiduTask:
    files: list[BaiduFile] = []
    for item in entries:
        entry, relativePath = item if isinstance(item, tuple) else (item, item.name)
        if entry.isDir:
            continue
        files.append(BaiduFile(
            index=len(files),
            relativePath=relativePath,
            size=int(entry.size or 0),
            fsId=str(entry.fsId or ""),
            remotePath=entry.path,
        ))
    if not files:
        raise TaskError("没有可下载的文件")
    taskName = Path(files[0].relativePath).name if len(files) == 1 else (name or "百度网盘")
    task = BaiduTask(
        name=taskName,
        url="https://pan.baidu.com/disk/main",
        outputFolder=Path(outputFolder),
        fileSize=sum(file.size for file in files),
        sourceKind="drive",
        files=files,
    )
    task.runner = runner
    return task


@dataclass(kw_only=True)
class BaiduStep(TaskStep):
    canPause: ClassVar[bool] = True

    downloadMode: str = "locate"
    threads: int = 8
    sampleInterval: float = field(default=0.5, repr=False)
    speedText: str = field(default="", repr=False, init=False)
    _task: BaiduTask = field(repr=False, init=False)

    @property
    def task(self) -> BaiduTask:
        return self._task

    @property
    def outputPath(self) -> str:
        return ""

    def setOptions(self, options: dict) -> None:
        if "downloadMode" in options:
            self.downloadMode = str(options["downloadMode"])
        if "threads" in options:
            self.threads = max(1, int(options["threads"]))

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        task = self.task
        runner = task.runner
        if runner is None:
            raise TaskError("BaiduPCS-Go 未就绪")
        pending = [file for file in task.files if file.selected and not file.completed]
        if not pending:
            self.progress = 100
            return

        stagingDir = task.partPath / STAGING_FOLDER
        stagingDir.mkdir(parents=True, exist_ok=True)
        totalSize = sum(file.size for file in task.files if file.selected)
        alreadyDoneBytes = sum(file.downloadedBytes for file in task.files if file.selected and file.completed)
        groupOffsetBytes = alreadyDoneBytes

        def onProgress(progress: BaiduDownloadProgress) -> None:
            self.speed = progress.speedBytes
            self.speedText = progress.speedText
            if progress.downloadedBytes > 0:
                self.receivedBytes = max(self.receivedBytes, groupOffsetBytes + progress.downloadedBytes)
            if totalSize > 0 and self.receivedBytes > 0:
                self.progress = min(99.0, self.receivedBytes / totalSize * 100.0)
            elif progress.percent > 0:
                self.progress = min(99.0, progress.percent)

        sampler = asyncio.create_task(
            self.sampleStaging(
                stagingDir,
                totalSize,
                lambda: groupOffsetBytes,
                reportSpeed,
                waitForSpeedLimit,
            )
        )
        try:
            for relParent, groupFiles in groupFilesByParent(pending):
                groupStagingDir = stagingDir / Path(relParent) if relParent else stagingDir
                groupStagingDir.mkdir(parents=True, exist_ok=True)
                remotePaths = [file.remotePath for file in groupFiles if file.remotePath]
                if len(remotePaths) != len(groupFiles):
                    raise TaskError("部分文件缺少网盘路径，无法开始下载")
                await runner.download(
                    remotePaths,
                    groupStagingDir,
                    mode=self.downloadMode or baiduConfig.downloadMode.value,
                    threads=self.threads or baiduConfig.threads.value,
                    onProgress=onProgress,
                )
                self.finalizeGroup(groupFiles, groupStagingDir)
                groupOffsetBytes += sum(file.downloadedBytes for file in groupFiles)
                self.receivedBytes = max(self.receivedBytes, groupOffsetBytes)
                if totalSize > 0:
                    self.progress = min(99.0, self.receivedBytes / totalSize * 100.0)

            self.cleanupEmptyStaging(stagingDir)
            self.progress = 100
        finally:
            sampler.cancel()
            with suppress(asyncio.CancelledError):
                await sampler

    async def sampleStaging(
        self,
        stagingDir: Path,
        totalSize: int,
        readBaseBytes: Callable[[], int],
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        last = measureFolder(stagingDir)
        while True:
            await asyncio.sleep(self.sampleInterval)
            current = measureFolder(stagingDir)
            delta = max(0, current - last)
            last = current
            if delta > 0:
                reportSpeed(delta)
            totalReceived = readBaseBytes() + current
            self.receivedBytes = max(self.receivedBytes, totalReceived)
            if totalSize > 0 and self.progress < 99.0:
                self.progress = max(self.progress, min(99.0, self.receivedBytes / totalSize * 100.0))
            await waitForSpeedLimit()

    def finalizeGroup(self, files: list[BaiduFile], groupStagingDir: Path) -> None:
        task = self.task
        for file in files:
            source = findDownloadedFile(groupStagingDir, file)
            if source is None:
                raise TaskError("BaiduPCS-Go 下载完成，但未找到输出文件：{name}", name=file.relativePath)
            target = task.toFilePath(file.index) if task.isOutputFolder else Path(task.outputPath)
            moveToTarget(source, target)
            markFileCompleted(file, target)

    def cleanupEmptyStaging(self, stagingDir: Path) -> None:
        if not stagingDir.exists():
            return
        for path in sorted(stagingDir.rglob("*"), reverse=True):
            if path.is_dir():
                with suppress(OSError):
                    path.rmdir()
        with suppress(OSError):
            stagingDir.rmdir()


@dataclass(kw_only=True, eq=False)
class BaiduTask(Task):
    packId: str = "baidu"
    canEdit: ClassVar[bool] = True
    fileType: ClassVar[type[TaskFile]] = BaiduFile
    _instances: ClassVar[weakref.WeakSet[BaiduTask]] = weakref.WeakSet()

    sourceKind: str = "share"
    shareKey: str = ""
    passcode: str = ""
    tempRemoteDir: str = ""
    files: list[BaiduFile] = field(default_factory=list)
    runner: BaiduPcsRunner | None = field(default=None, init=False, repr=False)

    @classmethod
    def protectedTempNames(cls) -> set[str]:
        names: set[str] = set()
        for task in list(cls._instances):
            if task.sourceKind != "share" or not task.tempRemoteDir:
                continue
            if task.status != TaskStatus.COMPLETED:
                names.add(posixpath.basename(task.tempRemoteDir.rstrip("/")))
        return names

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.steps:
            self.addStep(BaiduStep(
                stepIndex=0,
                downloadMode=baiduConfig.downloadMode.value,
                threads=baiduConfig.threads.value,
            ))
        if self.sourceKind == "share":
            self._instances.add(self)

    def setSelection(self, selectedIndexes: set[int]) -> None:
        super().setSelection(selectedIndexes)
        hasPending = any(file.selected and not file.completed for file in self.files)
        if not hasPending:
            return
        self.completedAt = 0
        for step in self.steps:
            if isinstance(step, BaiduStep) and step.status == TaskStatus.COMPLETED:
                step.status = TaskStatus.WAITING
                step.progress = 0
        self.updateStatus()

    def reset(self) -> TaskStatus:
        if self.sourceKind == "share":
            self.tempRemoteDir = ""
            for file in self.files:
                file.remotePath = ""
        for file in self.files:
            file.completed = False
            file.downloadedBytes = 0
        return super().reset()

    def deletePlaceholders(self) -> None:
        super().deletePlaceholders()
        if self.sourceKind == "share":
            self.tempRemoteDir = ""

    def deleteFiles(self) -> None:
        super().deleteFiles()
        if self.sourceKind == "share":
            self.tempRemoteDir = ""
        deletePath(self.partPath / STAGING_FOLDER)
        deletePath(self.partPath)

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        if self.runner is None:
            self.runner = baiduPcsRunner
        runner = self.runner
        isShare = self.sourceKind == "share"
        if isShare:
            try:
                await self.transferPending(runner)
            except asyncio.CancelledError:
                self.setStatus(TaskStatus.PAUSED)
                raise
            except Exception as error:
                step = next((item for item in self.steps if item.status != TaskStatus.COMPLETED), None)
                if step is not None:
                    step.setError(toTaskError(error))
                raise
        await super().run(reportSpeed, waitForSpeedLimit)
        await self.cleanTemp(runner)

    async def transferPending(self, runner: BaiduPcsRunner) -> None:
        pending = [file for file in self.files if file.selected and not file.completed and not file.remotePath]
        if not pending:
            return
        # 每次全新转存分配独立临时目录名，避免 reset() 后立即 redownload 撞上远端旧同名文件
        baseTempDir = self.tempRemoteDir or toRemotePath(TEMP_ROOT_DIR, f"{self.taskId}_{uuid4().hex[:6]}")
        self.tempRemoteDir = baseTempDir
        shareUrl = toShareBaseUrl(self.shareKey)
        for relParent, groupFiles in groupFilesByParent(pending):
            targetDir = toRemotePath(baseTempDir, relParent) if relParent else baseTempDir
            fsIds = [file.fsId for file in groupFiles if file.fsId]
            if len(fsIds) != len(groupFiles):
                raise TaskError("分享文件缺少 fs_id，无法转存下载")
            await runner.transferFiles(shareUrl, self.passcode, targetDir, fsIds)
            for file in groupFiles:
                fileName = posixpath.basename(file.relativePath.replace("\\", "/"))
                file.remotePath = toRemotePath(targetDir, fileName)

    async def cleanTemp(self, runner: BaiduPcsRunner) -> None:
        if self.sourceKind != "share" or self.status != TaskStatus.COMPLETED:
            return
        tempDir = self.tempRemoteDir
        if not tempDir:
            return
        try:
            await runner.removeRemote([tempDir])
        except Exception as error:
            logger.warning("静默清理百度分享临时目录失败：{}", error)
            return
        self.tempRemoteDir = ""
        for file in self.files:
            file.remotePath = ""


def groupFilesByParent(files: list[BaiduFile]) -> list[tuple[str, list[BaiduFile]]]:
    groups: dict[str, list[BaiduFile]] = {}
    order: list[str] = []
    for file in files:
        normalized = file.relativePath.replace("\\", "/").lstrip("/")
        parent = posixpath.dirname(normalized)
        if parent == ".":
            parent = ""
        if parent not in groups:
            order.append(parent)
            groups[parent] = []
        groups[parent].append(file)
    return [(parent, groups[parent]) for parent in order]


def hasDownloadingMarker(candidate: Path) -> bool:
    expectedMarker = f"{candidate.name}{TEMP_DOWNLOAD_SUFFIX}".casefold()
    try:
        return any(item.name.casefold() == expectedMarker for item in candidate.parent.iterdir())
    except OSError:
        return False


def findDownloadedFile(stagingDir: Path, file: BaiduFile) -> Path | None:
    expectedName = posixpath.basename(file.relativePath.replace("\\", "/"))
    direct = stagingDir / expectedName
    if direct.is_file() and not hasDownloadingMarker(direct):
        return direct
    for candidate in stagingDir.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.name.lower().endswith(TEMP_DOWNLOAD_SUFFIX):
            continue
        if candidate.name == expectedName and not hasDownloadingMarker(candidate):
            return candidate
    for candidate in stagingDir.rglob("*"):
        if candidate.is_file() and not candidate.name.lower().endswith(TEMP_DOWNLOAD_SUFFIX):
            if candidate.name.casefold() == expectedName.casefold() and not hasDownloadingMarker(candidate):
                return candidate
    return None


def moveToTarget(source: Path, target: Path) -> None:
    if target.is_dir():
        if any(target.iterdir()):
            raise TaskError("输出路径已存在同名文件夹：{path}", path=target)
        target.rmdir()
    elif target.is_file() and target.stat().st_size == 0:
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        shutil.move(str(source), str(target))


def markFileCompleted(file: BaiduFile, target: Path) -> None:
    size = target.stat().st_size
    file.completed = True
    file.downloadedBytes = size
    if file.size <= 0:
        file.size = size


def measureFolder(folder: Path) -> int:
    total = 0
    if not folder.exists():
        return 0
    for path in folder.rglob("*"):
        if not path.is_file() or path.name.lower().endswith(TEMP_DOWNLOAD_SUFFIX):
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total
