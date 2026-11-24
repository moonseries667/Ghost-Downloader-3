from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlsplit

from app.platform.filesystem import toSafeFilename

TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
INVITE_SEGMENTS = {"+", "joinchat"}
PREVIEW_SEGMENT = "s"
BUNDLE_NAME_LIMIT = 48


@dataclass(frozen=True)
class TelegramLink:
    url: str
    kind: str
    chat: str
    messageId: int
    topicId: int | None = None
    isSingle: bool = False


@dataclass(frozen=True)
class ExportedMediaItem:
    messageId: int
    fileName: str
    size: int
    groupedId: str
    date: int
    text: str
    rawMessage: dict


@dataclass(frozen=True)
class MediaBundle:
    bundleKey: str
    chatId: str
    groupedId: str
    firstMessageId: int
    lastMessageId: int
    date: int
    caption: str
    items: tuple[ExportedMediaItem, ...]
    totalSize: int


def parseTelegramUrl(url: str) -> TelegramLink | None:
    """解析 Telegram 消息链接，无法识别时返回 None。

    论坛话题链接单独标为 "topic"（公开频道）；私密论坛话题仍归 "private" 并携带 topicId，
    以便调用方只用 kind 区分公开与私密，再用 topicId 判断是否话题。
    """
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if parts.netloc.lower() not in TELEGRAM_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    head = segments[0].lower()
    if head in INVITE_SEGMENTS or head.startswith("+") or head == PREVIEW_SEGMENT:
        return None
    match = matchTelegramPath(segments)
    if match is None:
        return None
    kind, chat, topicId, messageId = match
    query = parse_qs(parts.query, keep_blank_values=True)
    link = TelegramLink(
        url="",
        kind=kind,
        chat=chat,
        messageId=messageId,
        topicId=topicId,
        isSingle="single" in query,
    )
    return replace(link, url=toCanonicalTelegramUrl(link))


def matchTelegramPath(segments: list[str]) -> tuple[str, str, int | None, int] | None:
    if segments[0].lower() == "c":
        if len(segments) not in (3, 4) or not isNumber(segments[1]):
            return None
        kind, chat, rest = "private", segments[1], segments[2:]
    else:
        if len(segments) not in (2, 3):
            return None
        kind, chat, rest = ("public" if len(segments) == 2 else "topic"), segments[0], segments[1:]
    if not all(isNumber(part) for part in rest):
        return None
    ids = [int(part) for part in rest]
    if any(value <= 0 for value in ids):
        return None
    return kind, chat, (ids[0] if len(ids) == 2 else None), ids[-1]


def isNumber(text: str) -> bool:
    return text.isascii() and text.isdigit()


def toCanonicalTelegramUrl(link: TelegramLink) -> str:
    prefix = "https://t.me/c" if link.kind == "private" else "https://t.me"
    path = f"{prefix}/{link.chat}"
    if link.topicId is not None:
        path += f"/{link.topicId}"
    path += f"/{link.messageId}"
    return f"{path}?single" if link.isSingle else path


def groupExportedMessages(payload: dict | list) -> list[MediaBundle]:
    """把 tdl 导出 JSON 聚合为媒体组，坏数据项一律跳过。"""
    chatId, messages = splitExportPayload(payload)
    groups: dict[str, list[ExportedMediaItem]] = {}
    for message in messages:
        item = parseExportedMessage(message)
        if item is None:
            continue
        if item.groupedId:
            key = f"{chatId}:g:{item.groupedId}"
        else:
            key = f"{chatId}:m:{item.messageId}"
        groups.setdefault(key, []).append(item)
    bundles = [buildBundle(key, chatId, items) for key, items in groups.items()]
    bundles.sort(key=lambda bundle: bundle.firstMessageId)
    return bundles


def splitExportPayload(payload: dict | list) -> tuple[str, list]:
    if isinstance(payload, dict):
        chatId = payload.get("id", payload.get("chatId"))
        chatText = "" if chatId is None else str(chatId)
        messages = payload.get("messages", [])
    elif isinstance(payload, list):
        chatText, messages = "", payload
    else:
        return "", []
    return chatText, messages if isinstance(messages, list) else []


