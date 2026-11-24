from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from app.config.paths import APP_DATA_DIR
from .parse import MediaBundle

INBOX_STATUSES = ("new", "queued", "completed", "skipped")


@dataclass
class InboxBundle:
    bundleKey: str
    firstMessageId: int
    lastMessageId: int
    date: int
    caption: str
    chatId: str = ""
    fileNames: list[str] = field(default_factory=list)
    fileSizes: list[int] = field(default_factory=list)
    totalSize: int = 0
    groupedId: str = ""
    status: str = "new"
    messageIds: list[int] = field(default_factory=list)
    rawMessages: list[dict] = field(default_factory=list)

    @classmethod
    def fromBundle(cls, bundle: MediaBundle) -> InboxBundle:
        return cls(
            bundleKey=bundle.bundleKey,
            firstMessageId=bundle.firstMessageId,
            lastMessageId=bundle.lastMessageId,
            date=bundle.date,
            caption=bundle.caption,
            chatId=bundle.chatId,
            fileNames=[item.fileName for item in bundle.items],
            fileSizes=[item.size for item in bundle.items],
            totalSize=bundle.totalSize,
            groupedId=bundle.groupedId,
            messageIds=[item.messageId for item in bundle.items],
            rawMessages=[dict(item.rawMessage) for item in bundle.items],
        )

    @classmethod
    def fromDict(cls, data: dict) -> InboxBundle:
        status = str(data["status"])
        if status not in INBOX_STATUSES:
            raise ValueError(f"未知收件箱状态: {status}")
        bundleKey = str(data["bundleKey"])
        chatId = str(data.get("chatId") or bundleKey.partition(":")[0])
        return cls(
            bundleKey=bundleKey,
            firstMessageId=int(data["firstMessageId"]),
            lastMessageId=int(data["lastMessageId"]),
            date=int(data["date"]),
            caption=str(data["caption"]),
            chatId=chatId,
            fileNames=[str(name) for name in data.get("fileNames") or []],
            fileSizes=[int(size) for size in data.get("fileSizes") or []],
            totalSize=int(data.get("totalSize", 0)),
            groupedId=str(data.get("groupedId") or ""),
            status=status,
            messageIds=[int(messageId) for messageId in data.get("messageIds") or []],
            rawMessages=[dict(message) for message in data.get("rawMessages") or []],
        )

    def toDict(self) -> dict:
        return {
            "bundleKey": self.bundleKey,
            "firstMessageId": self.firstMessageId,
            "lastMessageId": self.lastMessageId,
            "date": self.date,
            "caption": self.caption,
            "chatId": self.chatId,
            "fileNames": self.fileNames,
            "fileSizes": self.fileSizes,
            "totalSize": self.totalSize,
            "groupedId": self.groupedId,
            "status": self.status,
            "messageIds": self.messageIds,
            "rawMessages": self.rawMessages,
        }


class TelegramInbox:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else Path(APP_DATA_DIR) / "telegram_inbox.json"
        self._chatId = ""
        self._title = ""
        self._lastMsgId = 0
        self._bundles: list[InboxBundle] = []

    @property
    def chatId(self) -> str:
        return self._chatId

    @property
    def title(self) -> str:
        return self._title

    @property
    def lastMsgId(self) -> int:
        return self._lastMsgId

    @property
    def bundles(self) -> list[InboxBundle]:
        return list(self._bundles)

    def toDict(self) -> dict:
        return {
            "chatId": self._chatId,
            "title": self._title,
            "lastMsgId": self._lastMsgId,
            "bundles": [bundle.toDict() for bundle in self._bundles],
        }

    def load(self) -> None:
        if not self.path.exists():
            self._clearState()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("收件箱 JSON 顶层不是对象")
            bundles = [InboxBundle.fromDict(item) for item in data.get("bundles") or []]
            chatId = str(data.get("chatId", ""))
            title = str(data.get("title", ""))
            lastMsgId = int(data.get("lastMsgId", 0))
        except (OSError, ValueError, TypeError, KeyError) as error:
            logger.warning("读取 Telegram 收件箱失败, 已清空为空状态: {}", error)
            self._clearState()
            return
        self._chatId = chatId
        self._title = title
        self._lastMsgId = lastMsgId
        self._bundles = bundles

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self.toDict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def setChannel(self, chatId: str, title: str) -> None:
        chatId = str(chatId)
        if chatId != self._chatId:
            self._chatId = chatId
            self._title = str(title)
            self._lastMsgId = 0
            self._bundles = []
            return
        self._title = str(title)

    def mergeBundles(self, bundles: list[MediaBundle]) -> list[InboxBundle]:
        known = {bundle.bundleKey for bundle in self._bundles}
        added: list[InboxBundle] = []
        highest = self._lastMsgId
        for bundle in bundles:
            highest = max(highest, bundle.lastMessageId)
            if bundle.bundleKey in known:
                continue
            inboxBundle = InboxBundle.fromBundle(bundle)
            known.add(bundle.bundleKey)
            self._bundles.append(inboxBundle)
            added.append(inboxBundle)
        self._lastMsgId = highest
        self._bundles.sort(key=lambda bundle: bundle.firstMessageId)
        return added

    def markStatus(self, bundleKey: str, status: str) -> bool:
        if status not in INBOX_STATUSES:
            return False
        for bundle in self._bundles:
            if bundle.bundleKey == bundleKey:
                bundle.status = status
                return True
        return False

    def pendingBundles(self) -> list[InboxBundle]:
        return [bundle for bundle in self._bundles if bundle.status == "new"]

    def bundleByKey(self, key: str) -> InboxBundle | None:
        for bundle in self._bundles:
            if bundle.bundleKey == key:
                return bundle
        return None

    def _clearState(self) -> None:
        self._chatId = ""
        self._title = ""
        self._lastMsgId = 0
        self._bundles = []
