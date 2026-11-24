from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from app.config.paths import APP_DATA_DIR
from app.models.task import TaskError
from telegram_pack import runtime as runtimeModule
from telegram_pack.account import TelegramAccount
from telegram_pack.config import telegramConfig
from telegram_pack.runtime import (
    DOWNLOAD_TEMPLATE,
    TDL_REPO,
    TdlDownloadSpec,
    TdlRuntime,
    TdlRunner,
    matchAsset,
    readStreamLines,
    stripAnsi,
    toMaskedProxy,
)

DEFAULT_CHATS = [
    {"id": 111, "type": "channel", "visible_name": "私密频道", "username": "mychan"},
    {"ID": "222", "Type": "group", "VisibleName": "群组", "Username": ""},
]


class FakeProcess:
    def __init__(self, lines=(), *, isHanging=False, stopsOnTerminate=True, returncode=0):
        self.stdout = asyncio.StreamReader()
        self._isEofFed = False
        for line in lines:
            self.stdout.feed_data((line + "\n").encode("utf-8"))
        if not isHanging:
            self.stdout.feed_eof()
            self._isEofFed = True
        self.returncode = None if isHanging else returncode
        self.terminated = False
        self.killed = False
        self.stopsOnTerminate = stopsOnTerminate
        self.exitCode = returncode
        self.exitEvent = asyncio.Event()
        self.onExit = None
        self.isExitNotified = False

    def finish(self, code):
        self.returncode = code
        if not self._isEofFed:
            self._isEofFed = True
            self.stdout.feed_eof()
        self.exitEvent.set()

    def terminate(self):
        self.terminated = True
        if self.stopsOnTerminate:
            self.finish(self.exitCode)

    def kill(self):
        self.killed = True
        self.finish(self.exitCode)

    async def wait(self):
        if self.returncode is None:
            await self.exitEvent.wait()
        if not self.isExitNotified:
            self.isExitNotified = True
            if self.onExit is not None:
                self.onExit()
        return self.returncode

    async def communicate(self):
        data = await self.stdout.read()
        if self.returncode is None:
            await self.exitEvent.wait()
        return data, b""


class FakeExec:
    def __init__(self, respond=None):
        self.respond = respond if respond is not None else (lambda argv: FakeProcess(lines=["ok"]))
        self.calls = []
        self.inFlight = 0
        self.peakInFlight = 0
        self.started = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        argv = list(args)
        self.calls.append(argv)
        process = self.respond(argv)
        process.onExit = self.release
        self.inFlight += 1
        self.peakInFlight = max(self.peakInFlight, self.inFlight)
        self.started.set()
        return process

    def release(self):
        self.inFlight -= 1


class FakeTdlRuntime:
    def __init__(self, binary):
        self.binary = binary

    def path(self):
        return self.binary


def buildRunner(tmpPath: Path, exec: FakeExec, *, namespace="test", proxyOverride="",
                readGlobalProxy=None):
    return TdlRunner(
        runtime=FakeTdlRuntime(str(tmpPath / "tdl")),
        subprocessExec=exec,
        readGlobalProxy=readGlobalProxy if readGlobalProxy is not None else (lambda: None),
        storageDir=tmpPath / "data",
        namespace=namespace,
        proxyOverride=proxyOverride,
    )


def buildResponder(chatPayload=None, exportPayload=None):
    def respond(argv):
        if "export" in argv:
            target = Path(argv[argv.index("-o") + 1])
            payload = exportPayload if exportPayload is not None else {"id": "42", "messages": []}
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return FakeProcess(lines=[])
        if "ls" in argv:
            payload = chatPayload if chatPayload is not None else DEFAULT_CHATS
            return FakeProcess(lines=[json.dumps(payload, ensure_ascii=False)])
        return FakeProcess(lines=["ok"])

    return respond


def valueAfter(argv, flag):
    return argv[argv.index(flag) + 1]


