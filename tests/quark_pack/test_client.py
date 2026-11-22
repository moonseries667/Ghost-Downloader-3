from __future__ import annotations

import pytest
from loguru import logger

from app.config.cfg import cfg
from app.models.task import TaskError
from quark_pack.client import QUARK_PC_UA, QuarkClient, ShareEntry, mergeCookies
from quark_pack.config import quarkConfig
from tests.quark_pack.conftest import buildQuarkClient, buildQuarkTestClient

RAW_COOKIE = "__pus=fake-pus-value-DO-NOT-LEAK; __puus=fake-puus-value"


async def test_fetch_account_reads_profile_and_masks_cookie(quarkOrigin):
    cfg.set(quarkConfig.cookie, RAW_COOKIE)
    client = QuarkClient(
        clientFactory=buildQuarkTestClient,
        driveBase=quarkOrigin.driveBase,
        panBase=quarkOrigin.panBase,
        authBase=quarkOrigin.authBase,
    )
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        profile = await client.fetchAccount()
    finally:
        logger.remove(sink)

    assert profile.isLoggedIn is True
    assert profile.nickname == "测试用户"
    assert profile.accountId == "uid-1"
    assert profile.vipLabel == "超级会员"
    assert profile.vipExpireAt == "1893456000000"
    call = quarkOrigin.calls[0]
    assert call["path"].endswith("/account/info")
    assert call["referer"] == "https://pan.quark.cn/"
    assert call["origin"] == "https://pan.quark.cn"
    assert "fake-pus-value-DO-NOT-LEAK" in call["cookie"]
    blob = "\n".join(lines)
    assert "fake-pus-value-DO-NOT-LEAK" not in blob
    assert "****" in blob


async def test_fetch_account_reports_logged_out(quarkOrigin):
    quarkOrigin.forceLoggedOut = True
    client = buildQuarkClient(quarkOrigin, readCookie=lambda: RAW_COOKIE)
    profile = await client.fetchAccount()
    assert profile.isLoggedIn is False
    assert profile.nickname == ""
    assert profile.vipLabel == ""


async def test_fetch_share_tree_expands_nested_pages(quarkOrigin):
    client = buildQuarkClient(quarkOrigin, pageSize=1, readCookie=lambda: RAW_COOKIE)
    tree = await client.fetchShareTree("pwd1", "ab12")

    assert tree.stoken == "stoken-1"
    assert tree.title == "分享标题"
    assert tree.pwdId == "pwd1"
    assert [(item.relativePath, item.fid, item.pdirFid) for item in tree.files] == [
        ("目录甲/a.txt", "file-a", "dir-1"),
        ("b.txt", "file-b", "0"),
    ]
    assert all(not item.isDir for item in tree.files)
    tokenCall = quarkOrigin.calls[0]
    assert tokenCall["body"] == {"pwd_id": "pwd1", "passcode": "ab12"}
    assert tokenCall["query"]["pr"] == "ucpro"
    assert tokenCall["query"]["fr"] == "pc"
    assert tokenCall["referer"] == "https://pan.quark.cn/"
    assert "fake-pus-value-DO-NOT-LEAK" in tokenCall["cookie"]
    rootPages = [
        call["query"].get("_page")
        for call in quarkOrigin.calls
        if call["path"].endswith("/share/sharepage/detail") and call["query"].get("pdir_fid") == "0"
    ]
    assert rootPages == ["1", "2"]
    detail = next(call for call in quarkOrigin.calls if call["path"].endswith("/share/sharepage/detail"))
    assert detail["query"]["_fetch_share"] == "1"


async def test_fetch_share_tree_rejects_bad_passcode(quarkOrigin):
    quarkOrigin.tokenError = {"code": 41008, "status": 200, "message": "提取码错误", "data": None}
    client = buildQuarkClient(quarkOrigin, readCookie=lambda: RAW_COOKIE)
    with pytest.raises(TaskError, match="提取码") as caught:
        await client.fetchShareTree("pwd1", "bad1")
    assert "fake-pus-value-DO-NOT-LEAK" not in str(caught.value)


async def test_fetch_share_tree_rejects_expired_share(quarkOrigin):
    quarkOrigin.tokenError = {"code": 41011, "status": 200, "message": "分享已失效", "data": None}
    client = buildQuarkClient(quarkOrigin)
    with pytest.raises(TaskError, match="失效"):
        await client.fetchShareTree("pwd1", "")


