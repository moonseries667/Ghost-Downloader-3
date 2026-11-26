from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.pack import PackServices
from app.models.task import TaskError, TaskOptions
from app.services.coroutine_runner import CoroutineRunner
from baidu_pack.account import (
    QR_STATE_EXPIRED,
    QR_STATE_SCANNED,
    QR_STATE_SUCCESS,
    BaiduAccount,
)
from baidu_pack.cards import BaiduDraftCard, BaiduTaskCard
from baidu_pack.client import BaiduQrPollResult, BaiduQrSession, BaiduShareEntry, BaiduShareTree
from baidu_pack.pack import BaiduPack, BaiduParser
from baidu_pack.runtime import BaiduUserInfo
from baidu_pack.task import BaiduTask

OFFICIAL_COPY = (
    "通过网盘分享的文件：示例资料\n"
    "链接：https://pan.baidu.com/s/1BaiduDemo01\n"
    "提取码：ab12\n"
)


def test_baidu_parser_match_and_passive_capture():
    parser = BaiduParser()
    assert parser.match(TaskOptions(url="https://pan.baidu.com/s/1BaiduDemo01"))
    assert parser.match(TaskOptions(url="https://yun.baidu.com/s/1BaiduDemo01?pwd=ab12"))
    assert parser.match(TaskOptions(url="https://pan.baidu.com/share/init?surl=BaiduDemo01&pwd=ab12"))
    assert parser.match(TaskOptions(url=OFFICIAL_COPY))
    assert not parser.match(TaskOptions(url="https://pan.baidu.com/s/1AAA\nhttps://pan.baidu.com/s/1BBB"))
    assert not parser.match(TaskOptions(url="https://example.com/file.zip"))

    assert parser.matchPassive(TaskOptions(url="https://pan.baidu.com/s/1BaiduDemo01?pwd=ab12"))
    assert parser.matchPassive(TaskOptions(url="https://pan.baidu.com/share/init?surl=BaiduDemo01"))
    assert not parser.matchPassive(TaskOptions(url=OFFICIAL_COPY))
    assert not parser.matchPassive(TaskOptions(url="pan.baidu.com/s/1BaiduDemo01"))


async def test_feature_service_routes_baidu_before_http_readonly(engine, httpPack, downloadFolder):
    pack = BaiduPack(PackServices(
        coroutineRunner=engine.coroutineRunner,
        speedMeter=engine.speedMeter,
    ))
    called: list[tuple[str, str]] = []

    async def fakeFetchShareTree(shareKey: str, passcode: str = "") -> BaiduShareTree:
        called.append((shareKey, passcode))
        return BaiduShareTree(
            title="示例目录",
            shareKey=shareKey,
            files=[
                BaiduShareEntry("101", "a.pdf", 1024, False, "/示例目录/a.pdf", "示例目录/a.pdf"),
                BaiduShareEntry("102", "b.mp4", 2048, False, "/示例目录/b.mp4", "示例目录/b.mp4"),
            ],
        )

    pack.client.fetchShareTree = fakeFetchShareTree  # type: ignore[method-assign]
    engine.featureService.register(pack)

    task = await engine.featureService.parse(TaskOptions(
        url="https://pan.baidu.com/s/1BaiduDemo01?pwd=ab12",
        outputFolder=downloadFolder,
    ))

    assert isinstance(task, BaiduTask)
    assert task.packId == "baidu"
    assert task.shareKey == "1BaiduDemo01"
    assert task.passcode == "ab12"
    assert [file.relativePath for file in task.files] == ["示例目录/a.pdf", "示例目录/b.mp4"]
    assert called == [("1BaiduDemo01", "ab12")]
    assert pack.taskCardClass(task) is BaiduTaskCard
    assert pack.draftCardClass(task) is BaiduDraftCard
    assert BaiduParser.priority == 66
    assert BaiduParser.priority < httpPack.parsers[0].priority
    assert engine.featureService.matchPassive("https://pan.baidu.com/s/1BaiduDemo01?pwd=ab12")