def test_config_defaults(config):
    assert telegramConfig.installFolder.group == "Telegram"
    assert telegramConfig.namespace.value == "default"
    assert telegramConfig.threads.value == 4
    assert telegramConfig.poolSize.value == 8
    assert telegramConfig.useTakeout.value is False


def test_runner_storage_dir_defaults_to_app_data_tdl_data(config):
    assert TdlRunner().storageDir == Path(APP_DATA_DIR) / "TDL" / "data"
    assert TdlRunner(storageDir=Path("override") / "data").storageDir == Path("override") / "data"


def test_match_asset_picks_platform_and_arch():
    names = [
        "tdl_Linux_amd64.tar.gz",
        "tdl_macOS_arm64.tar.gz",
        "tdl_macOS_64bit.tar.gz",
        "tdl_Windows_arm64.zip",
        "tdl_Windows_64bit.zip",
    ]
    assert matchAsset(names, "win32", "x86_64") == "tdl_Windows_64bit.zip"
    assert matchAsset(names, "win32", "arm64") == "tdl_Windows_arm64.zip"
    assert matchAsset(names, "darwin", "arm64") == "tdl_macOS_arm64.tar.gz"
    assert matchAsset(names, "darwin", "x86_64") == "tdl_macOS_64bit.tar.gz"
    assert matchAsset(names, "linux", "x86_64") == "tdl_Linux_amd64.tar.gz"
    assert matchAsset(["tdl_Windows_64bit.zip"], "linux", "x86_64") == ""


def test_to_masked_proxy_hides_password():
    assert toMaskedProxy("socks5://user:{{PASSWORD_ywqxptln}}@127.0.0.1:7890") == (
        "socks5://user:***@127.0.0.1:7890"
    )
    assert toMaskedProxy("http://127.0.0.1:1080") == "http://127.0.0.1:1080"


async def test_serialize_concurrent_calls_to_single_process(tmp_path):
    exec = FakeExec(buildResponder())
    runner = buildRunner(tmp_path, exec)

    downloadResult, chats, payload = await asyncio.gather(
        runner.download(TdlDownloadSpec(outputFolder=tmp_path / "out", exportFile=tmp_path / "task.json")),
        runner.listChats(),
        runner.exportMessages("42"),
    )

    assert exec.peakInFlight == 1
    assert exec.inFlight == 0
    assert downloadResult is None
    assert [chat.chatId for chat in chats] == ["111", "222"]
    assert payload == {"id": "42", "messages": []}
    assert len(exec.calls) == 3


async def test_cancel_download_terminates_process_and_releases_lock(tmp_path):
    hanging = FakeProcess(lines=["0.0% 0.0 MB/s"], isHanging=True)
    exec = FakeExec(lambda argv: hanging)
    runner = buildRunner(tmp_path, exec)
    runner.stopTimeout = 0.5

    task = asyncio.create_task(
        runner.download(TdlDownloadSpec(outputFolder=tmp_path / "out", url="https://t.me/chan/12"))
    )
    await exec.started.wait()
    assert runner.isBusy is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging.terminated is True
    assert hanging.killed is False
    assert runner.isBusy is False


async def test_cancel_download_kills_process_when_terminate_ignored(tmp_path):
    hanging = FakeProcess(lines=[], isHanging=True, stopsOnTerminate=False)
    exec = FakeExec(lambda argv: hanging)
    runner = buildRunner(tmp_path, exec)
    runner.stopTimeout = 0.01

    task = asyncio.create_task(
        runner.download(TdlDownloadSpec(outputFolder=tmp_path / "out", url="https://t.me/chan/12"))
    )
    await exec.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging.terminated is True
    assert hanging.killed is True
    assert runner.isBusy is False

    exec.respond = lambda argv: FakeProcess(lines=["[]"])
    assert await runner.probeAuth() is True


