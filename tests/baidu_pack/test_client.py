from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.models.task import TaskError
from baidu_pack.client import (
    BaiduWebClient,
    HttpResponseData,
    extractCookieValue,
    mergeCookies,
    normalizeCookie,
    toMaskedCookie,
)


def jsonResponse(payload: dict, *, status: int = 200, headers: dict[str, list[str]] | None = None) -> HttpResponseData:
    return HttpResponseData(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers or {},
    )


def test_cookie_helpers_normalize_merge_and_mask():
    raw = "Cookie: BDUSS=secret-bduss-value; STOKEN=secret-stoken-val; BDUSS=dup\n"
    normalized = normalizeCookie(raw)
    assert normalized == "BDUSS=secret-bduss-value; STOKEN=secret-stoken-val"
    assert extractCookieValue(normalized, "bduss") == "secret-bduss-value"

    merged = mergeCookies(normalized, "STOKEN=new-stoken; BAIDUID=abc123")
    assert extractCookieValue(merged, "STOKEN") == "new-stoken"
    assert extractCookieValue(merged, "BAIDUID") == "abc123"

    masked = toMaskedCookie(normalized)
    assert "secret-bduss-value" not in masked
    assert "secret-stoken-val" not in masked
    assert "BDUSS=" in masked and "STOKEN=" in masked


async def test_fetch_share_tree_readonly_recursive_and_paginated():
    calls: list[tuple[str, str, dict[str, str]]] = []

    async def fakeRequest(method: str, url: str, *, headers=None, body=None):
        headers = headers or {}
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        calls.append((method, parsed.path, headers))

        if parsed.path == "/share/verify":
            assert body is not None and b"pwd=ab12" in body
            return jsonResponse({"errno": 0, "randsk": "rand-token-xyz"})

        if parsed.path == "/share/list":
            assert "BDCLND=rand-token-xyz" in headers.get("cookie", "")
            remoteDir = (query.get("dir") or [""])[0]
            page = int((query.get("page") or ["1"])[0])
            if not remoteDir:
                if page == 1:
                    return jsonResponse({
                        "errno": 0,
                        "title": "合集包",
                        "list": [
                            {"fs_id": 1001, "server_filename": "文档", "isdir": 1, "size": 0, "path": "/文档"},
                            {"fs_id": 1002, "server_filename": "readme.txt", "isdir": 0, "size": 128, "path": "/readme.txt"},
                        ],
                    })
                return jsonResponse({"errno": 0, "list": []})
            if remoteDir == "/文档":
                return jsonResponse({
                    "errno": 0,
                    "list": [
                        {"fs_id": 2001, "server_filename": "guide.pdf", "isdir": 0, "size": 2048, "path": "/文档/guide.pdf"},
                    ],
                })
        raise AssertionError(f"unexpected request: {method} {url}")

    client = BaiduWebClient(request=fakeRequest)
    tree = await client.fetchShareTree("1ShareTreeKey", "ab12")

    assert tree.title == "合集包"
    assert tree.shareKey == "1ShareTreeKey"
    assert [(item.fsId, item.relativePath, item.size) for item in tree.files] == [
        ("2001", "文档/guide.pdf", 2048),
        ("1002", "readme.txt", 128),
    ]
    assert not any("transfer" in path for _method, path, _headers in calls)


async def test_verify_share_maps_passcode_and_expired_errors():
    async def wrongPwd(method: str, url: str, *, headers=None, body=None):
        return jsonResponse({"errno": -9, "err_msg": "提取码错误"})

    client = BaiduWebClient(request=wrongPwd)
    with pytest.raises(TaskError) as excInfo:
        await client.fetchShareTree("1BadPwdKey", "0000")
    assert excInfo.value.params.get("kind") == "passcode"

    async def expired(method: str, url: str, *, headers=None, body=None):
        return jsonResponse({"errno": 105, "err_msg": "分享已失效"})

    clientExpired = BaiduWebClient(request=expired)
    with pytest.raises(TaskError) as excExpired:
        await clientExpired.fetchShareTree("1ExpiredKey", "")
    assert excExpired.value.params.get("kind") == "expired"


async def test_passport_qr_login_flow_exchanges_bduss_and_stoken():
    polls = iter([
        jsonResponse({"errno": 1}),
        jsonResponse({"errno": 0, "channel_v": json.dumps({"status": 1})}),
        jsonResponse({"errno": 0, "channel_v": json.dumps({"status": 0, "v": "temp-qr-token"})}),
    ])

    async def fakeRequest(method: str, url: str, *, headers=None, body=None):
        parsed = urlsplit(url)
        if parsed.path == "/v2/api/getqrcode":
            return jsonResponse({"errno": 0, "sign": "qr-sign-1", "imgurl": "passport.baidu.com/v2/api/qrcode?sign=qr-sign-1"})
        if parsed.path == "/v2/api/qrcode":
            return HttpResponseData(status=200, body=b"\x89PNG\r\nfake-qr", headers={})
        if parsed.path == "/channel/unicast":
            return next(polls)
        if parsed.path == "/v3/login/main/qrbdusslogin":
            return HttpResponseData(
                status=200,
                body=b"{}",
                headers={"set-cookie": ["BDUSS=real-bduss-token; Path=/; HttpOnly", "PTOKEN=pt-1; Path=/"]},
            )
        if parsed.path == "/disk/main":
            assert "BDUSS=real-bduss-token" in (headers or {}).get("cookie", "")
            return HttpResponseData(
                status=200,
                body=b"ok",
                headers={"set-cookie": ["STOKEN=real-stoken-token; Path=/"]},
            )
        raise AssertionError(f"unexpected url: {url}")

    client = BaiduWebClient(request=fakeRequest)
    session = await client.fetchQrSession()
    assert session.sign == "qr-sign-1"
    assert session.qrImageBytes.startswith(b"\x89PNG")

    assert (await client.pollQrStatus(session)).kind == "waiting"
    assert (await client.pollQrStatus(session)).kind == "scanned"
    confirmed = await client.pollQrStatus(session)
    assert confirmed.kind == "confirmed"
    assert confirmed.token == "temp-qr-token"

    cookie = await client.exchangeQrCookie(confirmed.token, session.gid)
    assert extractCookieValue(cookie, "BDUSS") == "real-bduss-token"
    assert extractCookieValue(cookie, "STOKEN") == "real-stoken-token"
