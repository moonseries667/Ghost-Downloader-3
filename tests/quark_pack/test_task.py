from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.config.cfg import cfg
from app.models.task import Task, TaskError, TaskOptions, TaskStatus
from quark_pack.client import QUARK_PC_UA, DriveEntry, QuarkClient
from quark_pack.config import quarkConfig
from quark_pack.pack import QuarkParser
from quark_pack.task import QuarkTask, buildDriveTask
from tests.helpers import buildRangeHandler, runStep, waitUntil
from tests.quark_pack.conftest import buildQuarkClient, buildQuarkTestClient

COOKIE = "__pus=fake-pus-value-DO-NOT-LEAK"


def dumped(task: Task) -> str:
    return json.dumps(task.toDict(), ensure_ascii=False)


def bindClient(origin) -> QuarkClient:
    cfg.set(quarkConfig.cookie, COOKIE)
    cfg.set(quarkConfig.subworkerCount, 1)
    origin.taskDoneAfter = 1
    origin.mapSavedFids = True
    return buildQuarkClient(origin, readCookie=lambda: str(quarkConfig.cookie.value or ""))


async def serveBytes(startServer, bodies: dict[str, bytes], *, failUntil: int = 0):
    state = {"hits": 0, "referers": [], "cookies": [], "userAgents": []}

    async def handler(request: web.Request) -> web.StreamResponse:
        state["hits"] += 1
        state["referers"].append(request.headers.get("Referer", ""))
        state["cookies"].append(request.headers.get("Cookie", ""))
        state["userAgents"].append(request.headers.get("User-Agent", ""))
        fid = request.path.rstrip("/").rsplit("/", 1)[-1]
        if state["hits"] <= failUntil:
            return web.Response(status=403)
        return await buildRangeHandler(bodies[fid])(request)

    served = await startServer(handler)
    return served.removesuffix("/file") + "/bytes", state


def shareFile(fid: str, name: str, size: int, token: str) -> dict:
    return {
        "fid": fid, "file_name": name, "size": size, "dir": False, "share_fid_token": token,
    }


async def parseShare(origin, url: str, folder):
    client = bindClient(origin)
    task = await QuarkParser(SimpleNamespace(client=client)).parse(
        TaskOptions(url=url, outputFolder=folder),
    )
    return task


async def test_share_parse_does_not_save_and_single_file_cleans_temp(quarkOrigin, startServer, downloadFolder):
    content = b"single-file-body" * 40
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    root, _state = await serveBytes(startServer, {"saved-file-b": content})
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    assert not any(call["path"].endswith("/share/sharepage/save") for call in quarkOrigin.calls)
    assert task.name == "b.txt"
    assert task.sourceKind == "share"

    await runStep(task.steps[0])

    assert (downloadFolder / "b.txt").read_bytes() == content
    assert quarkOrigin.deleteBodies == [{
        "action_type": 2,
        "filelist": [f"fid-{task.taskId}"],
        "exclude_fids": [],
    }]
    assert task.tempFolderFid == ""
    assert task.files[0].savedFid == ""
    assert task.status == TaskStatus.COMPLETED


async def test_share_run_writes_nested_files(quarkOrigin, startServer, downloadFolder):
    nested = b"nested-a" * 50
    rootFile = b"root-b" * 40
    quarkOrigin.shareLists = {
        "0": [
            {"fid": "dir-1", "file_name": "目录甲", "size": 0, "dir": True, "share_fid_token": "tok-dir"},
            shareFile("file-b", "b.txt", len(rootFile), "tok-b"),
        ],
        "dir-1": [shareFile("file-a", "a.txt", len(nested), "tok-a")],
    }
    root, state = await serveBytes(startServer, {
        "saved-file-a": nested,
        "saved-file-b": rootFile,
    })
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001?pwd=ab12", downloadFolder)
    assert [item.pdirFid for item in task.files] == ["dir-1", "0"]

    await runStep(task.steps[0])

    folder = downloadFolder / "分享标题"
    assert (folder / "目录甲" / "a.txt").read_bytes() == nested
    assert (folder / "b.txt").read_bytes() == rootFile
    assert [body["pdir_fid"] for body in quarkOrigin.saveBodies] == ["dir-1", "0"]
    assert state["referers"]
    assert all(item == "https://pan.quark.cn/" for item in state["referers"])
    assert all(COOKIE in item for item in state["cookies"])
    assert set(state["userAgents"]) == {QUARK_PC_UA}
    assert task.tempFolderFid == ""