async def test_ensure_temp_folder_creates_missing_root(quarkOrigin):
    client = buildQuarkClient(quarkOrigin)
    fid = await client.ensureTempFolder("task-9")
    assert fid == "fid-task-9"
    created = [call["body"]["file_name"] for call in quarkOrigin.calls if call["method"] == "POST"]
    assert created == ["_ghd_tmp", "task-9"]
    assert quarkOrigin.calls[0]["query"]["_sort"] == "file_type:asc,file_name:asc"


async def test_ensure_temp_folder_reuses_existing(quarkOrigin):
    quarkOrigin.drive["0"] = [{
        "fid": "tmp-root", "file_name": "_ghd_tmp", "size": 0, "dir": True,
    }]
    quarkOrigin.drive["tmp-root"] = [{
        "fid": "already", "file_name": "task-7", "size": 0, "dir": True,
    }]
    client = buildQuarkClient(quarkOrigin)
    assert await client.ensureTempFolder("task-7") == "already"
    assert [call for call in quarkOrigin.calls if call["method"] == "POST"] == []


async def test_fetch_temp_folders_lists_children(quarkOrigin):
    quarkOrigin.drive["0"] = [{
        "fid": "tmp-root", "file_name": "_ghd_tmp", "size": 0, "dir": True,
    }]
    quarkOrigin.drive["tmp-root"] = [
        {"fid": "c1", "file_name": "task-1", "size": 0, "dir": True},
        {"fid": "c2", "file_name": "task-2", "size": 0, "dir": True},
        {"fid": "f1", "file_name": "note.txt", "size": 4, "dir": False},
    ]
    folders = await buildQuarkClient(quarkOrigin).fetchTempFolders()
    assert [item.name for item in folders] == ["task-1", "task-2"]
    assert all(item.isDir for item in folders)


async def test_save_share_files_polls_until_fids_return(quarkOrigin):
    files = [
        ShareEntry("file-b", "b.txt", 12, False, "tok-b", "b.txt"),
        ShareEntry("file-a", "a.txt", 3, False, "tok-a", "目录甲/a.txt"),
    ]
    result = await buildQuarkClient(quarkOrigin).saveShareFiles("pwd1", "stoken-1", files, "task-42")

    assert result.taskId == "task-remote-1"
    assert result.folderFid == "fid-task-42"
    assert result.savedFids == ["saved-b", "saved-a"]
    assert result.savedFidBySource == {"file-b": "saved-b", "file-a": "saved-a"}
    assert quarkOrigin.taskHits == 2
    assert [item["retry_index"] for item in quarkOrigin.taskQueries] == ["0", "1"]
    assert quarkOrigin.saveBody == {
        "fid_list": ["file-b", "file-a"],
        "fid_token_list": ["tok-b", "tok-a"],
        "to_pdir_fid": "fid-task-42",
        "pwd_id": "pwd1",
        "stoken": "stoken-1",
        "pdir_fid": "0",
        "scene": "link",
    }


async def test_fetch_download_urls_maps_fids(quarkOrigin):
    links = await buildQuarkClient(quarkOrigin).fetchDownloadUrls(["f1", "f2"])
    assert links["f1"].url == "https://download.example/f1"
    assert links["f2"].fid == "f2"
    body = next(call["body"] for call in quarkOrigin.calls if call["path"].endswith("/file/download"))
    assert body == {"fids": ["f1", "f2"]}


async def test_delete_remote_skips_empty_list(quarkOrigin):
    created: list[bool] = []

    def factory(*, headers=None, **_kwargs):
        created.append(True)
        return buildQuarkTestClient(headers=headers)

    client = QuarkClient(
        clientFactory=factory,
        readCookie=lambda: RAW_COOKIE,
        driveBase=quarkOrigin.driveBase,
        panBase=quarkOrigin.panBase,
        authBase=quarkOrigin.authBase,
    )
    await client.deleteRemote([])
    assert created == []
    assert quarkOrigin.calls == []


async def test_delete_remote_rejects_root(quarkOrigin):
    created: list[bool] = []

    def factory(*, headers=None, **_kwargs):
        created.append(True)
        return buildQuarkTestClient(headers=headers)

    client = QuarkClient(
        clientFactory=factory,
        readCookie=lambda: RAW_COOKIE,
        driveBase=quarkOrigin.driveBase,
        panBase=quarkOrigin.panBase,
        authBase=quarkOrigin.authBase,
    )
    with pytest.raises(TaskError, match="根目录"):
        await client.deleteRemote(["0"])
    assert created == []
    assert quarkOrigin.calls == []