async def test_build_base_args_injects_namespace_storage_and_normalized_proxy(tmp_path):
    exec = FakeExec()
    runner = buildRunner(
        tmp_path, exec, namespace="alice",
        readGlobalProxy=lambda: "socks5h://127.0.0.1:7890",
    )

    await runner.probeAuth()

    argv = exec.calls[0]
    assert argv[:5] == [
        str(tmp_path / "tdl"), "-n", "alice", "--storage", f"type=bolt,path={tmp_path / 'data'}",
    ]
    assert valueAfter(argv, "--proxy") == "socks5://127.0.0.1:7890"


async def test_proxy_override_wins_over_global_proxy(tmp_path):
    exec = FakeExec()
    runner = buildRunner(
        tmp_path, exec, proxyOverride="http://127.0.0.1:1080",
        readGlobalProxy=lambda: "socks5h://127.0.0.1:7890",
    )

    await runner.probeAuth()

    assert valueAfter(exec.calls[0], "--proxy") == "http://127.0.0.1:1080"


async def test_namespace_and_proxy_override_read_lazily(tmp_path, config):
    exec = FakeExec()
    telegramConfig.namespace.value = "first"
    telegramConfig.proxyOverride.value = ""
    runner = buildRunner(
        tmp_path, exec, namespace=None, proxyOverride=None,
        readGlobalProxy=lambda: None,
    )

    telegramConfig.namespace.value = "second"
    telegramConfig.proxyOverride.value = "socks5h://127.0.0.1:7890"
    await runner.probeAuth()

    argv = exec.calls[0]
    assert valueAfter(argv, "-n") == "second"
    assert valueAfter(argv, "--proxy") == "socks5://127.0.0.1:7890"


async def test_logging_masks_passcode_and_proxy_password(tmp_path):
    exec = FakeExec()
    runner = buildRunner(
        tmp_path, exec,
        readGlobalProxy=lambda: "socks5://user:{{PASSWORD_wpmhjcgb}}@127.0.0.1:7890",
    )
    messages = []
    sinkId = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        await runner.loginDesktop(desktopPath="tdata", passcode="{{PASSWORD_stpgxjvi}}")
    finally:
        logger.remove(sinkId)

    logs = "\n".join(messages)
    assert "运行 tdl" in logs
    assert "{{PASSWORD_stpgxjvi}}" not in logs
    assert "{{PASSWORD_wpmhjcgb}}" not in logs
    assert "user:***@127.0.0.1:7890" in logs
    assert valueAfter(exec.calls[0], "-p") == "{{PASSWORD_stpgxjvi}}"


async def test_probe_auth_detects_unauthorized_output(tmp_path):
    exec = FakeExec(lambda argv: FakeProcess(lines=["Error: not authorized"], returncode=1))
    runner = buildRunner(tmp_path, exec)
    assert await runner.probeAuth() is False

    exec.respond = lambda argv: FakeProcess(lines=["Unauthorized, please login"])
    assert await runner.probeAuth() is False

    exec.respond = lambda argv: FakeProcess(lines=["ok"])
    assert await runner.probeAuth() is False


async def test_probe_auth_accepts_json_array_with_login_in_channel_name(tmp_path):
    chats = [{"id": 1, "type": "channel", "visible_name": "Telegram Login Alerts", "username": "login_bot"}]
    exec = FakeExec(lambda argv: FakeProcess(lines=[json.dumps(chats)]))
    runner = buildRunner(tmp_path, exec)

    assert await runner.probeAuth() is True


async def test_probe_auth_raises_when_binary_missing(tmp_path):
    runner = TdlRunner(
        runtime=FakeTdlRuntime(""),
        subprocessExec=FakeExec(),
        readGlobalProxy=lambda: None,
        storageDir=tmp_path / "data",
        namespace="test",
        proxyOverride="",
    )
    with pytest.raises(TaskError):
        await runner.probeAuth()


