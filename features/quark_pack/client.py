from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from loguru import logger

from app.client import buildClient
from app.models.task import TaskError

DRIVE_BASE = "https://drive-pc.quark.cn/1/clouddrive"
PAN_BASE = "https://pan.quark.cn"
AUTH_BASE = "https://uop.quark.cn"
PAN_REFERER = "https://pan.quark.cn/"
PAN_ORIGIN = "https://pan.quark.cn"
TEMP_FOLDER_NAME = "_ghd_tmp"
PAGE_SIZE = 50
MAX_PAGES = 100
SAVE_BATCH_SIZE = 50
TASK_STATUS_DONE = 2
TASK_STATUS_FAILED = 3
SAVE_POLL_LIMIT = 30
TASK_POLL_INTERVAL = 0.5

# 网页扫码。client_id / v / 2000000 / 50004001 / 50004002 来自公开实现与 2026-10 抓包记录。
# 50004003 没有公开抓包，只为"已扫码未确认"预留。
QR_CLIENT_ID = "532"
QR_VERSION = "1.2"
QR_LOGIN_PAGE = "https://su.quark.cn/4_eMHBJ"
QR_API_SUCCESS = 2000000
QR_API_WAITING = 50004001
QR_API_EXPIRED = 50004002
QR_API_SCANNED = 50004003

# 网盘接口按 UA 区分桌面端与网页端，网页 UA 会被 file/download 等接口拒绝（HTTP 400）
QUARK_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)

PASSCODE_CODES = {41006, 41008}
EXPIRED_CODES = {41004, 41010, 41011, 41012, 41019}
AUTH_CODES = {31001, 31004}
CAPACITY_CODES = {32003}
SAVE_LIMIT_CODES = {41016, 41028}
RATE_LIMIT_CODES = {31019, 32004}
NON_RETRYABLE_SAVE_KINDS = frozenset({
    "auth", "passcode", "expired", "capacity", "save_limit", "rate_limit",
})
EXPIRED_WORDS = ("失效", "不存在", "已取消", "已删除")
CAPACITY_WORDS = ("capacity limit", "容量不足", "空间不足", "容量已满", "空间已满")
SAVE_LIMIT_WORDS = ("file count limit", "save limit", "文件数", "转存数量", "超出限制", "超过上限")
RATE_LIMIT_WORDS = ("too many requests", "rate limit", "频繁", "限流")


@dataclass(frozen=True)
class QuarkProfile:
    isLoggedIn: bool
    nickname: str
    accountId: str
    vipLabel: str
    vipExpireAt: str


@dataclass(frozen=True)
class ShareEntry:
    fid: str
    name: str
    size: int
    isDir: bool
    shareFidToken: str
    relativePath: str
    pdirFid: str = "0"


@dataclass(frozen=True)
class ShareTree:
    title: str
    stoken: str
    pwdId: str
    files: list[ShareEntry]


@dataclass(frozen=True)
class DriveEntry:
    fid: str
    name: str
    size: int
    isDir: bool


@dataclass(frozen=True)
class DownloadLink:
    fid: str
    url: str


@dataclass(frozen=True)
class SaveResult:
    taskId: str
    savedFids: list[str]
    folderFid: str
    savedFidBySource: dict[str, str]


@dataclass(frozen=True)
class QrToken:
    token: str
    requestId: str
    loginUrl: str


@dataclass(frozen=True)
class QrPoll:
    kind: str
    message: str = ""
    serviceTicket: str = ""


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: dict
    cookie: str


def readConfigCookie() -> str:
    from .config import quarkConfig
    return str(quarkConfig.cookie.value or "")


def toCookie(raw: str) -> str:
    text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
    parts: list[str] = []
    seen: set[str] = set()
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def toMaskedCookie(raw: str) -> str:
    cookie = toCookie(raw)
    if not cookie:
        return ""
    masked: list[str] = []
    for part in cookie.split("; "):
        name, value = part.split("=", 1)
        hidden = "****" if len(value) <= 4 else f"{value[:2]}****{value[-2:]}"
        masked.append(f"{name}={hidden}")
    return "; ".join(masked)