async def test_delete_remote_sends_body(quarkOrigin):
    client = buildQuarkClient(quarkOrigin, readCookie=lambda: RAW_COOKIE)
    await client.deleteRemote(["fid-1", "fid-2"])
    assert quarkOrigin.deleteBodies == [{
        "action_type": 2,
        "filelist": ["fid-1", "fid-2"],
        "exclude_fids": [],
    }]
    call = next(item for item in quarkOrigin.calls if item["path"].endswith("/file/delete"))
    assert call["referer"] == "https://pan.quark.cn/"
    assert call["cookie"] == RAW_COOKIE


async def test_drive_requests_send_pc_user_agent_and_param(quarkOrigin):
    client = buildQuarkClient(quarkOrigin, readCookie=lambda: RAW_COOKIE)
    await client.fetchDriveFiles()
    await client.fetchDownloadUrls(["f1"])

    assert len(quarkOrigin.calls) == 2
    for call in quarkOrigin.calls:
        assert call["userAgent"] == QUARK_PC_UA
        assert call["query"]["uc_param_str"] == ""
        assert call["query"]["pr"] == "ucpro"
        assert call["query"]["fr"] == "pc"


async def test_fetch_download_urls_reports_http_400_detail(quarkOrigin):
    quarkOrigin.downloadError = (400, {"code": 23018, "message": "需要桌面客户端"})
    client = buildQuarkClient(quarkOrigin, readCookie=lambda: RAW_COOKIE)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        with pytest.raises(TaskError) as caught:
            await client.fetchDownloadUrls(["f1"])
    finally:
        logger.remove(sink)

    message = str(caught.value)
    assert "400" in message
    assert "23018" in message
    assert "需要桌面客户端" in message
    assert caught.value.params["status"] == 400
    assert RAW_COOKIE not in message
    assert "fake-puus-value" not in "\n".join(lines)
    assert "23018" in "\n".join(lines)


def test_merge_cookies_overrides_same_name_and_appends():
    assert mergeCookies("__pus=a; __puus=old", "__puus=new; extra=1") == "__pus=a; __puus=new; extra=1"
    assert mergeCookies("", "__pus=a") == "__pus=a"
    assert mergeCookies("__pus=a", "") == "__pus=a"
    assert mergeCookies("__pus=a;; =blank; __puus=", "__puus=new; __pus=a2") == "__pus=a2; __puus=new"
    assert mergeCookies("__pus=a", "__pus=b; __pus=c") == "__pus=c"


async def test_response_cookie_refreshes_current_cookie_and_callback(quarkOrigin):
    quarkOrigin.responseCookies = {"__puus": "refreshed-val"}
    updates: list[str] = []
    client = buildQuarkClient(
        quarkOrigin,
        readCookie=lambda: "__pus=alpha-value; __puus=old",
        onCookieUpdated=updates.append,
    )

    await client.fetchDriveFiles()

    assert len(updates) == 1
    assert "__puus=refreshed-val" in updates[0]
    assert "__pus=alpha-value" in updates[0]
    assert client.currentCookie() == updates[0]
    assert "refreshed-val" not in quarkOrigin.calls[0]["cookie"]

    await client.fetchDriveFiles()

    assert len(updates) == 1
    assert "__puus=refreshed-val" in quarkOrigin.calls[1]["cookie"]


async def test_qr_requests_do_not_merge_response_cookie(quarkOrigin):
    quarkOrigin.responseCookies = {"__puus": "qr-refreshed-val"}
    updates: list[str] = []
    client = buildQuarkClient(
        quarkOrigin, readCookie=lambda: "__pus=alpha-value", onCookieUpdated=updates.append,
    )
    before = client.currentCookie()

    token = await client.fetchQrToken()
    await client.pollQr(token)

    assert updates == []
    assert client.currentCookie() == before
    assert all(call["cookie"] == "" for call in quarkOrigin.calls)


async def test_switching_read_cookie_drops_merged_cookie(quarkOrigin):
    quarkOrigin.responseCookies = {"__puus": "refreshed-val"}
    configured = {"value": "__pus=alpha-value"}
    client = buildQuarkClient(
        quarkOrigin, readCookie=lambda: configured["value"], onCookieUpdated=lambda _v: None,
    )

    await client.fetchDriveFiles()
    assert "__puus=refreshed-val" in client.currentCookie()

    configured["value"] = "__pus=beta-value"
    assert client.currentCookie() == "__pus=beta-value"

    configured["value"] = "__pus=alpha-value"
    client.clearCookie()
    assert client.currentCookie() == "__pus=alpha-value"
