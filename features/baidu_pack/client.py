from __future__ import annotations

import json
import posixpath
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote, urlencode
from uuid import uuid4

from app.client import buildClient
from app.models.task import TaskError
from .parse import BaiduShareRef, toNormalizedShareKey

PAN_BASE = "https://pan.baidu.com"
PASSPORT_BASE = "https://passport.baidu.com"
PAGE_SIZE = 100
MAX_PAGES = 100
REQUEST_TIMEOUT = 20

BAIDU_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PASSCODE_ERRNOS = {-9, -12, -62}
EXPIRED_ERRNOS = {-7, 105, 145, 2}
JSONP_WRAPPER_PATTERN = re.compile(r"^[A-Za-z0-9_$.]+\((.*)\)\s*;?\s*$", re.DOTALL)


@dataclass(frozen=True)
class BaiduShareEntry:
    fsId: str
    name: str
    size: int
    isDir: bool
    remotePath: str
    relativePath: str


@dataclass(frozen=True)
class BaiduShareTree:
    title: str
    shareKey: str
    files: list[BaiduShareEntry]


@dataclass(frozen=True)
class BaiduQrSession:
    sign: str
    gid: str
    imgUrl: str
    qrImageBytes: bytes = b""


@dataclass(frozen=True)
class BaiduQrPollResult:
    kind: str
    token: str = ""
    message: str = ""


@dataclass(frozen=True)
class HttpResponseData:
    status: int
    body: bytes
    headers: dict[str, list[str]]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="ignore")

    @property
    def cookies(self) -> str:
        parts: list[str] = []
        for name, values in self.headers.items():
            if name.lower() != "set-cookie":
                continue
            for value in values:
                pair = value.split(";", 1)[0].strip()
                if "=" in pair:
                    parts.append(pair)
        return normalizeCookie("; ".join(parts))


def normalizeCookie(raw: str) -> str:
    text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    pairs: list[str] = []
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
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def mergeCookies(current: str, incoming: str) -> str:
    values: dict[str, str] = {}
    order: list[str] = []
    for raw in (current, incoming):
        for part in str(raw or "").split(";"):
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