def mergeCookies(current: str, incoming: str) -> str:
    values: dict[str, str] = {}
    order: list[str] = []
    for raw in (current, incoming):
        for part in raw.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name or not value:
                continue
            if name not in values:
                order.append(name)
            values[name] = value
    return "; ".join(f"{name}={values[name]}" for name in order)


def loggedOutProfile() -> QuarkProfile:
    return QuarkProfile(False, "", "", "", "")


def toVipLabel(memberType: str) -> str:
    labels = {
        "SUPER_VIP": "超级会员",
        "SVIP": "超级会员",
        "VIP": "会员",
        "NORMAL": "普通用户",
    }
    return labels.get(memberType.strip().upper(), memberType.strip())


def toProfile(data: dict) -> QuarkProfile:
    nickname = str(data.get("nickname") or "").strip()
    if not nickname:
        return loggedOutProfile()
    accountId = str(data.get("uid") or data.get("user_id") or data.get("origin_uid") or "")
    memberType = str(data.get("member_type") or data.get("vip_type") or "")
    expire = data.get("super_vip_exp_at")
    if expire is None:
        expire = data.get("vip_exp_at")
    return QuarkProfile(
        isLoggedIn=True,
        nickname=nickname,
        accountId=accountId,
        vipLabel=toVipLabel(memberType) if memberType else "",
        vipExpireAt="" if expire is None else str(expire),
    )


def mergeMember(profile: QuarkProfile, data: dict) -> QuarkProfile:
    memberType = str(data.get("member_type") or data.get("vip_type") or "")
    expire = data.get("super_vip_exp_at")
    if expire is None:
        expire = data.get("vip_exp_at")
    vipLabel = toVipLabel(memberType) if memberType else (profile.vipLabel or "普通用户")
    return QuarkProfile(
        isLoggedIn=profile.isLoggedIn,
        nickname=profile.nickname,
        accountId=profile.accountId,
        vipLabel=vipLabel,
        vipExpireAt=profile.vipExpireAt if expire is None else str(expire),
    )


def toCodeNumber(code: object) -> int | None:
    try:
        return int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def sanitize(text: str) -> str:
    return text.replace("{", "(").replace("}", ")")


def toKnownDriveError(
    codeNumber: int | None, message: str, httpStatus: int = 200,
) -> TaskError | None:
    lowered = message.lower()
    if httpStatus in (401, 403) or codeNumber in AUTH_CODES or "未登录" in message or "not login" in lowered:
        return TaskError("夸克账号未登录或 Cookie 已失效，请在设置中重新登录", kind="auth")
    if httpStatus == 429 or codeNumber in RATE_LIMIT_CODES or any(word in lowered for word in RATE_LIMIT_WORDS):
        return TaskError("夸克网盘请求过于频繁，请稍后再试", kind="rate_limit")
    if codeNumber in CAPACITY_CODES or any(word in lowered for word in CAPACITY_WORDS):
        return TaskError("夸克网盘空间不足，请清理网盘文件、临时目录或回收站后重试", kind="capacity")
    if codeNumber in SAVE_LIMIT_CODES or any(word in lowered for word in SAVE_LIMIT_WORDS):
        return TaskError("超出夸克网盘单次转存文件数量限制，请分批勾选后重试", kind="save_limit")
    if codeNumber in PASSCODE_CODES or "提取码" in message:
        return TaskError("提取码错误，请核对后再试", kind="passcode")
    if codeNumber in EXPIRED_CODES or any(word in message for word in EXPIRED_WORDS):
        return TaskError("分享已失效或已取消", kind="expired")
    return None


def toTaskFailureReason(data: dict, payload: dict) -> str:
    for candidate in (
        data.get("message"),
        data.get("err_msg"),
        data.get("error_msg"),
        payload.get("message"),
        data.get("task_title"),
    ):
        text = str(candidate or "").strip()
        if text and text.lower() != "ok":
            return text
    return ""


