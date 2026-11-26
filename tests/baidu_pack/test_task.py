from __future__ import annotations

import asyncio
import posixpath
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.pack import PackServices
from app.models.task import TaskStatus
from baidu_pack.pack import BaiduPack
from baidu_pack.runtime import BaiduDownloadProgress, BaiduDriveEntry, TEMP_ROOT_DIR
from baidu_pack.task import BaiduFile, BaiduTask, buildDriveTask


class FakeTaskRunner:
    def __init__(self, *, installed: bool = True, pauseOnce: bool = False) -> None:
        self.runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go" if installed else "")
        self.transferCalls: list[tuple[str, str, str, list[str]]] = []
        self.downloadCalls: list[tuple[list[str], Path, str, int]] = []
        self.removedPaths: list[list[str]] = []
        self.remoteTempDirs: list[BaiduDriveEntry] = []
        self._pauseOnce = pauseOnce
        self._didPause = False

    def resolveProxy(self) -> str | None:
        return None

    async def probeUser(self):
        from baidu_pack.runtime import BaiduUserInfo
        return BaiduUserInfo(isLoggedIn=True, uid="100", username="tester", totalQuota="2TB", usedQuota="10GB")

    async def transferFiles(self, shareBaseUrl: str, passcode: str, targetDir: str, fsIds: list[str]) -> None:
        self.transferCalls.append((shareBaseUrl, passcode, targetDir, list(fsIds)))

    async def download(self, remotePaths: list[str], saveDir: Path, *, mode="locate", threads=8, onProgress=None) -> None:
        self.downloadCalls.append((list(remotePaths), saveDir, mode, threads))
        if self._pauseOnce and not self._didPause:
            self._didPause = True
            saveDir.mkdir(parents=True, exist_ok=True)
            (saveDir / "partial.bin.BaiduPCS-Go-downloading").write_bytes(b"state")
            await asyncio.Event().wait()
        saveDir.mkdir(parents=True, exist_ok=True)
        for remotePath in remotePaths:
            name = posixpath.basename(remotePath)
            (saveDir / name).write_bytes(f"data:{remotePath}".encode("utf-8"))
        if onProgress is not None:
            onProgress(BaiduDownloadProgress(100, 100, 100.0, 1024, "1KB/s", "[1] ↓ 100B/100B 1KB/s"))

    async def removeRemote(self, remotePaths: list[str]) -> None:
        self.removedPaths.append(list(remotePaths))
        removedSet = set(remotePaths)
        self.remoteTempDirs = [item for item in self.remoteTempDirs if item.path not in removedSet]

    async def listDirectory(self, remotePath: str = "/") -> list[BaiduDriveEntry]:
        if remotePath == TEMP_ROOT_DIR:
            return list(self.remoteTempDirs)
        return []

    async def cleanStaleTempDirs(self, keepNames: set[str]) -> list[str]:
        stale = [item.path for item in self.remoteTempDirs if item.isDir and item.name not in keepNames]
        if stale:
            await self.removeRemote(stale)
        return stale

    async def stopAll(self) -> None:
        pass


async def noopLimit() -> None:
    return None


async def test_drive_task_downloads_directly_without_transfer(downloadFolder):
    runner = FakeTaskRunner()
    entries = [
        (BaiduDriveEntry("1", "a.txt", "/文档/a.txt", 16, False), "文档/a.txt"),
        (BaiduDriveEntry("2", "b.txt", "/文档/b.txt", 16, False), "文档/b.txt"),
    ]
    task = buildDriveTask(entries, downloadFolder, name="我的文档", runner=runner)
    task.setStatus(TaskStatus.RUNNING)

    await task.run(lambda _b: None, noopLimit)

    assert task.status == TaskStatus.COMPLETED
    assert runner.transferCalls == []
    assert runner.removedPaths == []
    assert (downloadFolder / "我的文档" / "文档" / "a.txt").read_text(encoding="utf-8") == "data:/文档/a.txt"
    assert (downloadFolder / "我的文档" / "文档" / "b.txt").read_text(encoding="utf-8") == "data:/文档/b.txt"


