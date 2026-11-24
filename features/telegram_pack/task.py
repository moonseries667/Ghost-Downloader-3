from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from loguru import logger

from app.format import BYTE_SCALE, toBytes
from app.models.task import Task, TaskError, TaskFile, TaskStatus, TaskStep
from app.platform.filesystem import deletePath, splitStemExt, toSafeFilename
from .config import telegramConfig
from .inbox import InboxBundle
from .parse import ExportedMediaItem, MediaBundle, buildBundleName, buildExportPayload
from .runtime import DOWNLOAD_TEMPLATE, TdlDownloadSpec, TdlProgress, TdlRunner

STAGING_FOLDER = "staging"
EXPORT_FILE = "export.json"
SAMPLE_SUFFIXES = {".json"}
OUTPUT_SUFFIXES = {".tmp", ".part", ".json"}
SPEED_VALUE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?i?B)/s", re.IGNORECASE)
MESSAGE_PREFIX_PATTERN = re.compile(r"^(\d+)_(.+)$")


@dataclass(kw_only=True)
class TelegramFile(TaskFile):
    messageId: int = 0
    groupedId: str = ""
    rawMessage: dict = field(default_factory=dict)


@dataclass(kw_only=True)
class TelegramStep(TaskStep):
    canPause: ClassVar[bool] = True

    sourceMode: str = "export"
    threads: int = 4
    poolSize: int = 8
    useTakeout: bool = False
    sampleInterval: float = field(default=0.5, repr=False)
    speedText: str = field(default="", repr=False, init=False)
    _task: TelegramTask = field(repr=False, init=False)

    @property
    def task(self) -> TelegramTask:
        return self._task

    @property
    def outputPath(self) -> str:
        return ""

    def setOptions(self, options: dict) -> None:
        if "sourceMode" in options:
            self.sourceMode = str(options["sourceMode"])
        if "threads" in options:
            self.threads = int(options["threads"])
        if "poolSize" in options:
            self.poolSize = int(options["poolSize"])
        if "useTakeout" in options:
            self.useTakeout = bool(options["useTakeout"])

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        task = self.task
        runner = task.runner
        if runner is None:
            raise TaskError("tdl 未就绪")
        stagingDir = task.partPath / STAGING_FOLDER
        stagingDir.mkdir(parents=True, exist_ok=True)
        spec = self.buildSpec(stagingDir)
        totalSize = sum(file.size for file in task.files if file.selected)

        def onProgress(progress: TdlProgress) -> None:
            self.progress = min(100.0, max(0.0, progress.percent))
            if task.fileSize > 0:
                self.receivedBytes = max(self.receivedBytes, int(task.fileSize * self.progress / 100))
            self.speed = parseSpeed(progress.speed)
            self.speedText = progress.speed

        sampler = asyncio.create_task(self.sampleStaging(stagingDir, totalSize, reportSpeed, waitForSpeedLimit))
        try:
            await runner.download(spec, onProgress=onProgress)
            self.finalizeOutputs(stagingDir)
            self.progress = 100
        finally:
            sampler.cancel()
            with suppress(asyncio.CancelledError):
                await sampler

    def buildSpec(self, stagingDir: Path) -> TdlDownloadSpec:
        task = self.task
        if self.sourceMode == "url":
            return TdlDownloadSpec(
                outputFolder=stagingDir,
                url=task.url,
                threads=self.threads,
                poolSize=self.poolSize,
                useTakeout=self.useTakeout,
                template=DOWNLOAD_TEMPLATE,
            )
        files = [file for file in task.files if file.selected and not file.completed]
        if not files:
            files = [file for file in task.files if file.selected]
        if not files:
            raise TaskError("没有可下载的选中文件")
        exportPath = task.partPath / EXPORT_FILE
        exportPath.parent.mkdir(parents=True, exist_ok=True)
        payload = buildExportPayload(task.chatId, [toExportItem(file) for file in files])
        exportPath.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return TdlDownloadSpec(
            outputFolder=stagingDir,
            exportFile=exportPath,
            threads=self.threads,
            poolSize=self.poolSize,
            useTakeout=self.useTakeout,
            template=DOWNLOAD_TEMPLATE,
        )

    async def sampleStaging(
        self,
        stagingDir: Path,
        totalSize: int,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        last = measureFolder(stagingDir)
        while True:
            await asyncio.sleep(self.sampleInterval)
            total = measureFolder(stagingDir)
            delta = max(0, total - last)
            last = total
            if delta > 0:
                reportSpeed(delta)
            self.receivedBytes = max(self.receivedBytes, total)
            if self.progress <= 0 and totalSize > 0:
                self.progress = min(99.0, total / totalSize * 100)
            await waitForSpeedLimit()

    def finalizeOutputs(self, stagingDir: Path) -> None:
        task = self.task
        outputs = [
            path
            for path in stagingDir.rglob("*")
            if path.is_file() and path.suffix.lower() not in OUTPUT_SUFFIXES
        ]
        if not outputs:
            raise TaskError("tdl 下载完成，但未找到输出文件")
        outputBytes = sum(path.stat().st_size for path in outputs)
        if task.isOutputFolder or len(outputs) > 1:
            self.moveToFolder(outputs)
        else:
            self.moveToFile(outputs[0])
        self.receivedBytes = max(self.receivedBytes, outputBytes)
        deletePath(task.partPath / EXPORT_FILE)
        try:
            stagingDir.rmdir()
        except OSError:
            pass

    def moveToFolder(self, outputs: list[Path]) -> None:
        task = self.task
        targetDir = Path(task.outputPath)
        if targetDir.is_file() and targetDir.stat().st_size == 0:
            targetDir.unlink()
        targetDir.mkdir(parents=True, exist_ok=True)
        pending = [file for file in task.files if not file.completed]
        for output in outputs:
            messageId, strippedName = splitMessagePrefix(output.name)
            file = popMatchedFile(pending, messageId, strippedName, output.name)
            name = Path(file.relativePath).name if file is not None else strippedName
            target = toUniquePath(targetDir, toSafeFilename(name, fallback=output.name))
            moveFile(output, target)
            if file is not None:
                markFileCompleted(file, target)

    def moveToFile(self, output: Path) -> None:
        task = self.task
        target = Path(task.outputPath)
        if task.sourceMode == "url" and target.suffix == "" and output.suffix != "":
            # URL 降级任务默认无扩展名，转为同名文件夹落盘以保留真实文件名且兼容 TaskStore 监听
            if target.is_file() and target.stat().st_size == 0:
                target.unlink()
            target.mkdir(parents=True, exist_ok=True)
            _, strippedName = splitMessagePrefix(output.name)
            destination = toUniquePath(target, toSafeFilename(strippedName, fallback=output.name))
            moveFile(output, destination)
            if len(task.files) == 1:
                markFileCompleted(task.files[0], destination)
            return
        if target.is_dir():
            if any(target.iterdir()):
                raise TaskError("输出路径已存在同名文件夹：{path}", path=target)
            target.rmdir()
        elif target.is_file() and target.stat().st_size == 0:
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        moveFile(output, target)
        if len(task.files) == 1:
            markFileCompleted(task.files[0], target)


@dataclass(kw_only=True, eq=False)
class TelegramTask(Task):
    packId: str = "telegram"
    canEdit: ClassVar[bool] = True
    fileType: ClassVar[type[TaskFile]] = TelegramFile
    _onCompleted: ClassVar[Callable[[TelegramTask], None] | None] = None

    chatId: str = ""
    groupedId: str = ""
    bundleKey: str = ""
    sourceMode: str = "export"
    files: list[TelegramFile] = field(default_factory=list)
    runner: TdlRunner | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.steps:
            self.addStep(TelegramStep(
                stepIndex=0,
                sourceMode=self.sourceMode,
                threads=telegramConfig.threads.value,
                poolSize=telegramConfig.poolSize.value,
                useTakeout=telegramConfig.useTakeout.value,
            ))

    def setOptions(self, options: dict) -> None:
        if "sourceMode" in options:
            self.sourceMode = str(options["sourceMode"])
        super().setOptions(options)
        for step in self.steps:
            if isinstance(step, TelegramStep):
                step.sourceMode = self.sourceMode

    def reset(self) -> TaskStatus:
        for file in self.files:
            file.completed = False
            file.downloadedBytes = 0
        return super().reset()

    def deleteFiles(self) -> None:
        super().deleteFiles()
        deletePath(self.partPath / STAGING_FOLDER)
        deletePath(self.partPath / EXPORT_FILE)
        deletePath(self.partPath)

    async def run(
        self,
        reportSpeed: Callable[[int], None],
        waitForSpeedLimit: Callable[[], Awaitable[None]],
    ) -> None:
        if self.runner is None:
            # 恢复的任务没有 pack 注入的 runner，复用全局单例以共用同一把 BoltDB 锁
            from .runtime import tdlRunner
            self.runner = tdlRunner
        await super().run(reportSpeed, waitForSpeedLimit)
        if self.status == TaskStatus.COMPLETED and self.bundleKey and type(self)._onCompleted is not None:
            try:
                type(self)._onCompleted(self)
            except Exception as error:
                logger.warning("Telegram 任务完成回调失败：{}", error)


def buildBundleTask(
    bundle: MediaBundle | InboxBundle,
    outputFolder: Path,
    *,
    chatId: str = "",
    url: str = "",
    runner: TdlRunner | None = None,
) -> TelegramTask:
    media = toMediaBundle(bundle)
    files: list[TelegramFile] = []
    for item in media.items:
        files.append(TelegramFile(
            index=len(files),
            relativePath=toSafeFilename(item.fileName, fallback=str(item.messageId)),
            size=item.size,
            messageId=item.messageId,
            groupedId=item.groupedId,
            rawMessage=dict(item.rawMessage),
        ))
    task = TelegramTask(
        name=buildBundleName(media),
        url=url,
        outputFolder=Path(outputFolder),
        chatId=media.chatId or chatId,
        groupedId=media.groupedId,
        bundleKey=media.bundleKey,
        sourceMode="url" if url and not media.items else "export",
        files=files,
        fileSize=sum(file.size for file in files) or media.totalSize,
    )
    task.runner = runner
    return task


def toMediaBundle(bundle: MediaBundle | InboxBundle) -> MediaBundle:
    if isinstance(bundle, MediaBundle):
        return bundle
    items = []
    for position, messageId in enumerate(bundle.messageIds):
        fileName = bundle.fileNames[position] if position < len(bundle.fileNames) else str(messageId)
        rawMessage = bundle.rawMessages[position] if position < len(bundle.rawMessages) else {}
        if position < len(bundle.fileSizes):
            size = bundle.fileSizes[position]
        else:
            size = bundle.totalSize if len(bundle.messageIds) == 1 else 0
        items.append(ExportedMediaItem(
            messageId=messageId,
            fileName=fileName,
            size=size,
            groupedId=bundle.groupedId,
            date=bundle.date,
            text=bundle.caption,
            rawMessage=dict(rawMessage),
        ))
    return MediaBundle(
        bundleKey=bundle.bundleKey,
        chatId=bundle.chatId,
        groupedId=bundle.groupedId,
        firstMessageId=bundle.firstMessageId,
        lastMessageId=bundle.lastMessageId,
        date=bundle.date,
        caption=bundle.caption,
        items=tuple(items),
        totalSize=bundle.totalSize,
    )


def splitMessagePrefix(name: str) -> tuple[int | None, str]:
    match = MESSAGE_PREFIX_PATTERN.match(name)
    if match is None:
        return None, name
    return int(match.group(1)), match.group(2)


def popMatchedFile(
    pending: list[TelegramFile],
    messageId: int | None,
    strippedName: str,
    rawName: str,
) -> TelegramFile | None:
    if messageId is not None:
        for index, file in enumerate(pending):
            if file.messageId == messageId:
                return pending.pop(index)
    for candidate in (strippedName.casefold(), rawName.casefold()):
        for index, file in enumerate(pending):
            if Path(file.relativePath).name.casefold() == candidate:
                return pending.pop(index)
    return None


def toExportItem(file: TelegramFile) -> ExportedMediaItem:
    return ExportedMediaItem(
        messageId=file.messageId,
        fileName=file.relativePath,
        size=file.size,
        groupedId=file.groupedId,
        date=0,
        text="",
        rawMessage=dict(file.rawMessage),
    )


def markFileCompleted(file: TelegramFile, target: Path) -> None:
    size = target.stat().st_size
    file.completed = True
    file.downloadedBytes = size
    if file.size <= 0:
        file.size = size


def toUniquePath(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = splitStemExt(name)
    index = 2
    while True:
        candidate = folder / toSafeFilename(f"{stem} ({index}){suffix}", fallback=name)
        if not candidate.exists():
            return candidate
        index += 1


def measureFolder(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() in SAMPLE_SUFFIXES:
            continue
        try:
            total += path.stat().st_size
        except OSError:
            # tdl 续传时可能在扫描间隙重命名或删除临时文件
            continue
    return total


def moveFile(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError:
        shutil.move(str(source), str(target))


def parseSpeed(text: str) -> int:
    match = SPEED_VALUE_PATTERN.search(text or "")
    if match is None:
        return 0
    unit = match.group(2).upper().replace("I", "")
    if unit not in BYTE_SCALE:
        return 0
    return toBytes(match.group(1), unit)
