import asyncio
import unittest
from unittest import mock

from app.routers import tools as tools_router
from app.services.llm_tools import ToolSpec
from app.services import storage_service as storage_module


class Storage:
    def __init__(self, disabled):
        self.disabled = disabled

    def get_settings(self):
        return {"disabled_toolkits": self.disabled}


class Composio:
    def get_api_key(self):
        return "k"


def _connector(name, toolkit, title, read_only):
    return ToolSpec(
        name=name,
        description=f"[{toolkit}] {title}",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        group="connector",
        needs_approval=not read_only,
        connector={"slug": name.upper(), "toolkit": toolkit, "toolkit_name": toolkit.title(), "title": title, "read_only": read_only},
    )


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_groups_builtin_and_connector_tools(self):
        specs = [
            _connector("gmail_fetch_emails", "gmail", "Fetch Emails", True),
            _connector("gmail_send_email", "gmail", "Send Email", False),
            _connector("exa_search", "exa", "Search", True),
        ]

        async def fake_specs(service=None):
            return specs

        with mock.patch.object(tools_router, "connector_tool_specs", fake_specs), mock.patch.object(
            storage_module, "storage_service", Storage(["exa", 7])
        ), mock.patch.object(tools_router, "composio_service", Composio()), mock.patch.object(
            tools_router.settings, "COMPUTER_PROVIDER", "docker"
        ):
            catalog = asyncio.run(tools_router.tool_catalog())

        self.assertEqual([g["id"] for g in catalog["groups"]], ["workspace", "computer", "procedures", "share", "memory", "projects", "todo", "schedule", "questions", "help", "orchestration", "connectors", "mcp"])
        self.assertEqual(catalog["groups"][0]["label"], "Shared folder")
        self.assertIn("shared_folder_write", [t["name"] for t in catalog["groups"][0]["tools"]])
        self.assertEqual([t["slug"] for t in catalog["toolkits"]], ["gmail", "exa"])
        gmail = catalog["toolkits"][0]
        self.assertEqual(gmail["name"], "Gmail")
        self.assertEqual([(t["name"], t["needs_approval"]) for t in gmail["tools"]], [("gmail_fetch_emails", False), ("gmail_send_email", True)])
        self.assertEqual(gmail["tools"][1]["title"], "Send Email")
        self.assertEqual(catalog["disabled_toolkits"], ["exa"])
        # The hand-written GitHub fallback is not offered when live connectors exist.
        self.assertNotIn("github", [g["id"] for g in catalog["groups"]])


if __name__ == "__main__":
    unittest.main()