async def test_share_task_transfers_only_selected_preserves_structure_and_cleans_temp(downloadFolder):
    runner = FakeTaskRunner()
    task = BaiduTask(
        name="共享资料",
        url="https://pan.baidu.com/s/1ShareABC?pwd=ab12",
        outputFolder=downloadFolder,
        sourceKind="share",
        shareKey="1ShareABC",
        passcode="ab12",
        files=[
            BaiduFile(index=0, relativePath="dir1/readme.txt", size=12, fsId="101", selected=True),
            BaiduFile(index=1, relativePath="dir2/readme.txt", size=12, fsId="102", selected=True),
            BaiduFile(index=2, relativePath="dir2/skip.zip", size=999, fsId="103", selected=False),
        ],
    )
    task.runner = runner
    task.setStatus(TaskStatus.RUNNING)

    await task.run(lambda _b: None, noopLimit)

    assert task.status == TaskStatus.COMPLETED
    # 仅转存勾选的两个文件，并按子目录分组避免同名冲突
    assert len(runner.transferCalls) == 2
    baseDir1 = posixpath.dirname(runner.transferCalls[0][2])
    baseDir2 = posixpath.dirname(runner.transferCalls[1][2])
    assert baseDir1 == baseDir2
    assert baseDir1.startswith(f"/_ghd_tmp/{task.taskId}_")
    assert runner.transferCalls == [
        ("https://pan.baidu.com/s/1ShareABC", "ab12", f"{baseDir1}/dir1", ["101"]),
        ("https://pan.baidu.com/s/1ShareABC", "ab12", f"{baseDir1}/dir2", ["102"]),
    ]
    assert (downloadFolder / "共享资料" / "dir1" / "readme.txt").is_file()
    assert (downloadFolder / "共享资料" / "dir2" / "readme.txt").is_file()
    assert not (downloadFolder / "共享资料" / "dir2" / "skip.zip").exists()
    # 完成后立即删除临时目录
    assert runner.removedPaths == [[baseDir1]]
    assert task.tempRemoteDir == ""
    assert posixpath.basename(baseDir1) not in BaiduTask.protectedTempNames()

    # Revive：对已完成任务勾选第 3 个文件，应回到 WAITING 并仅转存下载新文件
    task.setSelection({0, 1, 2})
    assert task.status == TaskStatus.WAITING
    task.setStatus(TaskStatus.RUNNING)
    await task.run(lambda _b: None, noopLimit)
    assert task.status == TaskStatus.COMPLETED
    assert (downloadFolder / "共享资料" / "dir2" / "skip.zip").is_file()


async def test_paused_share_task_keeps_temp_during_silent_cleanup_and_resumes(engine, downloadFolder):
    runner = FakeTaskRunner(pauseOnce=True)
    task = BaiduTask(
        name="single.bin",
        url="https://pan.baidu.com/s/1PauseKey?pwd=1234",
        outputFolder=downloadFolder,
        sourceKind="share",
        shareKey="1PauseKey",
        passcode="1234",
        files=[BaiduFile(index=0, relativePath="single.bin", size=32, fsId="555", selected=True)],
    )
    task.runner = runner
    task.setStatus(TaskStatus.RUNNING)

    runTask = asyncio.create_task(task.run(lambda _b: None, noopLimit))
    await asyncio.sleep(0.05)
    runTask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runTask
    task.setStatus(TaskStatus.PAUSED)

    # 暂停中：临时目录被保护，静默清理只删除无关残留目录
    tempName = posixpath.basename(task.tempRemoteDir)
    assert task.tempRemoteDir.startswith(f"/_ghd_tmp/{task.taskId}_")
    assert tempName in BaiduTask.protectedTempNames()
    runner.remoteTempDirs = [
        BaiduDriveEntry("901", tempName, task.tempRemoteDir, 0, True),
        BaiduDriveEntry("902", "tsk_orphan_old", "/_ghd_tmp/tsk_orphan_old", 0, True),
    ]

    pack = BaiduPack(PackServices(coroutineRunner=engine.coroutineRunner, speedMeter=engine.speedMeter))
    pack.runner = runner
    cleaned = await pack.cleanStaleTempDirs()
    assert cleaned == ["/_ghd_tmp/tsk_orphan_old"]
    assert any(item.name == tempName for item in runner.remoteTempDirs)

    # 恢复继续下载，不重复转存，完成后立即删除临时目录
    expectedTempDir = task.tempRemoteDir
    task.setStatus(TaskStatus.RUNNING)
    await task.run(lambda _b: None, noopLimit)
    assert task.status == TaskStatus.COMPLETED
    assert len(runner.transferCalls) == 1
    assert (downloadFolder / "single.bin").is_file()
    assert [expectedTempDir] in runner.removedPaths
    assert tempName not in BaiduTask.protectedTempNames()


def test_find_downloaded_file_rejects_partial_with_downloading_marker(tmp_path):
    from baidu_pack.task import findDownloadedFile

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "movie.mkv").write_bytes(b"partial")
    (staging / "movie.mkv.BaiduPCS-Go-downloading").write_bytes(b"state")
    file = BaiduFile(index=0, relativePath="movie.mkv", size=100, fsId="1")
    assert findDownloadedFile(staging, file) is None

    (staging / "movie.mkv.BaiduPCS-Go-downloading").unlink()
    assert findDownloadedFile(staging, file) == staging / "movie.mkv"
