"""Composio adapter used by governed connector actions.

Talks to Composio's v3 REST API with the project API key. Composio's hosted
MCP endpoint (connect.composio.dev) no longer accepts API keys, only OAuth
bearer tokens, so connection management and tool execution go through the
REST endpoints instead: auth configs, connected accounts, and
`/tools/execute/{tool_slug}`.
"""

import contextlib
import contextvars
import os
import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx
import time

from app.config import settings
from app.services.identity import LOCAL_USER_ID, get_current_owner
from app.services import storage_service as storage_module
from app.services.storage_service import StorageService, current_storage

# Whose connections a call uses when not the current owner's: on a bot shared
# with a teammate, an app the owner set to "each person's own" (sharing.py).
_account: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("composio_account", default=None)


@contextlib.contextmanager
def as_account(account_id: Optional[str]) -> Iterator[None]:
    """Use this account's Composio connections (and its app settings) inside the block."""
    token = _account.set(account_id)
    try:
        yield
    finally:
        _account.reset(token)


BACKEND_URL = "https://backend.composio.dev/api/v3"
# Composio's own toolkit: catalog search and connection management. It needs
# no auth, so it is on as soon as a key is set.
COMPOSIO_TOOLKIT = "composio"
# Composio scopes connected accounts to a user id. This app has one owner, so
# one fixed id keeps link creation, status checks, and tool execution aligned.
# The value predates the rename to Cubicle and is kept on purpose: every
# connected account in Composio is keyed by it.
DEFAULT_USER_ID = os.getenv("COMPOSIO_USER_ID", "").strip() or "open-grok-bot-local-user"

