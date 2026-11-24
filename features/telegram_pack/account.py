from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.signal import Signal
from .config import telegramConfig
from .runtime import TdlRunner, tdlRunner, toMaskedProxy

if TYPE_CHECKING:
    from app.services.coroutine_runner import CoroutineRunner

QR_STATE_LOADING = "loading"
QR_STATE_READY = "ready"
QR_STATE_NEED_2FA = "need_2fa"
QR_STATE_VERIFYING_2FA = "verifying_2fa"
QR_STATE_SUCCESS = "success"
QR_STATE_FAILED = "failed"


def readProxy(runner: object) -> str | None:
    resolve = getattr(runner, "resolveProxy", None)
    return resolve() if callable(resolve) else None


def toSafeErrorText(error: Exception, passcode: str = "", proxyUrl: str | None = None) -> str:
    message = str(error)
    if passcode and passcode in message:
        message = message.replace(passcode, "****")
    for candidate in (proxyUrl, str(telegramConfig.proxyOverride.value or "").strip()):
        if candidate and candidate in message:
            message = message.replace(candidate, toMaskedProxy(candidate) or "****")
    return message


class TelegramAccount:
    stateChanged = Signal()
    qrMatrixChanged = Signal(object)
    qrStateChanged = Signal(str, str)

    def __init__(
        self,
        coroutineRunner: CoroutineRunner | None,
        runner: TdlRunner | None = None,
    ) -> None:
        self._coroutineRunner = coroutineRunner
        self._runner = runner if runner is not None else tdlRunner
        self._isLoggedIn = False
        self._isProbing = False
        self._isLoggingIn = False
        self._isLoggingOut = False
        self._lastError = ""
        self._generation = 0
        self._probeWorkId = ""
        self._loginWorkId = ""
        self._logoutWorkId = ""
        self._passwordLoop: asyncio.AbstractEventLoop | None = None
        self._passwordFuture: asyncio.Future[str | None] | None = None

    @property
    def runner(self) -> TdlRunner:
        return self._runner

    @property
    def isInstalled(self) -> bool:
        return bool(self._runner.runtime.path())

    @property
    def isLoggedIn(self) -> bool:
        return self._isLoggedIn

    @property
    def isBusy(self) -> bool:
        return self._isLoggingIn or self._isProbing or self._isLoggingOut

    @property
    def isLoggingIn(self) -> bool:
        return self._isLoggingIn

    @property
    def lastError(self) -> str:
        return self._lastError

    @property
    def statusText(self) -> str:
        if not self.isInstalled:
            return "未安装 tdl，请先点击上方一键安装"
        if self._isLoggingIn:
            return "正在登录，请在扫码弹窗内完成验证..."
        if self._isProbing:
            return "正在检测登录状态..."
        if self._isLoggedIn:
            return "已登录"
        return "未登录"

    def refreshAuth(self) -> None:
        if self._isLoggingIn or self._isLoggingOut:
            return
        if not self.isInstalled:
            self._cancelProbe()
            self._generation += 1
            self._isProbing = False
            self._isLoggedIn = False
            self._lastError = ""
            self.stateChanged.emit()
            return
        if self._coroutineRunner is None:
            return
        self._cancelProbe()
        self._generation += 1
        generation = self._generation
        self._isProbing = True
        self._lastError = ""
        self.stateChanged.emit()
        self._probeWorkId = self._coroutineRunner.submit(
            self._runner.probeAuth(),
            done=lambda isLoggedIn, gen=generation: self._onProbeDone(gen, isLoggedIn),
            failed=lambda error, gen=generation: self._onProbeFailed(gen, error),
        )

    def startQrLogin(self) -> None:
        if not self.isInstalled:
            self._isLoggedIn = False
            self._lastError = "未找到 tdl，请先在设置中安装 Telegram 下载器"
            self.stateChanged.emit()
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
        self.stateChanged.emit()
        self.qrStateChanged.emit(QR_STATE_LOADING, "正在获取登录二维码...")
        self._loginWorkId = self._coroutineRunner.submit(
            self._runQrLogin(generation),
            done=lambda isLoggedIn, gen=generation: self._onQrLoginDone(gen, isLoggedIn),
            failed=lambda error, gen=generation: self._onQrLoginFailed(gen, error),
        )

    def submit2faPassword(self, password: str) -> bool:
        loop = self._passwordLoop
        future = self._passwordFuture
        if loop is None or future is None:
            return False

        def resolve() -> None:
            if not future.done():
                future.set_result(password)

        try:
            loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            return False
        self.qrStateChanged.emit(QR_STATE_VERIFYING_2FA, "正在校验两步验证密码...")
        return True

    def cancelQrLogin(self) -> None:
        self.cancelLogin()

    def startLogin(
        self,
        mode: str = "qr",
        *,
        desktopPath: str = "",
        passcode: str = "",
    ) -> None:
        if mode == "qr":
            self.startQrLogin()
            return
        if not self.isInstalled:
            self._isLoggedIn = False
            self._lastError = "未找到 tdl，请先在设置中安装 Telegram 下载器"
            self.stateChanged.emit()
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
        self.stateChanged.emit()
        self._loginWorkId = self._coroutineRunner.submit(
            self._runInteractiveLogin(mode, desktopPath, passcode),
            done=lambda isLoggedIn, gen=generation: self._onLoginDone(gen, isLoggedIn),
            failed=lambda error, gen=generation, secret=passcode: self._onLoginFailed(gen, error, secret),
        )

    def cancelLogin(self) -> None:
        if not self._loginWorkId and not self._isLoggingIn:
            return
        self._generation += 1
        self._cancelPasswordWait()
        self._cancelLoginWork()
        self._isLoggingIn = False
        self._lastError = ""
        self.stateChanged.emit()

    def logout(self) -> None:
        self._cancelAllWork()
        self._generation += 1
        generation = self._generation
        self._isLoggingIn = False
        self._isProbing = False
        self._isLoggedIn = False
        self._lastError = ""
        if self._coroutineRunner is None:
            self._isLoggingOut = False
            self.stateChanged.emit()
            return
        self._isLoggingOut = True
        self.stateChanged.emit()
        self._logoutWorkId = self._coroutineRunner.submit(
            self._runner.logout(),
            done=lambda _removed, gen=generation: self._onLogoutDone(gen),
            failed=lambda error, gen=generation: self._onLogoutFailed(gen, error),
        )

    async def _runQrLogin(self, generation: int) -> bool:
        coroutineRunner = self._coroutineRunner
        loop = asyncio.get_running_loop()

        def emitMatrix(matrix) -> None:
            if coroutineRunner is None:
                return
            coroutineRunner.post(self._emitQrMatrix, generation, matrix)

        def emitNeed2fa() -> None:
            if coroutineRunner is None:
                return
            coroutineRunner.post(
                self._emitQrState,
                generation,
                QR_STATE_NEED_2FA,
                "手机端已确认扫码，请输入两步验证（2FA）云密码",
            )

        def emitStatus(text: str) -> None:
            if coroutineRunner is None:
                return
            coroutineRunner.post(self._emitQrState, generation, QR_STATE_VERIFYING_2FA, text)

        async def readPassword() -> str | None:
            future: asyncio.Future[str | None] = loop.create_future()
            self._passwordLoop = loop
            self._passwordFuture = future
            try:
                return await future
            finally:
                if self._passwordFuture is future:
                    self._passwordFuture = None
                    self._passwordLoop = None

        await self._runner.loginQrSession(
            onQrMatrix=emitMatrix,
            onPasswordRequired=emitNeed2fa,
            readPassword=readPassword,
            onStatus=emitStatus,
        )
        return await self._runner.probeAuth()

    async def _runInteractiveLogin(self, mode: str, desktopPath: str, passcode: str) -> bool:
        await self._runner.loginInteractive(mode, desktopPath=desktopPath, passcode=passcode)
        return await self._runner.probeAuth()

    def _emitQrMatrix(self, generation: int, matrix) -> None:
        if generation != self._generation:
            return
        self.qrMatrixChanged.emit(matrix)
        self.qrStateChanged.emit(QR_STATE_READY, "请使用手机 Telegram 扫描二维码")

    def _emitQrState(self, generation: int, state: str, message: str) -> None:
        if generation != self._generation:
            return
        self.qrStateChanged.emit(state, message)

    def _onProbeDone(self, generation: int, isLoggedIn: bool) -> None:
        if generation != self._generation:
            return
        self._probeWorkId = ""
        self._isProbing = False
        self._isLoggedIn = bool(isLoggedIn)
        self._lastError = ""
        self.stateChanged.emit()

    def _onProbeFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._probeWorkId = ""
        self._isProbing = False
        self._isLoggedIn = False
        self._lastError = toSafeErrorText(error, proxyUrl=readProxy(self._runner))
        self.stateChanged.emit()

    def _onQrLoginDone(self, generation: int, isLoggedIn: bool) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._isLoggedIn = bool(isLoggedIn)
        self._lastError = "" if self._isLoggedIn else "登录未生效，请重试"
        self.stateChanged.emit()
        if self._isLoggedIn:
            self.qrStateChanged.emit(QR_STATE_SUCCESS, "登录成功")
        else:
            self.qrStateChanged.emit(QR_STATE_FAILED, self._lastError)

    def _onQrLoginFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._lastError = toSafeErrorText(error, proxyUrl=readProxy(self._runner))
        self.stateChanged.emit()
        self.qrStateChanged.emit(QR_STATE_FAILED, self._lastError)

    def _onLoginDone(self, generation: int, isLoggedIn: bool) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._isLoggedIn = bool(isLoggedIn)
        self._lastError = "" if self._isLoggedIn else "登录未生效，请重试"
        self.stateChanged.emit()

    def _onLoginFailed(self, generation: int, error: Exception, passcode: str) -> None:
        if generation != self._generation:
            return
        self._loginWorkId = ""
        self._isLoggingIn = False
        self._lastError = toSafeErrorText(error, passcode, readProxy(self._runner))
        self.stateChanged.emit()

    def _onLogoutDone(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._logoutWorkId = ""
        self._isLoggingOut = False
        self._isLoggedIn = False
        self._lastError = ""
        self.stateChanged.emit()

    def _onLogoutFailed(self, generation: int, error: Exception) -> None:
        if generation != self._generation:
            return
        self._logoutWorkId = ""
        self._isLoggingOut = False
        self._lastError = toSafeErrorText(error, proxyUrl=readProxy(self._runner))
        self.stateChanged.emit()

    def _cancelPasswordWait(self) -> None:
        loop = self._passwordLoop
        future = self._passwordFuture
        self._passwordLoop = None
        self._passwordFuture = None
        if loop is not None and future is not None:
            def cancel() -> None:
                if not future.done():
                    future.set_result(None)

            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass

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
        self._cancelPasswordWait()
        self._cancelProbe()
        self._cancelLoginWork()
        self._cancelLogoutWork()
