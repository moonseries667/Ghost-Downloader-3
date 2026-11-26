from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.task import TaskError
from baidu_pack import runtime as baiduRuntimeModule
from baidu_pack.runtime import (
    BaiduPcsRunner,
    BaiduPcsRuntime,
    findArchiveRoot,
    matchAsset,
    parseDownloadProgress,
    parseLsOutput,
    parseQuotaOutput,
    parseWhoOutput,
    toSafeArgs,
    toSafeOutput,
)

RELEASE_ASSETS = [
    "BaiduPCS-Go-v4.0.2-darwin-osx-amd64.zip",
    "BaiduPCS-Go-v4.0.2-darwin-osx-arm64.zip",
    "BaiduPCS-Go-v4.0.2-linux-386.zip",
    "BaiduPCS-Go-v4.0.2-linux-amd64.zip",
    "BaiduPCS-Go-v4.0.2-linux-arm.zip",
    "BaiduPCS-Go-v4.0.2-linux-arm64.zip",
    "BaiduPCS-Go-v4.0.2-linux-mips64.zip",
    "BaiduPCS-Go-v4.0.2-windows-arm64.zip",
    "BaiduPCS-Go-v4.0.2-windows-x64.zip",
    "BaiduPCS-Go-v4.0.2-windows-x86.zip",
]


class FakeStream:
    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self._chunks = list(chunks)
        self._delay = delay

    async def read(self, _n: int = 1024) -> bytes:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProcess:
    def __init__(
        self,
        output: str = "",
        returncode: int = 0,
        *,
        chunks: list[bytes] | None = None,
        delay: float = 0.0,
        hang: bool = False,
    ) -> None:
        self._hang = hang
        self._finalCode = returncode
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()
        if hang:
            self.stdout = self._HangingStream(self._done)
        else:
            rawChunks = chunks if chunks is not None else ([output.encode("utf-8")] if output else [])
            self.stdout = FakeStream(rawChunks, delay=delay)

    class _HangingStream:
        def __init__(self, done: asyncio.Event) -> None:
            self._done = done

        async def read(self, _n: int = 1024) -> bytes:
            await self._done.wait()
            return b""

    async def wait(self) -> int:
        if self._hang:
            await self._done.wait()
        if self.returncode is None:
            self.returncode = self._finalCode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()


def test_match_asset_covers_all_platforms_and_arches():
    assert matchAsset(RELEASE_ASSETS, "win32", "amd64") == "BaiduPCS-Go-v4.0.2-windows-x64.zip"
    assert matchAsset(RELEASE_ASSETS, "win32", "arm64") == "BaiduPCS-Go-v4.0.2-windows-arm64.zip"
    assert matchAsset(RELEASE_ASSETS, "darwin", "amd64") == "BaiduPCS-Go-v4.0.2-darwin-osx-amd64.zip"
    assert matchAsset(RELEASE_ASSETS, "darwin", "arm64") == "BaiduPCS-Go-v4.0.2-darwin-osx-arm64.zip"
    assert matchAsset(RELEASE_ASSETS, "linux", "amd64") == "BaiduPCS-Go-v4.0.2-linux-amd64.zip"
    assert matchAsset(RELEASE_ASSETS, "linux", "arm64") == "BaiduPCS-Go-v4.0.2-linux-arm64.zip"
    assert matchAsset(RELEASE_ASSETS, "freebsd", "amd64") == ""


