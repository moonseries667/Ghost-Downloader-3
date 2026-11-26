from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.signal import Signal
from .client import BaiduWebClient, normalizeCookie
from .runtime import BaiduPcsRunner, BaiduUserInfo, baiduPcsRunner, redactSecrets

if TYPE_CHECKING:
    from app.services.coroutine_runner import CoroutineRunner

QR_POLL_INTERVAL = 2.0
QR_STATE_LOADING = "loading"
QR_STATE_READY = "ready"
QR_STATE_WAITING = "waiting"
QR_STATE_SCANNED = "scanned"
QR_STATE_EXPIRED = "expired"
QR_STATE_SUCCESS = "success"
QR_STATE_FAILED = "failed"


def toSafeErrorText(error: Exception, secrets: tuple[str, ...] = (), proxyUrl: str | None = None) -> str:
    return redactSecrets(str(error), extraSecrets=secrets, proxyUrl=proxyUrl)


class BaiduAccount:
    accountChanged = Signal()
    qrStateChanged = Signal(str, str)
    qrImageChanged = Signal(bytes)

    def __init__(
        self,
        coroutineRunner: CoroutineRunner | None,
        runner: BaiduPcsRunner | None = None,
        client: BaiduWebClient | None = None,
        pollInterval: float = QR_POLL_INTERVAL,
    ) -> None:
        self._coroutineRunner = coroutineRunner
        self._runner = runner if runner is not None else baiduPcsRunner
        self._client = client if client is not None else BaiduWebClient()
        self._pollInterval = pollInterval
        self._user = BaiduUserInfo(isLoggedIn=False)
        self._isProbing = False
        self._isLoggingIn = False
        self._isLoggingOut = False
        self._lastError = ""
        self._generation = 0
        self._probeWorkId = ""
        self._loginWorkId = ""
        self._logoutWorkId = ""

    @property
    def runner(self) -> BaiduPcsRunner:
        return self._runner

    @property
    def client(self) -> BaiduWebClient:
        return self._client

    @property
    def isInstalled(self) -> bool:
        return bool(self._runner.runtime.path())

    @property
    def isLoggedIn(self) -> bool:
        return self._user.isLoggedIn

    @property
    def isBusy(self) -> bool:
        return self._isProbing or self._isLoggingIn or self._isLoggingOut

    @property
    def isLoggingIn(self) -> bool:
        return self._isLoggingIn

    @property
    def username(self) -> str:
        return self._user.username

    @property
    def uid(self) -> str:
        return self._user.uid

    @property
    def totalQuota(self) -> str:
        return self._user.totalQuota

    @property
    def usedQuota(self) -> str:
        return self._user.usedQuota

    @property
    def usagePercent(self) -> str:
        return self._user.usagePercent

    @property
    def lastError(self) -> str:
        return self._lastError

    @property
    def statusText(self) -> str:
        if not self.isInstalled:
            return "未安装 BaiduPCS-Go，请先点击上方一键安装"
        if self._isLoggingIn:
            return "正在登录百度账号..."
        if self._isLoggingOut:
            return "正在退出百度账号..."
        if self._isProbing:
            return "正在检测登录状态..."
        if self._user.isLoggedIn:
            if self._user.usedQuota and self._user.totalQuota:
                return f"已登录：{self._user.username}（空间 {self._user.usedQuota} / {self._user.totalQuota}）"
            return f"已登录：{self._user.username}"
        return "未登录"

    def applyUserInfo(self, user: BaiduUserInfo) -> None:
        self._user = user
        self._lastError = ""
        self.accountChanged.emit()

    def refreshAuth(self) -> None:
        if self._isLoggingIn or self._isLoggingOut:
            return
        if not self.isInstalled:
            self._cancelProbe()
            self._generation += 1
            self._isProbing = False
            self._user = BaiduUserInfo(isLoggedIn=False)
            self._lastError = ""
            self.accountChanged.emit()
            return
        if self._coroutineRunner is None:
            return
        self._cancelProbe()
        self._generation += 1
        generation = self._generation
        self._isProbing = True
        self._lastError = ""
        self.accountChanged.emit()
        self._probeWorkId = self._coroutineRunner.submit(
            self._runner.probeUser(),
            done=lambda user, gen=generation: self._onProbeDone(gen, user),
            failed=lambda error, gen=generation: self._onProbeFailed(gen, error),
        )

    def loginWithCookies(self, rawCookie: str) -> None:
        cookie = normalizeCookie(rawCookie)
        if not self.isInstalled:
            self._lastError = "未找到 BaiduPCS-Go，请先在设置中安装百度网盘下载器"
            self.accountChanged.emit()
            return
        if not cookie:
            self._lastError = "请输入有效的百度 Cookie"
            self.accountChanged.emit()
            return
        if self._coroutineRunner is None:
            return
        self._cancelAllWork()
        self._generation += 1
        generation = self._generation
        self._isLoggingIn = True
        self._isProbing = False
        self._isLoggingOut = False
        self._lastError = ""
        self.accountChanged.emit()
        self._loginWorkId = self._coroutineRunner.submit(
            self._runner.loginWithCookies(cookie),
            done=lambda user, gen=generation: self._onLoginDone(gen, user),
            failed=lambda error, gen=generation, secret=cookie: self._onLoginFailed(gen, error, (secret,)),
        )

    def loginWithCredentials(self, bduss: str, stoken: str = "") -> None:
        cleanBduss = str(bduss or "").strip()
        cleanStoken = str(stoken or "").strip()
        if not self.isInstalled:
            self._lastError = "未找到 BaiduPCS-Go，请先在设置中安装百度网盘下载器"
            self.accountChanged.emit()
            return
        if not cleanBduss:
            self._lastError = "请输入有效的 BDUSS"
            self.accountChanged.emit()
            return
        if self._coroutineRunner is None:
            return
        self._cancelAllWork()
        self._generation += 1
        generation = self._generation
        self._isLoggingIn = True
        self._isProbing = False
        self._isLoggingOut = False
        self._lastError = ""
        self.accountChanged.emit()
        secrets = (cleanBduss, cleanStoken) if cleanStoken else (cleanBduss,)
        self._loginWorkId = self._coroutineRunner.submit(
            self._runner.loginWithCredentials(cleanBduss, cleanStoken),
            done=lambda user, gen=generation: self._onLoginDone(gen, user),
            failed=lambda error, gen=generation, sec=secrets: self._onLoginFailed(gen, error, sec),
        )

    def startQrLogin(self) -> None:
        if not self.isInstalled:
            self._user = BaiduUserInfo(isLoggedIn=False)
            self._lastError = "未找到 BaiduPCS-Go，请先在设置中安装百度网盘下载器"
            self.accountChanged.emit()
            self.qrStateChanged.emit(QR_STATE_FAILED, self._lastError)
            return
        if self._coroutineRunner is None:
            return
        self._cancelAllWork()
        self._generation += 1
        generation = self._generation
        self._isLoggingIn = True
        self._isProbing = False
        self._isLoggingOut = False
        self._lastError = ""
        self.accountChanged.emit()
        self.qrStateChanged.emit(QR_STATE_LOADING, "正在获取百度扫码二维码...")
        self._loginWorkId = self._coroutineRunner.submit(
            self._pollQrLogin(generation),
            done=lambda user, gen=generation: self._onQrLoginDone(gen, user),
            failed=lambda error, gen=generation: self._onQrLoginFailed(gen, error),
        )

    def cancelQrLogin(self) -> None:
        if not self._loginWorkId and not self._isLoggingIn:
            return
        self._generation += 1
        self._cancelLoginWork()
        self._isLoggingIn = False
        self._lastError = ""
        self.accountChanged.emit()

    def logout(self) -> None:
        self._cancelAllWork()
        self._generation += 1
        generation = self._generation
        self._isLoggingIn = False
        self._isProbing = False
        self._user = BaiduUserInfo(isLoggedIn=False)
        self._lastError = ""
        if self._coroutineRunner is None or not self.isInstalled:
            self._isLoggingOut = False
            self.accountChanged.emit()
            return
        self._isLoggingOut = True
        self.accountChanged.emit()
        self._logoutWorkId = self._coroutineRunner.submit(
            self._runner.logout(),
            done=lambda _removed, gen=generation: self._onLogoutDone(gen),
            failed=lambda error, gen=generation: self._onLogoutFailed(gen, error),
        )

    async def _pollQrLogin(self, generation: int) -> BaiduUserInfo | None:
        session = await self._client.fetchQrSession()
        if self._coroutineRunner is not None:
            if session.qrImageBytes:
                self._coroutineRunner.post(self._emitQrImage, generation, session.qrImageBytes)
            self._coroutineRunner.post(self._emitQrState, generation, QR_STATE_READY, session.imgUrl)

        while True:
            await asyncio.sleep(self._pollInterval)
            poll = await self._client.pollQrStatus(session)
            if poll.kind == "waiting":
                if self._coroutineRunner is not None:
                    self._coroutineRunner.post(self._emitQrState, generation, QR_STATE_WAITING, "等待手机扫码")
                continue
            if poll.kind == "scanned":
                if self._coroutineRunner is not None:
                    self._coroutineRunner.post(
                        self._emitQrState, generation, QR_STATE_SCANNED, "已扫码，请在手机百度 App 上确认登录",
                    )
                continue
            if poll.kind == "expired":
                if self._coroutineRunner is not None:
                    self._coroutineRunner.post(
                        self._emitQrState, generation, QR_STATE_EXPIRED, poll.message or "二维码已失效，请刷新二维码",
                    )
                return None
            if poll.kind == "confirmed":
                cookie = await self._client.exchangeQrCookie(poll.token, session.gid)
                return await self._runner.loginWithCookies(cookie)
            if self._coroutineRunner is not None:
                self._coroutineRunner.post(
                    self._emitQrState, generation, QR_STATE_FAILED, poll.message or "扫码登录失败",
                )
            return None

    def _emitQrImage(self, generation: int, imageBytes: bytes) -> None:
        if generation != self._generation:
            return
        self.qrImageChanged.emit(imageBytes)

    def _emitQrState(self, generation: int, state: str, message: str) -> None:
        if generation != self._generation:
            return
        self.qrStateChanged.emit(state, message)

    def _onProbeDone(self, generation: int, user: BaiduUserInfo) -> None:
        if generation != self._generation:
            return
        self._probeWorkId = ""
        self._isProbing = False
        self._user = user
        self._lastError = ""
        self.accountChanged.emit()

    def _onProbeFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._probeWorkId = ""
        self._isProbing = False
        self._user = BaiduUserInfo(isLoggedIn=False)
        self._lastError = toSafeErrorText(error, proxyUrl=self._runner.resolveProxy())
        self.accountChanged.emit()

    def _onLoginDone(self, generation: int, user: BaiduUserInfo) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._user = user
        self._lastError = "" if user.isLoggedIn else "登录未生效，请检查凭据后重试"
        self.accountChanged.emit()

    def _onLoginFailed(self, generation: int, error: Exception, secrets: tuple[str, ...]) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._lastError = toSafeErrorText(error, secrets=secrets, proxyUrl=self._runner.resolveProxy())
        self.accountChanged.emit()

    def _onQrLoginDone(self, generation: int, user: BaiduUserInfo | None) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        if user is None:
            self.accountChanged.emit()
            return
        self._user = user
        self._lastError = "" if user.isLoggedIn else "扫码确认后未能读取登录状态，请重试"
        self.accountChanged.emit()
        if user.isLoggedIn:
            self.qrStateChanged.emit(QR_STATE_SUCCESS, "登录成功")
        else:
            self.qrStateChanged.emit(QR_STATE_FAILED, self._lastError)

    def _onQrLoginFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._lastError = toSafeErrorText(error, proxyUrl=self._runner.resolveProxy())
        self.accountChanged.emit()
        self.qrStateChanged.emit(QR_STATE_FAILED, self._lastError)

    def _onLogoutDone(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._logoutWorkId = ""
        self._isLoggingOut = False
        self._user = BaiduUserInfo(isLoggedIn=False)
        self._lastError = ""
        self.accountChanged.emit()

    def _onLogoutFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._logoutWorkId = ""
        self._isLoggingOut = False
        self._lastError = toSafeErrorText(error, proxyUrl=self._runner.resolveProxy())
        self.accountChanged.emit()

    def _cancelProbe(self) -> None:
        workId = self._probeWorkId
        self._probeWorkId = ""
        if workId and self._coroutineRunner is not None:
            self._coroutineRunner.cancel(workId)

    def _cancelLoginWork(self) -> None:
        workId = self._loginWorkId
        self._loginWorkId = ""
        if workId and self._coroutineRunner is not None:
            self._coroutineRunner.cancel(workId)

    def _cancelLogoutWork(self) -> None:
        workId = self._logoutWorkId
        self._logoutWorkId = ""
        if workId and self._coroutineRunner is not None:
            self._coroutineRunner.cancel(workId)

    def _cancelAllWork(self) -> None:
        self._cancelProbe()
        self._cancelLoginWork()
        self._cancelLogoutWork()
