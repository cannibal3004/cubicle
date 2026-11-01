"""Web previews: PREFIX/<id>/... relayed to a web server in a bot's computer (services/previews).

Outside /api/v1, so the API's sign-in middleware does not apply: a page being opened is checked
here (signed in, and able to use the bot), and the page's own requests go by the preview's id.
"""

from __future__ import annotations

import asyncio
import html
from typing import Optional

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.services import previews
from app.services.auth_service import auth_service

router = APIRouter(tags=["previews"])

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
MAX_BODY = 50 * 1024 * 1024
_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
    return _client


def notice(status: int, title: str, text: str, *, sign_in: bool = False) -> HTMLResponse:
    """A small page in place of the preview: ended, not allowed, or the computer is stopped."""
    link = '<p><a href="/">Open Cubicle to sign in</a>, then open the preview again.</p>' if sign_in else ""
    body = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font:15px system-ui,sans-serif;background:#111114;color:#ddd;display:grid;place-items:center;height:100vh;margin:0'>"
        f"<div style='max-width:32rem;padding:1.5rem'><h2 style='margin:0 0 .5rem'>{html.escape(title)}</h2><p>{html.escape(text)}</p>{link}</div>"
    )
    return HTMLResponse(body, status_code=status, headers={"Content-Security-Policy": previews.SANDBOX, "Cache-Control": "no-store"})


@router.api_route(previews.PREFIX + "/{preview_id}", methods=METHODS, include_in_schema=False)
async def preview_root(preview_id: str, request: Request):
    query = request.url.query
    return RedirectResponse(f"{previews.PREFIX}/{preview_id}/" + (f"?{query}" if query else ""), status_code=307)


@router.api_route(previews.PREFIX + "/{preview_id}/{path:path}", methods=METHODS, include_in_schema=False)
async def preview_relay(preview_id: str, path: str, request: Request):
    record = previews.get(preview_id)
    if record is None:
        return notice(404, "This preview has ended", "Its time ran out, the bot closed it, or the address is wrong. Ask the bot to open it again.")
    if previews.is_document(dict(request.headers)) and not previews.may_view(auth_service.authenticate_request(request), record):
        return notice(401, "Sign in to see this preview", "Previews are open to the people who can use the bot that made them.", sign_in=True)
    try:
        found = previews.target(record)
    except previews.PreviewError as exc:
        return notice(503, "The preview is not available", str(exc))
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return Response("Request too large for a preview.", status_code=413)
    body = await request.body()
    headers = previews.request_headers(record, list(request.headers.items()), site=request.headers.get("host", ""))
    upstream = previews.upstream_path(record, path, request.url.query)

    async def send(base: str, extra: dict) -> httpx.Response:
        outgoing = client().build_request(request.method, base + upstream, headers={**headers, **extra}, content=body)
        return await client().send(outgoing, stream=True)

    try:
        if previews.relay_missing(found["key"]):
            reply = await send(found["direct"], {})
        else:
            reply = await send(found["relay"], found["headers"])
            if reply.status_code in (400, 502) and "json" in reply.headers.get("content-type", ""):
                content = await reply.aread()
                await reply.aclose()
                if previews.old_driver_reply(reply.status_code, content):
                    previews.mark_relay_missing(found["key"])
                    reply = await send(found["direct"], {})
                elif b"preview_unreachable" in content[:500]:
                    return notice(502, "Nothing is answering", f"Nothing is listening on port {record['port']} in the bot's computer right now. "
                                  "Ask the bot to start its server again.")
                else:
                    return Response(content, status_code=reply.status_code, headers=dict(previews.response_headers(record, list(reply.headers.items()))))
    except httpx.HTTPError:
        return notice(502, "Nothing is answering", f"Port {record['port']} in the bot's computer did not answer. Ask the bot to start its server again.")

    out_headers = previews.response_headers(record, list(reply.headers.multi_items()))
    if previews.is_html(list(reply.headers.multi_items())) and request.method != "HEAD":
        content = previews.with_storage_shim(await reply.aread())
        await reply.aclose()
        response = Response(content, status_code=reply.status_code)
    else:
        response = StreamingResponse(reply.aiter_raw(), status_code=reply.status_code, background=BackgroundTask(reply.aclose))
    for name, value in out_headers:
        response.headers.append(name, value)
    return response


@router.websocket(previews.PREFIX + "/{preview_id}/{path:path}")
async def preview_socket(websocket: WebSocket, preview_id: str, path: str):
    """A WebSocket the page opens (a game's server, a dev server's reload channel), by the preview's id."""
    record = previews.get(preview_id)
    if record is None:
        await websocket.close(code=4404)
        return
    try:
        found = previews.target(record)
    except previews.PreviewError:
        await websocket.close(code=4409)
        return
    upstream = previews.upstream_path(record, path, websocket.url.query)
    requested = websocket.headers.get("sec-websocket-protocol")
    protocols = [p.strip() for p in requested.split(",")] if requested else None
    origin = f"http://localhost:{record['port']}"
    attempts = [(found["direct_ws"], {})] if previews.relay_missing(found["key"]) else [(found["relay_ws"], found["headers"]), (found["direct_ws"], {})]
    connection = None
    for base, extra in attempts:
        try:
            connection = await websockets.connect(base + upstream, additional_headers=extra, subprotocols=protocols, origin=origin,
                                                  max_size=None, ping_interval=None, open_timeout=10)
            break
        except Exception:
            continue
    if connection is None:
        await websocket.close(code=4502)
        return
    await websocket.accept(subprotocol=connection.subprotocol)

    async def browser_to_app():
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await connection.send(message["bytes"])
            elif message.get("text") is not None:
                await connection.send(message["text"])

    async def app_to_browser():
        async for frame in connection:
            if isinstance(frame, (bytes, bytearray)):
                await websocket.send_bytes(bytes(frame))
            else:
                await websocket.send_text(str(frame))

    try:
        tasks = [asyncio.create_task(browser_to_app()), asyncio.create_task(app_to_browser())]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except Exception:
        pass
    finally:
        await connection.close()
        try:
            await websocket.close()
        except Exception:
            pass
