from __future__ import annotations

import pytest
from aiohttp import web

from app.client import buildClient
from quark_pack.client import QuarkClient


def buildQuarkTestClient(*, headers=None, userAgent=None, **_kwargs):
    return buildClient(
        emulation=None, headers=headers, userAgent=userAgent, shouldVerifySsl=False,
    )


def buildQuarkClient(
    origin: QuarkOrigin,
    *,
    pageSize: int = 50,
    saveBatchSize: int = 50,
    readCookie=None,
    onCookieUpdated=None,
) -> QuarkClient:
    return QuarkClient(
        clientFactory=buildQuarkTestClient,
        readCookie=readCookie or (lambda: "__pus=fake-pus-value"),
        driveBase=origin.driveBase,
        panBase=origin.panBase,
        authBase=origin.authBase,
        pageSize=pageSize,
        saveBatchSize=saveBatchSize,
        onCookieUpdated=onCookieUpdated,
    )


def qrPayload(step: str) -> dict:
    if step == "waiting":
        return {"status": 50004001, "data": {"members": {}}}
    if step == "scanned":
        return {"status": 50004003, "data": {"members": {}}}
    if step == "success":
        return {"status": 2000000, "data": {"members": {"service_ticket": "fake-service-ticket"}}}
    return {"status": 50004002, "data": {"members": {}}}


