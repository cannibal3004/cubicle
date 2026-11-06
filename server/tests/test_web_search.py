"""Web search: which provider, what each is sent and what comes back, reading pages, and what never leaves."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import web_search
from app.services import storage_service as storage_module
from app.services.llm_tools import ToolCallError, available_tools, build_invocation
from app.services.storage_service import StorageService
from app.services.web_search import WebConfig, WebSearchError


class Recorder:
    """httpx clients in web_search answer from `handler`, and every request is kept."""

    def __init__(self, handler):
        self.requests = []
        self.handler = handler
        real = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self._handle)
            return real(*args, **kwargs)

        self.patch = mock.patch.object(web_search.httpx, "AsyncClient", client)

    def _handle(self, request):
        self.requests.append(request)
        return self.handler(request)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def body(self, n=-1):
        return json.loads(self.requests[n].content)


def run(coro):
    return asyncio.run(coro)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        web_search._pages.clear()

    def test_exa_search_answer_and_page(self):
        def exa(request):
            path = request.url.path
            if path == "/search":
                return httpx.Response(200, json={"results": [{"title": "Docker 29 released", "url": "https://docs.docker.com/r", "publishedDate": "2026-10-30T10:00:00Z",
                                                              "highlights": ["Docker 29 ships", "with compose 3"]}], "costDollars": {"total": 0.007}})
            if path == "/answer":
                return httpx.Response(200, json={"answer": "29.0", "citations": [{"title": "Release notes", "url": "https://docs.docker.com/r"}], "costDollars": {"total": 0.005}})
            return httpx.Response(200, json={"results": [{"title": "Notes", "text": "x" * 25_000}], "statuses": [{"status": "success"}]})

        config = WebConfig("exa", "exa-key")
        with Recorder(exa) as rec:
            found = run(web_search.search(config, query="docker release", max_results=3, since="2026-10-01", include_domains=["docs.docker.com"], category="news", deep=True))
            self.assertEqual(rec.requests[0].headers["x-api-key"], "exa-key")
            body = rec.body()
            self.assertEqual((body["numResults"], body["type"], body["category"], body["includeDomains"], body["startPublishedDate"]),
                             (3, "deep", "news", ["docs.docker.com"], "2026-10-01T00:00:00Z"))
            self.assertEqual(found["results"][0], {"title": "Docker 29 released", "url": "https://docs.docker.com/r", "published": "2026-10-30",
                                                   "snippet": "Docker 29 ships … with compose 3"})
            self.assertEqual(found["cost_usd"], 0.007)
            answered = run(web_search.answer(config, "Latest Docker?"))
            self.assertEqual((answered["answer"], answered["sources"][0]["title"]), ("29.0", "Release notes"))
            page = run(web_search.fetch(config, "https://docs.docker.com/r", max_chars=10_000))
            self.assertEqual((page["read_by"], page["end"], page["total_chars"]), ("Exa", 10_000, 25_000))
            self.assertIn("start=10000", page["more"])
            # The next window comes from the copy kept, not another request.
            count = len(rec.requests)
            rest = run(web_search.fetch(config, "https://docs.docker.com/r", start=20_000))
            self.assertEqual((rest["end"], len(rec.requests), "more" in rest), (25_000, count, False))

    def test_tavily_search_and_errors(self):
        def tavily(request):
            if json.loads(request.content).get("query") == "bad key":
                return httpx.Response(401, json={"detail": {"error": "Unauthorized"}})
            return httpx.Response(200, json={"results": [{"title": "CVE-2026-1", "url": "https://nvd.nist.gov/x", "content": "A flaw", "published_date": "2026-11-01"}],
                                             "usage": {"credits": 1}})

        config = WebConfig("tavily", "tvly-1")
        with Recorder(tavily) as rec:
            found = run(web_search.search(config, query="openssh cve", recency="week", category="news", exclude_domains=["reddit.com"]))
            self.assertEqual(rec.requests[0].headers["authorization"], "Bearer tvly-1")
            body = rec.body()
            self.assertEqual((body["time_range"], body["topic"], body["exclude_domains"], body["search_depth"]), ("week", "news", ["reddit.com"], "basic"))
            self.assertEqual((found["results"][0]["published"], found["credits"]), ("2026-11-01", 1))
            with self.assertRaisesRegex(WebSearchError, "refused the API key"):
                run(web_search.search(config, query="bad key"))

    def test_searxng_filters_itself(self):
        def searx(request):
            if request.url.params.get("q") == "forbidden":
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, json={"results": [{"title": "A", "url": "https://www.reddit.com/r/x", "content": "a"},
                                                         {"title": "B", "url": "https://unraid.net/blog", "content": "b", "publishedDate": "2026-09-01T00:00:00"},
                                                         {"title": "C", "url": "https://forums.unraid.net/t", "content": "c"}]})

        config = WebConfig("searxng", url="https://searx.example")
        with Recorder(searx) as rec:
            found = run(web_search.search(config, query="unraid 8", since=(web_search.date.today() - web_search.timedelta(days=5)).isoformat(),
                                          exclude_domains=["reddit.com"], max_results=5))
            params = rec.requests[0].url.params
            self.assertEqual((params["format"], params["time_range"]), ("json", "week"))
            self.assertEqual([r["title"] for r in found["results"]], ["B", "C"])
            only = run(web_search.search(config, query="unraid 8", include_domains=["forums.unraid.net"]))
            self.assertIn("site:forums.unraid.net", rec.requests[-1].url.params["q"])
            self.assertEqual([r["title"] for r in only["results"]], ["C"])
            with self.assertRaisesRegex(WebSearchError, "search.formats"):
                run(web_search.search(config, query="forbidden"))
            with self.assertRaisesRegex(WebSearchError, "no answers of its own"):
                run(web_search.answer(config, "x"))


class PageTests(unittest.TestCase):
    def setUp(self):
        web_search._pages.clear()

    def test_html_becomes_text(self):
        title, text = web_search.html_to_text("<html><head><title>Guide</title><script>evil()</script></head><body><nav>menu</nav>"
                                              "<h2>Install</h2><p>Run <b>apt</b> install.</p><ul><li>one</li><li>two</li></ul></body></html>")
        self.assertEqual(title, "Guide")
        self.assertEqual(text, "## Install\n\nRun apt install.\n\none\n\ntwo")

    def test_private_addresses_are_not_read_directly(self):
        for url in ("http://127.0.0.1:8000/", "http://192.168.1.3:32443/api", "http://[::1]/", "file:///etc/passwd"):
            with self.assertRaises(WebSearchError, msg=url):
                run(web_search.fetch(None, url))

    def test_a_page_read_directly_with_its_redirect_checked(self):
        def site(request):
            if request.url.path == "/old":
                return httpx.Response(301, headers={"location": "/new"})
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<title>New</title><p>Hello</p>")

        checked = []

        async def public(url):
            checked.append(url)

        with Recorder(site), mock.patch.object(web_search, "_public", public):
            page = run(web_search.fetch(None, "https://example.com/old"))
        self.assertEqual((page["title"], page["text"], page["read_by"]), ("New", "Hello", "direct"))
        self.assertEqual(checked, ["https://example.com/old", "https://example.com/new"])


class SettingsAndToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = StorageService(Path(self.temp.name))
        patch = mock.patch.object(storage_module, "storage_service", self.storage)
        patch.start()
        self.addCleanup(patch.stop)

    def test_everyones_provider_an_accounts_own_and_none(self):
        with mock.patch.object(web_search.settings, "WEB_SEARCH_PROVIDER", ""):
            self.assertIsNone(web_search.current_config())
            self.assertEqual(web_search.provider_now(), "")
            self.storage.save_settings({"web_search_provider": "exa", "web_search_api_key": "exa-app"})
            self.assertEqual(web_search.current_config(), WebConfig("exa", "exa-app", "", "app"))
            # The account's own, when complete, wins; half set up, it does not.
            self.storage.save_settings({"user_web_search_provider": "searxng"})
            self.assertEqual(web_search.current_config().provider, "exa")
            self.storage.save_settings({"user_web_search_url": "https://searx.example/"})
            self.assertEqual(web_search.current_config(), WebConfig("searxng", "", "https://searx.example", "account"))
            self.assertEqual(self.storage.get_public_settings()["web_search_api_key"], "")  # never shown
            self.assertTrue(self.storage.get_public_settings()["web_search_api_key_configured"])

    def test_tools_offered_by_provider_and_their_checks(self):
        def names(web):
            return [s.name for s in available_tools(computer=False, web=web) if s.group == "web"]

        self.assertEqual(names(""), ["web_fetch"])
        self.assertEqual(names("searxng"), ["web_search", "web_fetch"])
        self.assertEqual(names("exa"), ["web_search", "web_answer", "web_fetch"])
        call = build_invocation("web_search", {"query": "docker", "recency": "week", "include_domains": "https://docs.docker.com/", "max_results": 50}, "bot-1")
        self.assertEqual((call.name, call.arguments["include_domains"], call.arguments["max_results"]), ("web.search", ["docs.docker.com"], 10))
        for bad in ({"query": ""}, {"query": "x", "recency": "decade"}, {"query": "x", "since": "last week"}):
            with self.assertRaises(ToolCallError):
                build_invocation("web_search", bad, "bot-1")
        with self.assertRaises(ToolCallError):
            build_invocation("web_fetch", {"url": "docs.docker.com"}, "bot-1")

    def test_a_secret_never_goes_out(self):
        with mock.patch.object(self.storage, "secret_values", lambda bot_id="": {"GITHUB_TOKEN": "ghp_abcdef123456"}):
            with self.assertRaisesRegex(WebSearchError, "GITHUB_TOKEN"):
                web_search.refuse_secrets("why is ghp_abcdef123456 rejected", "bot-1")
            web_search.refuse_secrets("why is my token rejected", "bot-1")


if __name__ == "__main__":
    unittest.main()