async def test_grouped_save_matches_name_and_size_when_listing_is_reversed(
    quarkOrigin, startServer, downloadFolder,
):
    fileA = b"A" * 300
    fileC = b"C" * 180
    fileB = b"B" * 420
    quarkOrigin.shareLists = {
        "0": [
            {"fid": "dir-1", "file_name": "目录甲", "size": 0, "dir": True, "share_fid_token": "tok-dir"},
            shareFile("file-b", "b.txt", len(fileB), "tok-b"),
        ],
        "dir-1": [
            shareFile("file-a", "a.txt", len(fileA), "tok-a"),
            shareFile("file-c", "c.txt", len(fileC), "tok-c"),
        ],
    }
    quarkOrigin.omitSaveAsFor = {"dir-1"}
    quarkOrigin.fallbackEntries = {
        "file-a": {"fid": "saved-file-a", "file_name": "a.txt", "size": len(fileA), "dir": False},
        "file-c": {"fid": "saved-file-c", "file_name": "c.txt", "size": len(fileC), "dir": False},
        "file-b": {"fid": "saved-file-b", "file_name": "b.txt", "size": len(fileB), "dir": False},
    }
    root, _state = await serveBytes(startServer, {
        "saved-file-a": fileA,
        "saved-file-c": fileC,
        "saved-file-b": fileB,
    })
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)

    await runStep(task.steps[0])

    folder = downloadFolder / "分享标题"
    assert (folder / "目录甲" / "a.txt").read_bytes() == fileA
    assert (folder / "目录甲" / "c.txt").read_bytes() == fileC
    assert (folder / "b.txt").read_bytes() == fileB
    nestedSave = next(body for body in quarkOrigin.saveBodies if body["pdir_fid"] == "dir-1")
    assert nestedSave["fid_list"] == ["file-a", "file-c"]


async def test_expired_link_refreshes_and_keeps_breakpoint(quarkOrigin, startServer, downloadFolder):
    content = b"resume-body" * 80
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    root, state = await serveBytes(startServer, {"saved-file-b": content}, failUntil=1)
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)

    await runStep(task.steps[0])

    assert (downloadFolder / "b.txt").read_bytes() == content
    downloads = [call for call in quarkOrigin.calls if call["path"].endswith("/file/download")]
    assert len(downloads) == 2
    assert state["hits"] == 2
    assert not (downloadFolder / "b.txt.ghd").exists()


async def test_repeated_forbidden_stops_with_task_error(quarkOrigin, startServer, downloadFolder):
    content = b"never" * 100
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    root, _state = await serveBytes(startServer, {"saved-file-b": content}, failUntil=99)
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)

    with pytest.raises(TaskError, match="失效"):
        await runStep(task.steps[0])

    downloads = [call for call in quarkOrigin.calls if call["path"].endswith("/file/download")]
    assert len(downloads) == 3
    assert (downloadFolder / "b.txt.ghd").exists()
    assert COOKIE not in dumped(task)


async def test_cookie_stays_out_of_task_record(quarkOrigin, startServer, downloadFolder):
    content = b"cookie-safe" * 40
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    root, _state = await serveBytes(startServer, {"saved-file-b": content})
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    assert COOKIE not in dumped(task)

    await runStep(task.steps[0])

    assert COOKIE not in dumped(task)
    restored = Task.fromDict(task.toDict())
    assert isinstance(restored, QuarkTask)
    assert restored.status == TaskStatus.COMPLETED
    assert [(item.relativePath, item.size, item.fid) for item in restored.files] == [
        ("b.txt", len(content), "file-b"),
    ]
    assert (downloadFolder / "b.txt").read_bytes() == content
    assert restored.steps[0].status == TaskStatus.COMPLETED
    assert restored.steps[0].progress == 100


