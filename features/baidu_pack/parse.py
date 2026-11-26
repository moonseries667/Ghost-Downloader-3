from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# 分享文案里提取码紧跟链接出现，超过该窗口视为下一条分享的内容
PASSCODE_WINDOW = 80

SHARE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:pan|yun)\.baidu\.com/"
    r"(?:s/(?P<surl>[A-Za-z0-9_-]+)|share/init\?(?P<query>[^\s]+))(?P<tail>[^\s]*)",
    re.IGNORECASE,
)
CODE_PATTERN = re.compile(r"(?:提取码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})", re.IGNORECASE)
PWD_PATTERN = re.compile(r"[?&]pwd=([A-Za-z0-9]{4})", re.IGNORECASE)
SURL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@dataclass(frozen=True)
class BaiduShareRef:
    shareKey: str
    passcode: str = ""

    @property
    def shortKey(self) -> str:
        return self.shareKey[1:] if self.shareKey.startswith("1") and len(self.shareKey) > 1 else self.shareKey


def toNormalizedShareKey(rawKey: str) -> str:
    key = str(rawKey or "").strip().rstrip("/")
    if not key or not SURL_TOKEN_PATTERN.match(key):
        return ""
    return key if key.startswith("1") else f"1{key}"


def parseShareMatch(match: re.Match[str]) -> BaiduShareRef | None:
    surl = match.group("surl")
    tail = match.group("tail") or ""
    if surl:
        shareKey = toNormalizedShareKey(surl)
        if not shareKey:
            return None
        return BaiduShareRef(shareKey, passcodeInText(tail))

    queryText = (match.group("query") or "") + tail
    params = parse_qs(urlsplit(f"https://pan.baidu.com/share/init?{queryText}").query)
    rawSurl = (params.get("surl") or [""])[0]
    shareKey = toNormalizedShareKey(rawSurl)
    if not shareKey:
        return None
    rawPwd = (params.get("pwd") or [""])[0].strip()
    passcode = rawPwd if re.fullmatch(r"[A-Za-z0-9]{4}", rawPwd) else passcodeInText(f"?{queryText}")
    return BaiduShareRef(shareKey, passcode)


def parseShareUrl(url: str) -> BaiduShareRef | None:
    try:
        text = str(url or "").strip()
        if not text or any(char.isspace() for char in text):
            return None
        match = SHARE_URL_PATTERN.fullmatch(text)
        if match is None:
            return None
        return parseShareMatch(match)
    except Exception:
        return None


def extractShareEntries(text: str) -> list[BaiduShareRef]:
    try:
        source = str(text or "")
        if not source:
            return []
        matches = list(SHARE_URL_PATTERN.finditer(source))
        found: list[BaiduShareRef] = []
        seen: set[str] = set()
        for index, match in enumerate(matches):
            parsed = parseShareMatch(match)
            if parsed is None or parsed.shareKey in seen:
                continue
            passcode = parsed.passcode
            if not passcode:
                windowEnd = match.end() + PASSCODE_WINDOW
                if index + 1 < len(matches):
                    windowEnd = min(windowEnd, matches[index + 1].start())
                passcode = passcodeInText(source[match.end():windowEnd])
            seen.add(parsed.shareKey)
            found.append(BaiduShareRef(parsed.shareKey, passcode))
        return found
    except Exception:
        return []


def toShareBaseUrl(shareKey: str) -> str:
    normalized = toNormalizedShareKey(shareKey) or str(shareKey or "").strip()
    return f"https://pan.baidu.com/s/{normalized}"


def toCanonicalShareUrl(ref: BaiduShareRef) -> str:
    url = toShareBaseUrl(ref.shareKey)
    if ref.passcode:
        return f"{url}?pwd={ref.passcode}"
    return url


def passcodeInText(text: str) -> str:
    candidates: list[tuple[int, str]] = []
    for pattern in (PWD_PATTERN, CODE_PATTERN):
        found = pattern.search(text)
        if found is not None:
            candidates.append((found.start(), found.group(1)))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]