def parseExportedMessage(message) -> ExportedMediaItem | None:
    if not isinstance(message, dict):
        return None
    messageId = toInt(message.get("id", message.get("ID")))
    if messageId <= 0:
        return None
    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    media = raw.get("Media") if isinstance(raw.get("Media"), dict) else {}
    fileName = firstText(message.get("file")) or documentFileName(media)
    if not fileName and not media:
        return None
    if not fileName:
        fileName = f"{messageId}{inferMediaExtension(media)}"
    return ExportedMediaItem(
        messageId=messageId,
        fileName=fileName,
        size=toInt(firstValue(message.get("size"), mediaDocument(media).get("Size"), media.get("Size"))),
        groupedId=normalizeGroupedId(message.get("grouped_id"), message.get("groupedId"), raw.get("GroupedID")),
        date=toInt(firstValue(message.get("date"), raw.get("Date"))),
        text=firstText(message.get("text"), message.get("message"), raw.get("Message")),
        rawMessage=dict(message),
    )


def mediaDocument(media: dict) -> dict:
    document = media.get("Document")
    return document if isinstance(document, dict) else {}


def documentFileName(media: dict) -> str:
    document = mediaDocument(media)
    attributes = document.get("Attributes")
    if isinstance(attributes, list):
        for attribute in attributes:
            if isinstance(attribute, dict):
                name = firstText(attribute.get("FileName"))
                if name:
                    return name
    return firstText(document.get("FileName"), media.get("FileName"))


def inferMediaExtension(media: dict) -> str:
    attributes = mediaDocument(media).get("Attributes")
    if isinstance(attributes, list):
        for attribute in attributes:
            if isinstance(attribute, dict):
                marker = str(attribute.get("_", "")).lower()
                if "video" in marker:
                    return ".mp4"
                if "audio" in marker:
                    return ".mp3"
    return ".jpg" if "Photo" in media else ""


def firstValue(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def firstText(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def toInt(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def normalizeGroupedId(*values) -> str:
    value = firstValue(*values)
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("", "0") else text


def buildBundle(bundleKey: str, chatId: str, items: list[ExportedMediaItem]) -> MediaBundle:
    ordered = sorted(items, key=lambda item: item.messageId)
    used: set[str] = set()
    uniqueItems = []
    for item in ordered:
        name = uniqueFileName(item.fileName, item.messageId, used)
        uniqueItems.append(replace(item, fileName=name))
    caption = next((item.text.strip() for item in uniqueItems if item.text.strip()), "")
    date = min((item.date for item in uniqueItems if item.date), default=0)
    return MediaBundle(
        bundleKey=bundleKey,
        chatId=chatId,
        groupedId=uniqueItems[0].groupedId,
        firstMessageId=uniqueItems[0].messageId,
        lastMessageId=uniqueItems[-1].messageId,
        date=date,
        caption=caption,
        items=tuple(uniqueItems),
        totalSize=sum(item.size for item in uniqueItems),
    )


def uniqueFileName(fileName: str, messageId: int, used: set[str]) -> str:
    base = toSafeFilename(fileName, fallback=str(messageId))
    candidate = base
    index = 2
    while candidate.casefold() in used:
        candidate = addCopySuffix(base, index)
        index += 1
    used.add(candidate.casefold())
    return candidate


def addCopySuffix(name: str, index: int) -> str:
    stem, dot, extension = name.rpartition(".")
    if stem and dot:
        return toSafeFilename(f"{stem} ({index}){dot}{extension}", fallback=name)
    return toSafeFilename(f"{name} ({index})", fallback=name)


def buildBundleName(bundle: MediaBundle) -> str:
    if len(bundle.items) == 1:
        return bundle.items[0].fileName
    caption = " ".join(bundle.caption.split())
    name = toSafeFilename(caption, fallback="")[:BUNDLE_NAME_LIMIT].rstrip() if caption else ""
    if name:
        return name
    if bundle.chatId:
        return f"tg_{bundle.chatId}_{bundle.firstMessageId}"
    return f"tg_{bundle.firstMessageId}"


def buildExportPayload(chatId: str, items: list[ExportedMediaItem]) -> dict:
    messages = []
    for item in items:
        message = dict(item.rawMessage)
        message["id"] = item.messageId
        messages.append(message)
    return {"id": toChatId(chatId), "messages": messages}


def toChatId(chatId: str):
    try:
        return int(chatId)
    except (TypeError, ValueError):
        return chatId
