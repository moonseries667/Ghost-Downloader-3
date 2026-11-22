from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.config.cfg import cfg
from app.signal import Signal
from .client import (
    QR_API_EXPIRED, QR_API_SCANNED, QR_API_WAITING, QuarkClient, QuarkProfile, loggedOutProfile,
    toCookie, toMaskedCookie,
)
from .config import quarkConfig

if TYPE_CHECKING:
    from app.services.coroutine_runner import CoroutineRunner

QR_POLL_INTERVAL = 2.0
QR_GOT_URL = 0
QR_UNSCANNED = QR_API_WAITING
QR_SCANNED = QR_API_SCANNED
QR_EXPIRED = QR_API_EXPIRED
QR_LOGIN_SUCCESS = 1
QR_LOGIN_FAILED = -1


class QuarkAccount:
    accountChanged = Signal()
    qrStateChanged = Signal(int, str)

    def __init__(
        self,
        coroutineRunner: CoroutineRunner,
        client: QuarkClient | None = None,
        pollInterval: float = QR_POLL_INTERVAL,
    ) -> None:
        self._coroutineRunner = coroutineRunner
        self.client = client or QuarkClient()
        self.client.onCookieUpdated = lambda merged: self._coroutineRunner.post(self.updateCookie, merged)
        self._pollInterval = pollInterval
        self._nickname = ""
        self._vip = ""
        self._qrWorkId = ""
        # 账号信息是否过期用登录代次判断：响应 Set-Cookie 会续期 Cookie 文本，但账号未变
        self._generation = 0

    @property
    def cookie(self) -> str:
        return str(quarkConfig.cookie.value or "")

    @property
    def isLoggedIn(self) -> bool:
        return bool(self.cookie)

    @property
    def nickname(self) -> str:
        return self._nickname

    @property
    def vip(self) -> str:
        return self._vip

    def setCookie(self, raw: str) -> None:
        cookie = toCookie(raw)
        self.client.clearCookie()
        self._generation += 1
        cfg.set(quarkConfig.cookie, cookie)
        if not cookie:
            self._nickname = ""
            self._vip = ""
        self.accountChanged.emit()
        if cookie:
            self.fetchAccountInfo()

    def updateCookie(self, raw: str) -> None:
        normalized = toCookie(raw)
        if normalized and normalized != self.cookie and self.isLoggedIn:
            cfg.set(quarkConfig.cookie, normalized)

    def logout(self) -> None:
        self.cancelQrLogin()
        self.client.clearCookie()
        self._generation += 1
        self._nickname = ""
        self._vip = ""
        cfg.set(quarkConfig.cookie, "")
        self.accountChanged.emit()

    def fetchAccountInfo(self) -> None:
        generation = self._generation
        self._coroutineRunner.submit(
            self._fetchAccountInfo(generation),
            done=self._onAccountInfoDone,
            failed=self._onAccountInfoFailed,
        )

    async def _fetchAccountInfo(self, generation: int) -> tuple[int, QuarkProfile]:
        if not self.cookie:
            return generation, loggedOutProfile()
        return generation, await self.client.fetchAccount()

    def _onAccountInfoDone(self, result: tuple[int, QuarkProfile]) -> None:
        generation, profile = result
        if generation != self._generation:
            return
        if profile.isLoggedIn:
            self._nickname = profile.nickname
            self._vip = profile.vipLabel
        else:
            self._nickname = ""
            self._vip = ""
        self.accountChanged.emit()

    def _onAccountInfoFailed(self, _error: Exception) -> None:
        self.accountChanged.emit()

    def startQrLogin(self) -> None:
        self.cancelQrLogin()
        self._qrWorkId = self._coroutineRunner.submit(
            self._pollQrLogin(), done=self._onQrLoginDone, failed=self._onQrLoginFailed,
        )

    def cancelQrLogin(self) -> None:
        workId = self._qrWorkId
        self._qrWorkId = ""
        if workId:
            self._coroutineRunner.cancel(workId)

    async def _pollQrLogin(self) -> str | None:
        token = await self.client.fetchQrToken()
        self._coroutineRunner.post(self.qrStateChanged.emit, QR_GOT_URL, token.loginUrl)
        while True:
            await asyncio.sleep(self._pollInterval)
            state = await self.client.pollQr(token)
            if state.kind == "waiting":
                self._coroutineRunner.post(self.qrStateChanged.emit, QR_UNSCANNED, state.message)
                continue
            if state.kind == "scanned":
                self._coroutineRunner.post(self.qrStateChanged.emit, QR_SCANNED, state.message)
                continue
            if state.kind == "expired":
                self._coroutineRunner.post(self.qrStateChanged.emit, QR_EXPIRED, "")
                return None
            if state.kind == "success":
                return await self.client.exchangeQrTicket(state.serviceTicket)
            return ""

    def _onQrLoginDone(self, cookie: str | None) -> None:
        self._qrWorkId = ""
        if cookie is None:
            return
        if cookie:
            self.setCookie(cookie)
            self.qrStateChanged.emit(QR_LOGIN_SUCCESS, "")
            return
        self.qrStateChanged.emit(QR_LOGIN_FAILED, "登录已确认，但未能取得 Cookie")

    def _onQrLoginFailed(self, error: Exception) -> None:
        self._qrWorkId = ""
        message = str(error)
        cookie = self.cookie
        if cookie and cookie in message:
            message = message.replace(cookie, toMaskedCookie(cookie) or "****")
        self.qrStateChanged.emit(QR_LOGIN_FAILED, message)
