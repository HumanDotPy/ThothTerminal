"""Stream the installed, authenticated Harness UI through its supported mount.

The target is process-owned and fixed. Browser cookies, Authorization, forwarded
hosts and user-selected upstream URLs never pass this boundary. Native browser
authentication stays server-side, and native API/WebSocket payloads stay native.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import aiohttp
from yarl import URL
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from thoth_terminal.web.deepseek_harness_service import HarnessUnavailable, harness_service
from starlette.concurrency import run_in_threadpool

_REQUEST_HEADERS = {"content-type", "accept", "accept-encoding", "range", "if-none-match", "if-modified-since"}
_RESPONSE_HEADERS = {"content-type", "content-encoding", "content-length", "etag", "last-modified", "accept-ranges", "content-range", "content-disposition", "cache-control", "vary"}


async def native_http(request: Request, path: str) -> Response:
    origin, cookie = await run_in_threadpool(harness_service.browser_connection)
    # A decoded path cannot escape the fixed mount into another authority or
    # smuggle a query. The native raw query is forwarded separately below.
    if any(part in {".", ".."} for part in path.split("/")) or "\\" in path or "?" in path or "#" in path:
        return Response(status_code=400)
    headers = {key: value for key, value in request.headers.items() if key.lower() in _REQUEST_HEADERS}
    if path in {"", "index.html"}:
        headers["accept-encoding"] = "identity"
    headers.update({"Cookie": cookie, "Origin": origin.rstrip("/"), "Sec-Fetch-Site": "same-origin"})
    client = aiohttp.ClientSession(auto_decompress=False, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=120))
    upstream = None

    async def close() -> None:
        if upstream is not None:
            upstream.close()
        await client.close()

    try:
        # Native plugin combo URLs use ??package/file&rev=hash. Parsing/rebuilding
        # them as ordinary query parameters changes their protocol and yields 404.
        query = request.scope.get("query_string", b"").decode("ascii")
        target = URL(origin + quote(path, safe="/@") + ("?" + query if query else ""), encoded=True)
        upstream = await client.request(request.method, target, headers=headers, data=request.stream() if request.method == "POST" else None, allow_redirects=False)
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() in _RESPONSE_HEADERS}
        # Index is small and gets one composition script, ahead of native modules.
        # Request it uncompressed so no bundle rewriting or decompression is needed.
        if path in {"", "index.html"} and upstream.status == 200:
            html = (await upstream.read()).decode("utf-8")
            html = html.replace("</head>", '<script src="/static/harness_theme.js?v=20261005-1"></script></head>', 1)
            await close()
            return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        # Never forward the native authentication exchange or cookie to a browser.
        if upstream.status in {301, 302, 303, 307, 308}:
            await close()
            return Response("Harness authentication changed; reload the workspace.", status_code=503)

        async def chunks():
            try:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                await close()

        return StreamingResponse(chunks(), status_code=upstream.status, headers=response_headers, background=BackgroundTask(close))
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await close()
        raise HarnessUnavailable("Harness connection was interrupted. Reload the workspace.") from None
    except BaseException:
        await close()
        raise


async def native_socket(socket: WebSocket) -> None:
    origin, cookie = await run_in_threadpool(harness_service.browser_connection)
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=None, sock_connect=5)) as client:
        async with client.ws_connect(origin.replace("http://", "ws://", 1) + "api/remote.mux", headers={"Cookie": cookie, "Origin": origin.rstrip("/"), "Sec-Fetch-Site": "same-origin"}, max_msg_size=0, compress=0) as upstream:
            await socket.accept()

            async def to_native():
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send_str(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send_bytes(message["bytes"])

            async def to_browser():
                async for message in upstream:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await socket.send_text(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await socket.send_bytes(message.data)

            tasks = [asyncio.create_task(to_native()), asyncio.create_task(to_browser())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except WebSocketDisconnect:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await upstream.close()
                if socket.client_state.name != "DISCONNECTED":
                    await socket.close()