async def test_account_qr_cookie_credentials_and_logout_flow():
    loop = asyncio.get_running_loop()
    coroutineRunner = CoroutineRunner(loop.call_soon, loop=loop)
    loggedInUser = BaiduUserInfo(
        isLoggedIn=True, uid="42", username="baidu_user", totalQuota="2TB", usedQuota="50GB", usagePercent="2.5%",
    )

    class FakeAccountRunner:
        def __init__(self) -> None:
            self.runtime = SimpleNamespace(path=lambda: "/fake/BaiduPCS-Go")
            self.cookiesCalls: list[str] = []
            self.credCalls: list[tuple[str, str]] = []
            self.loggedOut = False

        def resolveProxy(self) -> str | None:
            return None

        async def probeUser(self) -> BaiduUserInfo:
            return BaiduUserInfo(isLoggedIn=False) if self.loggedOut else loggedInUser

        async def loginWithCookies(self, cookies: str) -> BaiduUserInfo:
            self.cookiesCalls.append(cookies)
            self.loggedOut = False
            return loggedInUser

        async def loginWithCredentials(self, bduss: str, stoken: str = "") -> BaiduUserInfo:
            self.credCalls.append((bduss, stoken))
            if bduss == "bad-secret":
                raise TaskError("登录失败: BDUSS=bad-secret")
            self.loggedOut = False
            return loggedInUser

        async def logout(self) -> bool:
            self.loggedOut = True
            return True

    polls = iter([
        BaiduQrPollResult("scanned"),
        BaiduQrPollResult("confirmed", token="qr-v-token"),
    ])

    class FakeAccountClient:
        async def fetchQrSession(self) -> BaiduQrSession:
            return BaiduQrSession(sign="s1", gid="g1", imgUrl="https://passport.baidu.com/qr", qrImageBytes=b"png")

        async def pollQrStatus(self, _session: BaiduQrSession) -> BaiduQrPollResult:
            return next(polls, BaiduQrPollResult("expired"))

        async def exchangeQrCookie(self, token: str, _gid: str = "") -> str:
            assert token == "qr-v-token"
            return "BDUSS=qr-bduss; STOKEN=qr-stoken"

    runner = FakeAccountRunner()
    account = BaiduAccount(coroutineRunner, runner, FakeAccountClient(), pollInterval=0.01)
    states: list[tuple[str, str]] = []
    images: list[bytes] = []
    account.qrStateChanged.connect(lambda state, msg: states.append((state, msg)))
    account.qrImageChanged.connect(images.append)

    account.startQrLogin()
    for _ in range(100):
        if any(state == QR_STATE_SUCCESS for state, _ in states):
            break
        await asyncio.sleep(0.01)
    assert account.isLoggedIn is True
    assert account.username == "baidu_user"
    assert images == [b"png"]
    assert any(state == QR_STATE_SCANNED for state, _ in states)
    assert runner.cookiesCalls == ["BDUSS=qr-bduss; STOKEN=qr-stoken"]

    account.logout()
    for _ in range(50):
        if not account.isLoggedIn and not account.isBusy:
            break
        await asyncio.sleep(0.01)
    assert account.isLoggedIn is False

    # 错误提示严禁泄露明文 BDUSS
    account.loginWithCredentials("bad-secret", "stoken-secret")
    for _ in range(50):
        if account.lastError:
            break
        await asyncio.sleep(0.01)
    assert "bad-secret" not in account.lastError

    account.loginWithCredentials("good-bduss", "good-stoken")
    for _ in range(50):
        if account.isLoggedIn:
            break
        await asyncio.sleep(0.01)
    assert account.isLoggedIn is True
    assert runner.credCalls[-1] == ("good-bduss", "good-stoken")

    # 验证二维码过期状态流转
    states.clear()
    account.startQrLogin()
    for _ in range(50):
        if any(state == QR_STATE_EXPIRED for state, _ in states):
            break
        await asyncio.sleep(0.01)
    assert any(state == QR_STATE_EXPIRED for state, _ in states)