async def test_list_chats_parses_case_variants(tmp_path):
    runner = buildRunner(tmp_path, FakeExec(buildResponder()))

    chats = await runner.listChats()

    assert [chat.chatId for chat in chats] == ["111", "222"]
    assert chats[0].type == "channel"
    assert chats[0].title == "私密频道"
    assert chats[0].username == "mychan"
    assert chats[1].type == "group"
    assert chats[1].title == "群组"
    assert chats[1].username == ""


async def test_start_qr_login_captures_token(tmp_path):
    exec = FakeExec(lambda argv: FakeProcess(lines=[
        "Please scan the QR code", "tg://login?token=abc-123_XYZ", "████████",
    ]))
    runner = buildRunner(tmp_path, exec)
    seen = []

    await runner.startQrLogin(seen.append)

    assert "tg://login?token=abc-123_XYZ" in seen
    assert exec.calls[0][-3:] == ["login", "-T", "qr"]


async def test_export_messages_builds_last_since_and_range_modes(tmp_path):
    payload = {"id": "42", "messages": [{"messageId": 7}]}
    exec = FakeExec(buildResponder(exportPayload=payload))
    runner = buildRunner(tmp_path, exec)

    assert await runner.exportMessages("42", limit=30) == payload
    assert await runner.exportMessages("42", sinceMsgId=100) == payload
    assert await runner.exportMessages("42", idRange=(10, 20)) == payload

    lastCall, sinceCall, rangeCall = exec.calls
    assert valueAfter(lastCall, "-i") == "30"
    assert valueAfter(sinceCall, "-i") == "101,2147483647"
    assert valueAfter(rangeCall, "-i") == "10,20"
    for call in (lastCall, sinceCall, rangeCall):
        assert valueAfter(call, "-c") == "42"
        assert "--with-content" in call and "--raw" in call
        assert not Path(valueAfter(call, "-o")).exists()


async def test_export_messages_keeps_explicit_output_path(tmp_path):
    exec = FakeExec(buildResponder(exportPayload={"id": "42", "messages": []}))
    runner = buildRunner(tmp_path, exec)
    target = tmp_path / "export.json"

    await runner.exportMessages("42", outputPath=target)

    assert target.exists()
    assert valueAfter(exec.calls[0], "-o") == str(target)


async def test_download_builds_export_args_and_reports_progress(tmp_path):
    exec = FakeExec(lambda argv: FakeProcess(lines=[
        "Downloading 12.5% 3.2 MB/s", "Downloading 100.0% 1.5 MB/s",
    ]))
    runner = buildRunner(tmp_path, exec)
    progresses = []
    spec = TdlDownloadSpec(
        outputFolder=tmp_path / "out", exportFile=tmp_path / "task.json",
        threads=6, poolSize=3, useTakeout=True,
    )

    await runner.download(spec, progresses.append)

    assert [progress.percent for progress in progresses] == [12.5, 100.0]
    assert progresses[0].speed == "3.2 MB/s"
    argv = exec.calls[0]
    assert valueAfter(argv, "-f") == str(tmp_path / "task.json")
    assert valueAfter(argv, "-d") == str(tmp_path / "out")
    assert valueAfter(argv, "-t") == "6"
    assert valueAfter(argv, "--pool") == "3"
    assert "--continue" in argv
    assert "--disable-progress-ps" in argv
    assert valueAfter(argv, "--template") == DOWNLOAD_TEMPLATE
    assert "--takeout" in argv
    assert "-u" not in argv


async def test_download_omits_template_when_empty(tmp_path):
    exec = FakeExec()
    runner = buildRunner(tmp_path, exec)

    await runner.download(TdlDownloadSpec(
        outputFolder=tmp_path / "out",
        url="https://t.me/chan/12",
        template="",
    ))

    argv = exec.calls[0]
    assert "--disable-progress-ps" in argv
    assert "--template" not in argv


