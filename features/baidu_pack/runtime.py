from __future__ import annotations

import asyncio
import codecs
import os
import posixpath
import re
import sys
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.config.cfg import proxy as cfgProxy
from app.config.paths import APP_DATA_DIR
from app.format import BYTE_SCALE, toBytes
from app.i18n import N
from app.install import installArchive
from app.models.pack import BinaryRuntime, VersionInfo
from app.models.task import TaskError
from app.platform.android import IS_ANDROID
from app.platform.arch import MACHINE_ARCH
from app.platform.filesystem import findExecutable
from app.sources import Repo, fetchLatestRelease, fetchReleaseAsset

from .config import baiduConfig

BAIDU_PCS_REPO = Repo("qjfoidnh/BaiduPCS-Go")
PLATFORM = sys.platform
TEMP_ROOT_DIR = "/_ghd_tmp"
TRANSFER_BATCH_SIZE = 100

ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
LINE_BREAK_PATTERN = re.compile(r"[\r\n]+")
VERSION_PATTERN = re.compile(r"v?\d+\.\d+(?:\.\d+)?")
WHO_PATTERN = re.compile(r"uid:\s*(\d+),\s*用户名:\s*([^,\r\n]*)")
QUOTA_PATTERN = re.compile(
    r"用户名:\s*([^,\r\n]+),\s*总空间:\s*([^,\r\n]+),\s*已用空间:\s*([^,\r\n]+),\s*比率:\s*([^\s,\r\n]+)"
)
SIZE_VALUE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?i?B)", re.IGNORECASE)
DOWNLOAD_PROGRESS_PATTERN = re.compile(
    r"↓\s*(\d+(?:\.\d+)?\s*[KMGT]?i?B)\s*/\s*(\d+(?:\.\d+)?\s*[KMGT]?i?B)\s+(\d+(?:\.\d+)?\s*[KMGT]?i?B/s)",
    re.IGNORECASE,
)
PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
SPEED_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?i?B)/s", re.IGNORECASE)
LS_DETAIL_ROW_PATTERN = re.compile(
    r"^\s*\d+\s+(\d+)\s+\d+\s+([^\s]+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+"
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+(.+?)\s*$"
)
LS_SIMPLE_ROW_PATTERN = re.compile(
    r"^\s*\d+\s+([^\s]+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+?)\s*$"
)
SECRET_KV_PATTERN = re.compile(
    r"(?i)\b(BDUSS|STOKEN|PTOKEN|COOKIE|COOKIES|PASSWD|PASSWORD)\s*[:=]\s*([^\s;\"']+)"
)
SECRET_FLAG_NAMES = {
    "-cookies", "--cookies",
    "-bduss", "--bduss",
    "-stoken", "--stoken",
    "-ptoken", "--ptoken",
    "-password", "--password",
}
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
TRANSFER_ERROR_HINTS = (
    ("未登录", "auth", "百度账号未登录，请在设置中登录"),
    ("stoken", "auth", "缺少网盘 STOKEN，请重新导入完整 Cookie 或扫码登录"),
    ("提取码", "passcode", "提取码错误或缺失，请核对后重试"),
    ("失效", "expired", "分享链接已失效或已取消"),
    ("页面不存在", "expired", "分享链接不存在或已失效"),
    ("上限", "save_limit", "超出百度网盘单次转存文件数量限制，请减少勾选后重试"),
    ("空间", "capacity", "百度网盘空间不足，请清理空间后重试"),
    ("验证", "rate_limit", "百度网盘触发风控验证，请稍后再试"),
)
DOWNLOAD_ERROR_MARKERS = (
    "下载文件失败",
    "获取下载链接失败",
    "初始化下载发生错误",
    "检测文件有效性失败",
    "检测文件大小一致性失败",
    "遇到错误",
    "user is not authorized",
    "文件或目录不存在",
    "请先登录",
    "未登录",
)


