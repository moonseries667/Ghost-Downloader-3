from __future__ import annotations

import re
from dataclasses import dataclass

# 分享文案里提取码跟在链接后面，超过这个窗口就视为另一条分享的提取码
PASSCODE_WINDOW = 80

SHARE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?pan\.quark\.cn/s/(?P<pwdId>[A-Za-z0-9]+)(?P<tail>[^\s]*)",
    re.IGNORECASE,
)
CODE_PATTERN = re.compile(r"提取码\s*[:：]?\s*([A-Za-z0-9]{4})", re.IGNORECASE)
PWD_PATTERN = re.compile(r"[?&]pwd=([A-Za-z0-9]{4})", re.IGNORECASE)


@dataclass(frozen=True)
class QuarkShareRef:
    pwdId: str
    passcode: str


def parseShareUrl(url: str) -> QuarkShareRef | None:
    try:
        text = str(url or "").strip()
        if not text or any(char.isspace() for char in text):
            return None
        match = SHARE_URL_PATTERN.fullmatch(text)
        if match is None:
            return None
        return QuarkShareRef(match.group("pwdId"), passcodeInText(match.group("tail")))
    except Exception:
        return None


def extractShareEntries(text: str) -> list[QuarkShareRef]:
    try:
        source = str(text or "")
        if not source:
            return []
        matches = list(SHARE_URL_PATTERN.finditer(source))
        found: list[QuarkShareRef] = []
        seen: set[str] = set()
        for index, match in enumerate(matches):
            pwdId = match.group("pwdId")
            if pwdId in seen:
                continue
            passcode = passcodeInText(match.group("tail"))
            if not passcode:
                windowEnd = match.end() + PASSCODE_WINDOW
                if index + 1 < len(matches):
                    windowEnd = min(windowEnd, matches[index + 1].start())
                passcode = passcodeInText(source[match.end():windowEnd])
            seen.add(pwdId)
            found.append(QuarkShareRef(pwdId, passcode))
        return found
    except Exception:
        return []


def toCanonicalShareUrl(ref: QuarkShareRef) -> str:
    url = f"https://pan.quark.cn/s/{ref.pwdId}"
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