async def test_pause_strips_cookie_before_record(quarkOrigin, startServer, downloadFolder):
    content = b"pause-body" * 40
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        started.set()
        await release.wait()
        return await buildRangeHandler(content)(request)

    served = await startServer(handler)
    quarkOrigin.downloadRoot = served.removesuffix("/file") + "/bytes"
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    async def waitForSpeedLimit() -> None:
        return None

    task.setStatus(TaskStatus.RUNNING)
    running = asyncio.create_task(task.run(lambda _speed: None, waitForSpeedLimit))
    await asyncio.wait_for(started.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    release.set()

    assert task.status == TaskStatus.PAUSED
    assert COOKIE not in dumped(task)
    assert "cookie" not in {key.lower() for key in task.steps[0].headers}


async def test_drive_task_never_saves_or_deletes(quarkOrigin, startServer, downloadFolder):
    content = b"drive-file" * 30
    root, _state = await serveBytes(startServer, {"fid-1": content})
    quarkOrigin.downloadRoot = root
    client = bindClient(quarkOrigin)
    task = buildDriveTask(
        [DriveEntry(fid="fid-1", name="note.txt", size=len(content), isDir=False)],
        downloadFolder,
    )
    task.client = client

    await runStep(task.steps[0])

    assert (downloadFolder / "note.txt").read_bytes() == content
    assert task.sourceKind == "drive"
    assert not any(call["path"].endswith("/share/sharepage/save") for call in quarkOrigin.calls)
    assert not any(call["path"].endswith("/file/delete") for call in quarkOrigin.calls)
    assert task.tempFolderFid == ""


async def test_save_pending_failure_marks_task_failed(
    quarkOrigin, downloadFolder, engine, monkeypatch,
):
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", 20, "tok-b")]}
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    cfg.set(quarkConfig.cookie, "")
    observed: list[bool] = []
    savePending = task.savePending

    async def spy(client):
        observed.append(task.taskId in QuarkTask.activeTempFolderNames)
        await savePending(client)

    monkeypatch.setattr(task, "savePending", spy)
    engine.taskService.add(task)
    await waitUntil(lambda: task.status == TaskStatus.FAILED)

    assert task.status == TaskStatus.FAILED
    assert task.status != TaskStatus.RUNNING
    assert task.lastError is not None
    assert "登录" in str(task.lastError)
    assert observed == [True]
    assert task.taskId not in QuarkTask.activeTempFolderNames


async def test_drive_tree_downloads_nested_files_without_save_or_delete(
    quarkOrigin, startServer, downloadFolder,
):
    topBody = b"top-root" * 30
    nestedBody = b"nested-a" * 40
    deepBody = b"deep-b" * 50
    quarkOrigin.drive = {
        "0": [
            {"fid": "dir-docs", "file_name": "docs", "size": 0, "dir": True},
            {"fid": "fid-top", "file_name": "top.txt", "size": len(topBody), "dir": False},
        ],
        "dir-docs": [
            {"fid": "dir-sub", "file_name": "sub", "size": 0, "dir": True},
            {"fid": "fid-nested", "file_name": "a.txt", "size": len(nestedBody), "dir": False},
        ],
        "dir-sub": [
            {"fid": "fid-deep", "file_name": "b.txt", "size": len(deepBody), "dir": False},
        ],
    }
    root, _state = await serveBytes(startServer, {
        "fid-top": topBody,
        "fid-nested": nestedBody,
        "fid-deep": deepBody,
    })
    quarkOrigin.downloadRoot = root
    client = bindClient(quarkOrigin)
    selected = await client.fetchDriveFiles("0")
    tree = await client.fetchDriveTree(selected)
    task = buildDriveTask(tree, downloadFolder, name="网盘选择")
    task.client = client

    assert task.sourceKind == "drive"
    assert task.tempFolderFid == ""
    assert {relativePath for _entry, relativePath in tree} == {
        "top.txt", "docs/a.txt", "docs/sub/b.txt",
    }

    await runStep(task.steps[0])

    folder = downloadFolder / task.name
    assert (folder / "docs" / "a.txt").read_bytes() == nestedBody
    assert (folder / "docs" / "sub" / "b.txt").read_bytes() == deepBody
    assert (folder / "top.txt").read_bytes() == topBody
    assert not any(call["path"].endswith("/share/sharepage/save") for call in quarkOrigin.calls)
    assert not any(call["path"].endswith("/file/delete") for call in quarkOrigin.calls)


async def test_clean_temp_can_be_turned_off(quarkOrigin, startServer, downloadFolder):
    content = b"keep-temp" * 40
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    root, _state = await serveBytes(startServer, {"saved-file-b": content})
    quarkOrigin.downloadRoot = root
    cfg.set(quarkConfig.shouldCleanTempAfterDone, False)
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)

    await runStep(task.steps[0])

    assert quarkOrigin.deleteBodies == []
    assert task.tempFolderFid == f"fid-{task.taskId}"
    assert task.status == TaskStatus.COMPLETED