@dataclass(frozen=True)
class BaiduUserInfo:
    isLoggedIn: bool
    uid: str = ""
    username: str = ""
    totalQuota: str = ""
    usedQuota: str = ""
    usagePercent: str = ""


@dataclass(frozen=True)
class BaiduDriveEntry:
    fsId: str
    name: str
    path: str
    size: int
    isDir: bool
    modifiedAt: str = ""


@dataclass(frozen=True)
class BaiduDownloadProgress:
    downloadedBytes: int
    totalBytes: int
    percent: float
    speedBytes: int
    speedText: str
    text: str


def matchAsset(names: list[str], platform: str, arch: str) -> str:
    platform = platform.lower()
    arch = arch.lower()
    if platform in ("win32", "windows"):
        words = ("windows",)
    elif platform in ("darwin", "macos"):
        words = ("darwin", "macos", "osx")
    elif platform == "linux":
        words = ("linux",)
    else:
        return ""

    for name in names:
        lowered = name.lower()
        if not lowered.endswith(".zip") or not any(word in lowered for word in words):
            continue
        if arch in ("arm64", "aarch64"):
            if "arm64" in lowered or "aarch64" in lowered:
                return name
        elif all(token not in lowered for token in ("arm64", "aarch64", "mips", "386", "x86.", "-arm.", "-x86.zip")):
            if any(token in lowered for token in ("amd64", "x64", "x86_64", "64bit")):
                return name
    return ""


def findArchiveRoot(archive: Path, binaryName: str) -> str:
    candidates = {binaryName.lower(), f"{binaryName.lower()}.exe"}
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                base = posixpath.basename(name).lower()
                if base in candidates:
                    parent = posixpath.dirname(name)
                    return f"{parent}/" if parent else ""
    except (OSError, zipfile.BadZipFile):
        return ""
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


def redactSecrets(text: str, extraSecrets: tuple[str, ...] = (), proxyUrl: str | None = None) -> str:
    result = str(text or "")
    for secret in extraSecrets:
        if secret and secret in result:
            result = result.replace(secret, "****")
    if proxyUrl and proxyUrl in result:
        result = result.replace(proxyUrl, toMaskedProxy(proxyUrl) or "****")
    result = SECRET_KV_PATTERN.sub(lambda m: f"{m.group(1)}=****", result)
    return result


def toSafeArgs(args: list[str]) -> list[str]:
    safe: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        lowered = argument.lower()
        if lowered in SECRET_FLAG_NAMES:
            safe.extend([argument, "****"])
            index += 2
            continue
        if any(lowered.startswith(f"{flag}=") for flag in SECRET_FLAG_NAMES):
            prefix, _, _ = argument.partition("=")
            safe.append(f"{prefix}=****")
            index += 1
            continue
        if lowered in ("-proxy", "--proxy"):
            proxy = args[index + 1] if index + 1 < len(args) else ""
            safe.extend([argument, toMaskedProxy(proxy)])
            index += 2
            continue
        if lowered.startswith(("-proxy=", "--proxy=")):
            prefix, _, proxy = argument.partition("=")
            safe.append(f"{prefix}={toMaskedProxy(proxy)}")
            index += 1
            continue
        safe.append(redactSecrets(argument))
        index += 1
    return safe


def toSafeOutput(
    text: str,
    args: list[str] = (),
    extraSecrets: tuple[str, ...] = (),
    proxyUrl: str | None = None,
) -> str:
    secrets = list(extraSecrets)
    result = str(text or "")
    index = 0
    while index < len(args):
        argument = args[index]
        lowered = argument.lower()
        if lowered in SECRET_FLAG_NAMES and index + 1 < len(args):
            secrets.append(args[index + 1])
            index += 2
            continue
        if any(lowered.startswith(f"{flag}=") for flag in SECRET_FLAG_NAMES):
            _, _, value = argument.partition("=")
            if value:
                secrets.append(value)
            index += 1
            continue
        if lowered in ("-proxy", "--proxy") and index + 1 < len(args):
            proxy = args[index + 1]
            if proxy and proxy in result:
                result = result.replace(proxy, toMaskedProxy(proxy) or "****")
            index += 2
            continue
        if lowered.startswith(("-proxy=", "--proxy=")):
            _, _, proxy = argument.partition("=")
            if proxy and proxy in result:
                result = result.replace(proxy, toMaskedProxy(proxy) or "****")
            index += 1
            continue
        index += 1
    return redactSecrets(result, tuple(secrets), proxyUrl=proxyUrl)