GITHUB_LIST_ISSUES_TOOL = "GITHUB_LIST_REPOSITORY_ISSUES"
GITHUB_CREATE_ISSUE_TOOL = "GITHUB_CREATE_AN_ISSUE"
_SAFE_GITHUB_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SAFE_TOOLKIT_SLUG = re.compile(r"^[a-z0-9_-]{1,64}$")
_SAFE_TOOL_SLUG = re.compile(r"^[A-Z0-9_]{1,128}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TOOLS_CACHE_SECONDS = 600.0
CONNECTED_CACHE_SECONDS = 60.0

ClientFactory = Callable[[httpx.Timeout], httpx.AsyncClient]


class ConnectorServiceError(ValueError):
    """Raised when a connector action cannot be validated or completed."""


def _default_client_factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def extract_error_message(payload: Any, default: str) -> str:
    """Composio errors arrive as {"error": {"message": ...}} or {"error": "..."}."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("slug")
            if message:
                suggestion = error.get("suggested_fix")
                return f"{message} {suggestion}".strip() if suggestion else str(message)
        elif isinstance(error, str) and error:
            return error
        if isinstance(payload.get("message"), str) and payload["message"]:
            return payload["message"]
    return default


class ComposioService:
    def __init__(
        self,
        storage: Optional[StorageService] = None,
        client_factory: Optional[ClientFactory] = None,
        user_id: Optional[str] = None,
        base_url: str = BACKEND_URL,
    ):
        self._storage = storage
        self._client_factory = client_factory or _default_client_factory
        self._user_id = user_id
        self.base_url = base_url.rstrip("/")
        # Tool catalogs change rarely; connections change when the user acts.
        self._tools_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._connected_caches: Dict[str, Tuple[float, List[str]]] = {}
        self._allowed_cache: Dict[str, Tuple[float, Optional[List[str]]]] = {}

    @property
    def storage(self) -> StorageService:
        if self._storage is not None:
            return self._storage
        account = _account.get()
        return storage_module.storage_service.for_owner(account) if account else current_storage()

    @property
    def user_id(self) -> str:
        """The Composio user (entity) the current account's connections live under.
        The first account keeps the id it has always had; others get their own."""
        if self._user_id:
            return self._user_id
        owner = _account.get() or get_current_owner()
        return DEFAULT_USER_ID if owner == LOCAL_USER_ID else f"cubicle-{owner}"

    @property
    def _connected_cache(self) -> Optional[Tuple[float, List[str]]]:
        return self._connected_caches.get(self.user_id)

    @_connected_cache.setter
    def _connected_cache(self, value: Optional[Tuple[float, List[str]]]) -> None:
        if value is None:
            self._connected_caches.pop(self.user_id, None)
        else:
            self._connected_caches[self.user_id] = value

    def get_api_key(self) -> str:
        config = self.storage.get_settings()
        return str(
            config.get("composio_api_key")
            or config.get("composio_key")
            or settings.COMPOSIO_API_KEY
            or ""
        ).strip()

    # ---- low-level -------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api_key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        key = (api_key or self.get_api_key()).strip()
        if not key:
            raise ConnectorServiceError(
                "Configure a Composio API key in App Settings before using connectors."
            )

        try:
            async with self._client_factory(httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"x-api-key": key, "accept": "application/json"},
                    params=params,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise ConnectorServiceError(f"Composio is unreachable: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.status_code == 401:
            raise ConnectorServiceError(
                "Composio rejected the API key (HTTP 401). Check the key in App Settings."
            )
        if response.status_code == 403:
            # A valid key without permission for this operation, typically a
            # scoped project key. Pass Composio's explanation through.
            detail = extract_error_message(data, "the key is not allowed to perform this operation")
            raise ConnectorServiceError(
                f"Composio refused the request (HTTP 403): {detail} "
                "Check the API key's permissions in the Composio dashboard."
            )
        if response.status_code >= 400:
            raise ConnectorServiceError(
                extract_error_message(data, f"Composio returned HTTP {response.status_code}.")
            )
        return data

    @staticmethod
    def _validate_toolkit_slug(slug: str) -> str:
        candidate = (slug or "").strip().lower()
        if not _SAFE_TOOLKIT_SLUG.fullmatch(candidate):
            raise ConnectorServiceError("Connector name is invalid.")
        return candidate

    # ---- connected accounts ---------------------------------------------------

    async def list_connected_accounts(
        self, toolkit_slugs: List[str], api_key: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        slugs = [self._validate_toolkit_slug(slug) for slug in toolkit_slugs]
        if not slugs:
            return []
        data = await self._request(
            "GET",
            "/connected_accounts",
            api_key=api_key,
            params={"toolkit_slugs": ",".join(slugs), "user_ids": self.user_id, "limit": 100},
        )
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    async def list_connected_toolkits(self, api_key: Optional[str] = None) -> List[str]:
        """Slugs of every toolkit with an ACTIVE connection for this user."""
        now = time.monotonic()
        if self._connected_cache and self._connected_cache[0] > now:
            return self._with_no_auth(self._connected_cache[1])
        data = await self._request(
            "GET",
            "/connected_accounts",
            api_key=api_key,
            params={"user_ids": self.user_id, "statuses": "ACTIVE", "limit": 100},
        )
        items = data.get("items") if isinstance(data, dict) else None
        slugs: List[str] = []
        for account in items or []:
            if not isinstance(account, dict) or account.get("is_disabled"):
                continue
            if str(account.get("status") or "").upper() != "ACTIVE":
                continue
            slug = str((account.get("toolkit") or {}).get("slug") or "").lower()
            if slug and _SAFE_TOOLKIT_SLUG.fullmatch(slug) and slug not in slugs:
                slugs.append(slug)
        self._connected_cache = (now + CONNECTED_CACHE_SECONDS, slugs)
        return self._with_no_auth(slugs)

    def _with_no_auth(self, slugs: List[str]) -> List[str]:
        merged = list(slugs)
        for slug in self.enabled_no_auth_toolkits():
            if slug not in merged:
                merged.append(slug)
        return merged

    # ---- toolkits that need no authentication ----------------------------------
    #
    # Composio refuses to create an auth config for a toolkit whose only scheme
    # is NO_AUTH ("it does not require authentication"), and its tools run with
    # just a user id. So connecting one is purely local bookkeeping: the slug
    # is kept in app settings and counts as connected from then on.

    def enabled_no_auth_toolkits(self) -> List[str]:
        """No-auth toolkits that count as connected: Composio's own (whenever a key
        is set, unless the user switched it off) plus the ones the user connected.
        """
        config = self.storage.get_settings()
        off = self._no_auth_off(config)
        slugs: List[str] = []
        if COMPOSIO_TOOLKIT not in off and self.get_api_key():
            slugs.append(COMPOSIO_TOOLKIT)
        raw = config.get("no_auth_toolkits") or []
        for slug in raw if isinstance(raw, list) else []:
            text = str(slug).lower()
            if _SAFE_TOOLKIT_SLUG.fullmatch(text) and text not in slugs and text not in off:
                slugs.append(text)
        return slugs

    @staticmethod
    def _stored_no_auth(config: Dict[str, Any]) -> List[str]:
        raw = config.get("no_auth_toolkits") or []
        return [str(s).lower() for s in raw if isinstance(s, str) and _SAFE_TOOLKIT_SLUG.fullmatch(str(s).lower())] if isinstance(raw, list) else []

    @staticmethod
    def _no_auth_off(config: Dict[str, Any]) -> List[str]:
        raw = config.get("no_auth_toolkits_off") or []
        return [str(s).lower() for s in raw if isinstance(s, str)] if isinstance(raw, list) else []

    def _set_no_auth_toolkits(self, slugs: List[str], off: Optional[List[str]] = None) -> None:
        save = getattr(self.storage, "save_settings", None)
        if callable(save):
            data: Dict[str, Any] = {"no_auth_toolkits": sorted(set(slugs))}
            if off is not None:
                data["no_auth_toolkits_off"] = sorted(set(off))
            save(data)
        self.forget_connections()

    async def is_no_auth_toolkit(self, toolkit_slug: str, api_key: Optional[str] = None) -> bool:
        info = await self.toolkit_auth_info(toolkit_slug, api_key=api_key)
        return "NO_AUTH" in info["auth_schemes"] and not info["managed_schemes"]

    def forget_connections(self) -> None:
        """Drop the cached connection list after the user connects or disconnects."""
        self._connected_cache = None
        self._allowed_cache = {}

    async def allowed_tools(self, toolkit_slug: str, api_key: Optional[str] = None) -> Optional[List[str]]:
        """The allow list set on the toolkit's auth config in the Composio dashboard.

        Returns None when no restriction applies. With several enabled auth
        configs for one toolkit, the lists are combined; a config without a
        list means the toolkit is unrestricted.
        """
        slug = self._validate_toolkit_slug(toolkit_slug)
        now = time.monotonic()
        cached = self._allowed_cache.get(slug)
        if cached and cached[0] > now:
            return list(cached[1]) if cached[1] is not None else None
        data = await self._request("GET", "/auth_configs", api_key=api_key, params={"toolkit_slug": slug, "limit": 50})
        items = data.get("items") if isinstance(data, dict) else None
        allowed: List[str] = []
        restricted = False
        unrestricted = False
        for config in items or []:
            if not isinstance(config, dict) or str(config.get("status") or "ENABLED").upper() != "ENABLED":
                continue
            access = config.get("tool_access_config") if isinstance(config.get("tool_access_config"), dict) else {}
            tools = [str(t) for t in (access.get("tools_available_for_execution") or []) if _SAFE_TOOL_SLUG.fullmatch(str(t))]
            if tools:
                restricted = True
                allowed.extend(t for t in tools if t not in allowed)
            else:
                unrestricted = True
        result = allowed if restricted and not unrestricted else None
        self._allowed_cache[slug] = (now + CONNECTED_CACHE_SECONDS, result)
        return list(result) if result is not None else None

    async def list_tools(
        self,
        toolkit_slug: str,
        *,
        important_only: bool = True,
        limit: int = 40,
        tool_slugs: Optional[List[str]] = None,
        api_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Composio's tool descriptions (slug, description, input schema, tags) for one toolkit.

        With `tool_slugs`, exactly those tools are requested (in batches), so
        an allow list is honoured even for toolkits with hundreds of tools.
        """
        slug = self._validate_toolkit_slug(toolkit_slug)
        wanted = sorted({s for s in (tool_slugs or []) if _SAFE_TOOL_SLUG.fullmatch(s)})
        cache_key = f"{slug}:{int(bool(important_only))}:{limit}:{','.join(wanted)}"
        now = time.monotonic()
        cached = self._tools_cache.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1])

        tools: List[Dict[str, Any]] = []
        if wanted:
            for start in range(0, len(wanted), 50):
                batch = wanted[start:start + 50]
                data = await self._request(
                    "GET", "/tools", api_key=api_key,
                    params={"toolkit_slug": slug, "tool_slugs": ",".join(batch), "limit": len(batch),
                            "include_deprecated": "false", "toolkit_versions": "latest"},
                )
                items = data.get("items") if isinstance(data, dict) else None
                tools.extend(item for item in (items or []) if isinstance(item, dict) and item.get("slug"))
        else:
            # Composio includes deprecated tools unless told otherwise; they
            # would only waste the per-app budget. MCP-backed toolkits have no
            # pinned version and list nothing unless "latest" is requested.
            params: Dict[str, Any] = {
                "toolkit_slug": slug,
                "limit": max(1, min(int(limit), 200)),
                "include_deprecated": "false",
                "toolkit_versions": "latest",
            }
            if important_only:
                params["important"] = "true"
            data = await self._request("GET", "/tools", api_key=api_key, params=params)
            items = data.get("items") if isinstance(data, dict) else None
            tools = [item for item in (items or []) if isinstance(item, dict) and item.get("slug")]
        self._tools_cache[cache_key] = (now + TOOLS_CACHE_SECONDS, tools)
        return list(tools)

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], api_key: Optional[str] = None, version: Optional[str] = None
    ) -> Dict[str, Any]:
        """Run a Composio tool and return its data, raising when Composio reports failure."""
        response = (await self.call_tool(name, arguments, api_key=api_key, version=version))["data"]
        if response.get("successful") is False or response.get("error"):
            raise ConnectorServiceError(
                extract_error_message(response.get("error"), f"Composio could not run {name}.")
                if not isinstance(response.get("error"), str)
                else str(response.get("error"))
            )
        return {"tool": name, "data": response.get("data")}

    async def connection_status(
        self, toolkit_slugs: List[str], api_key: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Map each toolkit slug to whether an ACTIVE connection exists."""
        slugs = [self._validate_toolkit_slug(slug) for slug in toolkit_slugs]
        status: Dict[str, Dict[str, Any]] = {slug: {"connected": False} for slug in slugs}
        for account in await self.list_connected_accounts(slugs, api_key=api_key):
            slug = str((account.get("toolkit") or {}).get("slug") or "").lower()
            if slug not in status:
                continue
            active = str(account.get("status") or "").upper() == "ACTIVE" and not account.get("is_disabled")
            if active:
                status[slug] = {"connected": True, "account_id": account.get("id")}
            elif not status[slug]["connected"]:
                status[slug] = {"connected": False, "pending_status": account.get("status")}
        for slug in self.enabled_no_auth_toolkits():
            if slug in status:
                status[slug] = {"connected": True, "no_auth": True}
        return status

    async def toolkit_auth_info(self, toolkit_slug: str, api_key: Optional[str] = None) -> Dict[str, Any]:
        """Which auth schemes a toolkit offers and which Composio manages itself."""
        slug = self._validate_toolkit_slug(toolkit_slug)
        data = await self._request("GET", f"/toolkits/{slug}", api_key=api_key)
        if not isinstance(data, dict):
            return {"slug": slug, "managed_schemes": [], "auth_schemes": []}
        details = data.get("auth_config_details") or []
        schemes = [
            str(item.get("mode"))
            for item in details
            if isinstance(item, dict) and item.get("mode")
        ] or [str(s) for s in (data.get("auth_schemes") or [])]
        return {
            "slug": slug,
            "managed_schemes": [str(s) for s in (data.get("composio_managed_auth_schemes") or [])],
            "auth_schemes": schemes,
        }

    async def list_auth_config_slugs(self, api_key: Optional[str] = None) -> set:
        """Toolkit slugs that already have an enabled auth config in the project."""
        slugs: set = set()
        cursor: Optional[str] = None
        for _ in range(5):
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/auth_configs", api_key=api_key, params=params)
            if not isinstance(data, dict):
                break
            for item in data.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "ENABLED").upper() != "ENABLED":
                    continue
                slug = str((item.get("toolkit") or {}).get("slug") or "").lower()
                if slug:
                    slugs.add(slug)
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return slugs

    async def ensure_auth_config(self, toolkit_slug: str, api_key: Optional[str] = None, info: Optional[Dict[str, Any]] = None) -> str:
        """Return an enabled auth config id for the toolkit, creating a
        Composio-managed one when the project has none yet."""
        slug = self._validate_toolkit_slug(toolkit_slug)
        data = await self._request(
            "GET", "/auth_configs", api_key=api_key, params={"toolkit_slug": slug, "limit": 20}
        )
        items = [
            item
            for item in ((data.get("items") if isinstance(data, dict) else None) or [])
            if isinstance(item, dict)
            and item.get("id")
            and str(item.get("status") or "ENABLED").upper() == "ENABLED"
            and str((item.get("toolkit") or {}).get("slug") or slug).lower() == slug
        ]
        if items:
            managed = [item for item in items if item.get("is_composio_managed")]
            return str((managed or items)[0]["id"])

        if info is None:
            info = await self.toolkit_auth_info(slug, api_key=api_key)
        if not info["managed_schemes"]:
            schemes = ", ".join(info["auth_schemes"]) or "its own credentials"
            raise ConnectorServiceError(
                f"Composio has no managed credentials for {slug}. Create an auth config for it in "
                f"the Composio dashboard (Auth Configs) using your own credentials ({schemes}), "
                "then connect again."
            )

        created = await self._request(
            "POST",
            "/auth_configs",
            api_key=api_key,
            json={"toolkit": {"slug": slug}, "auth_config": {"type": "use_composio_managed_auth"}},
        )
        auth_config = created.get("auth_config") if isinstance(created, dict) else None
        if not isinstance(auth_config, dict) or not auth_config.get("id"):
            raise ConnectorServiceError(f"Composio did not return an auth config for {slug}.")
        return str(auth_config["id"])

    async def create_auth_link(
        self,
        toolkit_slug: str,
        callback_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start an OAuth connection and return the URL the user must open.

        A toolkit that needs no authentication is connected on the spot: the
        result has no url and `connected` is true.
        """
        slug = self._validate_toolkit_slug(toolkit_slug)
        info = await self.toolkit_auth_info(slug, api_key=api_key)
        if "NO_AUTH" in info["auth_schemes"] and not info["managed_schemes"]:
            config = self.storage.get_settings()
            off = [s for s in self._no_auth_off(config) if s != slug]
            stored = self._stored_no_auth(config)
            self._set_no_auth_toolkits(stored + ([slug] if slug != COMPOSIO_TOOLKIT else []), off=off)
            return {"url": None, "connected": True, "no_auth": True}
        auth_config_id = await self.ensure_auth_config(slug, api_key=api_key, info=info)
        body: Dict[str, Any] = {"auth_config_id": auth_config_id, "user_id": self.user_id}
        if callback_url:
            body["callback_url"] = callback_url
        data = await self._request("POST", "/connected_accounts/link", api_key=api_key, json=body)
        url = data.get("redirect_url") if isinstance(data, dict) else None
        if not url:
            raise ConnectorServiceError(f"Composio did not return an authorization link for {slug}.")
        return {
            "url": url,
            "connected_account_id": data.get("connected_account_id"),
            "expires_at": data.get("expires_at"),
        }

    async def disconnect(self, toolkit_slug: str, api_key: Optional[str] = None) -> int:
        """Delete every connected account for the toolkit; returns how many."""
        slug = self._validate_toolkit_slug(toolkit_slug)
        removed = 0
        if slug in self.enabled_no_auth_toolkits():
            config = self.storage.get_settings()
            self._set_no_auth_toolkits([s for s in self._stored_no_auth(config) if s != slug], off=self._no_auth_off(config) + [slug])
            removed += 1
        for account in await self.list_connected_accounts([slug], api_key=api_key):
            account_id = account.get("id")
            if not account_id:
                continue
            await self._request("DELETE", f"/connected_accounts/{account_id}", api_key=api_key)
            removed += 1
        return removed

    # ---- tool execution -------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        api_key: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a Composio tool for this app's user.

        Returns {"data": <execute response>} where the execute response has
        `successful`, `data`, and `error`, which the normalisers below check.
        `version` pins the toolkit version the tool schema came from.
        """
        if not _SAFE_TOOL_SLUG.fullmatch(name or ""):
            raise ConnectorServiceError("Connector tool name is invalid.")
        body: Dict[str, Any] = {"user_id": self.user_id, "arguments": arguments}
        if version and _SAFE_VERSION.fullmatch(str(version)):
            body["version"] = str(version)
        result = await self._request(
            "POST",
            f"/tools/execute/{name}",
            api_key=api_key,
            json=body,
        )
        if not isinstance(result, dict):
            raise ConnectorServiceError("Composio returned an invalid tool result.")
        return {"data": result}

    # ---- GitHub actions -------------------------------------------------------

    @staticmethod
    def _validate_github_part(value: str, label: str) -> str:
        if not isinstance(value, str) or not _SAFE_GITHUB_PART.fullmatch(value):
            raise ConnectorServiceError(f"GitHub {label} is invalid.")
        return value

    async def list_github_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 10,
    ) -> Dict[str, Any]:
        owner = self._validate_github_part(owner, "owner")
        repo = self._validate_github_part(repo, "repository")
        if state not in {"open", "closed", "all"}:
            raise ConnectorServiceError("Issue state must be open, closed, or all.")
        if not isinstance(per_page, int) or not 1 <= per_page <= 20:
            raise ConnectorServiceError("Issue page size must be between 1 and 20.")

        response = await self.call_tool(
            GITHUB_LIST_ISSUES_TOOL,
            {
                "owner": owner,
                "repo": repo,
                "state": state,
                "per_page": per_page,
            },
        )
        payload = response.get("data", response)
        if isinstance(payload, dict) and payload.get("successful") is False:
            raise ConnectorServiceError(payload.get("error") or "GitHub issue lookup failed.")
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]

        if isinstance(payload, list):
            raw_issues = payload
        elif isinstance(payload, dict):
            raw_issues = payload.get("issues") or payload.get("items") or payload.get("details") or []
        else:
            raw_issues = []

        issues = []
        for item in raw_issues[:per_page]:
            if not isinstance(item, dict):
                continue
            labels = item.get("labels") or []
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "url": item.get("html_url") or item.get("url"),
                    "author": (item.get("user") or {}).get("login"),
                    "labels": [
                        label.get("name")
                        for label in labels
                        if isinstance(label, dict) and label.get("name")
                    ],
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )

        return {
            "connector": "github",
            "operation": "list_issues",
            "repository": f"{owner}/{repo}",
            "state": state,
            "count": len(issues),
            "issues": issues,
        }

    async def create_github_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
    ) -> Dict[str, Any]:
        owner = self._validate_github_part(owner, "owner")
        repo = self._validate_github_part(repo, "repository")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 256:
            raise ConnectorServiceError("Issue title must be between 1 and 256 characters.")
        if not isinstance(body, str) or len(body) > 10000:
            raise ConnectorServiceError("Issue body must be at most 10000 characters.")

        response = await self.call_tool(
            GITHUB_CREATE_ISSUE_TOOL,
            {
                "owner": owner,
                "repo": repo,
                "title": title.strip(),
                "body": body,
            },
        )
        payload = response.get("data", response)
        if isinstance(payload, dict) and payload.get("successful") is False:
            raise ConnectorServiceError(payload.get("error") or "GitHub issue creation failed.")
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            payload = {}

        return {
            "connector": "github",
            "operation": "create_issue",
            "repository": f"{owner}/{repo}",
            "created": True,
            "issue": {
                "number": payload.get("number"),
                "title": payload.get("title") or title.strip(),
                "state": payload.get("state") or "open",
                "url": payload.get("html_url") or payload.get("url"),
            },
        }


composio_service = ComposioService()