async def test_read_stream_lines_strips_ansi_and_splits_carriage_return():
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"\x1b[32mDownloading 12.5% 2.0 MB/s\x1b[0m\r"
        b"\x1b[2KDownloading 88.0% 5.1 MB/s\r\n"
        b"   \x1b[0m\n"
        b"Done"
    )
    reader.feed_eof()
    seen: list[str] = []

    lines = await readStreamLines(reader, seen.append)

    assert stripAnsi("\x1b[31merr\x1b[0m") == "err"
    assert lines == ["Downloading 12.5% 2.0 MB/s", "Downloading 88.0% 5.1 MB/s", "Done"]
    assert seen == lines


async def test_run_command_creates_storage_dir(tmp_path):
    storageDir = tmp_path / "nested" / "data"
    assert not storageDir.exists()
    exec = FakeExec(lambda argv: FakeProcess(lines=["[]"]))
    runner = TdlRunner(
        runtime=FakeTdlRuntime(str(tmp_path / "tdl")),
        subprocessExec=exec,
        readGlobalProxy=lambda: None,
        storageDir=storageDir,
        namespace="test",
        proxyOverride="",
    )

    assert await runner.probeAuth() is True
    assert storageDir.is_dir()


async def test_login_interactive_modes_masking_and_errors(tmp_path):
    exec = FakeExec(lambda argv: FakeProcess(lines=[]))
    runner = buildRunner(tmp_path, exec)

    await runner.loginInteractive("qr", desktopPath="ignored", passcode="ignored")
    assert exec.calls[0][-3:] == ["login", "-T", "qr"]
    assert "-d" not in exec.calls[0]
    assert "-p" not in exec.calls[0]

    messages: list[str] = []
    sinkId = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        await runner.loginInteractive("desktop", desktopPath="tdata", passcode="secret123")
    finally:
        logger.remove(sinkId)
    assert valueAfter(exec.calls[1], "-T") == "desktop"
    assert valueAfter(exec.calls[1], "-d") == "tdata"
    assert valueAfter(exec.calls[1], "-p") == "secret123"
    assert "secret123" not in "\n".join(messages)

    await runner.loginInteractive("code")
    assert exec.calls[2][-3:] == ["login", "-T", "code"]

    with pytest.raises(TaskError):
        await runner.loginInteractive("unknown")

    exec.respond = lambda argv: FakeProcess(lines=[], returncode=2)
    with pytest.raises(TaskError) as errorInfo:
        await runner.loginInteractive("qr")
    assert "2" in str(errorInfo.value)


async def test_login_interactive_cancel_stops_process(tmp_path):
    hanging = FakeProcess(lines=[], isHanging=True)
    exec = FakeExec(lambda argv: hanging)
    runner = buildRunner(tmp_path, exec)

    task = asyncio.create_task(runner.loginInteractive("qr"))
    await exec.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging.terminated is True
    assert runner.isBusy is False


async def test_logout_deletes_only_current_namespace(tmp_path):
    runner = buildRunner(tmp_path, FakeExec(), namespace="alice")
    runner.storageDir.mkdir(parents=True, exist_ok=True)
    aliceFile = runner.storageDir / "alice"
    bobFile = runner.storageDir / "bob"
    aliceFile.write_bytes(b"bolt")
    bobFile.write_bytes(b"bolt")

    assert await runner.logout() is True
    assert not aliceFile.exists()
    assert bobFile.exists()
    assert await runner.logout() is False


class InlineCoroutineRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._seq = 0

    def submit(self, work, done=None, failed=None, *args, owner=None, **kwargs) -> str:
        self._seq += 1
        workId = f"w{self._seq}"

        async def run():
            try:
                result = await work
            except asyncio.CancelledError:
                return
            except Exception as error:
                if failed is not None:
                    failed(error, *args, **kwargs)
                return
            finally:
                self._tasks.pop(workId, None)
            if done is not None:
                done(result, *args, **kwargs)

        self._tasks[workId] = asyncio.create_task(run())
        return workId

    def cancel(self, workId: str, finished=None) -> bool:
        task = self._tasks.pop(workId, None)
        if task is not None:
            task.cancel()
            return True
        return False

    def post(self, callback, *args, **kwargs) -> None:
        callback(*args, **kwargs)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