async def test_revive_resaves_the_newly_selected_file(quarkOrigin, startServer, downloadFolder):
    first = b"first" * 80
    second = b"second" * 70
    quarkOrigin.shareLists = {"0": [
        shareFile("file-b", "b.txt", len(first), "tok-b"),
        shareFile("file-a", "a.txt", len(second), "tok-a"),
    ]}
    root, _state = await serveBytes(startServer, {
        "saved-file-b": first,
        "saved-file-a": second,
    })
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    task.setSelection([0])

    await runStep(task.steps[0])

    assert task.status == TaskStatus.COMPLETED
    assert quarkOrigin.deleteBodies
    assert task.tempFolderFid == ""
    folder = downloadFolder / "分享标题"
    assert (folder / "b.txt").read_bytes() == first
    assert not (folder / "a.txt").exists()

    quarkOrigin.deleteBodies.clear()
    quarkOrigin.saveBodies.clear()
    task.setSelection([0, 1])
    await runStep(task.steps[0])

    assert (folder / "a.txt").read_bytes() == second
    assert quarkOrigin.saveBodies[-1]["fid_list"] == ["file-a"]
    assert task.tempFolderFid == ""
    assert quarkOrigin.deleteBodies


async def test_run_asks_for_login_before_save(quarkOrigin, downloadFolder):
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", 20, "tok-b")]}
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)
    cfg.set(quarkConfig.cookie, "")

    with pytest.raises(TaskError, match="请先在设置中登录夸克账号"):
        await runStep(task.steps[0])

    assert not any(call["path"].endswith("/share/sharepage/save") for call in quarkOrigin.calls)


async def test_expired_stoken_is_refreshed_once(quarkOrigin, startServer, downloadFolder):
    content = b"token-refresh" * 20
    quarkOrigin.shareLists = {"0": [shareFile("file-b", "b.txt", len(content), "tok-b")]}
    quarkOrigin.failSaves = 1
    root, _state = await serveBytes(startServer, {"saved-file-b": content})
    quarkOrigin.downloadRoot = root
    task = await parseShare(quarkOrigin, "https://pan.quark.cn/s/plain001", downloadFolder)

    await runStep(task.steps[0])

    tokenCalls = [call for call in quarkOrigin.calls if call["path"].endswith("/share/sharepage/token")]
    assert len(tokenCalls) == 2
    assert len(quarkOrigin.saveBodies) == 1
    assert (downloadFolder / "b.txt").read_bytes() == content


async def test_refresh_link_uses_pc_user_agent_and_drive_query(quarkOrigin, downloadFolder):
    client = bindClient(quarkOrigin)
    task = buildDriveTask(
        [DriveEntry(fid="fid-1", name="note.txt", size=12, isDir=False)], downloadFolder,
    )
    task.client = client

    await task.steps[0].refreshLink()

    step = task.steps[0]
    assert step.userAgent == QUARK_PC_UA
    assert step.headers["cookie"] == task.client.currentCookie()
    download = next(call for call in quarkOrigin.calls if call["path"].endswith("/file/download"))
    assert download["userAgent"] == QUARK_PC_UA
    assert download["query"]["uc_param_str"] == ""
    assert COOKIE in download["cookie"]


async def test_run_without_client_creates_client_and_keeps_cookie_out_of_record(
    quarkOrigin, startServer, downloadFolder, monkeypatch,
):
    content = b"revived" * 30
    root, _state = await serveBytes(startServer, {"fid-1": content})
    quarkOrigin.downloadRoot = root
    cfg.set(quarkConfig.cookie, COOKIE)
    cfg.set(quarkConfig.subworkerCount, 1)
    monkeypatch.setattr("quark_pack.task.QuarkClient", lambda: buildQuarkClient(quarkOrigin))
    task = buildDriveTask(
        [DriveEntry(fid="fid-1", name="note.txt", size=len(content), isDir=False)],
        downloadFolder,
    )
    assert task.client is None

    await runStep(task.steps[0])

    assert isinstance(task.client, QuarkClient)
    assert (downloadFolder / "note.txt").read_bytes() == content
    assert COOKIE not in dumped(task)


async def test_qr_requests_do_not_send_saved_cookie(quarkOrigin):
    cfg.set(quarkConfig.cookie, COOKIE)
    client = QuarkClient(
        clientFactory=buildQuarkTestClient,
        readCookie=lambda: str(quarkConfig.cookie.value or ""),
        driveBase=quarkOrigin.driveBase,
        panBase=quarkOrigin.panBase,
        authBase=quarkOrigin.authBase,
    )
    token = await client.fetchQrToken()
    await client.pollQr(token)
    await client.exchangeQrTicket("fake-service-ticket")
    qrCalls = [
        call for call in quarkOrigin.calls
        if "Qrcode" in call["path"] or call["path"].endswith("/account/info")
    ]
    assert len(qrCalls) == 3
    assert all(call["cookie"] == "" for call in qrCalls)
    assert COOKIE not in json.dumps(qrCalls)
