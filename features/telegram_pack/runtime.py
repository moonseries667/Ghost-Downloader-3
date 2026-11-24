from __future__ import annotations

import asyncio
import codecs
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.config.cfg import proxy as cfgProxy
from app.config.paths import APP_DATA_DIR
from app.install import installArchive
from app.i18n import N
from app.models.pack import BinaryRuntime, VersionInfo
from app.models.task import TaskError
from app.platform.android import IS_ANDROID
from app.platform.arch import MACHINE_ARCH
from app.platform.filesystem import deletePath, findExecutable
from app.sources import Repo, fetchLatestRelease, fetchReleaseAsset

from .config import telegramConfig
from .conpty import TerminalScreen, createConPtyProcess, extractQrMatrix
from .parse import parseTelegramUrl

TDL_REPO = Repo("iyear/tdl")
PLATFORM = sys.platform

DOWNLOAD_TEMPLATE = "{{ .MessageID }}_{{ filenamify .FileName }}"
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
LINE_BREAK_PATTERN = re.compile(r"[\r\n]+")
VERSION_PATTERN = re.compile(r"v?\d+\.\d+(?:\.\d+)?")
PROGRESS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
SPEED_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[KMGT]i?B/s", re.IGNORECASE)
QR_TOKEN_PATTERN = re.compile(r"tg://login\?token=[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class TelegramChat:
    chatId: str
    type: str
    title: str
    username: str


@dataclass(frozen=True)
class TdlDownloadSpec:
    outputFolder: Path
    exportFile: Path | None = None
    url: str = ""
    threads: int = 4
    poolSize: int = 8
    useTakeout: bool = False
    template: str = DOWNLOAD_TEMPLATE


@dataclass(frozen=True)
class TdlProgress:
    percent: float
    speed: str
    text: str


def matchAsset(names: list[str], platform: str, arch: str) -> str:
    platform = platform.lower()
    if platform in ("win32", "windows"):
        words = ("windows",)
        extension = ".zip"
    elif platform in ("darwin", "macos"):
        words = ("macos", "darwin")
        extension = ".tar.gz"
    elif platform == "linux":
        words = ("linux",)
        extension = ".tar.gz"
    else:
        return ""

    for name in names:
        lowered = name.lower()
        if not lowered.endswith(extension) or not any(word in lowered for word in words):
            continue
        if arch == "arm64":
            if "arm64" in lowered:
                return name
        elif "arm64" not in lowered and (
            "64bit" in lowered or "amd64" in lowered or "x86_64" in lowered
        ):
            return name
    return ""


def toProxyUrl(url: str | None) -> str | None:
    if not url:
        return None
    value = str(url).strip()
    if not value:
        return None
    if value.startswith("socks5h://"):
        return "socks5://" + value[len("socks5h://"):]
    if "://" not in value:
        return "http://" + value
    return value


def toMaskedProxy(url: str | None) -> str:
    if not url:
        return ""
    value = str(url).strip()
    scheme, separator, remainder = value.partition("://")
    if not separator:
        return "***" if "@" in value else value
    authority, atSign, host = remainder.rpartition("@")
    if not atSign:
        return value
    user, colon, _ = authority.partition(":")
    if not colon:
        return f"{scheme}://***@{host}"
    return f"{scheme}://{user}:***@{host}"


def toSafeArgs(args: list[str]) -> list[str]:
    safe: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in ("-p", "--passcode"):
            safe.extend([argument, "****"])
            index += 2
        elif argument.startswith("--passcode="):
            safe.append("--passcode=****")
            index += 1
        elif argument == "--proxy":
            proxy = args[index + 1] if index + 1 < len(args) else ""
            safe.extend([argument, toMaskedProxy(proxy)])
            index += 2
        elif argument.startswith("--proxy="):
            safe.append("--proxy=" + toMaskedProxy(argument[len("--proxy="):]))
            index += 1
        else:
            safe.append(argument)
            index += 1
    return safe


def toSafeOutput(text: str, args: list[str]) -> str:
    result = text
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in ("-p", "--passcode") and index + 1 < len(args):
            secret = args[index + 1]
            if secret and secret in result:
                result = result.replace(secret, "****")
            index += 2
        elif argument.startswith("--passcode="):
            secret = argument[len("--passcode="):]
            if secret and secret in result:
                result = result.replace(secret, "****")
            index += 1
        elif argument == "--proxy" and index + 1 < len(args):
            proxy = args[index + 1]
            if proxy and proxy in result:
                result = result.replace(proxy, toMaskedProxy(proxy) or "****")
            index += 2
        elif argument.startswith("--proxy="):
            proxy = argument[len("--proxy="):]
            if proxy and proxy in result:
                result = result.replace(proxy, toMaskedProxy(proxy) or "****")
            index += 1
        else:
            index += 1
    return result


def stripAnsi(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


async def readStreamLines(stream, onLine: Callable[[str], None] | None = None) -> list[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    lines: list[str] = []
    buffer = ""
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        parts = LINE_BREAK_PATTERN.split(buffer)
        buffer = parts.pop()
        for part in parts:
            line = stripAnsi(part).strip()
            if not line:
                continue
            lines.append(line)
            if onLine is not None:
                onLine(line)
    tail = stripAnsi(buffer + decoder.decode(b"", final=True)).strip()
    if tail:
        lines.append(tail)
        if onLine is not None:
            onLine(tail)
    return lines


def parseProgress(line: str) -> TdlProgress | None:
    percentMatch = PROGRESS_PATTERN.search(line)
    if percentMatch is None:
        return None
    speedMatch = SPEED_PATTERN.search(line)
    return TdlProgress(
        percent=float(percentMatch.group(1)),
        speed=speedMatch.group(0) if speedMatch else "",
        text=line.strip(),
    )


def pickField(item: dict, names: tuple[str, ...]) -> object:
    for name in names:
        value = item.get(name)
        if value is not None:
            return value
    return ""


def parseJsonArray(output: str) -> list:
    start = output.find("[")
    end = output.rfind("]")
    if start < 0 or end < start:
        raise TaskError("无法从 tdl 输出中解析频道列表")
    try:
        payload = json.loads(output[start:end + 1])
    except json.JSONDecodeError as error:
        raise TaskError("无法解析 tdl 输出的频道列表：{detail}", detail=str(error)) from error
    if not isinstance(payload, list):
        raise TaskError("tdl 输出的频道列表格式不正确")
    return payload


class ConsoleProcess:
    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()


def createConsoleProcess(args: list[str]) -> ConsoleProcess:
    # 宿主在 Windows 上会替换 asyncio.create_subprocess_exec 为隐藏窗口版本，交互登录需直接拉起可见控制台
    return ConsoleProcess(subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE))


class TdlRuntime(BinaryRuntime):
    name = "tdl"
    binaryName = "tdl"
    canInstall = not IS_ANDROID
    title = N("BinaryRuntime", "Telegram 下载器 (tdl)")
    description = N("BinaryRuntime", "支持 Telegram 频道、媒体组与受保护内容下载")
    icon = "SEND"

    def installFolder(self) -> Path:
        return Path(telegramConfig.installFolder.value)

    def path(self) -> str:
        return findExecutable(self.installFolder(), "tdl")

    def installedPaths(self) -> list[Path]:
        suffix = ".exe" if PLATFORM == "win32" else ""
        return [self.installFolder() / f"tdl{suffix}"]

    async def probeVersion(self) -> VersionInfo:
        path = self.path()
        if not path:
            return VersionInfo("")
        for arguments in (["version"], ["--version"]):
            process = await asyncio.create_subprocess_exec(
                path, *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await process.communicate()
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            output = stdout.decode("utf-8", errors="ignore")
            match = VERSION_PATTERN.search(output)
            if process.returncode == 0 and match:
                return VersionInfo(match.group(0))
        return VersionInfo("")

    async def fetchLatestVersion(self) -> str:
        return (await fetchLatestRelease(TDL_REPO)).version

    async def install(self, version: str, onProgress: Callable[[float], None]) -> None:
        release = await fetchLatestRelease(TDL_REPO)
        assetName = matchAsset([asset.name for asset in release.assets], PLATFORM, MACHINE_ARCH)
        if not assetName:
            raise TaskError(
                "未找到适配 {platform} {arch} 的 tdl 安装包",
                platform=PLATFORM, arch=MACHINE_ARCH,
            )
        archive = self.installFolder() / assetName
        await fetchReleaseAsset(TDL_REPO, release.version, assetName, archive, onProgress)
        await installArchive(archive, self.installFolder())


tdlRuntime = TdlRuntime()


class TdlRunner:
    stopTimeout = 3.0

    def __init__(
        self,
        runtime: BinaryRuntime | None = None,
        subprocessExec: Callable[..., object] | None = None,
        readGlobalProxy: Callable[[], str | None] | None = None,
        storageDir: Path | None = None,
        namespace: str | None = None,
        proxyOverride: str | None = None,
        ptyFactory: Callable[[list[str]], object] | None = None,
    ) -> None:
        self.runtime = runtime if runtime is not None else tdlRuntime
        self._subprocessExec = subprocessExec
        self._ptyFactory = ptyFactory
        self.readGlobalProxy = readGlobalProxy if readGlobalProxy is not None else cfgProxy
        self.storageDir = Path(storageDir) if storageDir is not None else Path(APP_DATA_DIR) / "TDL" / "data"
        self.readNamespace = (lambda: namespace) if namespace is not None else (lambda: telegramConfig.namespace.value)
        self.readProxyOverride = (
            (lambda: proxyOverride) if proxyOverride is not None else (lambda: telegramConfig.proxyOverride.value)
        )
        self._lock = asyncio.Lock()

    @property
    def isBusy(self) -> bool:
        return self._lock.locked()

    def resolveProxy(self) -> str | None:
        override = str(self.readProxyOverride() or "").strip()
        return toProxyUrl(override if override else self.readGlobalProxy())

    def buildBaseArgs(self) -> list[str]:
        binary = self.runtime.path()
        if not binary:
            raise TaskError("未找到 tdl，请先在设置中安装 Telegram 下载器")
        args = [binary, "-n", self.readNamespace(), "--storage", f"type=bolt,path={self.storageDir}"]
        proxyUrl = self.resolveProxy()
        if proxyUrl:
            args.extend(["--proxy", proxyUrl])
        return args

    async def stopProcess(self, process) -> None:
        # 进程可能已自行退出，Windows 上 terminate()/kill() 会抛 ProcessLookupError
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self.stopTimeout)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def runCommand(self, args: list[str], onLine: Callable[[str], None] | None = None) -> str:
        async with self._lock:
            self.storageDir.mkdir(parents=True, exist_ok=True)
            logger.debug("运行 tdl：{}", " ".join(toSafeArgs(args)))
            execFn = self._subprocessExec if self._subprocessExec is not None else asyncio.create_subprocess_exec
            process = await execFn(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            lines: list[str] = []
            try:
                lines = await readStreamLines(process.stdout, onLine)
                await process.wait()
            finally:
                if process.returncode is None:
                    await self.stopProcess(process)
            if process.returncode != 0:
                detail = toSafeOutput(lines[-1], args) if lines else "无输出"
                raise TaskError(
                    "tdl 执行失败（{code}）：{detail}",
                    code=process.returncode, detail=detail,
                )
            return "\n".join(lines)

    async def probeAuth(self) -> bool:
        args = self.buildBaseArgs() + ["chat", "ls", "-o", "json"]
        try:
            output = await self.runCommand(args)
            parseJsonArray(output)
        except TaskError:
            return False
        return True

    async def loginInteractive(
        self,
        mode: str = "qr",
        *,
        desktopPath: str = "",
        passcode: str = "",
    ) -> None:
        if mode not in ("qr", "desktop", "code"):
            raise TaskError("不支持的登录方式：{mode}", mode=mode)
        async with self._lock:
            self.storageDir.mkdir(parents=True, exist_ok=True)
            tdlArgs = self.buildBaseArgs() + ["login", "-T", mode]
            if mode == "desktop":
                if desktopPath:
                    tdlArgs.extend(["-d", desktopPath])
                if passcode:
                    tdlArgs.extend(["-p", passcode])
            logger.debug("交互登录 tdl：{}", " ".join(toSafeArgs(tdlArgs)))
            if self._subprocessExec is not None:
                process = await self._subprocessExec(*tdlArgs)
            elif sys.platform == "win32":
                process = createConsoleProcess(tdlArgs)
            else:
                process = await asyncio.create_subprocess_exec(*tdlArgs)
            try:
                await process.wait()
            finally:
                if process.returncode is None:
                    await self.stopProcess(process)
            if process.returncode != 0:
                raise TaskError("Telegram 登录未完成或已取消（退出码 {code}）", code=process.returncode)

    async def loginQrSession(
        self,
        *,
        onQrMatrix: Callable[[tuple[tuple[bool, ...], ...]], None],
        onPasswordRequired: Callable[[], None],
        readPassword: Callable[[], Awaitable[str | None]],
        onStatus: Callable[[str], None] | None = None,
        pollInterval: float = 0.05,
    ) -> None:
        async with self._lock:
            self.storageDir.mkdir(parents=True, exist_ok=True)
            tdlArgs = self.buildBaseArgs() + ["login", "-T", "qr"]
            logger.debug("内置二维码登录 tdl：{}", " ".join(toSafeArgs(tdlArgs)))
            factory = self._ptyFactory if self._ptyFactory is not None else createConPtyProcess
            process = factory(tdlArgs)
            screen = TerminalScreen(cols=120, rows=60)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
            lastMatrix: tuple[tuple[bool, ...], ...] | None = None
            passwordTask: asyncio.Task[str | None] | None = None
            promptCount = 0
            exitCode: int | None = None
            try:
                while True:
                    chunk = process.readAvailable(4096)
                    if chunk:
                        screen.feed(decoder.decode(chunk))
                        lines = screen.lines()
                        fullText = "\n".join(lines)
                        lowered = fullText.lower()

                        currentPrompts = lowered.count("enter 2fa password")
                        if currentPrompts > promptCount and passwordTask is None:
                            promptCount = currentPrompts
                            onPasswordRequired()
                            passwordTask = asyncio.create_task(readPassword())

                        if passwordTask is None:
                            matrix = extractQrMatrix(lines)
                            if matrix is not None and matrix != lastMatrix:
                                lastMatrix = matrix
                                onQrMatrix(matrix)

                        if onStatus is not None and "login successfully" in lowered:
                            onStatus("登录成功，正在同步会话...")

                    if passwordTask is not None and passwordTask.done():
                        password = passwordTask.result()
                        passwordTask = None
                        if password is None:
                            raise asyncio.CancelledError
                        if onStatus is not None:
                            onStatus("正在校验两步验证密码...")
                        await process.writeInput(f"{password}\r")

                    code = process.returncode
                    if code is not None:
                        # 进程刚退出时排空残留输出，确保读取完整错误或成功提示
                        for _ in range(4):
                            tailChunk = process.readAvailable(4096)
                            if not tailChunk:
                                break
                            screen.feed(decoder.decode(tailChunk))
                        screen.feed(decoder.decode(b"", final=True))
                        exitCode = code
                        break

                    await asyncio.sleep(pollInterval)
            finally:
                if passwordTask is not None and not passwordTask.done():
                    passwordTask.cancel()
                    with suppress(asyncio.CancelledError):
                        await passwordTask
                if process.returncode is None:
                    with suppress(OSError, ProcessLookupError):
                        process.terminate()
                    with suppress(TimeoutError, OSError):
                        await asyncio.wait_for(process.wait(), self.stopTimeout)
                with suppress(OSError):
                    process.close()

            if exitCode != 0:
                raise toQrLoginError(screen.text(), tdlArgs, exitCode or -1)

    async def logout(self) -> bool:
        async with self._lock:
            namespace = str(self.readNamespace() or "").strip()
            if not namespace:
                return False
            target = self.storageDir / namespace
            if not target.exists():
                return False
            deletePath(target)
            if target.exists():
                raise TaskError("无法清除登录会话，请关闭其他正在运行的 tdl 进程后重试")
            return True

    async def loginDesktop(self, desktopPath: str = "", passcode: str = "") -> None:
        args = self.buildBaseArgs() + ["login", "-T", "desktop"]
        if desktopPath:
            args.extend(["-d", desktopPath])
        if passcode:
            args.extend(["-p", passcode])
        await self.runCommand(args)

    async def startQrLogin(self, onQrText: Callable[[str], None]) -> None:
        def onLine(line: str) -> None:
            match = QR_TOKEN_PATTERN.search(line)
            onQrText(match.group(0) if match else line)

        await self.runCommand(self.buildBaseArgs() + ["login", "-T", "qr"], onLine=onLine)

    async def listChats(self) -> list[TelegramChat]:
        output = await self.runCommand(self.buildBaseArgs() + ["chat", "ls", "-o", "json"])
        chats: list[TelegramChat] = []
        for item in parseJsonArray(output):
            if not isinstance(item, dict):
                continue
            chatId = str(pickField(item, ("id", "ID")) or "")
            if not chatId:
                continue
            chats.append(TelegramChat(
                chatId=chatId,
                type=str(pickField(item, ("type", "Type")) or ""),
                title=str(pickField(item, ("visible_name", "VisibleName", "title")) or ""),
                username=str(pickField(item, ("username", "Username")) or ""),
            ))
        return chats

    async def exportMessages(
        self,
        chatId: str,
        *,
        sinceMsgId: int = 0,
        limit: int = 50,
        idRange: tuple[int, int] | None = None,
        outputPath: Path | None = None,
    ) -> dict:
        args = self.buildBaseArgs() + ["chat", "export", "-c", chatId]
        if idRange is not None:
            args.extend(["-T", "id", "-i", f"{idRange[0]},{idRange[1]}"])
        elif sinceMsgId > 0:
            args.extend(["-T", "id", "-i", f"{sinceMsgId + 1},2147483647"])
        else:
            args.extend(["-T", "last", "-i", str(limit)])
        args.extend(["--with-content", "--raw"])

        temporary = outputPath is None
        if temporary:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as file:
                path = Path(file.name)
        else:
            path = outputPath
        args.extend(["-o", str(path)])

        try:
            await self.runCommand(args)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise TaskError("无法解析 tdl 导出的消息：{detail}", detail=str(error)) from error
        finally:
            if temporary:
                path.unlink(missing_ok=True)

        if isinstance(payload, list):
            return {"id": chatId, "messages": payload}
        if isinstance(payload, dict):
            return payload
        raise TaskError("tdl 导出的消息格式不正确")

    async def download(
        self,
        spec: TdlDownloadSpec,
        onProgress: Callable[[TdlProgress], None] | None = None,
    ) -> None:
        args = self.buildBaseArgs() + ["dl"]
        if spec.exportFile is not None:
            args.extend(["-f", str(spec.exportFile)])
        elif spec.url:
            link = parseTelegramUrl(spec.url)
            args.extend(["-u", spec.url])
            if not (link and link.isSingle):
                args.append("--group")
        else:
            raise TaskError("下载需要导出文件或消息链接")
        args.extend([
            "-d", str(spec.outputFolder),
            "-t", str(spec.threads),
            "--pool", str(spec.poolSize),
            "--continue",
            "--disable-progress-ps",
        ])
        if spec.template:
            args.extend(["--template", spec.template])
        if spec.useTakeout:
            args.append("--takeout")

        def onLine(line: str) -> None:
            if onProgress is None:
                return
            progress = parseProgress(line)
            if progress is not None:
                onProgress(progress)

        await self.runCommand(args, onLine=onLine)


def toQrLoginError(screenText: str, args: list[str], code: int) -> TaskError:
    lowered = screenText.lower()
    if "password_hash_invalid" in lowered:
        return TaskError("两步验证（2FA）密码错误，请刷新二维码后重试")
    if "auth_token_expired" in lowered:
        return TaskError("二维码已失效，请点击刷新二维码重试")
    if "forcibly closed" in lowered or "connection refused" in lowered or "i/o timeout" in lowered:
        return TaskError("无法连接到 Telegram 服务器，请检查代理配置后重试")
    meaningful = [
        line.strip()
        for line in screenText.splitlines()
        if line.strip() and not set(line.strip()) <= {" ", "\u2580", "\u2584", "\u2588"}
    ]
    detail = toSafeOutput(meaningful[-1], args) if meaningful else f"退出码 {code}"
    return TaskError("Telegram 扫码登录失败：{detail}", detail=detail)


# 进程内共享默认单例：pack 与重启恢复的任务复用同一把 BoltDB 锁
tdlRunner = TdlRunner()