async def test_account_status_refresh_login_cancel_and_logout(tmp_path):
    coRunner = InlineCoroutineRunner()
    uninstalled = TelegramAccount(coRunner, TdlRunner(runtime=FakeTdlRuntime("")))
    assert uninstalled.isInstalled is False
    assert uninstalled.statusText == "未安装 tdl，请先点击上方一键安装"
    uninstalled.startLogin("qr")
    assert uninstalled.lastError != ""

    authState = {"loggedIn": False}
    loginGate = asyncio.Event()
    logoutCalled = []

    class FakeAccountRunner:
        runtime = FakeTdlRuntime("/fake/tdl")

        def resolveProxy(self):
            return "socks5://user:topsecret@127.0.0.1:7890"

        async def probeAuth(self) -> bool:
            await asyncio.sleep(0)
            return authState["loggedIn"]

        async def loginInteractive(self, mode="qr", *, desktopPath="", passcode="") -> None:
            await loginGate.wait()
            if passcode == "badpass":
                raise TaskError("密码 badpass 与代理 socks5://user:topsecret@127.0.0.1:7890 错误")
            authState["loggedIn"] = True

        async def loginQrSession(self, *, onQrMatrix, onPasswordRequired, readPassword, onStatus=None) -> None:
            await loginGate.wait()
            authState["loggedIn"] = True

        async def logout(self) -> bool:
            logoutCalled.append(True)
            authState["loggedIn"] = False
            return True

    account = TelegramAccount(coRunner, FakeAccountRunner())
    events: list[str] = []
    account.stateChanged.connect(lambda: events.append(account.statusText))

    assert account.statusText == "未登录"
    authState["loggedIn"] = True
    account.refreshAuth()
    assert account.isBusy is True
    assert account.statusText == "正在检测登录状态..."
    await coRunner.drain()
    assert account.isBusy is False
    assert account.isLoggedIn is True
    assert account.statusText == "已登录"

    # 登出应立即置未登录并清理本地会话
    account.logout()
    await coRunner.drain()
    assert logoutCalled == [True]
    assert account.isLoggedIn is False
    assert account.statusText == "未登录"

    # 登录中取消应立刻恢复空闲且屏蔽旧回调
    account.startLogin("qr")
    assert account.isLoggingIn is True
    assert "扫码弹窗" in account.statusText
    account.cancelLogin()
    assert account.isLoggingIn is False
    assert account.isBusy is False
    loginGate.set()
    await coRunner.drain()
    assert account.isLoggedIn is False

    # 登录失败时脱敏密码与代理密码
    account.startLogin("desktop", desktopPath="tdata", passcode="badpass")
    await coRunner.drain()
    assert account.isLoggingIn is False
    assert "badpass" not in account.lastError
    assert "topsecret" not in account.lastError
    assert "****" in account.lastError

    # 正常登录完成后自动复检为已登录
    account.startLogin("qr")
    await coRunner.drain()
    assert account.isLoggedIn is True
    assert account.lastError == ""
    assert events


async def test_download_url_mode_passes_group(tmp_path):
    exec = FakeExec()
    runner = buildRunner(tmp_path, exec)

    await runner.download(TdlDownloadSpec(outputFolder=tmp_path / "out", url="https://t.me/chan/12"))

    argv = exec.calls[0]
    assert valueAfter(argv, "-u") == "https://t.me/chan/12"
    assert "--group" in argv
    assert "-f" not in argv