def test_output_parsers_and_secret_redaction():
    assert parseWhoOutput("当前帐号 uid: 123456, 用户名: alice, 性别: 未知, 年龄: 0.0") == ("123456", "alice")
    assert parseWhoOutput("当前帐号 uid: 0, 用户名: , 性别: 未知, 年龄: 0.0") == ("", "")

    assert parseQuotaOutput("用户名: alice, 总空间: 2.00TB, 已用空间: 120.50GB, 比率: 5.88%") == (
        "alice", "2.00TB", "120.50GB", "5.88%",
    )

    lsOut = (
        "\n当前目录: /资料\n----\n"
        "#  fs_id  app_id  文件大小  创建日期  修改日期  md5(截图请打码)  文件(目录)\n"
        "0  9001  250528  -  2026-01-01 10:00:00  2026-01-02 11:00:00  -  子目录/\n"
        "1  9002  250528  10.50MB  2026-01-01 10:00:00  2026-01-02 12:00:00  abc123  demo file.zip\n"
        "总: 10.50MB 文件总数: 1, 目录总数: 1\n----\n"
    )
    entries = parseLsOutput(lsOut, "/资料")
    assert len(entries) == 2
    assert entries[0].isDir is True
    assert entries[0].path == "/资料/子目录"
    assert entries[1].isDir is False
    assert entries[1].fsId == "9002"
    assert entries[1].name == "demo file.zip"
    assert entries[1].path == "/资料/demo file.zip"
    assert entries[1].size > 10 * 1024 * 1024

    progress = parseDownloadProgress("[1] ↓ 25.00MB/100.00MB 5.00MB/s in 5s, left 15s ...")
    assert progress is not None
    assert progress.percent == pytest.approx(25.0, rel=0.02)
    assert progress.speedBytes > 4 * 1024 * 1024

    rawArgs = [
        "BaiduPCS-Go", "login", "-cookies=BDUSS=SUPER_SECRET_1; STOKEN=SUPER_SECRET_2",
        "-bduss", "BDUSS_VAL", "-stoken=STOKEN_VAL", "-proxy=http://user:pass@127.0.0.1:7890",
    ]
    safeArgs = " ".join(toSafeArgs(rawArgs))
    for secret in ("SUPER_SECRET_1", "SUPER_SECRET_2", "BDUSS_VAL", "STOKEN_VAL", "pass"):
        assert secret not in safeArgs

    safeOut = toSafeOutput(
        "失败: BDUSS=SUPER_SECRET_1 STOKEN=SUPER_SECRET_2 http://user:pass@127.0.0.1:7890",
        rawArgs,
    )
    for secret in ("SUPER_SECRET_1", "SUPER_SECRET_2", "pass"):
        assert secret not in safeOut


async def test_runtime_install_strips_nested_zip_root(tmp_path, monkeypatch):
    installDir = tmp_path / "BaiduPCS"
    archiveBytesPath = tmp_path / "source.zip"
    with zipfile.ZipFile(archiveBytesPath, "w") as zf:
        zf.writestr("BaiduPCS-Go-v4.0.2-linux-amd64/README.md", "readme")
        zf.writestr("BaiduPCS-Go-v4.0.2-linux-amd64/BaiduPCS-Go", "#!/bin/sh\necho BaiduPCS-Go version v4.0.2\n")

    assert findArchiveRoot(archiveBytesPath, "BaiduPCS-Go") == "BaiduPCS-Go-v4.0.2-linux-amd64/"

    async def fakeLatest(_repo):
        return SimpleNamespace(
            version="v4.0.2",
            assets=[SimpleNamespace(name="BaiduPCS-Go-v4.0.2-linux-amd64.zip")],
        )

    async def fakeDownload(_repo, _version, _assetName, target: Path, onProgress):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archiveBytesPath.read_bytes())
        onProgress(100.0)

    monkeypatch.setattr(baiduRuntimeModule, "PLATFORM", "linux")
    monkeypatch.setattr(baiduRuntimeModule, "MACHINE_ARCH", "amd64")
    monkeypatch.setattr(baiduRuntimeModule, "fetchLatestRelease", fakeLatest)
    monkeypatch.setattr(baiduRuntimeModule, "fetchReleaseAsset", fakeDownload)

    runtime = BaiduPcsRuntime()
    monkeypatch.setattr(runtime, "installFolder", lambda: installDir)
    progresses: list[float] = []
    await runtime.install("v4.0.2", progresses.append)

    binary = installDir / "BaiduPCS-Go"
    assert binary.is_file()
    assert progresses == [100.0]


