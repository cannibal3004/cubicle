"""Web search for every bot: web_search, web_answer and web_fetch, through one provider.

The provider is Exa, Tavily or a SearXNG server. An admin sets it for everyone in App Settings
(web_search_provider, web_search_api_key, web_search_url; else the WEB_SEARCH_* environment
variables), and an account may set its own (user_web_search_*), which its bots use instead.

- web_search returns compact results (title, link, date, a few passages), never whole pages.
- web_answer is the provider's own answer with its sources (Exa's /answer, Tavily's answer).
- web_fetch reads one page as text: through the provider (Exa /contents, Tavily /extract) when
  there is one that can, else directly, which needs no key. A direct fetch reads only public
  addresses: this server can reach things a bot's computer cannot (other containers, the
  LAN), so a name that resolves to a private address is refused. Pages are read in windows
  (start, max_chars), kept a few minutes so the next window costs nothing.

A query or address holding the value of one of the bot's secrets is refused before anything
leaves the server. Everything read here is text from the web: untrusted, never instructions.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, current_storage

PROVIDERS = ("exa", "tavily", "searxng")
PROVIDER_NAMES = {"exa": "Exa", "tavily": "Tavily", "searxng": "SearXNG"}
ANSWERS = ("exa", "tavily")  # providers with an answer of their own
RECENCY = ("day", "week", "month", "year")
RECENCY_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
MAX_RESULTS = 10
SNIPPET_CHARS = 900
FETCH_CHARS = 12_000
FETCH_MAX_CHARS = 30_000
PAGE_MAX_BYTES = 4 * 1024 * 1024
PROVIDER_PAGE_CHARS = 200_000
TIMEOUT = 30.0
EXA_URL = "https://api.exa.ai"
TAVILY_URL = "https://api.tavily.com"
USER_AGENT = "Mozilla/5.0 (compatible; Cubicle web_fetch)"


class WebSearchError(RuntimeError):
    pass


# ---- which provider ------------------------------------------------------------------------------


@dataclass(frozen=True)
class WebConfig:
    provider: str
    api_key: str = ""
    url: str = ""
    scope: str = "app"  # app (everyone's) or account (this account's own)


def _usable(provider: str, key: str, url: str) -> bool:
    return provider in PROVIDERS and (bool(url) if provider == "searxng" else bool(key))


def current_config(storage: Optional[StorageService] = None) -> Optional[WebConfig]:
    """The provider this account's bots use: its own when it set one up, else everyone's; None when neither."""
    values = (storage or current_storage()).get_settings()
    own = str(values.get("user_web_search_provider") or "").strip().lower()
    own_key = str(values.get("user_web_search_api_key") or "").strip()
    own_url = str(values.get("user_web_search_url") or "").strip().rstrip("/")
    if _usable(own, own_key, own_url):
        return WebConfig(own, own_key, own_url, "account")
    provider = str(values.get("web_search_provider") or settings.WEB_SEARCH_PROVIDER or "").strip().lower()
    key = str(values.get("web_search_api_key") or settings.WEB_SEARCH_API_KEY or "").strip()
    url = str(values.get("web_search_url") or settings.WEB_SEARCH_URL or "").strip().rstrip("/")
    return WebConfig(provider, key, url, "app") if _usable(provider, key, url) else None


def provider_now(storage: Optional[StorageService] = None) -> str:
    """The provider's id, or "" without one (the bot then has web_fetch alone)."""
    try:
        config = current_config(storage)
    except Exception:
        return ""
    return config.provider if config else ""


# ---- what goes out ----------------------------------------------------------------------------------


def refuse_secrets(text: str, bot_id: str) -> None:
    """A query or address must not carry the value of one of the bot's secrets to a third party."""
    try:
        values = current_storage().secret_values(bot_id)
    except Exception:
        return
    for name, value in values.items():
        if value and len(value) >= 6 and value in text:
            raise WebSearchError(f"That holds the value of the secret {name}; leave it out. Secrets never go to a search engine or a web address.")


def _since(recency: str, since: str) -> Optional[date]:
    if since:
        try:
            return date.fromisoformat(since)
        except ValueError:
            raise WebSearchError("'since' is a date like 2026-10-01.")
    if recency:
        return (datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS[recency])).date()
    return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _domain_ok(url: str, include: List[str], exclude: List[str]) -> bool:
    host = _host(url)

    def under(domain: str) -> bool:
        domain = domain.lower().removeprefix("www.").split("/")[0]
        return host == domain or host.endswith("." + domain)

    return (not include or any(under(d) for d in include)) and not any(under(d) for d in exclude)


def _snippet(*parts: Any) -> str:
    text = " … ".join(" ".join(str(p).split()) for p in parts if p)
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


