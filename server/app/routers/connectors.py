import asyncio
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from typing import List, Optional
from datetime import datetime, timezone
from app.config import settings
from app.services.composio_service import ConnectorServiceError, composio_service
from app.services.storage_service import storage_service

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

BACKEND_URL = "https://backend.composio.dev/api/v3"

# ─── Curated fallback catalog ────────────────────────────────────────────────
CURATED = [
    {"slug": "slack",          "label": "Slack",           "blurb": "Post updates and read channels",       "domain": "slack.com",            "logo": None},
    {"slug": "github",         "label": "GitHub",          "blurb": "Issues, pull requests, and code",     "domain": "github.com",           "logo": None},
    {"slug": "gmail",          "label": "Gmail",           "blurb": "Read and send email",                  "domain": "gmail.com",            "logo": None},
    {"slug": "googlecalendar", "label": "Google Calendar", "blurb": "Read and create events",              "domain": "calendar.google.com",  "logo": None},
    {"slug": "googlesheets",   "label": "Google Sheets",   "blurb": "Read and update spreadsheets",        "domain": "sheets.google.com",    "logo": None},
    {"slug": "googledocs",     "label": "Google Docs",     "blurb": "Read and write documents",             "domain": "docs.google.com",      "logo": None},
    {"slug": "googledrive",    "label": "Google Drive",    "blurb": "Browse and manage files",              "domain": "drive.google.com",     "logo": None},
    {"slug": "notion",         "label": "Notion",          "blurb": "Pages and databases",                  "domain": "notion.so",            "logo": None},
    {"slug": "linear",         "label": "Linear",          "blurb": "Issues and project tracking",         "domain": "linear.app",           "logo": None},
    {"slug": "discord",        "label": "Discord",         "blurb": "Messages and channels",               "domain": "discord.com",          "logo": None},
    {"slug": "x",              "label": "X (Twitter)",     "blurb": "Post and read on X",                  "domain": "x.com",                "logo": None},
    {"slug": "hubspot",        "label": "HubSpot",         "blurb": "CRM search & updates",                "domain": "hubspot.com",          "logo": None},
    {"slug": "salesforce",     "label": "Salesforce",      "blurb": "CRM records and reports",             "domain": "salesforce.com",       "logo": None},
    {"slug": "jira",           "label": "Jira",            "blurb": "Issues and sprints",                   "domain": "atlassian.com",        "logo": None},
    {"slug": "asana",          "label": "Asana",           "blurb": "Tasks and projects",                  "domain": "asana.com",            "logo": None},
    {"slug": "trello",         "label": "Trello",          "blurb": "Boards and cards",                    "domain": "trello.com",           "logo": None},
    {"slug": "dropbox",        "label": "Dropbox",         "blurb": "Files and folders",                   "domain": "dropbox.com",          "logo": None},
    {"slug": "airtable",       "label": "Airtable",        "blurb": "Bases and records",                   "domain": "airtable.com",         "logo": None},
    {"slug": "figma",          "label": "Figma",           "blurb": "Files and comments",                  "domain": "figma.com",            "logo": None},
    {"slug": "stripe",         "label": "Stripe",          "blurb": "Payments and customers",              "domain": "stripe.com",           "logo": None},
    {"slug": "zapier",         "label": "Zapier",          "blurb": "Connect 9,000+ apps",                 "domain": "zapier.com",           "logo": None},
    {"slug": "reddit",         "label": "Reddit",          "blurb": "Browse and post",                     "domain": "reddit.com",           "logo": None},
    {"slug": "sentry",         "label": "Sentry",          "blurb": "Errors and alerts",                   "domain": "sentry.io",            "logo": None},
    {"slug": "posthog",        "label": "PostHog",         "blurb": "Analytics and feature flags",         "domain": "posthog.com",          "logo": None},
]

# ─── In-memory toolkit cache (10 min) ────────────────────────────────────────
_toolkit_cache: Optional[dict] = None
_toolkit_cache_at: float = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Routes ──────────────────────────────────────────────────────────────────

# Composio lists about 1,600 toolkits, 200 per page. Fetch them all so the
# long tail (MCP servers, niche apps) is searchable, with a sane page cap.
CATALOG_PAGE_SIZE = 200
CATALOG_MAX_PAGES = 15


async def _fetch_all_toolkits(composio_key: str):
    """Every toolkit, most used first. Returns (items, error)."""
    items = []
    cursor = None
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(CATALOG_MAX_PAGES):
            params = {"limit": CATALOG_PAGE_SIZE, "sort_by": "usage"}
            if cursor:
                params["cursor"] = cursor
            res = await client.get(f"{BACKEND_URL}/toolkits", params=params, headers={"x-api-key": composio_key})
            # httpx responses have no `.ok`; the old check raised and the
            # bare except silently fell back to the curated list every time.
            if res.status_code != 200:
                return items, f"Composio toolkit listing failed (HTTP {res.status_code})."
            data = res.json()
            page = data.get("items") or data.get("data") or []
            items.extend(page)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or not page:
                break
    return items, None