async def test_runner_env_proxy_lock_transfer_and_cancellation(tmp_path):
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    configOverlap = False
    inConfig = False

    async def fakeExec(*args, **kwargs):
        nonlocal configOverlap, inConfig
        env = kwargs.get("env", {})
        calls.append((args, env))
        cmd = args[1]

        if cmd in ("login", "config", "cd", "transfer"):
            if inConfig:
                configOverlap = True
            inConfig = True
            await asyncio.sleep(0.02)
            inConfig = False

        if cmd == "login":
            return FakeProcess("百度帐号登录成功: alice\n")
        if cmd == "who":
            return FakeProcess("当前帐号 uid: 888, 用户名: alice, 性别: 未知, 年龄: 0.0\n")
        if cmd == "quota":
            return FakeProcess("用户名: alice, 总空间: 2TB, 已用空间: 100GB, 比率: 5.0%\n")
        if cmd == "mkdir":
            return FakeProcess(f"创建目录成功: {args[2]}\n")
        if cmd == "cd":
            return FakeProcess(f"改变工作目录: {args[2]}\n")
        if cmd == "transfer":
            assert args[2] == "--fs_id"
            return FakeProcess("分享文件保存成功, 保存了demo.zip到当前目录\n")
        if cmd == "download":
            return FakeProcess(hang=True)
        return FakeProcess("")

    runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go")
    configDir = tmp_path / "cfg"
    runner = BaiduPcsRunner(
        runtime=runtime,
        subprocessExec=fakeExec,
        readGlobalProxy=lambda: "socks5h://127.0.0.1:7890",
        configDir=configDir,
    )

    user, _ = await asyncio.gather(
        runner.loginWithCookies("BDUSS=secret-1; STOKEN=secret-2"),
        runner.setConfig(max_parallel=16),
    )
    assert user.isLoggedIn is True
    assert user.username == "alice"
    assert user.totalQuota == "2TB"
    assert configOverlap is False

    # 校验 BAIDUPCS_GO_CONFIG_DIR 与全局代理继承（socks5h -> socks5）
    firstEnv = calls[0][1]
    assert firstEnv["BAIDUPCS_GO_CONFIG_DIR"] == str(configDir)
    assert firstEnv["HTTP_PROXY"] == "socks5://127.0.0.1:7890"
    assert firstEnv["ALL_PROXY"] == "socks5://127.0.0.1:7890"

    await runner.transferFiles("https://pan.baidu.com/s/1Demo", "ab12", "/_ghd_tmp/tsk_1", ["101", "102"])
    transferCall = next(args for args, _env in calls if args[1] == "transfer")
    assert transferCall[1:] == ("transfer", "--fs_id", "101,102", "https://pan.baidu.com/s/1Demo", "ab12")
    # 转存后自动复位工作目录
    cdCalls = [args for args, _env in calls if args[1] == "cd"]
    assert cdCalls[-1][2] == "/"

    # 验证取消下载时可靠终止子进程
    createdProc: list[FakeProcess] = []

    async def hangExec(*args, **kwargs):
        proc = FakeProcess(hang=True)
        createdProc.append(proc)
        return proc

    cancelRunner = BaiduPcsRunner(runtime=runtime, subprocessExec=hangExec, configDir=configDir)
    dlTask = asyncio.create_task(cancelRunner.download(["/a.bin"], tmp_path / "staging"))
    await asyncio.sleep(0.03)
    dlTask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dlTask
    assert createdProc and createdProc[0].terminated is True


async def test_login_failure_redacts_secrets(tmp_path):
    async def failExec(*args, **kwargs):
        return FakeProcess(
            "错误: invalid BDUSS=MY_RAW_BDUSS_SECRET STOKEN=MY_RAW_STOKEN_SECRET http://alice:topsecret@127.0.0.1:7890",
            returncode=1,
        )

    runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go")
    runner = BaiduPcsRunner(
        runtime=runtime,
        subprocessExec=failExec,
        readGlobalProxy=lambda: "http://alice:topsecret@127.0.0.1:7890",
        configDir=tmp_path / "cfg",
    )
    with pytest.raises(TaskError) as excInfo:
        await runner.loginWithCredentials("MY_RAW_BDUSS_SECRET", "MY_RAW_STOKEN_SECRET")
    message = str(excInfo.value)
    assert "MY_RAW_BDUSS_SECRET" not in message
    assert "MY_RAW_STOKEN_SECRET" not in message
    assert "topsecret" not in message


async def test_download_batch_failure_and_cd_guard(tmp_path):
    runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go")

    async def partialFailExec(*args, **kwargs):
        return FakeProcess(
            "[1] 下载完成, 保存位置: /tmp/a.bin\n"
            "[2] 下载文件失败: remote error\n"
        )

    runner = BaiduPcsRunner(runtime=runtime, subprocessExec=partialFailExec, configDir=tmp_path / "cfg")
    with pytest.raises(TaskError):
        await runner.download(["/a.bin", "/b.bin"], tmp_path / "staging")

    async def badCdExec(*args, **kwargs):
        if args[1] == "mkdir":
            return FakeProcess("创建目录成功\n")
        if args[1] == "cd":
            return FakeProcess("请先登录\n")
        raise AssertionError("cd 失败后绝不应调用 transfer")

    cdRunner = BaiduPcsRunner(runtime=runtime, subprocessExec=badCdExec, configDir=tmp_path / "cfg")
    with pytest.raises(TaskError):
        await cdRunner.transferFiles("https://pan.baidu.com/s/1Demo", "ab12", "/_ghd_tmp/tsk_x", ["101"])
