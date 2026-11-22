from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer

from app.services.coroutine_runner import CoroutineRunner
from quark_pack.account import (
    QR_EXPIRED, QR_GOT_URL, QR_LOGIN_SUCCESS, QR_SCANNED, QR_UNSCANNED, QuarkAccount,
)
from quark_pack.client import toCookie
from quark_pack.config import quarkConfig
from quark_pack.pack import QuarkPack
from tests.quark_pack.conftest import buildQuarkClient


async def waitUntil(predicate, timeout: float = 8.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def waitUi(predicate, timeout: float = 8.0) -> None:
    from PySide6.QtWidgets import QApplication
    for _ in range(int(timeout / 0.02)):
        QApplication.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def buildAccount(origin, *, pollInterval: float = 0.05) -> QuarkAccount:
    loop = asyncio.get_running_loop()
    runner = CoroutineRunner(loop.call_soon, loop=loop)
    client = buildQuarkClient(origin, readCookie=lambda: str(quarkConfig.cookie.value or ""))
    return QuarkAccount(runner, client, pollInterval=pollInterval)


async def test_config_defaults(config):
    assert quarkConfig.cookie.value == ""
    assert quarkConfig.subworkerCount.value == 8
    assert quarkConfig.subworkerCount.range == (1, 32)
    assert quarkConfig.shouldCleanTempAfterDone.value is True


async def test_pack_exposes_account():
    loop = asyncio.get_running_loop()
    runner = CoroutineRunner(loop.call_soon, loop=loop)
    pack = QuarkPack(SimpleNamespace(coroutineRunner=runner))
    assert pack.packId == "quark"
    assert [parser.priority for parser in pack.parsers] == [65]
    assert pack.config is quarkConfig
    assert isinstance(pack.account, QuarkAccount)
    assert pack.client is pack.account.client


async def test_set_cookie_normalizes_and_saves(quarkOrigin):
    account = buildAccount(quarkOrigin)
    account.setCookie(" \n __pus=abc \r\n ; __puus=def \n ")
    assert quarkConfig.cookie.value == "__pus=abc; __puus=def"
    await waitUntil(lambda: account.nickname == "测试用户")
    assert account.vip == "超级会员"
    assert account.isLoggedIn is True


async def test_fetch_account_info_emits_nickname_and_vip(quarkOrigin):
    account = buildAccount(quarkOrigin)
    snapshots: list[tuple[str, str]] = []
    account.accountChanged.connect(lambda: snapshots.append((account.nickname, account.vip)))
    account.setCookie("__pus=fake-pus-value; __puus=fake-puus-value")
    await waitUntil(lambda: ("测试用户", "超级会员") in snapshots)
    assert account.nickname == "测试用户"
    assert account.vip == "超级会员"


async def test_qr_login_waits_scans_then_saves_cookie(quarkOrigin):
    account = buildAccount(quarkOrigin)
    states: list[tuple[int, str]] = []
    account.qrStateChanged.connect(lambda code, message: states.append((code, message)))
    account.startQrLogin()
    await waitUntil(lambda: any(code == QR_LOGIN_SUCCESS for code, _message in states))

    codes = [code for code, _message in states]
    assert codes.index(QR_GOT_URL) < codes.index(QR_UNSCANNED)
    assert codes.index(QR_UNSCANNED) < codes.index(QR_SCANNED) < codes.index(QR_LOGIN_SUCCESS)
    loginUrl = next(message for code, message in states if code == QR_GOT_URL)
    assert "qr-token-1" in loginUrl
    assert "client_id=532" in loginUrl
    assert quarkConfig.cookie.value == "__pus=qr-pus-value; __puus=qr-puus-value"
    await waitUntil(lambda: account.nickname == "测试用户")
    assert account.vip == "超级会员"


async def test_qr_login_expired_does_not_save_cookie(quarkOrigin):
    quarkOrigin.qrScript = ["waiting", "expired"]
    account = buildAccount(quarkOrigin)
    states: list[int] = []
    account.qrStateChanged.connect(lambda code, _message: states.append(code))
    account.startQrLogin()
    await waitUntil(lambda: QR_EXPIRED in states)
    await asyncio.sleep(0.2)
    assert QR_LOGIN_SUCCESS not in states
    assert account.cookie == ""


async def test_cancel_qr_login_does_not_save_cookie(quarkOrigin):
    quarkOrigin.successGate = asyncio.Event()
    account = buildAccount(quarkOrigin)
    account.setCookie("__pus=keep-me-value")
    await waitUntil(lambda: account.nickname == "测试用户")
    states: list[int] = []
    account.qrStateChanged.connect(lambda code, _message: states.append(code))
    try:
        account.startQrLogin()
        await waitUntil(lambda: QR_SCANNED in states)
        account.cancelQrLogin()
        await asyncio.sleep(0.3)
        assert "qr-pus-value" not in account.cookie
        assert "keep-me-value" in account.cookie
    finally:
        quarkOrigin.successGate.set()
    await asyncio.sleep(0.3)
    assert "qr-pus-value" not in account.cookie
    assert quarkConfig.cookie.value == "__pus=keep-me-value"


async def test_logout_clears_cookie_and_emits(quarkOrigin):
    account = buildAccount(quarkOrigin)
    account.setCookie("__pus=abc; __puus=def")
    await waitUntil(lambda: account.nickname == "测试用户")
    cleared: list[str] = []
    account.accountChanged.connect(lambda: cleared.append(account.cookie))
    account.logout()
    assert account.cookie == ""
    assert account.nickname == ""
    assert account.vip == ""
    assert account.isLoggedIn is False
    assert cleared == [""]
    assert account.client.currentCookie() == ""
    await asyncio.sleep(0.1)
    assert account.nickname == ""
    assert account.cookie == ""


async def test_response_cookie_refreshes_configured_cookie(quarkOrigin):
    raw = "__pus=fake-pus-value"
    account = buildAccount(quarkOrigin)
    account.setCookie(raw)
    await waitUntil(lambda: account.nickname == "测试用户")
    quarkOrigin.responseCookies = {"__puus": "refreshed-val"}

    await account.client.fetchDriveFiles()
    await waitUntil(lambda: "__puus=refreshed-val" in account.cookie)

    assert toCookie(raw) in account.cookie


async def test_response_cookie_refreshes_configured_cookie_on_main_thread(config, quarkOrigin, qapp):
    mainId = threading.get_ident()
    runner = CoroutineRunner(lambda fn: QTimer.singleShot(0, qapp, fn), loop=asyncio.new_event_loop())
    runner.start()
    try:
        client = buildQuarkClient(
            quarkOrigin, readCookie=lambda: str(quarkConfig.cookie.value or ""),
        )
        account = QuarkAccount(runner, client)
        account.setCookie("__pus=fake-pus-value")
        await waitUi(lambda: account.nickname == "测试用户")
        updateThreads: list[int] = []
        updateCookie = account.updateCookie

        def record(raw: str) -> None:
            updateThreads.append(threading.get_ident())
            updateCookie(raw)

        account.updateCookie = record
        quarkOrigin.responseCookies = {"__puus": "refreshed-val"}
        runner.submit(account.client.fetchDriveFiles())

        await waitUi(lambda: "refreshed-val" in account.cookie)
    finally:
        runner.stop()
        # 排空留在 Qt 队列里的回调，避免它在下一个用例的 config 夹具之后才写盘
        for _ in range(3):
            qapp.processEvents()
            await asyncio.sleep(0.02)

    assert updateThreads
    assert all(threadId == mainId for threadId in updateThreads)


async def test_response_cookie_keeps_nickname(quarkOrigin):
    quarkOrigin.responseCookies = {"__puus": "refreshed-val"}
    account = buildAccount(quarkOrigin)
    account.setCookie("__pus=fake-pus-value")
    await waitUntil(lambda: account.nickname == "测试用户")

    account.fetchAccountInfo()
    await waitUntil(lambda: "refreshed-val" in account.cookie)

    assert account.nickname == "测试用户"
    assert account.vip == "超级会员"


async def test_qr_poll_does_not_overwrite_logged_in_cookie(quarkOrigin):
    account = buildAccount(quarkOrigin)
    account.setCookie("__pus=fake-pus-value")
    await waitUntil(lambda: account.nickname == "测试用户")
    before = account.cookie
    quarkOrigin.responseCookies = {"__puus": "qr-refreshed-val"}
    quarkOrigin.qrScript = ["waiting"]
    states: list[int] = []
    account.qrStateChanged.connect(lambda code, _message: states.append(code))

    account.startQrLogin()
    await waitUntil(lambda: QR_UNSCANNED in states)
    account.cancelQrLogin()
    await asyncio.sleep(0.2)

    assert account.cookie == before


async def test_update_cookie_before_login_does_not_write_config(config, quarkOrigin):
    account = buildAccount(quarkOrigin)

    account.updateCookie("__puus=early-val")

    assert quarkConfig.cookie.value == ""