def toDriveError(payload: dict, httpStatus: int) -> TaskError | None:
    code = payload.get("code", 0)
    message = str(payload.get("message") or "").strip()
    codeNumber = toCodeNumber(code)
    if httpStatus < 400 and code in (0, "0", None, "OK") and payload.get("status", 200) in (200, "200", None):
        return None
    known = toKnownDriveError(codeNumber, message, httpStatus)
    if known is not None:
        return known
    if httpStatus >= 400:
        if code not in (0, "0", None, ""):
            return TaskError(
                "夸克网盘接口返回错误（{status}，code={code}）：{detail}",
                status=httpStatus, code=code, detail=sanitize(message or "无详情"),
            )
        if message:
            return TaskError(
                "夸克网盘接口返回错误（{status}）：{detail}",
                status=httpStatus, detail=sanitize(message),
            )
        return TaskError("夸克网盘接口返回错误（{status}）", status=httpStatus)
    detail = sanitize(message or f"错误码 {code}")
    return TaskError("夸克网盘请求失败：{detail}", detail=detail)


def toInt(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def isDirectory(item: dict) -> bool:
    return bool(item.get("dir"))


def toShareEntry(item: dict, relativePath: str, pdirFid: str) -> ShareEntry:
    return ShareEntry(
        fid=str(item.get("fid") or ""),
        name=str(item.get("file_name") or ""),
        size=toInt(item.get("size")),
        isDir=False,
        shareFidToken=str(item.get("share_fid_token") or ""),
        relativePath=relativePath,
        pdirFid=pdirFid or "0",
    )


def groupShareEntries(files: list[ShareEntry]) -> list[tuple[str, list[ShareEntry]]]:
    groups: dict[str, list[ShareEntry]] = {}
    order: list[str] = []
    for entry in files:
        pdirFid = entry.pdirFid or "0"
        if pdirFid not in groups:
            order.append(pdirFid)
            groups[pdirFid] = []
        groups[pdirFid].append(entry)
    return [(pdirFid, groups[pdirFid]) for pdirFid in order]


def mapSavedFids(
    group: list[ShareEntry],
    ordered: list[str] | None,
    entries: list[DriveEntry],
    used: set[str],
) -> dict[str, str]:
    if ordered is not None and len(ordered) == len(group):
        return {entry.fid: fid for entry, fid in zip(group, ordered, strict=True)}
    pool = [entry for entry in entries if not entry.isDir and entry.fid not in used]
    mapped: dict[str, str] = {}
    for source in group:
        found = next((
            item for item in pool
            if item.name == source.name and item.size == source.size and item.fid not in mapped.values()
        ), None)
        if found is None:
            raise TaskError("转存完成，但未能对应文件 {name}", name=source.name)
        mapped[source.fid] = found.fid
    return mapped


def toDriveEntry(item: dict) -> DriveEntry:
    return DriveEntry(
        fid=str(item.get("fid") or ""),
        name=str(item.get("file_name") or ""),
        size=toInt(item.get("size")),
        isDir=isDirectory(item),
    )


def membersOf(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    members = data.get("members")
    return members if isinstance(members, dict) else {}


def toQrPoll(payload: dict) -> QrPoll:
    status = toCodeNumber(payload.get("status"))
    members = membersOf(payload)
    if status == QR_API_SUCCESS:
        ticket = str(members.get("service_ticket") or "")
        if not ticket:
            return QrPoll("expired")
        return QrPoll("success", serviceTicket=ticket)
    if status == QR_API_EXPIRED:
        return QrPoll("expired")
    nickname = str(members.get("nickname") or "").strip()
    if status == QR_API_SCANNED or (status == QR_API_WAITING and nickname):
        return QrPoll("scanned", nickname)
    if status == QR_API_WAITING:
        return QrPoll("waiting")
    return QrPoll("expired")


def buildQrLoginUrl(token: str) -> str:
    query = urlencode({
        "token": token,
        "client_id": QR_CLIENT_ID,
        "ssb": "weblogin",
        "uc_param_str": "",
        "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
    })
    return f"{QR_LOGIN_PAGE}?{query}"


def headerText(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        return value.tobytes().decode("utf-8", "ignore")
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value)


def setCookieHeaders(headers) -> list[str]:
    rawValues = []
    getAll = getattr(headers, "get_all", None)
    if getAll is not None:
        rawValues = list(getAll("set-cookie") or [])
    if not rawValues:
        single = headers.get("set-cookie")
        if single is not None:
            rawValues = [single]
    return [headerText(value) for value in rawValues]


def cookiesFromResponse(response) -> str:
    pairs: list[str] = []
    seen: set[str] = set()

    def add(name: str, value: str) -> None:
        name, value = name.strip(), value.strip()
        if not name or not value or name in seen:
            return
        seen.add(name)
        pairs.append(f"{name}={value}")

    for cookie in response.cookies:
        add(str(cookie.name), str(cookie.value))
    if pairs:
        return "; ".join(pairs)
    for raw in setCookieHeaders(response.headers):
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        add(name, value)
    return "; ".join(pairs)


class QuarkClient:
    def __init__(
        self,
        clientFactory: Callable[..., object] | None = None,
        readCookie: Callable[[], str] | None = None,
        driveBase: str = DRIVE_BASE,
        panBase: str = PAN_BASE,
        authBase: str = AUTH_BASE,
        pageSize: int = PAGE_SIZE,
        saveBatchSize: int = SAVE_BATCH_SIZE,
        onCookieUpdated: Callable[[str], None] | None = None,
    ) -> None:
        self._clientFactory = clientFactory or buildClient
        self._readCookie = readCookie or readConfigCookie
        self.driveBase = driveBase.rstrip("/")
        self.panBase = panBase.rstrip("/")
        self.authBase = authBase.rstrip("/")
        self.pageSize = pageSize
        self.saveBatchSize = max(1, saveBatchSize)
        self.onCookieUpdated = onCookieUpdated
        self._cookie = ""
        self._baseCookie = ""
        self._tempFolderLock = asyncio.Lock()

    def driveQuery(self, extra: dict | None = None) -> dict:
        return {"pr": "ucpro", "fr": "pc", "uc_param_str": "", **(extra or {})}

    def currentCookie(self) -> str:
        configured = self._readConfigured()
        if self._cookie and configured in (self._baseCookie, self._cookie):
            return self._cookie
        return configured

    def _readConfigured(self) -> str:
        try:
            return self._readCookie() or ""
        except Exception:
            return ""

    def clearCookie(self) -> None:
        self._cookie = ""
        self._baseCookie = ""

    async def fetchAccount(self) -> QuarkProfile:
        result = await self.requestRaw(
            "GET", f"{self.panBase}/account/info", query={"fr": "pc", "platform": "pc"},
        )
        if result.status in (401, 403):
            return loggedOutProfile()
        data = result.payload.get("data")
        if not isinstance(data, dict):
            return loggedOutProfile()
        profile = toProfile(data)
        if not profile.isLoggedIn or profile.vipLabel:
            return profile
        member = await self.requestRaw("GET", f"{self.driveBase}/member", query=self.driveQuery())
        if member.status >= 400 or not isinstance(member.payload.get("data"), dict):
            return profile
        return mergeMember(profile, member.payload["data"])

    async def fetchShareTree(self, pwdId: str, passcode: str = "") -> ShareTree:
        payload = await self.requestJson(
            "POST", f"{self.driveBase}/share/sharepage/token",
            query=self.driveQuery(),
            body={"pwd_id": pwdId, "passcode": passcode or ""},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        stoken = str(data.get("stoken") or "")
        if not stoken:
            raise TaskError("无法获取分享访问凭证")
        files: list[ShareEntry] = []
        await self.collectShareFiles(pwdId, stoken, "0", "", files)
        return ShareTree(
            title=str(data.get("title") or ""),
            stoken=stoken,
            pwdId=pwdId,
            files=files,
        )

    async def collectShareFiles(
        self, pwdId: str, stoken: str, pdirFid: str, parentPath: str, files: list[ShareEntry],
    ) -> None:
        items = await self.fetchPaged(
            f"{self.driveBase}/share/sharepage/detail",
            self.driveQuery({
                "pwd_id": pwdId,
                "stoken": stoken,
                "pdir_fid": pdirFid,
                "_fetch_share": 1,
                "_fetch_total": 1,
                "force": 0,
            }),
        )
        for item in items:
            name = str(item.get("file_name") or "")
            relativePath = f"{parentPath}/{name}" if parentPath else name
            if isDirectory(item):
                fid = str(item.get("fid") or "")
                if fid:
                    await self.collectShareFiles(pwdId, stoken, fid, relativePath, files)
                continue
            files.append(toShareEntry(item, relativePath, pdirFid))

    async def fetchDriveFiles(self, pdirFid: str = "0") -> list[DriveEntry]:
        items = await self.fetchPaged(
            f"{self.driveBase}/file/sort",
            self.driveQuery({
                "pdir_fid": pdirFid,
                "_sort": "file_type:asc,file_name:asc",
                "_fetch_total": 1,
            }),
        )
        return [toDriveEntry(item) for item in items]

    async def fetchDriveTree(self, entries: list[DriveEntry]) -> list[tuple[DriveEntry, str]]:
        tree: list[tuple[DriveEntry, str]] = []
        seen: set[str] = set()
        await self.collectDriveEntries(entries, "", seen, tree)
        return tree

    async def collectDriveEntries(
        self,
        entries: list[DriveEntry],
        parentPath: str,
        seen: set[str],
        tree: list[tuple[DriveEntry, str]],
    ) -> None:
        for entry in entries:
            if entry.fid:
                if entry.fid in seen:
                    continue
                seen.add(entry.fid)
            relativePath = f"{parentPath}/{entry.name}" if parentPath else entry.name
            if entry.isDir:
                if entry.fid:
                    children = await self.fetchDriveFiles(entry.fid)
                    await self.collectDriveEntries(children, relativePath, seen, tree)
                continue
            tree.append((entry, relativePath))

    async def ensureTempFolder(self, taskId: str) -> str:
        async with self._tempFolderLock:
            root = await self.fetchDriveFiles("0")
            tempRoot = next((entry for entry in root if entry.isDir and entry.name == TEMP_FOLDER_NAME), None)
            tempRootFid = tempRoot.fid if tempRoot is not None else await self.createFolder("0", TEMP_FOLDER_NAME)
            children = await self.fetchDriveFiles(tempRootFid)
            existing = next((entry for entry in children if entry.isDir and entry.name == taskId), None)
            if existing is not None:
                return existing.fid
            return await self.createFolder(tempRootFid, taskId)

    async def fetchTempFolders(self) -> list[DriveEntry]:
        root = await self.fetchDriveFiles("0")
        tempRoot = next((entry for entry in root if entry.isDir and entry.name == TEMP_FOLDER_NAME), None)
        if tempRoot is None:
            return []
        children = await self.fetchDriveFiles(tempRoot.fid)
        return [entry for entry in children if entry.isDir]

    async def createFolder(self, pdirFid: str, name: str) -> str:
        payload = await self.requestJson(
            "POST", f"{self.driveBase}/file", query=self.driveQuery(), body={
                "pdir_fid": pdirFid,
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": False,
            },
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        fid = str(data.get("fid") or "")
        if not fid:
            raise TaskError("创建网盘目录失败")
        return fid

    async def saveShareFiles(
        self,
        pwdId: str,
        stoken: str,
        files: list[ShareEntry],
        taskId: str,
        folderFid: str = "",
        *,
        onFolderReady: Callable[[str], None] | None = None,
        onBatchSaved: Callable[[dict[str, str]], None] | None = None,
        usedFids: set[str] | None = None,
    ) -> SaveResult:
        if not files:
            raise TaskError("没有可转存的文件")
        if not folderFid:
            folderFid = await self.ensureTempFolder(taskId)
        if onFolderReady is not None:
            onFolderReady(folderFid)
        savedFidBySource: dict[str, str] = {}
        used: set[str] = set(usedFids or ())
        remoteTaskId = ""
        for pdirFid, group in groupShareEntries(files):
            for offset in range(0, len(group), self.saveBatchSize):
                batch = group[offset:offset + self.saveBatchSize]
                payload = await self.requestJson(
                    "POST", f"{self.driveBase}/share/sharepage/save",
                    query=self.driveQuery(),
                    body={
                        "fid_list": [entry.fid for entry in batch],
                        "fid_token_list": [entry.shareFidToken for entry in batch],
                        "to_pdir_fid": folderFid,
                        "pwd_id": pwdId,
                        "stoken": stoken,
                        "pdir_fid": pdirFid,
                        "scene": "link",
                    },
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                remoteTaskId = str(data.get("task_id") or "")
                if not remoteTaskId:
                    raise TaskError("转存失败：未返回任务编号")
                ordered, listed = await self.pollSaveTask(remoteTaskId, folderFid)
                if ordered is not None and len(ordered) != len(batch):
                    ordered = None
                    listed = await self.fetchDriveFiles(folderFid)
                mapped = mapSavedFids(batch, ordered, listed, used)
                savedFidBySource.update(mapped)
                used.update(mapped.values())
                if onBatchSaved is not None:
                    onBatchSaved(mapped)
        return SaveResult(
            taskId=remoteTaskId,
            savedFids=[savedFidBySource[entry.fid] for entry in files],
            folderFid=folderFid,
            savedFidBySource=savedFidBySource,
        )

    async def pollSaveTask(self, remoteTaskId: str, folderFid: str) -> tuple[list[str] | None, list[DriveEntry]]:
        for index in range(SAVE_POLL_LIMIT):
            payload = await self.requestJson(
                "GET", f"{self.driveBase}/task",
                query=self.driveQuery({"task_id": remoteTaskId, "retry_index": index}),
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            status = toInt(data.get("status"))
            if status == TASK_STATUS_DONE:
                saved = data.get("save_as") if isinstance(data.get("save_as"), dict) else {}
                fids = saved.get("save_as_top_fids") if isinstance(saved, dict) else None
                if isinstance(fids, list) and fids:
                    return [str(fid) for fid in fids], []
                entries = await self.fetchDriveFiles(folderFid)
                return None, entries
            if status == TASK_STATUS_FAILED:
                reason = toTaskFailureReason(data, payload)
                codeNumber = toCodeNumber(data.get("code") or payload.get("code"))
                known = toKnownDriveError(codeNumber, reason)
                if known is not None:
                    raise known
                raise TaskError("转存失败：{detail}", detail=sanitize(reason or "任务失败"))
            await asyncio.sleep(TASK_POLL_INTERVAL)
        raise TaskError("转存超时，请稍后重试")

    async def fetchDownloadUrls(self, fids: list[str]) -> dict[str, DownloadLink]:
        if not fids:
            return {}
        payload = await self.requestJson(
            "POST", f"{self.driveBase}/file/download",
            query=self.driveQuery(),
            body={"fids": [str(fid) for fid in fids]},
        )
        rows = payload.get("data")
        if isinstance(rows, dict):
            rows = rows.get("list") or []
        if not isinstance(rows, list):
            rows = []
        links: dict[str, DownloadLink] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            fid = str(row.get("fid") or "")
            url = str(row.get("download_url") or "")
            if fid and url:
                links[fid] = DownloadLink(fid=fid, url=url)
        return links

    async def deleteRemote(self, fids: list[str]) -> None:
        if not fids:
            return
        if any(str(fid) == "0" for fid in fids):
            raise TaskError("拒绝删除网盘根目录")
        await self.requestJson(
            "POST", f"{self.driveBase}/file/delete",
            query=self.driveQuery(),
            body={
                "action_type": 2,
                "filelist": [str(fid) for fid in fids],
                "exclude_fids": [],
            },
        )

    async def fetchQrToken(self) -> QrToken:
        requestId = str(uuid4())
        result = await self.requestRaw(
            "GET", f"{self.authBase}/cas/ajax/getTokenForQrcodeLogin",
            query={"client_id": QR_CLIENT_ID, "v": QR_VERSION, "request_id": requestId},
            sendCookie=False,
        )
        members = membersOf(result.payload)
        token = str(members.get("token") or "")
        if result.status >= 400 or toCodeNumber(result.payload.get("status")) != QR_API_SUCCESS or not token:
            raise TaskError("获取登录二维码失败")
        return QrToken(token=token, requestId=requestId, loginUrl=buildQrLoginUrl(token))

    async def pollQr(self, token: QrToken) -> QrPoll:
        result = await self.requestRaw(
            "GET", f"{self.authBase}/cas/ajax/getServiceTicketByQrcodeToken",
            query={
                "client_id": QR_CLIENT_ID,
                "v": QR_VERSION,
                "request_id": token.requestId,
                "token": token.token,
            },
            sendCookie=False,
        )
        if result.status >= 400:
            raise TaskError("查询扫码状态失败")
        return toQrPoll(result.payload)

    async def exchangeQrTicket(self, serviceTicket: str) -> str:
        result = await self.requestRaw(
            "GET", f"{self.panBase}/account/info",
            query={"st": serviceTicket, "fr": "pc", "platform": "pc", "lw": "scan"},
            sendCookie=False,
        )
        if result.status >= 400:
            raise TaskError("登录已确认，但未能取得 Cookie")
        return toCookie(result.cookie)

    async def fetchPaged(self, url: str, query: dict) -> list[dict]:
        items: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            payload = await self.requestJson(url=url, method="GET", query={
                **query, "_page": page, "_size": self.pageSize,
            })
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            pageItems = data.get("list") if isinstance(data.get("list"), list) else []
            pageItems = [item for item in pageItems if isinstance(item, dict)]
            items.extend(pageItems)
            if self.isLastPage(payload, pageItems, page):
                return items
            page += 1
        raise TaskError("夸克网盘目录分页超过上限")

    def isLastPage(self, payload: dict, pageItems: list, page: int) -> bool:
        if len(pageItems) < self.pageSize:
            return True
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        total = toInt(metadata.get("_total"))
        if total <= 0:
            return False
        return page * self.pageSize >= total

    async def requestJson(
        self, method: str, url: str, *, query: dict | None = None, body: dict | None = None,
    ) -> dict:
        result = await self.requestRaw(method, url, query=query, body=body)
        error = toDriveError(result.payload, result.status)
        if error is not None:
            raise error
        return result.payload

    def _mergeResponseCookie(self, cookie: str, incoming: str) -> None:
        if not cookie or not incoming:
            return
        merged = mergeCookies(cookie, incoming)
        if merged == cookie:
            return
        configured = self._readConfigured()
        if not self._cookie or configured not in (self._baseCookie, self._cookie):
            self._baseCookie = configured
        self._cookie = merged
        if self.onCookieUpdated is not None:
            self.onCookieUpdated(merged)

    async def requestRaw(
        self,
        method: str,
        url: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        sendCookie: bool = True,
    ) -> HttpResult:
        cookie = self.currentCookie() if sendCookie else ""
        headers = {
            "referer": PAN_REFERER,
            "origin": PAN_ORIGIN,
            "accept": "application/json",
        }
        if cookie:
            headers["cookie"] = cookie
        logger.debug("夸克请求 {} {} cookie={}", method, urlsplit(url).path, toMaskedCookie(cookie) or "无")
        client = None
        response = None
        try:
            client = self._clientFactory(headers=headers, userAgent=QUARK_PC_UA)
            queryText = {str(key): str(value) for key, value in (query or {}).items()}
            if method == "GET":
                response = await client.get(url, query=queryText)
            else:
                response = await client.post(url, query=queryText, json=body if body is not None else {})
            status = response.status.as_int()
            try:
                payload = await response.json()
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            incoming = cookiesFromResponse(response)
            if sendCookie:
                self._mergeResponseCookie(cookie, incoming)
            if status >= 400:
                message = str(payload.get("message") or "")
                if cookie and cookie in message:
                    message = message.replace(cookie, toMaskedCookie(cookie))
                logger.warning(
                    "夸克接口异常 {} {} status={} code={} msg={}",
                    method, urlsplit(url).path, status, payload.get("code", ""), message,
                )
            return HttpResult(status=status, payload=payload, cookie=incoming)
        except asyncio.CancelledError:
            raise
        except TaskError:
            raise
        except Exception as error:
            logger.warning(
                "夸克网盘请求失败 {} cookie={}", type(error).__name__, toMaskedCookie(cookie) or "无",
            )
            if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
                raise TaskError("夸克网盘请求超时，请检查网络或代理设置后重试") from None
            raise TaskError("夸克网盘网络连接失败，请检查网络或代理设置后重试") from None
        finally:
            if response is not None:
                close = response.close()
                if asyncio.iscoroutine(close):
                    await close
            if client is not None:
                client.close()
