from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.models.task import Task, TaskStatus
from telegram_pack.config import telegramConfig
from telegram_pack.inbox import InboxBundle
from telegram_pack.parse import groupExportedMessages
from telegram_pack.runtime import DOWNLOAD_TEMPLATE, TdlDownloadSpec, TdlProgress
from telegram_pack.task import TelegramTask, buildBundleTask, measureFolder, toMediaBundle


def fileMessage(messageId: int, fileName: str, *, size: int = 100, groupedId=None, text: str = "") -> dict:
    return {
        "id": messageId,
        "file": fileName,
        "size": size,
        "grouped_id": groupedId,
        "date": 0,
        "text": text,
        "raw": {
            "ID": messageId,
            "Media": {"Document": {"Size": size, "Attributes": [{"FileName": fileName}]}},
        },
    }


def mediaBundle(messages: list[dict], chatId: str = "c1"):
    return groupExportedMessages({"id": chatId, "messages": messages})[0]


class FakeTdlRunner:
    """只写磁盘的 tdl 替身，记录每次下载 spec 与 export.json 内容。"""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        isBusy: bool = False,
        onDownload=None,
        chunkCount: int = 1,
        chunkDelay: float = 0.0,
        progress: TdlProgress | None = None,
        onProgressSent=None,
    ) -> None:
        self.files = dict(files or {})
        self._isBusy = isBusy
        self.onDownload = onDownload
        self.chunkCount = chunkCount
        self.chunkDelay = chunkDelay
        self.progress = progress
        self.onProgressSent = onProgressSent
        self.specs: list[TdlDownloadSpec] = []
        self.exportPayloads: list[dict] = []
        self.runtime = SimpleNamespace(path=lambda: "/fake/tdl")

    @property
    def isBusy(self) -> bool:
        return self._isBusy

    async def download(self, spec: TdlDownloadSpec, onProgress=None) -> None:
        self.specs.append(spec)
        if spec.exportFile is not None and spec.exportFile.exists():
            self.exportPayloads.append(json.loads(spec.exportFile.read_text(encoding="utf-8")))
        self._isBusy = True
        try:
            if self.onDownload is not None:
                await self.onDownload(spec, onProgress)
            else:
                await self.writeFiles(spec)
            if self.progress is not None and onProgress is not None:
                onProgress(self.progress)
                if self.onProgressSent is not None:
                    self.onProgressSent()
        finally:
            self._isBusy = False

    async def writeFiles(self, spec: TdlDownloadSpec) -> None:
        for name, content in self.files.items():
            target = spec.outputFolder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            chunkSize = max(1, (len(content) + self.chunkCount - 1) // self.chunkCount)
            for start in range(0, len(content), chunkSize):
                with target.open("wb" if start == 0 else "ab") as file:
                    file.write(content[start:start + chunkSize])
                await asyncio.sleep(self.chunkDelay)


async def noSpeedLimit() -> None:
    pass


def buildUrlTask(folder, runner: FakeTdlRunner, *, name: str = "channel_42") -> TelegramTask:
    task = TelegramTask(
        name=name,
        url="https://t.me/channel/42",
        outputFolder=folder,
        chatId="channel",
        sourceMode="url",
    )
    task.runner = runner
    return task


async def test_run_single_export_writes_file_and_cleans_part_path(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([fileMessage(10, "clip.mp4", size=5)])
    runner = FakeTdlRunner({"clip.mp4": b"hello"})
    task = buildBundleTask(bundle, folder, chatId="c1", runner=runner)

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    assert runner.exportPayloads[0]["messages"][0]["id"] == 10
    assert (folder / task.name).read_bytes() == b"hello"
    assert not task.partPath.exists()
    assert task.status == TaskStatus.COMPLETED


async def test_run_selected_group_files_land_in_named_folder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([
        fileMessage(10, "a.mp4", size=4, groupedId=900),
        fileMessage(11, "b.mp4", size=5, groupedId=900),
        fileMessage(12, "c.mp4", size=6, groupedId=900),
    ])
    runner = FakeTdlRunner({"a.mp4": b"aaaa", "c.mp4": b"cccccc"})
    task = buildBundleTask(bundle, folder, chatId="c1", runner=runner)

    assert len(task.files) == 3
    task.setSelection({0, 2})
    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    assert {message["id"] for message in runner.exportPayloads[0]["messages"]} == {10, 12}
    output = folder / task.name
    assert output.is_dir()
    assert {path.name for path in output.iterdir()} == {"a.mp4", "c.mp4"}
    assert task.isOutputFolder is True


async def test_run_url_mode_single_output_replaces_placeholder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    runner = FakeTdlRunner({"42_solo.bin": b"payload"})
    task = buildUrlTask(folder, runner)
    placeholder = folder / task.name
    placeholder.write_bytes(b"")

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    spec = runner.specs[0]
    assert spec.url == task.url
    assert spec.exportFile is None
    assert spec.template == DOWNLOAD_TEMPLATE
    assert placeholder.is_dir()
    assert (placeholder / "solo.bin").read_bytes() == b"payload"


async def test_run_url_mode_single_output_with_extension_stays_file(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    runner = FakeTdlRunner({"42_solo.bin": b"payload"})
    task = buildUrlTask(folder, runner, name="solo.bin")
    placeholder = folder / task.name
    placeholder.write_bytes(b"")

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    assert placeholder.is_file()
    assert placeholder.read_bytes() == b"payload"


async def test_run_url_mode_multiple_outputs_land_in_folder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    runner = FakeTdlRunner({"one.bin": b"1", "two.bin": b"22"})
    task = buildUrlTask(folder, runner)
    placeholder = folder / task.name
    placeholder.write_bytes(b"")

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    output = folder / task.name
    assert runner.specs[0].exportFile is None
    assert output.is_dir()
    assert {path.name for path in output.iterdir()} == {"one.bin", "two.bin"}


async def test_sample_staging_reports_speed_and_progress_without_percent_log(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([fileMessage(10, "big.bin", size=200_000)])
    runner = FakeTdlRunner({"big.bin": b"x" * 200_000}, chunkCount=10, chunkDelay=0.005)
    task = buildBundleTask(bundle, folder, chatId="c1", runner=runner)
    step = task.steps[0]
    step.sampleInterval = 0.01

    speeds: list[int] = []
    progressSamples: list[float] = []

    def reportSpeed(delta: int) -> None:
        speeds.append(delta)
        progressSamples.append(step.progress)

    task.setStatus(TaskStatus.RUNNING)
    await task.run(reportSpeed, noSpeedLimit)

    assert speeds
    assert step.receivedBytes > 0
    assert step.progress > 0
    assert any(progress > 0 for progress in progressSamples)


async def test_percent_log_updates_step_progress(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([fileMessage(10, "small.bin", size=3)])
    runner = FakeTdlRunner(
        {"small.bin": b"abc"},
        progress=TdlProgress(percent=42.0, speed="1.2 MB/s", text=""),
    )
    task = buildBundleTask(bundle, folder, chatId="c1", runner=runner)
    snapshots: list[float] = []
    runner.onProgressSent = lambda: snapshots.append(task.steps[0].progress)

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    assert 42.0 in snapshots


async def test_cancel_keeps_half_file_and_resume_completes(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([fileMessage(10, "clip.mp4", size=5)])

    async def hang(spec: TdlDownloadSpec, onProgress) -> None:
        (spec.outputFolder / "clip.mp4").write_bytes(b"half")
        await asyncio.Event().wait()

    runner = FakeTdlRunner({"clip.mp4": b"half"}, onDownload=hang)
    task = buildBundleTask(bundle, folder, chatId="c1", runner=runner)
    task.setStatus(TaskStatus.RUNNING)

    running = asyncio.create_task(task.run([].append, noSpeedLimit))
    await asyncio.sleep(0.02)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    halfFile = task.partPath / "staging" / "clip.mp4"
    assert halfFile.read_bytes() == b"half"
    assert runner.isBusy is False

    data = task.toDict()
    assert "runner" not in data
    restored = Task.fromDict(data)
    assert isinstance(restored, TelegramTask)
    assert restored.runner is None

    resumedRunner = FakeTdlRunner({"clip.mp4": b"half plus"})
    restored.runner = resumedRunner
    restored.setStatus(TaskStatus.RUNNING)
    await restored.run([].append, noSpeedLimit)

    # --continue 由 TdlRunner.download 统一追加（runtime.py），Fake 只能观测 spec；
    # 此处确认恢复任务仍走 export 模式并复用原 staging 目录。
    assert resumedRunner.specs[0].exportFile is not None
    assert resumedRunner.specs[0].outputFolder == restored.partPath / "staging"
    assert (folder / restored.name).read_bytes() == b"half plus"
    assert restored.status == TaskStatus.COMPLETED
    assert not restored.partPath.exists()


async def test_delete_files_removes_staging_and_export(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    task = buildBundleTask(mediaBundle([fileMessage(10, "clip.mp4")]), folder, chatId="c1")
    staging = task.partPath / "staging"
    staging.mkdir(parents=True)
    (staging / "clip.mp4").write_bytes(b"left")
    exportFile = task.partPath / "export.json"
    exportFile.write_text("{}", encoding="utf-8")

    task.deleteFiles()

    assert not staging.exists()
    assert not exportFile.exists()
    assert not task.partPath.exists()


async def test_task_inherits_telegram_config_options_and_template(downloadFolder, config):
    telegramConfig.threads.value = 12
    telegramConfig.poolSize.value = 16
    telegramConfig.useTakeout.value = True
    bundle = mediaBundle([fileMessage(10, "clip.mp4", size=5)])
    runner = FakeTdlRunner({"10_clip.mp4": b"hello"})
    task = buildBundleTask(bundle, downloadFolder, runner=runner)

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    spec = runner.specs[0]
    assert spec.threads == 12
    assert spec.poolSize == 16
    assert spec.useTakeout is True
    assert spec.template == DOWNLOAD_TEMPLATE


def test_measure_folder_counts_tmp_files_and_skips_json(tmp_path):
    (tmp_path / "10_clip.mp4.tmp").write_bytes(b"123456")
    (tmp_path / "11_done.mp4").write_bytes(b"78")
    (tmp_path / "export.json").write_bytes(b'{"ignore": true}')

    assert measureFolder(tmp_path) == 8


async def test_move_to_folder_matches_message_id_prefix_and_strips_unmatched(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([
        fileMessage(10, "video.mp4", size=4, groupedId=900),
        fileMessage(11, "video.mp4", size=5, groupedId=900),
    ])
    runner = FakeTdlRunner({
        "10_video.mp4": b"aaaa",
        "11_video.mp4": b"bbbbb",
        "99_extra.jpg": b"cc",
    })
    task = buildBundleTask(bundle, folder, runner=runner)

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    output = folder / task.name
    assert (output / "video.mp4").read_bytes() == b"aaaa"
    assert (output / "video (2).mp4").read_bytes() == b"bbbbb"
    assert (output / "extra.jpg").read_bytes() == b"cc"
    assert all(file.completed for file in task.files)


def test_build_bundle_task_prefers_numeric_media_chat_id_and_restores_inbox_bundle(tmp_path):
    bundle = mediaBundle([
        fileMessage(10, "a.mp4", size=111, groupedId=77),
        fileMessage(11, "b.mp4", size=222, groupedId=77),
    ], chatId="-100123456")
    task = buildBundleTask(bundle, tmp_path, chatId="public_username")
    assert task.chatId == "-100123456"

    inboxBundle = InboxBundle.fromBundle(bundle)
    restored = toMediaBundle(inboxBundle)
    assert restored.groupedId == "77"
    assert [item.size for item in restored.items] == [111, 222]
    assert [item.groupedId for item in restored.items] == ["77", "77"]


async def test_task_on_completed_hook_called_and_errors_swallowed(tmp_path, monkeypatch):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    bundle = mediaBundle([fileMessage(10, "clip.mp4", size=5)])
    runner = FakeTdlRunner({"10_clip.mp4": b"hello"})
    completedKeys: list[str] = []

    def onCompleted(task: TelegramTask) -> None:
        completedKeys.append(task.bundleKey)
        raise RuntimeError("hook boom")

    monkeypatch.setattr(TelegramTask, "_onCompleted", onCompleted)
    task = buildBundleTask(bundle, folder, runner=runner)

    task.setStatus(TaskStatus.RUNNING)
    await task.run([].append, noSpeedLimit)

    assert completedKeys == [bundle.bundleKey]
    assert task.status == TaskStatus.COMPLETED
