from __future__ import annotations

from types import SimpleNamespace

from app.models.pack import PackServices
from app.models.task import TaskOptions
from quark_pack.cards import QuarkDraftCard, QuarkTaskCard
from quark_pack.pack import QuarkPack, QuarkParser
from quark_pack.task import QuarkTask
from tests.quark_pack.conftest import buildQuarkClient

OFFICIAL = (
    "我用夸克网盘分享了「示例文件」，点击链接即可保存。\n"
    "链接：https://pan.quark.cn/s/shareid01\n"
    "提取码：ab12\n"
)


def test_match_accepts_one_share_and_rejects_a_batch():
    parser = QuarkParser()
    assert parser.match(TaskOptions(url="https://pan.quark.cn/s/plain001"))
    assert parser.match(TaskOptions(url="https://pan.quark.cn/s/pwd00001?pwd=ab12"))
    assert parser.match(TaskOptions(url=OFFICIAL))
    assert not parser.match(TaskOptions(url="https://pan.quark.cn/s/aaaa1111\nhttps://pan.quark.cn/s/bbbb2222\n"))
    assert not parser.match(TaskOptions(url="https://example.com/file"))


def test_match_passive_accepts_only_a_share_url():
    parser = QuarkParser()
    assert parser.matchPassive(TaskOptions(url="https://pan.quark.cn/s/plain001"))
    assert parser.matchPassive(TaskOptions(url="https://pan.quark.cn/s/pwd00001?pwd=ab12#/list/share"))
    assert not parser.matchPassive(TaskOptions(url=OFFICIAL))
    assert not parser.matchPassive(TaskOptions(url="pan.quark.cn/s/plain001"))


async def test_feature_service_parses_quark_before_http(engine, httpPack, quarkOrigin, downloadFolder):
    pack = QuarkPack(PackServices(
        coroutineRunner=engine.coroutineRunner,
        speedMeter=engine.speedMeter,
    ))
    pack.client = buildQuarkClient(quarkOrigin)
    engine.featureService.register(pack)

    task = await engine.featureService.parse(TaskOptions(
        url="https://pan.quark.cn/s/pwd00001?pwd=ab12",
        outputFolder=downloadFolder,
    ))

    assert isinstance(task, QuarkTask)
    assert task.packId == "quark"
    assert task.stoken == "stoken-1"
    assert task.pwdId == "pwd00001"
    assert task.passcode == "ab12"
    assert [item.relativePath for item in task.files] == ["目录甲/a.txt", "b.txt"]
    assert not any(call["path"].endswith("/share/sharepage/save") for call in quarkOrigin.calls)
    assert pack.taskCardClass(task) is QuarkTaskCard
    assert pack.draftCardClass(task) is QuarkDraftCard
    assert pack.parsers[0].priority < httpPack.parsers[0].priority


async def test_activate_refreshes_account_when_cookie_is_set(quarkOrigin):
    import asyncio

    from app.config.cfg import cfg
    from app.services.coroutine_runner import CoroutineRunner
    from quark_pack.config import quarkConfig

    loop = asyncio.get_running_loop()
    runner = CoroutineRunner(loop.call_soon, loop=loop)
    pack = QuarkPack(SimpleNamespace(coroutineRunner=runner))
    cfg.set(quarkConfig.cookie, "__pus=fake-pus-value")
    pack.client = buildQuarkClient(quarkOrigin, readCookie=lambda: str(quarkConfig.cookie.value or ""))
    pack.account.client = pack.client
    await pack.activate()
    for _ in range(100):
        if pack.account.nickname == "测试用户":
            break
        await asyncio.sleep(0.02)
    assert pack.account.nickname == "测试用户"
    assert pack.account.vip == "超级会员"