class QuarkOrigin:
    """本机回环上的夸克网盘。"""

    def __init__(self) -> None:
        self.driveBase = ""
        self.panBase = ""
        self.authBase = ""
        self.calls: list[dict] = []
        self.tokenError: dict | None = None
        self.forceLoggedOut = False
        self.accountBody = {
            "code": "OK",
            "data": {
                "nickname": "测试用户",
                "uid": "uid-1",
                "member_type": "SUPER_VIP",
                "super_vip_exp_at": 1893456000000,
            },
        }
        self.shareLists = {
            "0": [
                {
                    "fid": "dir-1", "file_name": "目录甲", "size": 0,
                    "dir": True, "share_fid_token": "tok-dir",
                },
                {
                    "fid": "file-b", "file_name": "b.txt", "size": 12,
                    "dir": False, "share_fid_token": "tok-b",
                },
            ],
            "dir-1": [
                {
                    "fid": "file-a", "file_name": "a.txt", "size": 3,
                    "dir": False, "share_fid_token": "tok-a",
                },
            ],
        }
        self.drive: dict[str, list[dict]] = {"0": []}
        self.savedFids = ["saved-b", "saved-a"]
        self.taskDoneAfter = 2
        self.taskHits = 0
        self.taskQueries: list[dict] = []
        self.taskPolls: dict[str, int] = {}
        self.saveBody: dict | None = None
        self.saveBodies: list[dict] = []
        self.mapSavedFids = False
        self.omitSaveAsFor: set[str] = set()
        self.fallbackEntries: dict[str, dict] = {}
        self.failSaves = 0
        self.saveError: tuple[int, dict] | None = None
        self.saveErrorByPdir: dict[str, tuple[int, dict]] = {}
        self.taskFailure: dict | None = None
        self.omitMetadataTotal = False
        self.createFolderDelay = 0.0
        self.downloadRoot = "https://download.example"
        self.deleteBodies: list[dict] = []
        self.qrScript = ["waiting", "scanned", "success"]
        self.qrCursor = 0
        self.successGate = None
        self.responseCookies: dict[str, str] = {}
        self.downloadError: tuple[int, dict] | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.method == "POST" else None
        self.calls.append({
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "body": body,
            "cookie": request.headers.get("Cookie", ""),
            "referer": request.headers.get("Referer", ""),
            "origin": request.headers.get("Origin", ""),
            "userAgent": request.headers.get("User-Agent", ""),
        })
        response = await self.route(request, body)
        for name, value in self.responseCookies.items():
            response.set_cookie(name, value)
        return response

    async def route(self, request: web.Request, body: dict | None) -> web.StreamResponse:
        path = request.path
        if path.endswith("/account/info"):
            return self.accountResponse(request)
        if path.endswith("/getTokenForQrcodeLogin"):
            return web.json_response({
                "status": 2000000,
                "data": {"members": {"token": "qr-token-1"}},
            })
        if path.endswith("/getServiceTicketByQrcodeToken"):
            return await self.qrResponse()
        if path.endswith("/share/sharepage/token"):
            if self.tokenError is not None:
                return web.json_response(self.tokenError)
            return self.ok({"stoken": "stoken-1", "title": "分享标题"})
        if path.endswith("/share/sharepage/detail"):
            pdir = request.query.get("pdir_fid", "0")
            return self.pageOf(self.shareLists.get(pdir, []), request.query)
        if path.endswith("/share/sharepage/save"):
            return self.saveResponse(body or {})
        if path.endswith("/file/sort"):
            pdir = request.query.get("pdir_fid", "0")
            return self.pageOf(self.drive.get(pdir, []), request.query)
        if path.endswith("/file/download"):
            if self.downloadError is not None:
                status, payload = self.downloadError
                return web.json_response(payload, status=status)
            rows = [
                {"fid": fid, "download_url": f"{self.downloadRoot}/{fid}"}
                for fid in (body or {}).get("fids") or []
            ]
            return web.json_response({"code": 0, "status": 200, "message": "ok", "data": rows})
        if path.endswith("/file/delete"):
            self.deleteBodies.append(body or {})
            return self.ok({})
        if path.endswith("/task"):
            return self.taskResponse(request)
        if request.method == "POST" and path.endswith("/file"):
            if self.createFolderDelay > 0:
                import asyncio
                await asyncio.sleep(self.createFolderDelay)
            return self.createFolder(body or {})
        return web.json_response({"code": 404, "message": "not found"}, status=404)

    def accountResponse(self, request: web.Request) -> web.StreamResponse:
        if "st" in request.query:
            response = web.json_response({"code": "OK", "data": {"nickname": "扫码用户"}})
            response.set_cookie("__pus", "qr-pus-value")
            response.set_cookie("__puus", "qr-puus-value")
            return response
        if self.forceLoggedOut:
            return web.json_response({
                "status": 401, "code": 31001, "message": "未登录", "data": None,
            })
        return web.json_response(self.accountBody)

    async def qrResponse(self) -> web.StreamResponse:
        if not self.qrScript:
            step = "expired"
        else:
            step = self.qrScript[min(self.qrCursor, len(self.qrScript) - 1)]
            self.qrCursor += 1
        if step == "success" and self.successGate is not None:
            await self.successGate.wait()
        return web.json_response(qrPayload(step))

    def saveResponse(self, body: dict) -> web.StreamResponse:
        pdir = str(body.get("pdir_fid") or "0")
        if pdir in self.saveErrorByPdir:
            status, payload = self.saveErrorByPdir[pdir]
            return web.json_response(payload, status=status)
        if self.saveError is not None:
            status, payload = self.saveError
            return web.json_response(payload, status=status)
        if self.failSaves > 0:
            self.failSaves -= 1
            return web.json_response({
                "code": 40001, "status": 200, "message": "stoken expired", "data": None,
            })
        taskId = f"task-remote-{len(self.saveBodies) + 1}"
        self.saveBodies.append(body)
        self.saveBody = body
        self.taskPolls[taskId] = 0
        return self.ok({"task_id": taskId})

    def fidsFor(self, body: dict) -> list[str]:
        if self.mapSavedFids:
            return [f"saved-{fid}" for fid in body.get("fid_list") or []]
        return list(self.savedFids)

    def plantFallback(self, body: dict) -> None:
        folder = str(body.get("to_pdir_fid") or "")
        items = []
        for fid in body.get("fid_list") or []:
            item = self.fallbackEntries.get(str(fid))
            if item is not None:
                items.append(dict(item))
        if str(body.get("pdir_fid") or "") in self.omitSaveAsFor:
            items.reverse()
        bucket = self.drive.setdefault(folder, [])
        for item in items:
            if not any(entry.get("fid") == item.get("fid") for entry in bucket):
                bucket.append(item)

    def taskResponse(self, request: web.Request) -> web.StreamResponse:
        self.taskHits += 1
        query = dict(request.query)
        self.taskQueries.append(query)
        if self.taskFailure is not None:
            return web.json_response(self.taskFailure)
        taskId = query.get("task_id", "")
        polls = self.taskPolls.get(taskId, 0) + 1
        self.taskPolls[taskId] = polls
        body = self.saveBody or {}
        for saved in self.saveBodies:
            if f"task-remote-{self.saveBodies.index(saved) + 1}" == taskId:
                body = saved
                break
        if polls < self.taskDoneAfter:
            data = {"status": 0, "task_title": "分享-转存"}
        else:
            data = {"status": 2, "task_title": "分享-转存"}
            self.plantFallback(body)
            if str(body.get("pdir_fid") or "") not in self.omitSaveAsFor:
                data["save_as"] = {"save_as_top_fids": self.fidsFor(body)}
        return web.json_response({"code": 0, "status": 200, "message": "ok", "data": data})

    def createFolder(self, body: dict) -> web.StreamResponse:
        pdir = str(body.get("pdir_fid") or "0")
        name = str(body.get("file_name") or "")
        fid = f"fid-{name}"
        self.drive.setdefault(pdir, []).append({
            "fid": fid, "file_name": name, "size": 0, "dir": True,
        })
        self.drive.setdefault(fid, [])
        return self.ok({"fid": fid})

    def pageOf(self, items: list[dict], query) -> web.StreamResponse:
        page = int(query.get("_page") or 1)
        size = int(query.get("_size") or 50)
        start = (page - 1) * size
        return web.json_response({
            "code": 0,
            "status": 200,
            "message": "ok",
            "data": {"list": items[start:start + size]},
            "metadata": {
                "_total": 0 if self.omitMetadataTotal else len(items),
                "_page": page,
                "_size": size,
            },
        })

    def ok(self, data: dict) -> web.StreamResponse:
        return web.json_response({"code": 0, "status": 200, "message": "ok", "data": data})


@pytest.fixture
async def quarkOrigin(config):
    origin = QuarkOrigin()
    app = web.Application()
    app.router.add_get("/{path:.*}", origin.handle)
    app.router.add_post("/{path:.*}", origin.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    origin.driveBase = f"{base}/drive"
    origin.panBase = f"{base}/pan"
    origin.authBase = f"{base}/uop"
    try:
        yield origin
    finally:
        await runner.cleanup()