async def test_probe_version_falls_back_to_version_flag(tmp_path, monkeypatch):
    runtime = TdlRuntime()
    monkeypatch.setattr(runtime, "path", lambda: str(tmp_path / "tdl"))
    calls = []

    async def fakeExec(*args, **kwargs):
        calls.append(args)
        if args[1] == "version":
            return FakeProcess(lines=["unknown command"], returncode=1)
        return FakeProcess(lines=["tdl version v1.2.3"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fakeExec)

    info = await runtime.probeVersion()

    assert info.version == "v1.2.3"
    assert [call[1] for call in calls] == ["version", "--version"]


async def test_install_matches_windows_asset_and_unpacks(tmp_path, config, monkeypatch):
    runtime = TdlRuntime()
    telegramConfig.installFolder.value = str(tmp_path)
    release = SimpleNamespace(version="v1.2.3", assets=[
        SimpleNamespace(name="tdl_Windows_arm64.zip"),
        SimpleNamespace(name="tdl_Windows_64bit.zip"),
    ])
    calls = {}

    async def fakeFetchLatestRelease(repo):
        calls["repo"] = repo
        return release

    async def fakeFetchReleaseAsset(repo, tag, assetName, outputPath, onProgress):
        calls["asset"] = (repo, tag, assetName, outputPath)

    async def fakeInstallArchive(archive, folder):
        calls["install"] = (archive, folder)

    monkeypatch.setattr(runtimeModule, "fetchLatestRelease", fakeFetchLatestRelease)
    monkeypatch.setattr(runtimeModule, "fetchReleaseAsset", fakeFetchReleaseAsset)
    monkeypatch.setattr(runtimeModule, "installArchive", fakeInstallArchive)
    monkeypatch.setattr(runtimeModule, "PLATFORM", "win32")
    monkeypatch.setattr(runtimeModule, "MACHINE_ARCH", "x86_64")

    await runtime.install("ignored", lambda progress: None)

    assert calls["repo"] == TDL_REPO
    assert calls["asset"] == (TDL_REPO, "v1.2.3", "tdl_Windows_64bit.zip", tmp_path / "tdl_Windows_64bit.zip")
    assert calls["install"] == (tmp_path / "tdl_Windows_64bit.zip", tmp_path)


async def test_hidden_subprocess_compatibility_and_console_login(tmp_path, monkeypatch):
    import subprocess as sp

    seenKwargs = []

    async def strictHiddenExec(*args, **kwargs):
        assert "creationflags" not in kwargs
        seenKwargs.append(kwargs)
        return FakeProcess(lines=["[]"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", strictHiddenExec)
    runner = TdlRunner(
        runtime=FakeTdlRuntime(str(tmp_path / "tdl")),
        readGlobalProxy=lambda: None,
        storageDir=tmp_path / "data",
        namespace="test",
        proxyOverride="",
    )
    assert await runner.probeAuth() is True
    assert seenKwargs

    popenCalls = []

    class FakePopen:
        returncode = 0

        def wait(self):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def fakePopen(args, **kwargs):
        popenCalls.append((args, kwargs))
        return FakePopen()

    monkeypatch.setattr(runtimeModule.sys, "platform", "win32")
    monkeypatch.setattr(sp, "Popen", fakePopen)
    await runner.loginInteractive("qr")

    assert len(popenCalls) == 1
    assert popenCalls[0][0][-3:] == ["login", "-T", "qr"]
    assert popenCalls[0][1]["creationflags"] == sp.CREATE_NEW_CONSOLE


def buildSmallQrBlock(size: int = 21) -> str:
    # 构造带白边与棋盘格内部模块的有效正方形矩阵并编码为 go-qrcode ToSmallString(false) 格式
    pixels = []
    for r in range(size):
        row = []
        for c in range(size):
            if r < 2 or r >= size - 2 or c < 2 or c >= size - 2:
                row.append(False)
            else:
                row.append((r + c) % 2 == 0)
        pixels.append(row)
    encode = {
        (True, True): " ",
        (False, True): "\u2580",
        (True, False): "\u2584",
        (False, False): "\u2588",
    }
    lines = []
    for r in range(0, size, 2):
        chars = []
        for c in range(size):
            top = pixels[r][c]
            bottom = pixels[r + 1][c] if r + 1 < size else True
            chars.append(encode[(top, bottom)])
        lines.append("".join(chars))
    return "\r\n".join(lines) + "\r\n"


def test_terminal_screen_extracts_qr_matrix_across_redraw():
    from telegram_pack.conpty import TerminalScreen, extractQrMatrix

    screen = TerminalScreen(cols=80, rows=40)
    block = buildSmallQrBlock(21)
    screen.feed("\x1b[2J\x1b[H\x1b]0;tdl.exe\x07\x1b[34mScan QR code...\r\n\x1b[m" + block)
    matrix = extractQrMatrix(screen.lines())
    assert matrix is not None
    assert len(matrix) == 21
    assert len(matrix[0]) == 21
    assert all(not cell for cell in matrix[0])
    assert all(not cell for cell in matrix[-1])
    assert matrix[2][2] is True
    assert matrix[2][3] is False


async def test_login_qr_session_supports_2fa_and_maps_errors(tmp_path):
    block = buildSmallQrBlock(21)

    class FakePty:
        def __init__(self, *, wrongPassword: bool = False) -> None:
            self.wrongPassword = wrongPassword
            self.chunks = [
                f"\x1b[2J\x1b[HScan QR code...\r\n{block}".encode("utf-8"),
                b"\x1b[2J\x1b[H? Enter 2FA Password: ",
            ]
            self.written: list[str] = []
            self.returncode = None
            self.closed = False

        def readAvailable(self, _maxBytes: int = 4096) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

        async def writeInput(self, text: str) -> None:
            self.written.append(text)
            if self.wrongPassword:
                self.chunks.append(b"\r\nError: 2fa auth: PASSWORD_HASH_INVALID\r\n")
                self.returncode = 1
            else:
                self.chunks.append(b"\r\nLogin successfully! ID: 123, Username: demo\r\n")
                self.returncode = 0

        async def wait(self) -> int:
            return 0 if self.returncode is None else self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def close(self) -> None:
            self.closed = True

    pty = FakePty(wrongPassword=False)
    runner = TdlRunner(
        runtime=FakeTdlRuntime(str(tmp_path / "tdl")),
        readGlobalProxy=lambda: None,
        storageDir=tmp_path / "data",
        namespace="test",
        proxyOverride="",
        ptyFactory=lambda _args: pty,
    )
    matrices = []
    prompts = []
    statuses = []

    async def providePassword() -> str:
        return "my-2fa-secret"

    await runner.loginQrSession(
        onQrMatrix=matrices.append,
        onPasswordRequired=lambda: prompts.append(True),
        readPassword=providePassword,
        onStatus=statuses.append,
        pollInterval=0.005,
    )

    assert len(matrices) == 1
    assert len(matrices[0]) == 21
    assert prompts == [True]
    assert pty.written == ["my-2fa-secret\r"]
    assert pty.closed is True

    badPty = FakePty(wrongPassword=True)
    badRunner = TdlRunner(
        runtime=FakeTdlRuntime(str(tmp_path / "tdl")),
        readGlobalProxy=lambda: None,
        storageDir=tmp_path / "data",
        namespace="test",
        proxyOverride="",
        ptyFactory=lambda _args: badPty,
    )
    with pytest.raises(TaskError) as errorInfo:
        await badRunner.loginQrSession(
            onQrMatrix=lambda _m: None,
            onPasswordRequired=lambda: None,
            readPassword=providePassword,
            pollInterval=0.005,
        )
    assert "2FA" in str(errorInfo.value)
    assert badPty.closed is True
