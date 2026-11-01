"""Web previews: a temporary address in Cubicle for a web server a bot runs in its computer.

The bot starts a server (a page it built, a game, a small app) and opens a preview of its port
with computer_preview. The preview lives under PREFIX/<id>/ on Cubicle's own address, so it works
wherever Cubicle is reached (LAN address, tunnel, front proxy) with no DNS or proxy changes. Each
request goes to the computer's driver, which relays it to the port on loopback (so a server that
listens only on localhost works too). A computer still on a runtime without the relay is reached
on its own address instead, which needs a server listening on 0.0.0.0.

Who may look: opening the page (a document request) needs a signed-in person who can use the bot,
its owner or a teammate it is shared with. The page's own requests (scripts, images, fetch calls,
WebSockets) carry no Cubicle cookie, so they are let through by the preview's id, a random value
only someone who could open the page learns.

The page must not act as the person viewing it: it is on Cubicle's origin, where the person's
session lives. So every response carries a sandbox Content-Security-Policy that gives the page an
origin of its own, opaque to the browser: it cannot read Cubicle's cookies or call Cubicle's API
with them (and the API refuses requests from such an origin outright). The price is that the page
has no cookies or storage of its own; a small script added to HTML pages swaps in storage that
lasts until the page is reloaded, so pages that use localStorage still run.

The app sees requests with the prefix removed (it is served at /), unless the preview was opened
with keep_prefix for an app set up to live under the prefix. Absolute-path redirects are put back
under the prefix; links and scripts in pages are not rewritten, so pages should use relative URLs.

A preview ends after its hours (DEFAULT_HOURS unless the bot says), when the bot closes it, or
when the bot is deleted; while the computer is stopped it says so. Previews are kept in
DATA_DIR/previews.json, so an API restart does not end them.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionInvocation, action_gateway
from app.services.identity import get_current_actor, get_current_owner

PREFIX = "/api/preview"
MAX_PER_BOT = 5
DEFAULT_HOURS = 8
MAX_HOURS = 72
TITLE_MAX = 120
INJECT_MAX_BYTES = 8 * 1024 * 1024
OPEN_ACTION = "preview.open"
CLOSE_ACTION = "preview.close"

# The page gets an opaque origin of its own; everything a page or game commonly needs is allowed.
SANDBOX = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads allow-pointer-lock "
           "allow-orientation-lock allow-presentation")
# Storage and cookies throw in an opaque origin; this keeps pages that use them running (in memory).
STORAGE_SHIM = (
    b"<script>(function(){function m(){var d={};return{getItem:function(k){return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null},"
    b"setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},"
    b"key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}}"
    b"['localStorage','sessionStorage'].forEach(function(n){try{window[n].length}catch(e){try{Object.defineProperty(window,n,{value:m(),configurable:true})}catch(_){}}});"
    b"try{document.cookie}catch(e){try{var c='';Object.defineProperty(document,'cookie',{get:function(){return c},set:function(v){},configurable:true})}catch(_){}}"
    b"})();</script>"
)
_HEAD = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_HTML = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)
_ABSOLUTE_REF = re.compile(r"""(?:src|href|action)\s*=\s*["'](/(?!/)[^"'\s>]*)""", re.IGNORECASE)
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
               "transfer-encoding", "upgrade"}

_lock = threading.Lock()


class PreviewError(RuntimeError):
    pass


# ---- the records ---------------------------------------------------------------------------------


def _store_path() -> Path:
    return Path(settings.DATA_DIR) / "previews.json"


def _now() -> datetime:
    return datetime.now()


def _load() -> List[Dict[str, Any]]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    now = _now().isoformat()
    return [p for p in data if isinstance(p, dict) and p.get("id") and str(p.get("expires_at") or "") > now]


def _save(records: List[Dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, indent=1), encoding="utf-8")
    temporary.replace(path)


def base_path(record: Dict[str, Any]) -> str:
    return f"{PREFIX}/{record['id']}/"


def url_of(record: Dict[str, Any]) -> str:
    return base_path(record) + str(record.get("path") or "").lstrip("/")


