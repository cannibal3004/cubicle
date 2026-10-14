import asyncio
import json
import unittest

import httpx

from app.services.composio_service import ComposioService, ConnectorServiceError


class KeyedStorage:
    def get_settings(self):
        return {"composio_api_key": "ak_test"}


class EmptyStorage:
    def get_settings(self):
        return {}


class SavingStorage:
    """Keeps what the service saves, as the real settings store would."""

    def __init__(self):
        self.values = {"composio_api_key": "ak_test"}

    def get_settings(self):
        return dict(self.values)

    def save_settings(self, data):
        self.values.update(data)


class FakeComposio:
    """Minimal stand-in for the Composio v3 REST API."""

    def __init__(self, accounts=None, auth_configs=None, execute=None, fail_with=None, unmanaged=(), no_auth=()):
        self.unmanaged = set(unmanaged)
        self.no_auth = set(no_auth)
        self.accounts = accounts or []
        self.auth_configs = auth_configs or []
        self.execute = execute or {"successful": True, "data": {}, "error": None}
        self.fail_with = fail_with
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return self.fail_with
        path = request.url.path
        if request.method == "GET" and path == "/api/v3/connected_accounts":
            return httpx.Response(200, json={"items": self.accounts, "total_items": len(self.accounts)})
        if request.method == "GET" and path == "/api/v3/auth_configs":
            return httpx.Response(200, json={"items": self.auth_configs})
        if request.method == "GET" and path.startswith("/api/v3/toolkits/"):
            slug = path.rsplit("/", 1)[-1]
            if slug in self.no_auth:
                # What Composio reports for hackernews, composio and the like.
                return httpx.Response(200, json={"slug": slug, "composio_managed_auth_schemes": [], "auth_config_details": [{"mode": "NO_AUTH"}]})
            managed = [] if slug in self.unmanaged else ["OAUTH2"]
            return httpx.Response(
                200,
                json={
                    "slug": slug,
                    "composio_managed_auth_schemes": managed,
                    "auth_config_details": [{"mode": "OAUTH2"}, {"mode": "API_KEY"}],
                },
            )
        if request.method == "POST" and path == "/api/v3/auth_configs":
            return httpx.Response(
                201,
                json={"toolkit": {"slug": "github"}, "auth_config": {"id": "ac_new", "auth_scheme": "OAUTH2", "is_composio_managed": True}},
            )
        if request.method == "POST" and path == "/api/v3/connected_accounts/link":
            return httpx.Response(
                201,
                json={
                    "link_token": "lt_1",
                    "redirect_url": "https://connect.composio.dev/link/lt_1",
                    "expires_at": "2026-10-07T00:00:00Z",
                    "connected_account_id": "ca_pending",
                },
            )
        if request.method == "DELETE" and path.startswith("/api/v3/connected_accounts/"):
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and path.startswith("/api/v3/tools/execute/"):
            return httpx.Response(200, json=self.execute)
        return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {path}"}})


def _service(fake: FakeComposio, storage=None) -> ComposioService:
    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), timeout=timeout)

    return ComposioService(storage or KeyedStorage(), client_factory=factory, user_id="test-user")