def extractCookieValue(cookie: str, targetName: str) -> str:
    wanted = targetName.strip().lower()
    for part in str(cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip().lower() == wanted:
            return value.strip()
    return ""


def toMaskedCookie(raw: str) -> str:
    cookie = normalizeCookie(raw)
    if not cookie:
        return ""
    masked: list[str] = []
    for part in cookie.split("; "):
        name, value = part.split("=", 1)
        hidden = "****" if len(value) <= 4 else f"{value[:2]}****{value[-2:]}"
        masked.append(f"{name}={hidden}")
    return "; ".join(masked)


def parseJsonPayload(text: str) -> dict:
    raw = str(text or "").strip()
    match = JSONP_WRAPPER_PATTERN.match(raw)
    if match is not None:
        raw = match.group(1).strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TaskError("百度接口返回格式异常：{detail}", detail=str(error)) from error
    if not isinstance(data, dict):
        raise TaskError("百度接口返回数据不是对象")
    return data


def toInt(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def toShareApiError(errno: int, message: str = "") -> TaskError:
    if errno == -62:
        return TaskError("百度分享触发安全验证，请稍后再试", kind="rate_limit")
    if errno in (-9, -12):
        return TaskError("提取码错误或需要输入提取码", kind="passcode")
    if errno in EXPIRED_ERRNOS:
        return TaskError("分享链接已失效或已取消", kind="expired")
    detail = message.strip() or f"errno={errno}"
    return TaskError("百度分享解析失败：{detail}", detail=detail)


def toGid() -> str:
    raw = uuid4().hex.upper()
    return f"{raw[:7]}-{raw[7:11]}-{raw[11:15]}-{raw[15:19]}-{raw[19:31]}"


async def defaultHttpRequest(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> HttpResponseData:
    client = buildClient(
        headers=headers,
        userAgent=BAIDU_WEB_UA,
        timeout=REQUEST_TIMEOUT,
        readTimeout=REQUEST_TIMEOUT,
    )
    try:
        if method.upper() == "POST":
            response = await client.post(url, body=body or b"")
        else:
            response = await client.get(url)
        try:
            rawBody = await response.bytes()
            headerMap: dict[str, list[str]] = {}
            rawHeaders = response.headers
            if hasattr(rawHeaders, "get_all"):
                keys = {str(k).lower() for k in rawHeaders.keys()}
                for key in keys:
                    headerMap[key] = [
                        v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                        for v in rawHeaders.get_all(key)
                    ]
            else:
                for key, value in dict(rawHeaders).items():
                    decoded = value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
                    headerMap.setdefault(str(key).lower(), []).append(decoded)
            return HttpResponseData(
                status=int(response.status.as_int() if hasattr(response.status, "as_int") else response.status),
                body=bytes(rawBody),
                headers=headerMap,
            )
        finally:
            response.close()
    finally:
        client.close()


class BaiduWebClient:
    def __init__(
        self,
        request: Callable[..., Awaitable[HttpResponseData]] | None = None,
    ) -> None:
        self._request = request if request is not None else defaultHttpRequest

    async def verifyShare(self, shareKey: str, passcode: str) -> str:
        ref = BaiduShareRef(toNormalizedShareKey(shareKey) or shareKey, passcode.strip())
        if not ref.passcode:
            return ""
        query = urlencode({
            "surl": ref.shortKey,
            "t": str(int(time.time() * 1000)),
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "clienttype": "0",
        })
        url = f"{PAN_BASE}/share/verify?{query}"
        formBody = urlencode({
            "pwd": ref.passcode,
            "vcode": "",
            "vcode_str": "",
        }).encode("utf-8")
        headers = {
            "referer": f"{PAN_BASE}/share/init?surl={ref.shortKey}",
            "origin": PAN_BASE,
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        response = await self._request("POST", url, headers=headers, body=formBody)
        if response.status >= 400:
            raise TaskError("验证百度分享提取码失败（HTTP {status}）", status=response.status)
        payload = parseJsonPayload(response.text)
        errno = toInt(payload.get("errno"))
        if errno != 0:
            raise toShareApiError(errno, str(payload.get("err_msg") or payload.get("errmsg") or ""))
        randsk = str(payload.get("randsk") or "").strip()
        if not randsk:
            randsk = extractCookieValue(response.cookies, "BDCLND")
        return randsk

    async def fetchShareList(
        self,
        shareKey: str,
        *,
        randsk: str = "",
        remoteDir: str = "",
        page: int = 1,
        num: int = PAGE_SIZE,
    ) -> tuple[list[dict], str]:
        ref = BaiduShareRef(toNormalizedShareKey(shareKey) or shareKey)
        params: dict[str, str] = {
            "shorturl": ref.shortKey,
            "page": str(page),
            "num": str(num),
            "order": "name",
            "desc": "0",
            "showempty": "0",
            "web": "1",
            "channel": "chunlei",
            "app_id": "250528",
            "clienttype": "0",
        }
        if remoteDir:
            params["dir"] = remoteDir
        else:
            params["root"] = "1"
        if randsk:
            params["sekey"] = randsk
        url = f"{PAN_BASE}/share/list?{urlencode(params)}"
        headers = {
            "referer": f"{PAN_BASE}/s/{ref.shareKey}",
            "x-requested-with": "XMLHttpRequest",
        }
        if randsk:
            headers["cookie"] = f"BDCLND={quote(randsk, safe='')}"
        response = await self._request("GET", url, headers=headers)
        if response.status >= 400:
            raise TaskError("读取百度分享文件列表失败（HTTP {status}）", status=response.status)
        payload = parseJsonPayload(response.text)
        errno = toInt(payload.get("errno"))
        if errno != 0:
            raise toShareApiError(errno, str(payload.get("err_msg") or payload.get("errmsg") or ""))
        items = payload.get("list")
        if not isinstance(items, list):
            items = []
        title = str(payload.get("title") or "").strip()
        return [item for item in items if isinstance(item, dict)], title

    async def fetchShareTree(self, shareKey: str, passcode: str = "") -> BaiduShareTree:
        normalizedKey = toNormalizedShareKey(shareKey)
        if not normalizedKey:
            raise TaskError("无法识别百度分享链接")
        randsk = ""
        if passcode.strip():
            randsk = await self.verifyShare(normalizedKey, passcode.strip())

        files: list[BaiduShareEntry] = []
        rootTitle = ""
        rootNames: list[str] = []

        async def walk(remoteDir: str, prefix: str) -> None:
            nonlocal rootTitle
            for page in range(1, MAX_PAGES + 1):
                items, title = await self.fetchShareList(
                    normalizedKey, randsk=randsk, remoteDir=remoteDir, page=page, num=PAGE_SIZE,
                )
                if not remoteDir and title and not rootTitle:
                    rootTitle = title
                if not items:
                    break
                for item in items:
                    name = str(item.get("server_filename") or "").strip()
                    if not name:
                        continue
                    remotePath = str(item.get("path") or "").strip() or posixpath.join(remoteDir or "/", name)
                    relativePath = f"{prefix}/{name}" if prefix else name
                    isDir = toInt(item.get("isdir")) == 1
                    if not remoteDir:
                        rootNames.append(name)
                    if isDir:
                        await walk(remotePath, relativePath)
                    else:
                        files.append(BaiduShareEntry(
                            fsId=str(item.get("fs_id") or "").strip(),
                            name=name,
                            size=toInt(item.get("size")),
                            isDir=False,
                            remotePath=remotePath,
                            relativePath=relativePath,
                        ))
                if len(items) < PAGE_SIZE:
                    break

        await walk("", "")
        if not files:
            raise TaskError("分享中没有可下载的文件")
        if len(files) == 1:
            resolvedTitle = files[0].name
        elif rootTitle:
            resolvedTitle = rootTitle
        elif len(rootNames) == 1:
            resolvedTitle = rootNames[0]
        else:
            resolvedTitle = f"baidu_{normalizedKey}"
        return BaiduShareTree(title=resolvedTitle, shareKey=normalizedKey, files=files)

    async def fetchQrSession(self) -> BaiduQrSession:
        gid = toGid()
        query = urlencode({
            "lp": "pc",
            "qrloginfrom": "pc",
            "gid": gid,
            "apiver": "v3",
            "tt": str(int(time.time() * 1000)),
            "tpl": "netdisk",
        })
        url = f"{PASSPORT_BASE}/v2/api/getqrcode?{query}"
        response = await self._request("GET", url, headers={"referer": f"{PAN_BASE}/"})
        if response.status >= 400:
            raise TaskError("获取百度扫码二维码失败（HTTP {status}）", status=response.status)
        payload = parseJsonPayload(response.text)
        errno = toInt(payload.get("errno"))
        if errno != 0:
            raise TaskError("获取百度扫码二维码失败（errno={errno}）", errno=errno)
        sign = str(payload.get("sign") or "").strip()
        rawImgUrl = str(payload.get("imgurl") or "").strip()
        if not sign or not rawImgUrl:
            raise TaskError("百度二维码接口未返回有效凭据")
        imgUrl = rawImgUrl if rawImgUrl.startswith(("http://", "https://")) else f"https://{rawImgUrl.lstrip('/')}"
        imageResponse = await self._request("GET", imgUrl, headers={"referer": f"{PAN_BASE}/"})
        if imageResponse.status >= 400 or not imageResponse.body:
            raise TaskError("无法加载百度扫码二维码图片，请检查网络后刷新重试")
        return BaiduQrSession(sign=sign, gid=gid, imgUrl=imgUrl, qrImageBytes=imageResponse.body)

    async def pollQrStatus(self, session: BaiduQrSession) -> BaiduQrPollResult:
        query = urlencode({
            "channel_id": session.sign,
            "tpl": "netdisk",
            "gid": session.gid,
            "callback": "",
            "apiver": "v3",
            "tt": str(int(time.time() * 1000)),
        })
        url = f"{PASSPORT_BASE}/channel/unicast?{query}"
        response = await self._request("GET", url, headers={"referer": f"{PAN_BASE}/"})
        if response.status >= 400:
            raise TaskError("查询百度扫码状态失败（HTTP {status}）", status=response.status)
        payload = parseJsonPayload(response.text)
        errno = toInt(payload.get("errno"))
        if errno == 1:
            return BaiduQrPollResult(kind="waiting")
        if errno != 0:
            return BaiduQrPollResult(kind="expired", message="二维码已失效")
        rawChannel = payload.get("channel_v")
        channelData = parseJsonPayload(rawChannel) if isinstance(rawChannel, str) else (
            rawChannel if isinstance(rawChannel, dict) else {}
        )
        status = toInt(channelData.get("status", -1))
        if status == 1:
            return BaiduQrPollResult(kind="scanned")
        if status == 0:
            token = str(channelData.get("v") or "").strip()
            if token:
                return BaiduQrPollResult(kind="confirmed", token=token)
            return BaiduQrPollResult(kind="error", message="扫码已确认，但未返回登录票据")
        return BaiduQrPollResult(kind="expired", message="二维码已取消或过期")

    async def exchangeQrCookie(self, token: str, gid: str = "") -> str:
        nowMs = str(int(time.time() * 1000))
        query = urlencode({
            "v": nowMs,
            "bduss": token,
            "u": f"{PAN_BASE}/disk/main",
            "loginVersion": "v4",
            "qrcode": "1",
            "tpl": "netdisk",
            "apiver": "v3",
            "tt": nowMs,
            "gid": gid,
        })
        loginUrl = f"{PASSPORT_BASE}/v3/login/main/qrbdusslogin?{query}"
        response = await self._request("GET", loginUrl, headers={"referer": f"{PAN_BASE}/"})
        if response.status >= 400:
            raise TaskError("交换百度扫码会话失败（HTTP {status}）", status=response.status)
        cookie = response.cookies
        if not extractCookieValue(cookie, "BDUSS"):
            payload = response.text
            bdussMatch = re.search(r'"bduss"\s*:\s*"([^"]+)"', payload, re.IGNORECASE)
            if bdussMatch:
                cookie = mergeCookies(cookie, f"BDUSS={bdussMatch.group(1)}")
        if not extractCookieValue(cookie, "BDUSS"):
            raise TaskError("扫码登录未获取到 BDUSS，请刷新二维码重试")
        # 补齐 pan.baidu.com 域下发的 STOKEN，确保转存功能可用
        if not extractCookieValue(cookie, "STOKEN"):
            try:
                panResponse = await self._request(
                    "GET",
                    f"{PAN_BASE}/disk/main",
                    headers={"cookie": cookie, "referer": f"{PAN_BASE}/"},
                )
                if panResponse.cookies:
                    cookie = mergeCookies(cookie, panResponse.cookies)
            except Exception:
                pass
        return normalizeCookie(cookie)