def get(preview_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return next((p for p in _load() if p["id"] == preview_id), None)


def list_for_bot(owner_id: str, bot_id: str) -> List[Dict[str, Any]]:
    with _lock:
        return [p for p in _load() if p.get("owner_id") == owner_id and p.get("bot_id") == bot_id]


def save_preview(*, owner_id: str, bot_id: str, port: int, title: str = "", path: str = "", keep_prefix: bool = False,
                 hours: float = DEFAULT_HOURS, created_by: str = "") -> Dict[str, Any]:
    """Open a preview of the port, or renew the one already open for it (same address)."""
    now = _now()
    with _lock:
        records = _load()
        record = next((p for p in records if p.get("owner_id") == owner_id and p.get("bot_id") == bot_id and p.get("port") == port), None)
        if record is None:
            if sum(1 for p in records if p.get("owner_id") == owner_id and p.get("bot_id") == bot_id) >= MAX_PER_BOT:
                raise PreviewError(f"You already have {MAX_PER_BOT} previews open; close one with computer_preview_close first.")
            record = {"id": secrets.token_urlsafe(18), "owner_id": owner_id, "bot_id": bot_id, "port": port, "created_at": now.isoformat()}
            records.append(record)
        record.update(title=title or record.get("title") or f"Port {port}", path=path, keep_prefix=bool(keep_prefix),
                      expires_at=(now + timedelta(hours=hours)).isoformat(timespec="seconds"), created_by=created_by or record.get("created_by"))
        _save(records)
        return dict(record)


def close_previews(owner_id: str, bot_id: str, port: Optional[int] = None) -> int:
    with _lock:
        records = _load()
        kept = [p for p in records if not (p.get("owner_id") == owner_id and p.get("bot_id") == bot_id and (port is None or p.get("port") == port))]
        _save(kept)
        return len(records) - len(kept)


def card(record: Dict[str, Any]) -> Dict[str, Any]:
    """The chat card: carried with the reply like a shared file's."""
    return {"kind": "preview", "source": "preview", "path": record["id"], "name": record.get("title") or f"Port {record['port']}",
            "url": url_of(record), "port": record["port"], "expires_at": record.get("expires_at")}


# ---- relaying ------------------------------------------------------------------------------------


def upstream_path(record: Dict[str, Any], path: str, query: str = "") -> str:
    """The path the app is asked for: under the prefix when it was set up for it, else from /."""
    path = path.lstrip("/")
    full = base_path(record) + path if record.get("keep_prefix") else "/" + path
    return f"{full}?{query}" if query else full


def rewrite_location(record: Dict[str, Any], value: str) -> str:
    """A redirect the app sends, put back under the preview's address."""
    parts = urlsplit(value)
    if parts.netloc:
        if (parts.hostname or "") not in _LOOPBACK:
            return value  # somewhere else on the web
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    else:
        path = value
    if not path.startswith("/"):
        return path  # relative: the browser resolves it under the preview already
    if record.get("keep_prefix") and path.startswith(base_path(record)):
        return path
    return base_path(record) + path.lstrip("/")


def request_headers(record: Dict[str, Any], headers: List[Tuple[str, str]], *, site: str = "") -> Dict[str, str]:
    """What the app is sent: no Cubicle credentials, no compression (HTML gets a script added), and an
    Origin it will accept from itself."""
    out: Dict[str, str] = {}
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_HEADERS or lower in {"host", "cookie", "authorization", "content-length", "accept-encoding", "referer"}:
            continue
        out[lower] = f"{out[lower]}, {value}" if lower in out else value
    if "origin" in out:
        out["origin"] = f"http://localhost:{record['port']}"
    out["accept-encoding"] = "identity"
    out["x-forwarded-prefix"] = base_path(record).rstrip("/")
    if site:
        out["x-forwarded-host"] = site
    return out


def response_headers(record: Dict[str, Any], headers: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_HEADERS or lower in {"content-length", "set-cookie", "x-frame-options", "strict-transport-security"}:
            continue
        if lower == "location":
            value = rewrite_location(record, value)
        out.append((name, value))
    out.append(("Content-Security-Policy", SANDBOX))
    out.append(("Referrer-Policy", "no-referrer"))
    if not any(n.lower() == "cache-control" for n, _ in out):
        out.append(("Cache-Control", "no-cache"))
    return out


def is_html(headers: List[Tuple[str, str]]) -> bool:
    kind = next((v for n, v in headers if n.lower() == "content-type"), "")
    encoded = next((v for n, v in headers if n.lower() == "content-encoding"), "identity")
    return kind.lower().startswith("text/html") and encoded.lower() in ("", "identity")


def with_storage_shim(body: bytes) -> bytes:
    if len(body) > INJECT_MAX_BYTES:
        return body
    for pattern in (_HEAD, _HTML):
        match = pattern.search(body)
        if match:
            return body[:match.end()] + STORAGE_SHIM + body[match.end():]
    return STORAGE_SHIM + body


def absolute_references(record: Dict[str, Any], html: str, limit: int = 5) -> List[str]:
    """Paths a page loads from the site root, which miss the preview (it lives under its prefix)."""
    found: List[str] = []
    for ref in _ABSOLUTE_REF.findall(html or ""):
        if record.get("keep_prefix") and ref.startswith(base_path(record)):
            continue
        if ref not in found:
            found.append(ref)
        if len(found) >= limit:
            break
    return found


def is_document(headers: Dict[str, str]) -> bool:
    """A page being opened (the browser says so; a request without the header, from a script or curl, counts too)."""
    dest = (headers.get("sec-fetch-dest") or "").lower()
    return dest in ("", "document", "iframe", "frame", "embed", "object")


def may_view(user: Optional[Dict[str, Any]], record: Dict[str, Any]) -> bool:
    from app.services import sharing

    if not user:
        return False
    access = sharing.bot_access(str(user.get("id") or ""), str(record.get("bot_id") or ""))
    return access is not None and access.owner_id == record.get("owner_id")


def target(record: Dict[str, Any]) -> Dict[str, Any]:
    """Where to reach the app now: the driver's relay and, for an older runtime, the computer itself."""
    from app.services.computer_provider import computer_provider

    status = computer_provider.describe(record["bot_id"])
    found = computer_provider.vnc_target(status.computer_id) if status.state == "running" else None
    if not found:
        raise PreviewError("The bot's computer is not running, so the preview is unavailable until it is started again.")
    return {
        "relay": f"http://{found['host']}:{found['port']}/preview/{record['port']}",
        "relay_ws": f"ws://{found['host']}:{found['port']}/preview/{record['port']}",
        "direct": f"http://{found['host']}:{record['port']}",
        "direct_ws": f"ws://{found['host']}:{record['port']}",
        "headers": {"x-computer-token": found["token"]},
        "key": f"{status.computer_id}:{status.generation}",
    }


# Computers whose driver has no relay (runtime before 2.15.0), by computer and generation.
_no_relay: Dict[str, bool] = {}


def relay_missing(key: str) -> bool:
    return _no_relay.get(key, False)


def mark_relay_missing(key: str) -> None:
    _no_relay[key] = True


def old_driver_reply(status: int, body: bytes) -> bool:
    return status == 400 and b"Runtime route not found" in body[:500]


# ---- the bot's tools ------------------------------------------------------------------------------


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise PreviewError("'port' is the port number your server listens on in your computer.")
    if not 1 <= port <= 65535:
        raise PreviewError("'port' must be between 1 and 65535.")
    return port


async def _probe(record: Dict[str, Any]) -> Tuple[int, str, str]:
    """Ask the app for the preview's start page, as a viewer would: (status, content type, the start of an HTML body)."""
    import httpx

    found = target(record)
    path = upstream_path(record, str(record.get("path") or ""))
    headers = {"accept": "text/html,*/*", "accept-encoding": "identity"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        reply = await client.get(found["relay"] + path, headers={**headers, **found["headers"]})
        if old_driver_reply(reply.status_code, reply.content):
            mark_relay_missing(found["key"])
            try:
                reply = await client.get(found["direct"] + path, headers=headers)
            except httpx.HTTPError:
                raise PreviewError(f"Nothing answered on port {record['port']} from outside your computer. This computer's runtime "
                                   "is older than previews need, so the server must listen on 0.0.0.0, not only on localhost "
                                   "(restarting the computer updates the runtime).")
        elif reply.status_code == 502 and b"preview_unreachable" in reply.content[:500]:
            raise PreviewError(f"Nothing is listening on port {record['port']} in your computer. Start the server first "
                               "(in the background, so it keeps running), then open the preview.")
    kind = reply.headers.get("content-type", "")
    return reply.status_code, kind, reply.text[:200_000] if kind.startswith("text/html") else ""


async def execute_open(call: ActionInvocation) -> Dict[str, Any]:
    from app.services.computer_provider import computer_provider

    bot_id = str(call.arguments.get("bot_id") or "")
    port = _port(call.arguments.get("port"))
    title = " ".join(str(call.arguments.get("title") or "").split())[:TITLE_MAX]
    path = str(call.arguments.get("path") or "").strip().lstrip("/")[:500]
    keep_prefix = bool(call.arguments.get("keep_prefix"))
    try:
        hours = float(call.arguments.get("hours") or DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    hours = max(0.25, min(MAX_HOURS, hours))
    if computer_provider.describe(bot_id).state != "running":
        raise PreviewError("Your computer is not running. Start it and your server first, then open the preview.")
    owner = get_current_owner()
    draft = {"id": "check", "owner_id": owner, "bot_id": bot_id, "port": port, "path": path, "keep_prefix": keep_prefix}
    status, kind, html = await _probe(draft)
    record = save_preview(owner_id=owner, bot_id=bot_id, port=port, title=title, path=path, keep_prefix=keep_prefix, hours=hours,
                          created_by=get_current_actor())
    warnings = []
    if status >= 400:
        warnings.append(f"The start page answered {status}; check the path, or what the server serves at it.")
    absolute = absolute_references(record, html)
    if absolute:
        warnings.append("The page loads these from the site root, which miss the preview: " + ", ".join(absolute)
                        + ". Make them relative (./app.js rather than /app.js)"
                        + ("." if keep_prefix else f", or set the app up to live under {base_path(record)} and open it with keep_prefix."))
    return {
        "opened": True,
        "url": url_of(record),
        "base_path": base_path(record),
        "expires_at": record["expires_at"],
        "attachment": card(record),
        "note": (
            "The user has a card in the chat to open it, and anyone who can use you can open it while it lasts. "
            + ("Requests reach your app with that path unchanged. " if keep_prefix else
               f"It is served at {base_path(record)}; your app receives requests with that prefix removed (it sees /), so pages "
               "and fetch calls must use relative URLs. ")
            + "The page has no cookies or lasting browser storage (localStorage works until the page is reloaded). It ends at "
            f"{record['expires_at']}, when you close it with computer_preview_close, or while your computer is stopped. "
            "Opening the same port again keeps the address and extends it."
        ),
        **({"warnings": warnings} if warnings else {}),
        "content_type": kind,
    }


async def execute_close(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = str(call.arguments.get("bot_id") or "")
    port = _port(call.arguments["port"]) if call.arguments.get("port") not in (None, "") else None
    closed = close_previews(get_current_owner(), bot_id, port)
    return {"closed": closed, "note": "Its address no longer works." if closed else "No preview of that port was open."}


def register_preview_actions() -> None:
    action_gateway.register_action(
        ActionDefinition(name=OPEN_ACTION, tool="preview", action="open", intent="Give a web server in the bot's computer a temporary address in Cubicle.",
                         risk="write", requires_approval=False),
        execute_open,
    )
    action_gateway.register_action(
        ActionDefinition(name=CLOSE_ACTION, tool="preview", action="close", intent="End a preview of the bot's computer.", risk="write", requires_approval=False),
        execute_close,
    )


register_preview_actions()