@router.get("/catalog")
async def catalog(refresh: bool = False):
    """Return toolkit catalog. Tries Composio backend API; falls back to curated list.

    `refresh=1` bypasses the ten-minute cache, e.g. after creating an auth
    config in the Composio dashboard.
    """
    import time
    global _toolkit_cache, _toolkit_cache_at

    cfg = storage_service.get_settings()
    composio_key = cfg.get("composio_api_key") or cfg.get("composio_key") or settings.COMPOSIO_API_KEY

    # Serve cache if fresh
    if not refresh and _toolkit_cache and (time.time() - _toolkit_cache_at) < 600:
        return {**_toolkit_cache, "configured": bool(composio_key)}

    error = None
    if composio_key:
        try:
            items, error = await _fetch_all_toolkits(composio_key)
            if items and not error:
                # Toolkits with an auth config you created yourself are
                # connectable even when Composio has no managed credentials.
                try:
                    own_configs = await composio_service.list_auth_config_slugs(api_key=composio_key)
                except ConnectorServiceError:
                    own_configs = set()
                cards = []
                for t in items:
                    if not isinstance(t, dict) or not (t.get("slug") or t.get("key") or t.get("name")):
                        continue
                    slug = str(t.get("slug") or t.get("key") or t.get("name")).lower()
                    managed = [str(s) for s in (t.get("composio_managed_auth_schemes") or [])]
                    schemes = [str(s) for s in (t.get("auth_schemes") or [])]
                    has_config = slug in own_configs
                    cards.append(
                        {
                            "slug": slug,
                            "label": t.get("name") or t.get("slug") or "",
                            "blurb": ((t.get("meta") or {}).get("description") or t.get("description") or "")[:90],
                            "logo": (t.get("meta") or {}).get("logo") or t.get("logo"),
                            "domain": None,
                            "managed_auth": bool(managed),
                            "auth_schemes": schemes,
                            "has_auth_config": has_config,
                            # True when clicking Connect cannot work until an
                            # auth config with your own credentials exists.
                            "needs_setup": not managed and not has_config and not t.get("no_auth"),
                        }
                    )
                if cards:
                    _toolkit_cache = {"cards": cards, "source": "api", "total": len(cards)}
                    _toolkit_cache_at = time.time()
                    return {**_toolkit_cache, "configured": True}
                error = "Composio returned an empty toolkit list."
            elif not error:
                error = "Composio returned an empty toolkit list."
        except Exception as exc:
            error = f"Composio toolkit listing failed: {exc}"

    result = {"cards": CURATED, "source": "curated", "configured": bool(composio_key)}
    if error:
        result["error"] = error
    return result


@router.get("")
async def connection_status(services: str = ""):
    """Check connection status for a comma-separated list of slugs."""
    cfg = storage_service.get_settings()
    composio_key = cfg.get("composio_api_key") or cfg.get("composio_key") or settings.COMPOSIO_API_KEY
    slugs = [s.strip() for s in services.split(",") if s.strip()]

    if not composio_key or not slugs:
        return {"services": {slug: {"connected": False} for slug in slugs}}

    try:
        status = await composio_service.connection_status(slugs, api_key=composio_key)
        return {"services": status}
    except Exception as e:
        return {"services": {slug: {"connected": False} for slug in slugs}, "error": str(e)}


@router.post("/{slug}/authorize")
async def authorize(slug: str):
    """Get OAuth URL to connect a service."""
    cfg = storage_service.get_settings()
    composio_key = cfg.get("composio_api_key") or cfg.get("composio_key") or settings.COMPOSIO_API_KEY
    if not composio_key:
        return JSONResponse({"error": "No Composio key configured"}, status_code=400)
    try:
        link = await composio_service.create_auth_link(slug, api_key=composio_key)
        # The new connection shows up as tools once the OAuth dance finishes.
        composio_service.forget_connections()
        if link.get("connected"):
            storage_service.add_audit_event({
                "event": "connector.connected",
                "connector": slug,
                "no_auth": True,
                "created_at": _now(),
            })
            return {"url": None, "connected": True, "no_auth": True}
        storage_service.add_audit_event({
            "event": "connector.authorization_requested",
            "connector": slug,
            "connected_account_id": link.get("connected_account_id"),
            "created_at": _now(),
        })
        return {"url": link["url"], "expires_at": link.get("expires_at")}
    except ConnectorServiceError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Authorization failed: {e}"}, status_code=502)


@router.delete("/{slug}")
async def disconnect(slug: str):
    """Disconnect a service by removing all connected accounts."""
    cfg = storage_service.get_settings()
    composio_key = cfg.get("composio_api_key") or cfg.get("composio_key") or settings.COMPOSIO_API_KEY
    if not composio_key:
        return JSONResponse({"error": "No Composio key configured"}, status_code=400)
    try:
        removed = await composio_service.disconnect(slug, api_key=composio_key)
        composio_service.forget_connections()
        storage_service.add_audit_event({
            "event": "connector.disconnected",
            "connector": slug,
            "removed": removed,
            "created_at": _now(),
        })
        return {"removed": removed, "slug": slug}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
