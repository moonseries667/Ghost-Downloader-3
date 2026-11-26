from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from app.models.pack import PackServices
from app.models.task import (
    ResourceTaskOptions, SpecialFileSize, TaskError, TaskOptions, TaskStatus,
)
from http_pack.pack import HttpPack
from http_pack.task import HttpTask
from tests.helpers import buildFileContent, buildRangeHandler, waitUntil

CONTENT = buildFileContent(1000)


@pytest.fixture(params=["desktop", "android"])
def engine(startEngine, request):
    return startEngine(request.param)


@pytest.fixture
def http(engine):
    services = PackServices(coroutineRunner=engine.coroutineRunner, speedMeter=engine.speedMeter)
    engine.featureService.register(HttpPack(services))
    return engine.featureService


def buildNamedHandler(name: str, requests: list[web.Request] | None = None):
    rangeHandler = buildRangeHandler(CONTENT)

    async def handler(request: web.Request) -> web.Response:
        if requests is not None:
            requests.append(request)
        response = await rangeHandler(request)
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return response
    return handler


async def test_parse_unclaimed_link_adds_plain_file(http, startServer):
    url = await startServer(buildNamedHandler("report.bin"))

    task = await http.parse(TaskOptions(url=f"{url}?id=1"))

    assert isinstance(task, HttpTask)
    assert task.name == "report.bin"
    assert task.fileSize == len(CONTENT)
    assert task.url == f"{url}?id=1"


async def test_parse_many_links_to_one_server(http, startServer):
    url = await startServer(buildNamedHandler("report.bin"))

    tasks = await asyncio.wait_for(
        asyncio.gather(*(http.parse(TaskOptions(url=f"{url}?id={i}")) for i in range(12))), 10)

    assert [task.name for task in tasks] == ["report.bin"] * 12


async def test_parse_resource_with_size_sends_no_request(http, startServer):
    requests = []
    url = await startServer(buildNamedHandler("a.zip", requests))

    task = await http.parse(ResourceTaskOptions(url=url, name="a.zip", size=len(CONTENT), canUseRangeRequests=True))

    assert task.name == "a.zip"
    assert requests == []


async def test_parse_unknown_size_single_stream_resource_skips_probe_and_downloads_once(
    engine, http, startServer, downloadFolder,
):
    requests = []

    async def chunkedStream(request: web.Request) -> web.StreamResponse:
        requests.append(request)
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/zip"})
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(CONTENT)
        await response.write_eof()
        return response

    url = await startServer(chunkedStream)
    options = ResourceTaskOptions(url=url, name="a.zip", size=0, canUseRangeRequests=False)

    task = await http.parse(options)

    assert isinstance(task, HttpTask)
    assert task.name == "a.zip"
    assert task.fileSize == SpecialFileSize.UNKNOWN
    assert not task.steps[0].canUseRangeRequests
    assert requests == []

    engine.taskService.add(task)
    await waitUntil(lambda: task.status == TaskStatus.COMPLETED)

    assert len(requests) == 1
    assert (downloadFolder / "a.zip").read_bytes() == CONTENT


@pytest.mark.parametrize("contentType", [
    "text/html; charset=utf-8",
    "application/xhtml+xml; charset=utf-8",
])
async def test_probe_rejects_html_for_non_html_path(http, startServer, contentType):
    async def htmlPage(request: web.Request) -> web.Response:
        return web.Response(body=b"<html><body>sign in</body></html>", headers={"Content-Type": contentType})

    url = await startServer(htmlPage)

    with pytest.raises(TaskError, match="网页内容"):
        await http.parse(TaskOptions(url=f"{url.replace('/file', '/download')}?id=1"))


@pytest.mark.parametrize("path", ["/guide.html", "/guide.htm", "/guide.xhtml"])
async def test_probe_allows_html_for_explicit_html_path(http, startServer, path):
    async def htmlPage(request: web.Request) -> web.Response:
        return web.Response(body=b"<html><body>doc</body></html>", headers={"Content-Type": "text/html; charset=utf-8"})

    url = await startServer(htmlPage)

    task = await http.parse(TaskOptions(url=url.replace("/file", path)))

    assert isinstance(task, HttpTask)
    assert task.name == path.lstrip("/")


async def test_probe_allows_html_attachment(http, startServer):
    async def htmlAttachment(request: web.Request) -> web.Response:
        return web.Response(
            body=b"<html><body>report</body></html>",
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Disposition": 'attachment; filename="report.html"',
            },
        )

    url = await startServer(htmlAttachment)

    task = await http.parse(TaskOptions(url=f"{url.replace('/file', '/download')}?id=1"))

    assert isinstance(task, HttpTask)
    assert task.name == "report.html"
