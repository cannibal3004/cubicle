"""Web previews: a temporary address in Cubicle for a web server in a bot's computer."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from starlette.requests import Request

from app.routers import previews as router
from app.services import previews
from app.services.llm_tools import build_invocation


def make_request(path, *, method="GET", headers=None, body=b"", query=""):
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                    "headers": raw, "scheme": "https", "server": ("cubicle", 443), "client": ("1.2.3.4", 1)}, receive)


TARGET = {"relay": "http://computer:3000/preview/8000", "relay_ws": "ws://computer:3000/preview/8000", "direct": "http://computer:8000",
          "direct_ws": "ws://computer:8000", "headers": {"x-computer-token": "t0ken"}, "key": "computer-1:1"}


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(previews, "_store_path", lambda: Path(self.temp.name) / "previews.json")
        patch.start()
        self.addCleanup(patch.stop)
        previews._no_relay.clear()

    def test_previews_are_kept_renewed_capped_closed_and_expire(self):
        first = previews.save_preview(owner_id="jamie", bot_id="bot-1", port=8000, title="Snake")
        again = previews.save_preview(owner_id="jamie", bot_id="bot-1", port=8000, path="game.html", hours=2)
        self.assertEqual(first["id"], again["id"])  # the same port keeps its address
        self.assertEqual((again["title"], previews.url_of(again)), ("Snake", f"/api/preview/{first['id']}/game.html"))
        self.assertGreater(len(first["id"]), 20)
        for port in range(8001, 8005):
            previews.save_preview(owner_id="jamie", bot_id="bot-1", port=port)
        with self.assertRaises(previews.PreviewError):
            previews.save_preview(owner_id="jamie", bot_id="bot-1", port=9000)
        previews.save_preview(owner_id="dana", bot_id="bot-1", port=9000)  # another owner's bot of the same id
        self.assertEqual(previews.close_previews("jamie", "bot-1", 8001), 1)
        self.assertEqual(len(previews.list_for_bot("jamie", "bot-1")), 4)
        self.assertEqual(previews.close_previews("jamie", "bot-1"), 4)
        self.assertEqual(len(previews.list_for_bot("dana", "bot-1")), 1)
        gone = previews.save_preview(owner_id="jamie", bot_id="bot-2", port=8000, hours=1)
        with mock.patch.object(previews, "_now", lambda: previews.datetime.now() + previews.timedelta(hours=2)):
            self.assertIsNone(previews.get(gone["id"]))

    def test_paths_redirects_and_headers(self):
        record = {"id": "abc", "port": 8000}
        kept = {**record, "keep_prefix": True}
        self.assertEqual(previews.upstream_path(record, "js/app.js", "v=2"), "/js/app.js?v=2")
        self.assertEqual(previews.upstream_path(kept, "js/app.js"), "/api/preview/abc/js/app.js")
        self.assertEqual(previews.rewrite_location(record, "/login?next=/"), "/api/preview/abc/login?next=/")
        self.assertEqual(previews.rewrite_location(record, "http://localhost:8000/done#top"), "/api/preview/abc/done#top")
        self.assertEqual(previews.rewrite_location(record, "https://example.com/x"), "https://example.com/x")
        self.assertEqual(previews.rewrite_location(record, "next.html"), "next.html")
        self.assertEqual(previews.rewrite_location(kept, "/api/preview/abc/home"), "/api/preview/abc/home")
        sent = previews.request_headers(record, [("Cookie", "cubicle_session=s"), ("Authorization", "Bearer x"), ("Origin", "null"),
                                                 ("Accept-Encoding", "gzip"), ("Accept", "text/html"), ("Host", "cubicle")], site="cubicle")
        self.assertNotIn("cookie", sent)
        self.assertNotIn("authorization", sent)
        self.assertEqual((sent["origin"], sent["accept-encoding"], sent["x-forwarded-prefix"]), ("http://localhost:8000", "identity", "/api/preview/abc"))
        back = dict(previews.response_headers(record, [("Set-Cookie", "a=b"), ("X-Frame-Options", "DENY"), ("Location", "/next"),
                                                       ("Content-Type", "text/html"), ("Content-Length", "10")]))
        self.assertNotIn("Set-Cookie", back)
        self.assertNotIn("X-Frame-Options", back)
        self.assertEqual(back["Location"], "/api/preview/abc/next")
        self.assertTrue(back["Content-Security-Policy"].startswith("sandbox allow-scripts"))
        self.assertNotIn("allow-same-origin", back["Content-Security-Policy"])

    def test_pages_get_storage_that_works_and_absolute_paths_are_found(self):
        shimmed = previews.with_storage_shim(b"<!doctype html><html><head><title>x</title></head><body></body></html>")
        self.assertTrue(shimmed.startswith(b"<!doctype html><html><head><script>"))
        self.assertIn(b"localStorage", shimmed)
        self.assertTrue(previews.with_storage_shim(b"<p>bare</p>").startswith(b"<script>"))
        record = {"id": "abc", "port": 8000}
        html = '<script src="/app.js"></script><link href="./style.css"><img src="//cdn.example/x.png"><a href="/scores">'
        self.assertEqual(previews.absolute_references(record, html), ["/app.js", "/scores"])

    def test_who_may_open_a_page(self):
        record = {"owner_id": "jamie", "bot_id": "bot-1"}
        self.assertTrue(previews.is_document({"sec-fetch-dest": "iframe"}))
        self.assertTrue(previews.is_document({}))
        self.assertFalse(previews.is_document({"sec-fetch-dest": "script"}))
        access = {("jamie", "bot-1"): SimpleNamespace(owner_id="jamie"), ("dana", "bot-1"): SimpleNamespace(owner_id="jamie"),
                  ("lee", "bot-1"): SimpleNamespace(owner_id="lee")}
        with mock.patch("app.services.sharing.bot_access", lambda user, bot: access.get((user, bot))):
            self.assertTrue(previews.may_view({"id": "jamie"}, record))
            self.assertTrue(previews.may_view({"id": "dana"}, record))  # a teammate it is shared with
            self.assertFalse(previews.may_view({"id": "lee"}, record))  # their own bot of the same id
            self.assertFalse(previews.may_view({"id": "sam"}, record))
            self.assertFalse(previews.may_view(None, record))

    def test_the_tools(self):
        call = build_invocation("computer_preview", {"port": "8000", "title": "Snake", "path": "/game.html", "keep_prefix": True, "hours": 2}, "bot-1")
        self.assertEqual((call.name, call.arguments["port"], call.arguments["keep_prefix"], call.arguments["hours"]), ("preview.open", 8000, True, 2.0))
        self.assertEqual(build_invocation("computer_preview_close", {}, "bot-1").arguments, {"bot_id": "bot-1"})
        running = SimpleNamespace(state="running")
        html = (200, "text/html", '<script src="/app.js"></script>')
        with mock.patch("app.services.computer_provider.computer_provider.describe", lambda bot: running), \
                mock.patch.object(previews, "_probe", lambda record: asyncio.sleep(0, result=html)), \
                mock.patch.object(previews, "get_current_owner", lambda: "jamie"):
            result = asyncio.run(previews.execute_open(call))
            self.assertEqual(result["attachment"]["kind"], "preview")
            self.assertEqual(result["url"], result["base_path"] + "game.html")
            self.assertIn("/app.js", result["warnings"][0])
            self.assertEqual(len(previews.list_for_bot("jamie", "bot-1")), 1)
            closed = asyncio.run(previews.execute_close(build_invocation("computer_preview_close", {"port": 8000}, "bot-1")))
            self.assertEqual(closed["closed"], 1)
        with mock.patch("app.services.computer_provider.computer_provider.describe", lambda bot: SimpleNamespace(state="stopped")):
            with self.assertRaises(previews.PreviewError):
                asyncio.run(previews.execute_open(call))

    def _relay(self, handler, path="", headers=None, viewer=True):
        record = previews.save_preview(owner_id="jamie", bot_id="bot-1", port=8000)
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with mock.patch.object(router, "client", lambda: transport), mock.patch.object(previews, "target", lambda r: TARGET), \
                mock.patch.object(previews, "may_view", lambda user, r: viewer), \
                mock.patch.object(router.auth_service, "authenticate_request", lambda request: {"id": "jamie"}):
            request = make_request(f"/api/preview/{record['id']}/{path}", headers=headers)
            response = asyncio.run(router.preview_relay(record["id"], path, request))
            body = response.body if hasattr(response, "body") else b""
            return response, body

    def test_the_relay(self):
        seen = []

        def app(request: httpx.Request):
            seen.append(request)
            if request.url.path.endswith("/style.css"):
                return httpx.Response(200, headers={"content-type": "text/css"}, content=b"body{}")
            return httpx.Response(200, headers={"content-type": "text/html", "set-cookie": "a=b"}, content=b"<html><head></head><body>hi</body></html>")

        # A page: relayed through the driver, without the Cubicle cookie, sandboxed, with storage added.
        response, body = self._relay(app, "", {"sec-fetch-dest": "document", "cookie": "cubicle_session=s"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(seen[-1].url), "http://computer:3000/preview/8000/")
        self.assertEqual(seen[-1].headers["x-computer-token"], "t0ken")
        self.assertNotIn("cookie", seen[-1].headers)
        self.assertIn(b"localStorage", body)
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertNotIn("set-cookie", response.headers)
        # Someone who cannot use the bot gets a sign-in page, and the app is not asked.
        count = len(seen)
        response, body = self._relay(app, "", {"sec-fetch-dest": "document"}, viewer=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(seen), count)
        # The page's own requests go by the preview's address.
        response, _ = self._relay(app, "style.css", {"sec-fetch-dest": "style"}, viewer=False)
        self.assertEqual(response.status_code, 200)

    def test_an_older_runtime_is_reached_directly_and_an_unknown_preview_has_ended(self):
        seen = []

        def old(request: httpx.Request):
            seen.append(str(request.url))
            if request.url.port == 3000:
                return httpx.Response(400, json={"error": "Runtime route not found."})
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

        response, _ = self._relay(old, "hello.txt", {"sec-fetch-dest": "document"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["http://computer:3000/preview/8000/hello.txt", "http://computer:8000/hello.txt"])
        self.assertTrue(previews.relay_missing(TARGET["key"]))
        response = asyncio.run(router.preview_relay("nope", "", make_request("/api/preview/nope/")))
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