def stripAnsi(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


def parseSizeText(text: str) -> int:
    cleaned = str(text or "").strip()
    if not cleaned or cleaned == "-":
        return 0
    if cleaned.isdigit():
        return int(cleaned)
    match = SIZE_VALUE_PATTERN.search(cleaned)
    if match is None:
        return 0
    unit = match.group(2).upper().replace("I", "")
    if unit not in BYTE_SCALE:
        return 0
    return toBytes(match.group(1), unit)


def parseSpeedText(text: str) -> int:
    match = SPEED_PATTERN.search(text or "")
    if match is None:
        return 0
    unit = match.group(2).upper().replace("I", "")
    if unit not in BYTE_SCALE:
        return 0
    return toBytes(match.group(1), unit)


def toRemotePath(parentPath: str, name: str) -> str:
    parent = posixpath.normpath("/" + str(parentPath or "/").lstrip("/"))
    if parent == ".":
        parent = "/"
    cleanedName = str(name or "").strip().strip("/")
    if not cleanedName:
        return parent
    return posixpath.join(parent, cleanedName)


def parseWhoOutput(output: str) -> tuple[str, str]:
    match = WHO_PATTERN.search(output or "")
    if match is None:
        return "", ""
    uid = match.group(1).strip()
    username = match.group(2).strip()
    if uid == "0" or not username:
        return "", ""
    return uid, username


def parseQuotaOutput(output: str) -> tuple[str, str, str, str]:
    match = QUOTA_PATTERN.search(output or "")
    if match is None:
        return "", "", "", ""
    return (
        match.group(1).strip(),
        match.group(2).strip(),
        match.group(3).strip(),
        match.group(4).strip(),
    )


def buildDriveEntry(
    fsId: str,
    sizeText: str,
    modifiedAt: str,
    rawName: str,
    parentPath: str,
) -> BaiduDriveEntry | None:
    isDir = rawName.endswith("/") or sizeText == "-"
    name = rawName.rstrip("/")
    if not name:
        return None
    return BaiduDriveEntry(
        fsId=fsId,
        name=name,
        path=toRemotePath(parentPath, name),
        size=0 if isDir else parseSizeText(sizeText),
        isDir=isDir,
        modifiedAt=modifiedAt.strip(),
    )


def parseLsOutput(output: str, parentPath: str = "/") -> list[BaiduDriveEntry]:
    entries: list[BaiduDriveEntry] = []
    for rawLine in (output or "").splitlines():
        line = stripAnsi(rawLine).strip()
        if not line or line.startswith(("当前目录:", "----", "#")) or "文件总数:" in line:
            continue

        detailMatch = LS_DETAIL_ROW_PATTERN.match(line)
        if detailMatch is not None:
            fsId, sizeText, modifiedAt, rawName = detailMatch.groups()
            entry = buildDriveEntry(fsId, sizeText, modifiedAt, rawName, parentPath)
            if entry is not None:
                entries.append(entry)
            continue

        simpleMatch = LS_SIMPLE_ROW_PATTERN.match(line)
        if simpleMatch is not None:
            sizeText, modifiedAt, rawName = simpleMatch.groups()
            entry = buildDriveEntry("", sizeText, modifiedAt, rawName, parentPath)
            if entry is not None:
                entries.append(entry)
    return entries


def parseDownloadProgress(line: str) -> BaiduDownloadProgress | None:
    cleaned = stripAnsi(line).strip()
    if not cleaned:
        return None
    match = DOWNLOAD_PROGRESS_PATTERN.search(cleaned)
    if match is not None:
        downloaded = parseSizeText(match.group(1))
        total = parseSizeText(match.group(2))
        speedText = match.group(3).strip()
        percent = min(100.0, downloaded / total * 100.0) if total > 0 else 0.0
        return BaiduDownloadProgress(
            downloadedBytes=downloaded,
            totalBytes=total,
            percent=percent,
            speedBytes=parseSpeedText(speedText),
            speedText=speedText,
            text=cleaned,
        )
    percentMatch = PERCENT_PATTERN.search(cleaned)
    speedMatch = SPEED_PATTERN.search(cleaned)
    if percentMatch is not None or speedMatch is not None:
        speedText = speedMatch.group(0) if speedMatch is not None else ""
        return BaiduDownloadProgress(
            downloadedBytes=0,
            totalBytes=0,
            percent=min(100.0, max(0.0, float(percentMatch.group(1)))) if percentMatch else 0.0,
            speedBytes=parseSpeedText(speedText),
            speedText=speedText,
            text=cleaned,
        )
    return None


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


def toTransferError(output: str, proxyUrl: str | None = None) -> TaskError:
    cleaned = redactSecrets(output, proxyUrl=proxyUrl).strip()
    lowered = cleaned.lower()
    for keyword, kind, message in TRANSFER_ERROR_HINTS:
        if keyword in lowered or keyword in cleaned:
            return TaskError(message, kind=kind)
    detail = cleaned.splitlines()[-1] if cleaned else "未知错误"
    return TaskError("百度分享转存失败：{detail}", detail=detail)


def checkMkdirOutput(output: str, proxyUrl: str | None = None) -> None:
    if "创建目录成功" in output or "已存在" in output or "already exists" in output.lower():
        return
    if "失败" in output or "错误" in output:
        detail = redactSecrets(output.splitlines()[-1], proxyUrl=proxyUrl)
        raise TaskError("创建百度网盘目录失败：{detail}", detail=detail)


class BaiduPcsRuntime(BinaryRuntime):
    name = "BaiduPCS-Go"
    binaryName = "BaiduPCS-Go"
    canInstall = not IS_ANDROID
    title = N("BinaryRuntime", "百度网盘下载器 (BaiduPCS-Go)")
    description = N("BinaryRuntime", "支持百度网盘分享转存、我的网盘浏览与高速多线程下载")
    icon = "CLOUD_DOWNLOAD"

    def installFolder(self) -> Path:
        return Path(baiduConfig.installFolder.value)

    def path(self) -> str:
        return findExecutable(self.installFolder(), self.binaryName)

    def installedPaths(self) -> list[Path]:
        suffix = ".exe" if PLATFORM == "win32" else ""
        return [self.installFolder() / f"{self.binaryName}{suffix}"]

    async def probeVersion(self) -> VersionInfo:
        binary = self.path()
        if not binary:
            return VersionInfo("")
        process = await asyncio.create_subprocess_exec(
            binary, "--version",
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
        return (await fetchLatestRelease(BAIDU_PCS_REPO)).version

    async def install(self, version: str, onProgress: Callable[[float], None]) -> None:
        release = await fetchLatestRelease(BAIDU_PCS_REPO)
        assetName = matchAsset([asset.name for asset in release.assets], PLATFORM, MACHINE_ARCH)
        if not assetName:
            raise TaskError(
                "未找到适配 {platform} {arch} 的 BaiduPCS-Go 安装包",
                platform=PLATFORM, arch=MACHINE_ARCH,
            )
        archive = self.installFolder() / assetName
        await fetchReleaseAsset(BAIDU_PCS_REPO, release.version, assetName, archive, onProgress)
        root = findArchiveRoot(archive, self.binaryName)
        await installArchive(archive, self.installFolder(), root=root)
        if PLATFORM != "win32":
            binary = self.installedPaths()[0]
            if binary.is_file():
                binary.chmod(binary.stat().st_mode | 0o755)


baiduPcsRuntime = BaiduPcsRuntime()


class BaiduPcsRunner:
    stopTimeout = 3.0

    def __init__(
        self,
        runtime: BinaryRuntime | None = None,
        subprocessExec: Callable[..., object] | None = None,
        readGlobalProxy: Callable[[], str | None] | None = None,
        configDir: Path | None = None,
    ) -> None:
        self.runtime = runtime if runtime is not None else baiduPcsRuntime
        self._subprocessExec = subprocessExec
        self.readGlobalProxy = readGlobalProxy if readGlobalProxy is not None else cfgProxy
        self.configDir = Path(configDir) if configDir is not None else Path(APP_DATA_DIR) / "BaiduPCS" / "config"
        self._configLock = asyncio.Lock()
        self._activeProcesses: set[object] = set()

    @property
    def isConfigBusy(self) -> bool:
        return self._configLock.locked()

    def resolveProxy(self) -> str | None:
        return toProxyUrl(self.readGlobalProxy())

    def buildEnv(self) -> dict[str, str]:
        env = dict(os.environ)
        env["BAIDUPCS_GO_CONFIG_DIR"] = str(self.configDir)
        proxyUrl = self.resolveProxy()
        if proxyUrl:
            env["HTTP_PROXY"] = proxyUrl
            env["HTTPS_PROXY"] = proxyUrl
            env["http_proxy"] = proxyUrl
            env["https_proxy"] = proxyUrl
            if proxyUrl.startswith("socks"):
                env["ALL_PROXY"] = proxyUrl
                env["all_proxy"] = proxyUrl
            else:
                env.pop("ALL_PROXY", None)
                env.pop("all_proxy", None)
        else:
            for key in PROXY_ENV_KEYS:
                env.pop(key, None)
        return env

    def binaryPath(self) -> str:
        binary = self.runtime.path()
        if not binary:
            raise TaskError("未找到 BaiduPCS-Go，请先在设置中安装百度网盘下载器")
        return binary

    async def stopProcess(self, process) -> None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self.stopTimeout)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def stopAll(self) -> None:
        processes = list(self._activeProcesses)
        for process in processes:
            if getattr(process, "returncode", None) is None:
                await self.stopProcess(process)

    async def runCommand(
        self,
        commandArgs: list[str],
        *,
        useConfigLock: bool = False,
        onLine: Callable[[str], None] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> str:
        if useConfigLock:
            async with self._configLock:
                return await self._exec(commandArgs, onLine=onLine, secrets=secrets)
        return await self._exec(commandArgs, onLine=onLine, secrets=secrets)

    async def _exec(
        self,
        commandArgs: list[str],
        *,
        onLine: Callable[[str], None] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> str:
        binary = self.binaryPath()
        args = [binary, *commandArgs]
        self.configDir.mkdir(parents=True, exist_ok=True)
        logger.debug("运行 BaiduPCS-Go：{}", " ".join(toSafeArgs(args)))
        execFn = self._subprocessExec if self._subprocessExec is not None else asyncio.create_subprocess_exec
        process = await execFn(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.buildEnv(),
        )
        self._activeProcesses.add(process)
        lines: list[str] = []
        try:
            lines = await readStreamLines(process.stdout, onLine)
            await process.wait()
        finally:
            self._activeProcesses.discard(process)
            if process.returncode is None:
                await self.stopProcess(process)
        output = "\n".join(lines)
        if process.returncode != 0:
            detail = toSafeOutput(lines[-1], args, secrets, proxyUrl=self.resolveProxy()) if lines else "无输出"
            raise TaskError(
                "BaiduPCS-Go 执行失败（{code}）：{detail}",
                code=process.returncode, detail=detail,
            )
        return output

    async def setConfig(self, **options: str | int | bool) -> str:
        args = ["config", "set"]
        for key, value in options.items():
            args.append(f"-{key}={value}")
        return await self.runCommand(args, useConfigLock=True)

    async def loginWithCookies(self, cookies: str) -> BaiduUserInfo:
        normalized = str(cookies or "").strip()
        if not normalized:
            raise TaskError("请输入有效的百度 Cookie")
        output = await self.runCommand(
            ["login", f"-cookies={normalized}"],
            useConfigLock=True,
            secrets=(normalized,),
        )
        if "登录成功" not in output:
            safeDetail = toSafeOutput(
                output.splitlines()[-1] if output else "登录失败",
                (),
                (normalized,),
                proxyUrl=self.resolveProxy(),
            )
            raise TaskError("百度账号登录失败：{detail}", detail=safeDetail)
        return await self.probeUser()

    async def loginWithCredentials(self, bduss: str, stoken: str = "") -> BaiduUserInfo:
        cleanBduss = str(bduss or "").strip()
        cleanStoken = str(stoken or "").strip()
        if not cleanBduss:
            raise TaskError("请输入有效的 BDUSS")
        args = ["login", f"-bduss={cleanBduss}"]
        secrets: list[str] = [cleanBduss]
        if cleanStoken:
            args.append(f"-stoken={cleanStoken}")
            secrets.append(cleanStoken)
        output = await self.runCommand(args, useConfigLock=True, secrets=tuple(secrets))
        if "登录成功" not in output:
            safeDetail = toSafeOutput(
                output.splitlines()[-1] if output else "登录失败",
                (),
                tuple(secrets),
                proxyUrl=self.resolveProxy(),
            )
            raise TaskError("百度账号登录失败：{detail}", detail=safeDetail)
        return await self.probeUser()

    async def logout(self) -> bool:
        output = await self.runCommand(["logout", "-y"], useConfigLock=True)
        if "未设置任何百度帐号" in output:
            return False
        if "退出用户成功" in output:
            return True
        if "失败" in output:
            detail = redactSecrets(output.splitlines()[-1], proxyUrl=self.resolveProxy())
            raise TaskError("退出百度账号失败：{detail}", detail=detail)
        return True

    async def probeUser(self) -> BaiduUserInfo:
        whoOutput = await self.runCommand(["who"])
        uid, username = parseWhoOutput(whoOutput)
        if not uid or not username:
            return BaiduUserInfo(isLoggedIn=False)
        totalQuota = ""
        usedQuota = ""
        usagePercent = ""
        try:
            quotaOutput = await self.runCommand(["quota"])
            quotaName, totalQuota, usedQuota, usagePercent = parseQuotaOutput(quotaOutput)
            if quotaName:
                username = quotaName
        except TaskError:
            pass
        return BaiduUserInfo(
            isLoggedIn=True,
            uid=uid,
            username=username,
            totalQuota=totalQuota,
            usedQuota=usedQuota,
            usagePercent=usagePercent,
        )

    async def listDirectory(self, remotePath: str = "/") -> list[BaiduDriveEntry]:
        target = toRemotePath(remotePath, "")
        output = await self.runCommand(["ls", "-l", target])
        if "当前目录:" not in output:
            if "未登录" in output or "请先登录" in output:
                raise TaskError("请先在设置中登录百度账号", kind="auth")
            if "不存在" in output:
                raise TaskError("网盘目录不存在：{path}", path=target, kind="not_found")
            if "遇到错误" in output or "失败" in output:
                detail = redactSecrets(output.splitlines()[-1], proxyUrl=self.resolveProxy()) if output else "目录读取失败"
                raise TaskError("读取百度网盘目录失败：{detail}", detail=detail)
        return parseLsOutput(output, target)

    async def mkdir(self, remotePath: str, *, useConfigLock: bool = False) -> None:
        target = toRemotePath(remotePath, "")
        if target == "/":
            return
        output = await self.runCommand(["mkdir", target], useConfigLock=useConfigLock)
        checkMkdirOutput(output, proxyUrl=self.resolveProxy())

    async def _ensureDirTreeUnlocked(self, remotePath: str) -> None:
        target = toRemotePath(remotePath, "")
        if target == "/":
            return
        parts = [part for part in target.split("/") if part]
        current = ""
        proxyUrl = self.resolveProxy()
        for part in parts:
            current = f"{current}/{part}"
            output = await self._exec(["mkdir", current])
            checkMkdirOutput(output, proxyUrl=proxyUrl)

    async def transferFiles(
        self,
        shareBaseUrl: str,
        passcode: str,
        targetDir: str,
        fsIds: list[str],
    ) -> None:
        cleanIds = [str(fsId).strip() for fsId in fsIds if str(fsId).strip()]
        if not cleanIds:
            return
        target = toRemotePath(targetDir, "")
        proxyUrl = self.resolveProxy()
        # BaiduPCS-Go transfer 依赖配置中的当前工作目录，在写配置锁内串行执行 mkdir -> cd -> transfer -> cd /
        async with self._configLock:
            await self._ensureDirTreeUnlocked(target)
            cdOut = await self._exec(["cd", target])
            if "改变工作目录" not in cdOut:
                detail = redactSecrets(cdOut.splitlines()[-1], proxyUrl=proxyUrl) if cdOut else "工作目录切换失败"
                raise TaskError("切换百度网盘目录失败：{detail}", detail=detail)
            try:
                for start in range(0, len(cleanIds), TRANSFER_BATCH_SIZE):
                    batch = cleanIds[start:start + TRANSFER_BATCH_SIZE]
                    args = ["transfer", "--fs_id", ",".join(batch), shareBaseUrl]
                    if passcode:
                        args.append(passcode)
                    output = await self._exec(args)
                    if "失败" in output or "保存了" not in output:
                        raise toTransferError(output, proxyUrl=proxyUrl)
            finally:
                with suppress(Exception):
                    await self._exec(["cd", "/"])

    async def download(
        self,
        remotePaths: list[str],
        saveDir: Path,
        *,
        mode: str = "locate",
        threads: int = 8,
        onProgress: Callable[[BaiduDownloadProgress], None] | None = None,
    ) -> None:
        targets = [toRemotePath(path, "") for path in remotePaths if str(path or "").strip()]
        if not targets:
            raise TaskError("没有可下载的百度网盘文件路径")
        saveDir.mkdir(parents=True, exist_ok=True)
        args = [
            "download",
            "--mode", mode or "locate",
            "-p", str(max(1, int(threads))),
            "--saveto", str(saveDir),
            *targets,
        ]

        def onLine(line: str) -> None:
            if onProgress is None:
                return
            progress = parseDownloadProgress(line)
            if progress is not None:
                onProgress(progress)

        output = await self.runCommand(args, onLine=onLine)
        proxyUrl = self.resolveProxy()
        for line in reversed(output.splitlines()):
            lowered = line.lower()
            if any(marker in line or marker in lowered for marker in DOWNLOAD_ERROR_MARKERS):
                raise TaskError("百度网盘下载失败：{detail}", detail=redactSecrets(line, proxyUrl=proxyUrl))

    async def removeRemote(self, remotePaths: list[str]) -> None:
        targets = [toRemotePath(path, "") for path in remotePaths if str(path or "").strip() and path != "/"]
        if not targets:
            return
        output = await self.runCommand(["rm", *targets])
        if "操作失败" in output and "不存在" not in output:
            detail = redactSecrets(output.splitlines()[-1], proxyUrl=self.resolveProxy())
            raise TaskError("删除百度网盘路径失败：{detail}", detail=detail)

    async def cleanStaleTempDirs(self, keepNames: set[str]) -> list[str]:
        if not self.runtime.path():
            return []
        try:
            entries = await self.listDirectory(TEMP_ROOT_DIR)
        except TaskError:
            return []
        stalePaths = [
            entry.path for entry in entries
            if entry.isDir and entry.name and entry.name not in keepNames
        ]
        if not stalePaths:
            return []
        try:
            await self.removeRemote(stalePaths)
        except TaskError as error:
            logger.warning("静默清理百度临时目录失败：{}", error)
            return []
        return stalePaths


baiduPcsRunner = BaiduPcsRunner()