def _day(value: Any) -> str:
    return str(value or "")[:10]


# ---- providers -------------------------------------------------------------------------------------


async def _post(url: str, body: Dict[str, Any], headers: Dict[str, str], provider: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise WebSearchError(f"{PROVIDER_NAMES[provider]} could not be reached: {exc.__class__.__name__}.") from exc
    return _reply(response, provider)


def _reply(response: httpx.Response, provider: str) -> Dict[str, Any]:
    name = PROVIDER_NAMES[provider]
    if response.status_code == 401:
        raise WebSearchError(f"{name} refused the API key (401). It is set in App Settings → Web search.")
    if response.status_code in (402, 432, 433):
        raise WebSearchError(f"{name} says the account is out of credits or over its plan's limit ({response.status_code}).")
    if response.status_code == 429:
        raise WebSearchError(f"{name} is rate-limiting requests (429); wait a little and try again.")
    if response.status_code >= 400:
        detail = ""
        try:
            data = response.json()
            detail = str(data.get("error") or data.get("detail") or data.get("message") or "")[:300]
        except ValueError:
            detail = response.text[:200]
        raise WebSearchError(f"{name} answered {response.status_code}" + (f": {detail}" if detail else "."))
    try:
        return response.json()
    except ValueError as exc:
        raise WebSearchError(f"{name} did not answer with JSON (is the address right?).") from exc


def _exa_headers(config: WebConfig) -> Dict[str, str]:
    return {"x-api-key": config.api_key}


def _tavily_headers(config: WebConfig) -> Dict[str, str]:
    return {"Authorization": f"Bearer {config.api_key}"}


async def _search_exa(config: WebConfig, q: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    body: Dict[str, Any] = {"query": q["query"], "numResults": q["max_results"], "type": "deep" if q["deep"] else "auto",
                            "contents": {"highlights": {"query": q["query"], "maxCharacters": SNIPPET_CHARS}}}
    if q["include_domains"]:
        body["includeDomains"] = q["include_domains"]
    if q["exclude_domains"]:
        body["excludeDomains"] = q["exclude_domains"]
    if q["since"]:
        body["startPublishedDate"] = f"{q['since'].isoformat()}T00:00:00Z"
    if q["category"]:
        body["category"] = q["category"]
    data = await _post(f"{EXA_URL}/search", body, _exa_headers(config), "exa")
    results = [{"title": r.get("title") or "", "url": r.get("url") or "", "published": _day(r.get("publishedDate")),
                "snippet": _snippet(*(r.get("highlights") or [r.get("summary") or (r.get("text") or "")[:SNIPPET_CHARS]]))}
               for r in data.get("results") or []]
    cost = (data.get("costDollars") or {}).get("total")
    return results, ({"cost_usd": cost} if cost is not None else {})


async def _search_tavily(config: WebConfig, q: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    body: Dict[str, Any] = {"query": q["query"], "max_results": q["max_results"], "search_depth": "advanced" if q["deep"] else "basic",
                            "include_published_date": True, "include_usage": True}
    if q["category"] in ("news", "finance"):
        body["topic"] = q["category"]
    if q["recency"]:
        body["time_range"] = q["recency"]
    elif q["since"]:
        body["start_date"] = q["since"].isoformat()
    if q["include_domains"]:
        body["include_domains"] = q["include_domains"]
    if q["exclude_domains"]:
        body["exclude_domains"] = q["exclude_domains"]
    data = await _post(f"{TAVILY_URL}/search", body, _tavily_headers(config), "tavily")
    results = [{"title": r.get("title") or "", "url": r.get("url") or "", "published": _day(r.get("published_date")), "snippet": _snippet(r.get("content"))}
               for r in data.get("results") or []]
    credits = (data.get("usage") or {}).get("credits")
    return results, ({"credits": credits} if credits is not None else {})


async def _search_searxng(config: WebConfig, q: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    params: Dict[str, Any] = {"q": q["query"], "format": "json"}
    recency = q["recency"]
    if not recency and q["since"]:
        days = (date.today() - q["since"]).days
        recency = next((r for r in RECENCY if days <= RECENCY_DAYS[r]), "")
    if recency:
        params["time_range"] = recency
    if q["category"] in ("news", "science", "it", "images", "videos", "map", "files", "music", "social media"):
        params["categories"] = q["category"]
    if len(q["include_domains"]) == 1:
        params["q"] += f" site:{q['include_domains'][0]}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.get(f"{config.url}/search", params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise WebSearchError(f"SearXNG at {config.url} could not be reached: {exc.__class__.__name__}.") from exc
    if response.status_code == 403:
        raise WebSearchError("SearXNG refused the JSON format (403): add json to search.formats in its settings.yml.")
    data = _reply(response, "searxng")
    results = []
    for r in data.get("results") or []:
        url = r.get("url") or ""
        if not url or not _domain_ok(url, q["include_domains"], q["exclude_domains"]):
            continue
        results.append({"title": r.get("title") or "", "url": url, "published": _day(r.get("publishedDate")), "snippet": _snippet(r.get("content"))})
        if len(results) >= q["max_results"]:
            break
    return results, {}


SEARCHERS = {"exa": _search_exa, "tavily": _search_tavily, "searxng": _search_searxng}


async def search(config: WebConfig, *, query: str, max_results: int = 5, recency: str = "", since: str = "", include_domains: Optional[List[str]] = None,
                 exclude_domains: Optional[List[str]] = None, category: str = "", deep: bool = False) -> Dict[str, Any]:
    q = {"query": query, "max_results": max(1, min(MAX_RESULTS, int(max_results or 5))), "recency": recency if recency in RECENCY else "",
         "since": _since(recency if recency in RECENCY else "", since), "include_domains": list(include_domains or []),
         "exclude_domains": list(exclude_domains or []), "category": (category or "").strip().lower(), "deep": bool(deep)}
    results, extra = await SEARCHERS[config.provider](config, q)
    return {"provider": PROVIDER_NAMES[config.provider], "results": results, **extra,
            **({} if results else {"note": "Nothing found; try other words, a wider time range or fewer filters."})}


async def answer(config: WebConfig, question: str) -> Dict[str, Any]:
    if config.provider == "exa":
        data = await _post(f"{EXA_URL}/answer", {"query": question}, _exa_headers(config), "exa")
        cost = (data.get("costDollars") or {}).get("total")
        return {"provider": "Exa", "answer": data.get("answer"),
                "sources": [{"title": c.get("title") or "", "url": c.get("url") or "", "published": _day(c.get("publishedDate"))} for c in data.get("citations") or []],
                **({"cost_usd": cost} if cost is not None else {})}
    if config.provider == "tavily":
        data = await _post(f"{TAVILY_URL}/search", {"query": question, "include_answer": "advanced", "search_depth": "advanced", "max_results": 5,
                                                   "include_usage": True}, _tavily_headers(config), "tavily")
        credits = (data.get("usage") or {}).get("credits")
        return {"provider": "Tavily", "answer": data.get("answer"),
                "sources": [{"title": r.get("title") or "", "url": r.get("url") or ""} for r in data.get("results") or []],
                **({"credits": credits} if credits is not None else {})}
    raise WebSearchError(f"{PROVIDER_NAMES[config.provider]} has no answers of its own; use web_search and read the pages.")


# ---- reading a page ---------------------------------------------------------------------------------


class _TextOf(HTMLParser):
    """An HTML page as plain text: headings, paragraphs, list items and table cells on lines of their
    own, links kept as text, scripts, styles, navigation and forms left out."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "form", "template", "iframe", "head"}
    BREAK = {"p", "div", "br", "li", "tr", "section", "article", "header", "main", "pre", "blockquote", "table", "ul", "ol", "dl", "dt", "dd", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skipping = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BREAK:
            self.parts.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BREAK or tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> Tuple[str, str]:
    parser = _TextOf()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return " ".join(parser.title.split()), parser.text()


async def _public(url: str) -> None:
    """Refuse an address this server should not read for a bot: not http(s), or one that resolves to a private address."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebSearchError("web_fetch reads http and https addresses.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebSearchError(f"{parts.hostname} does not resolve.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise WebSearchError(f"{parts.hostname} is on a private network; web_fetch reads public pages. For something on your network, "
                                 "use your computer (curl in its terminal, or its browser).")


async def _fetch_direct(url: str) -> Tuple[str, str]:
    """The page itself: redirects followed one by one (each checked), at most PAGE_MAX_BYTES."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.5"}) as client:
        for _ in range(6):
            await _public(url)
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code >= 400:
                    raise WebSearchError(f"{url} answered {response.status_code}.")
                kind = response.headers.get("content-type", "").lower()
                if "pdf" in kind or url.lower().endswith(".pdf"):
                    raise WebSearchError("That is a PDF; download it in your computer (curl -LO) and read it with pdftotext or markitdown.")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > PAGE_MAX_BYTES:
                        break
                text = body.decode(response.encoding or "utf-8", errors="replace")
                if "html" in kind or text.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
                    return html_to_text(text)
                if not any(t in kind for t in ("text/", "json", "xml", "markdown")) and kind:
                    raise WebSearchError(f"That is not a page of text ({kind}); download it in your computer instead.")
                return "", text
        raise WebSearchError("Too many redirects.")


async def _fetch_provider(config: WebConfig, url: str) -> Optional[Tuple[str, str]]:
    if config.provider == "exa":
        data = await _post(f"{EXA_URL}/contents", {"urls": [url], "text": {"maxCharacters": PROVIDER_PAGE_CHARS}}, _exa_headers(config), "exa")
        status = next(iter(data.get("statuses") or []), {})
        if status.get("status") == "error":
            raise WebSearchError(f"Exa could not read that page ({(status.get('error') or {}).get('tag') or 'error'}).")
        page = next(iter(data.get("results") or []), None)
        return (page.get("title") or "", page.get("text") or "") if page else None
    if config.provider == "tavily":
        data = await _post(f"{TAVILY_URL}/extract", {"urls": [url], "format": "markdown"}, _tavily_headers(config), "tavily")
        page = next(iter(data.get("results") or []), None)
        if page is None:
            failed = next(iter(data.get("failed_results") or []), {})
            raise WebSearchError(f"Tavily could not read that page: {failed.get('error') or 'no content'}.")
        return "", page.get("raw_content") or ""
    return None


_pages: Dict[str, Tuple[float, str, str, str]] = {}  # url -> (when, title, text, how)
PAGE_KEEP_SECONDS = 600


async def fetch(config: Optional[WebConfig], url: str, *, start: int = 0, max_chars: int = FETCH_CHARS) -> Dict[str, Any]:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise WebSearchError("Give the page's full address, starting with http:// or https://.")
    for key in [k for k, v in _pages.items() if time.monotonic() - v[0] > PAGE_KEEP_SECONDS]:
        _pages.pop(key, None)
    kept = _pages.get(url)
    if kept is None:
        page, failed = None, None
        if config:
            try:
                page = await _fetch_provider(config, url)
            except WebSearchError as exc:
                failed = exc  # the page itself may still be readable
        how = PROVIDER_NAMES[config.provider] if page is not None and config else "direct"
        if page is not None:
            title, text = page
        else:
            try:
                title, text = await _fetch_direct(url)
            except WebSearchError as exc:
                raise WebSearchError(f"{failed} Reading it directly failed too: {exc}" if failed else str(exc)) from exc
        kept = (time.monotonic(), title, text, how)
        _pages[url] = kept
        while len(_pages) > 20:
            _pages.pop(next(iter(_pages)))
    _, title, text, how = kept
    start = max(0, int(start or 0))
    size = max(1000, min(FETCH_MAX_CHARS, int(max_chars or FETCH_CHARS)))
    window = text[start:start + size]
    end = start + len(window)
    result: Dict[str, Any] = {"url": url, "title": title, "read_by": how, "text": window, "start": start, "end": end, "total_chars": len(text)}
    if end < len(text):
        result["more"] = f"{len(text) - end} more characters: call web_fetch again with start={end}."
    if not text.strip():
        result["note"] = "The page has no readable text (it may be built by scripts); open it in your computer's browser instead."
    return result


# ---- the bot's tools --------------------------------------------------------------------------------


def _config_or_refuse() -> WebConfig:
    config = current_config()
    if config is None:
        raise WebSearchError("No web search provider is set up; an admin adds one in App Settings → Web search.")
    return config


async def execute_search(call: ActionInvocation) -> Dict[str, Any]:
    args = call.arguments
    refuse_secrets(str(args.get("query") or ""), str(args.get("bot_id") or ""))
    return await search(_config_or_refuse(), query=args["query"], max_results=args.get("max_results") or 5, recency=args.get("recency") or "",
                        since=args.get("since") or "", include_domains=args.get("include_domains"), exclude_domains=args.get("exclude_domains"),
                        category=args.get("category") or "", deep=bool(args.get("deep")))


async def execute_answer(call: ActionInvocation) -> Dict[str, Any]:
    refuse_secrets(str(call.arguments.get("question") or ""), str(call.arguments.get("bot_id") or ""))
    return await answer(_config_or_refuse(), call.arguments["question"])


async def execute_fetch(call: ActionInvocation) -> Dict[str, Any]:
    url = str(call.arguments.get("url") or "")
    refuse_secrets(url, str(call.arguments.get("bot_id") or ""))
    return await fetch(current_config(), url, start=call.arguments.get("start") or 0, max_chars=call.arguments.get("max_chars") or FETCH_CHARS)


def register_web_actions() -> None:
    for name, intent, executor in (
        ("web.search", "Search the web.", execute_search),
        ("web.answer", "Ask the web search provider for an answer with sources.", execute_answer),
        ("web.fetch", "Read a public web page as text.", execute_fetch),
    ):
        action_gateway.register_action(ActionDefinition(name=name, tool="web", action=name.split(".")[1], intent=intent, risk="read", requires_approval=False), executor)


register_web_actions()