class ComposioServiceTests(unittest.TestCase):
    def test_requests_carry_api_key_and_user_id(self):
        fake = FakeComposio()
        asyncio.run(_service(fake).connection_status(["github", "slack"]))
        request = fake.requests[0]
        self.assertEqual(request.headers["x-api-key"], "ak_test")
        self.assertEqual(request.url.params["toolkit_slugs"], "github,slack")
        self.assertEqual(request.url.params["user_ids"], "test-user")

    def test_connection_status_only_counts_active_enabled_accounts(self):
        fake = FakeComposio(
            accounts=[
                {"id": "ca_1", "status": "ACTIVE", "is_disabled": False, "toolkit": {"slug": "github"}},
                {"id": "ca_2", "status": "INITIATED", "is_disabled": False, "toolkit": {"slug": "slack"}},
                {"id": "ca_3", "status": "ACTIVE", "is_disabled": True, "toolkit": {"slug": "gmail"}},
            ]
        )
        status = asyncio.run(_service(fake).connection_status(["github", "slack", "gmail"]))
        self.assertEqual(status["github"], {"connected": True, "account_id": "ca_1"})
        self.assertEqual(status["slack"], {"connected": False, "pending_status": "INITIATED"})
        self.assertFalse(status["gmail"]["connected"])

    def test_auth_link_creates_a_managed_auth_config_when_none_exists(self):
        fake = FakeComposio(auth_configs=[])
        link = asyncio.run(_service(fake).create_auth_link("github"))
        self.assertEqual(link["url"], "https://connect.composio.dev/link/lt_1")
        self.assertEqual(link["connected_account_id"], "ca_pending")

        methods = [(r.method, r.url.path) for r in fake.requests]
        self.assertEqual(
            methods,
            [
                ("GET", "/api/v3/toolkits/github"),
                ("GET", "/api/v3/auth_configs"),
                ("POST", "/api/v3/auth_configs"),
                ("POST", "/api/v3/connected_accounts/link"),
            ],
        )
        created = json.loads(fake.requests[2].content)
        self.assertEqual(created, {"toolkit": {"slug": "github"}, "auth_config": {"type": "use_composio_managed_auth"}})
        linked = json.loads(fake.requests[3].content)
        self.assertEqual(linked, {"auth_config_id": "ac_new", "user_id": "test-user"})

    def test_a_no_auth_toolkit_connects_locally_without_an_auth_config(self):
        storage = SavingStorage()
        fake = FakeComposio(auth_configs=[], no_auth={"hackernews"})
        service = _service(fake, storage)
        link = asyncio.run(service.create_auth_link("hackernews"))
        self.assertEqual(link, {"url": None, "connected": True, "no_auth": True})
        # One lookup of the toolkit, nothing created at Composio.
        self.assertEqual([(r.method, r.url.path) for r in fake.requests], [("GET", "/api/v3/toolkits/hackernews")])
        self.assertEqual(storage.values["no_auth_toolkits"], ["hackernews"])
        # It counts as connected everywhere the catalog looks, next to Composio's own toolkit.
        self.assertEqual(sorted(asyncio.run(service.list_connected_toolkits())), ["composio", "hackernews"])
        status = asyncio.run(service.connection_status(["hackernews", "github"]))
        self.assertEqual(status["hackernews"], {"connected": True, "no_auth": True})
        self.assertFalse(status["github"]["connected"])
        # Disconnecting forgets it, with no account to delete.
        self.assertEqual(asyncio.run(service.disconnect("hackernews")), 1)
        self.assertEqual(storage.values["no_auth_toolkits"], [])
        self.assertNotIn("hackernews", asyncio.run(service.list_connected_toolkits()))

    def test_composios_own_toolkit_is_on_with_a_key_until_switched_off(self):
        storage = SavingStorage()
        fake = FakeComposio(auth_configs=[], no_auth={"composio"})
        service = _service(fake, storage)
        self.assertEqual(asyncio.run(service.list_connected_toolkits()), ["composio"])
        self.assertEqual(asyncio.run(service.connection_status(["composio"]))["composio"], {"connected": True, "no_auth": True})
        # Without a key there is nothing to search with.
        self.assertEqual(_service(fake, EmptyStorage()).enabled_no_auth_toolkits(), [])
        # Disconnecting switches it off and remembers that; connecting again switches it back on.
        self.assertEqual(asyncio.run(service.disconnect("composio")), 1)
        self.assertEqual(storage.values["no_auth_toolkits_off"], ["composio"])
        self.assertEqual(asyncio.run(service.list_connected_toolkits()), [])
        asyncio.run(service.create_auth_link("composio"))
        self.assertEqual(storage.values["no_auth_toolkits_off"], [])
        self.assertEqual(asyncio.run(service.list_connected_toolkits()), ["composio"])

    def test_toolkit_without_managed_credentials_gets_setup_instructions(self):
        fake = FakeComposio(auth_configs=[], unmanaged={"klaviyo"})
        with self.assertRaisesRegex(ConnectorServiceError, "no managed credentials for klaviyo.*OAUTH2, API_KEY"):
            asyncio.run(_service(fake).create_auth_link("klaviyo"))
        self.assertNotIn("POST", [r.method for r in fake.requests])

    def test_auth_config_slugs_follow_pagination_and_skip_disabled(self):
        pages = {
            None: {"items": [{"status": "ENABLED", "toolkit": {"slug": "klaviyo"}}], "next_cursor": "p2"},
            "p2": {"items": [{"status": "DISABLED", "toolkit": {"slug": "x"}}, {"toolkit": {"slug": "Notion"}}], "next_cursor": None},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        service = ComposioService(KeyedStorage(), client_factory=factory, user_id="test-user")
        self.assertEqual(asyncio.run(service.list_auth_config_slugs()), {"klaviyo", "notion"})

    def test_auth_link_prefers_an_existing_composio_managed_config(self):
        fake = FakeComposio(
            auth_configs=[
                {"id": "ac_custom", "status": "ENABLED", "is_composio_managed": False, "toolkit": {"slug": "github"}},
                {"id": "ac_managed", "status": "ENABLED", "is_composio_managed": True, "toolkit": {"slug": "github"}},
                {"id": "ac_off", "status": "DISABLED", "is_composio_managed": True, "toolkit": {"slug": "github"}},
            ]
        )
        asyncio.run(_service(fake).create_auth_link("github"))
        self.assertNotIn("POST /api/v3/auth_configs", [f"{r.method} {r.url.path}" for r in fake.requests])
        linked = json.loads(fake.requests[-1].content)
        self.assertEqual(linked["auth_config_id"], "ac_managed")

    def test_disconnect_deletes_each_account(self):
        fake = FakeComposio(
            accounts=[
                {"id": "ca_1", "status": "ACTIVE", "toolkit": {"slug": "github"}},
                {"id": "ca_2", "status": "EXPIRED", "toolkit": {"slug": "github"}},
            ]
        )
        removed = asyncio.run(_service(fake).disconnect("github"))
        self.assertEqual(removed, 2)
        deletes = [r.url.path for r in fake.requests if r.method == "DELETE"]
        self.assertEqual(deletes, ["/api/v3/connected_accounts/ca_1", "/api/v3/connected_accounts/ca_2"])

    def test_tool_execution_posts_user_and_arguments_and_normalises_issues(self):
        fake = FakeComposio(
            execute={
                "successful": True,
                "error": None,
                "data": {
                    "details": [
                        {
                            "number": 7,
                            "title": "Streaming stalls",
                            "state": "open",
                            "html_url": "https://github.com/example/project/issues/7",
                            "user": {"login": "octocat"},
                            "labels": [{"name": "bug"}],
                            "body": "not copied",
                        }
                    ]
                },
            }
        )
        result = asyncio.run(_service(fake).list_github_issues("example", "project", "open", 5))
        request = fake.requests[-1]
        self.assertEqual(request.url.path, "/api/v3/tools/execute/GITHUB_LIST_REPOSITORY_ISSUES")
        body = json.loads(request.content)
        self.assertEqual(body["user_id"], "test-user")
        self.assertEqual(body["arguments"]["repo"], "project")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["issues"][0]["number"], 7)
        self.assertNotIn("body", result["issues"][0])

    def test_tool_catalog_requests_skip_deprecated_tools_and_honour_allow_lists(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            if request.url.path == "/api/v3/auth_configs":
                return httpx.Response(200, json={"items": [
                    {"status": "ENABLED", "tool_access_config": {"tools_available_for_execution": ["GMAIL_SEND_EMAIL", "GMAIL_LIST_THREADS"]}},
                    {"status": "DISABLED", "tool_access_config": {"tools_available_for_execution": []}},
                ]})
            slugs = request.url.params.get("tool_slugs")
            items = [{"slug": s} for s in slugs.split(",")] if slugs else [{"slug": "GMAIL_FETCH_EMAILS"}]
            return httpx.Response(200, json={"items": items})

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        service = ComposioService(KeyedStorage(), client_factory=factory, user_id="test-user")

        tools = asyncio.run(service.list_tools("gmail", important_only=True, limit=40))
        self.assertEqual([t["slug"] for t in tools], ["GMAIL_FETCH_EMAILS"])
        self.assertEqual(seen[-1], {"toolkit_slug": "gmail", "limit": "40", "include_deprecated": "false", "toolkit_versions": "latest", "important": "true"})

        allowed = asyncio.run(service.allowed_tools("gmail"))
        self.assertEqual(allowed, ["GMAIL_SEND_EMAIL", "GMAIL_LIST_THREADS"])
        tools = asyncio.run(service.list_tools("gmail", important_only=False, tool_slugs=allowed))
        self.assertEqual(sorted(t["slug"] for t in tools), ["GMAIL_LIST_THREADS", "GMAIL_SEND_EMAIL"])
        self.assertEqual(seen[-1]["tool_slugs"], "GMAIL_LIST_THREADS,GMAIL_SEND_EMAIL")
        self.assertEqual(seen[-1]["include_deprecated"], "false")

        # Both lists are cached; a second call makes no request.
        before = len(seen)
        asyncio.run(service.allowed_tools("gmail"))
        asyncio.run(service.list_tools("gmail", important_only=True, limit=40))
        self.assertEqual(len(seen), before)
        service.forget_connections()
        asyncio.run(service.allowed_tools("gmail"))
        self.assertEqual(len(seen), before + 1)

    def test_unsuccessful_execution_surfaces_the_error(self):
        fake = FakeComposio(execute={"successful": False, "error": "No active connection for github", "data": {}})
        with self.assertRaisesRegex(ConnectorServiceError, "No active connection"):
            asyncio.run(_service(fake).list_github_issues("example", "project"))

    def test_rejected_key_and_api_errors_are_readable(self):
        fake = FakeComposio(fail_with=httpx.Response(401, json={"error": {"message": "Unauthorized"}}))
        with self.assertRaisesRegex(ConnectorServiceError, "rejected the API key"):
            asyncio.run(_service(fake).connection_status(["github"]))

        fake = FakeComposio(fail_with=httpx.Response(403, json={"error": {"message": "Missing scope: auth_configs:write"}}))
        with self.assertRaisesRegex(ConnectorServiceError, "HTTP 403.*Missing scope: auth_configs:write"):
            asyncio.run(_service(fake).create_auth_link("github"))

        fake = FakeComposio(
            fail_with=httpx.Response(
                400, json={"error": {"message": "Toolkit not found", "suggested_fix": "Check the slug.", "code": 400, "slug": "x", "status": 400}}
            )
        )
        with self.assertRaisesRegex(ConnectorServiceError, "Toolkit not found Check the slug."):
            asyncio.run(_service(fake).create_auth_link("github"))

    def test_missing_key_and_bad_names_fail_before_any_request(self):
        fake = FakeComposio()
        with self.assertRaisesRegex(ConnectorServiceError, "Configure a Composio API key"):
            asyncio.run(_service(fake, storage=EmptyStorage()).connection_status(["github"]))
        with self.assertRaisesRegex(ConnectorServiceError, "Connector name is invalid"):
            asyncio.run(_service(fake).create_auth_link("git hub"))
        with self.assertRaisesRegex(ConnectorServiceError, "tool name is invalid"):
            asyncio.run(_service(fake).call_tool("github/issues", {}))
        self.assertEqual(fake.requests, [])


if __name__ == "__main__":
    unittest.main()
