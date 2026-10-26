"""Tool selection: app tools narrowed to the ones that match the message, built-ins kept, recent ones kept."""

import asyncio
import unittest
from unittest import mock

from app.services import retrieval, tool_selection
from app.services.llm_tools import ToolSpec, available_tools
from app.services.retrieval import RetrievalConfig, toy_embedding
from app.services.tool_selection import Selection, select_tools, tool_text

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1")


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


def app_tool(name, toolkit, description):
    return ToolSpec(name=name, description=description, parameters={"type": "object", "properties": {}}, group="connectors", needs_approval=True,
                    connector={"toolkit": toolkit, "toolkit_name": toolkit.title(), "title": name.replace("_", " ")})


APPS = [
    app_tool("gmail_fetch_emails", "gmail", "Fetch recent email messages from the inbox"),
    app_tool("gmail_send_email", "gmail", "Send an email message to recipients"),
    app_tool("gmail_create_label", "gmail", "Create a mailbox label"),
    app_tool("googledrive_list_files", "googledrive", "List files and folders in Google Drive"),
    app_tool("googledrive_upload_file", "googledrive", "Upload a file to Google Drive"),
    app_tool("openweather_current", "openweather_api", "Current weather conditions for a city"),
    app_tool("openweather_forecast", "openweather_api", "Weather forecast for the coming days"),
    app_tool("telegram_send_message", "telegram", "Send a Telegram chat message"),
]


class ToolSelectionTests(unittest.TestCase):
    def setUp(self):
        tool_selection._tool_vectors.clear()
        self.builtin = available_tools(computer=False)
        self.specs = self.builtin + APPS

    def test_under_the_limit_or_off_offers_everything(self):
        specs, info = asyncio.run(select_tools(self.specs, "hello", max_tools=0, config=CONFIG, embed=fake_embed))
        self.assertEqual((len(specs), info.narrowed), (len(self.specs), False))
        specs, info = asyncio.run(select_tools(self.specs, "hello", max_tools=500, config=CONFIG, embed=fake_embed))
        self.assertEqual(len(specs), len(self.specs))
        self.assertEqual(info.note(), "")

    def test_app_tools_are_narrowed_to_the_message_and_builtins_stay(self):
        limit = len(self.builtin) + 3
        with mock.patch.object(retrieval, "rerank", return_value=None):
            specs, info = asyncio.run(select_tools(self.specs, "what's the weather forecast for Dallas this weekend?", max_tools=limit, config=CONFIG, embed=fake_embed))
        names = [spec.name for spec in specs]
        self.assertTrue(all(spec.name in names for spec in self.builtin))
        apps = [spec.name for spec in specs if spec.connector]
        self.assertEqual(len(apps), 5)  # the floor: at least five app tools are offered
        self.assertIn("openweather_forecast", apps)
        self.assertIn("openweather_current", apps)
        self.assertTrue(info.narrowed)
        self.assertEqual(info.total, len(self.specs))
        self.assertIn("most relevant to the user's latest message", info.note())
        # The order follows the catalog, so the prompt is stable between turns.
        self.assertEqual(apps, [spec.name for spec in APPS if spec.name in apps])
        # The reranker, when it answers, decides the final pick.
        async def fake_rerank(query, documents, top_n, config=None):
            return [(i, 1.0) for i, d in enumerate(documents) if "Telegram" in d]
        with mock.patch.object(retrieval, "rerank", fake_rerank):
            specs, _ = asyncio.run(select_tools(self.specs, "ping me on telegram when done", max_tools=len(self.builtin) + 1, config=CONFIG, embed=fake_embed))
        self.assertIn("telegram_send_message", [s.name for s in specs if s.connector])

    def test_recently_used_tools_stay_and_failures_fall_back_to_everything(self):
        history = [{"raw_payload": {"tool_calls": [{"name": "googledrive_upload_file"}, {"name": "computer_screenshot"}]}}]
        with mock.patch.object(retrieval, "rerank", return_value=None):
            specs, info = asyncio.run(select_tools(self.specs, "now send the weather report", history=history, max_tools=len(self.builtin) + 2, config=CONFIG, embed=fake_embed))
        apps = [spec.name for spec in specs if spec.connector]
        self.assertIn("googledrive_upload_file", apps)  # in progress: kept whatever the message says
        self.assertEqual(tool_selection.recently_used(history), ["googledrive_upload_file", "computer_screenshot"])

        async def broken(texts, config=None):
            raise retrieval.RetrievalError("down")

        specs, info = asyncio.run(select_tools(self.specs, "weather", max_tools=len(self.builtin) + 2, config=CONFIG, embed=broken))
        self.assertEqual((len(specs), info.narrowed), (len(self.specs), False))
        # Vectors are embedded once and reused.
        tool_selection._tool_vectors.clear()
        calls = []

        async def counting(texts, config=None):
            calls.append(len(texts))
            return [toy_embedding(t) for t in texts]

        with mock.patch.object(retrieval, "rerank", return_value=None):
            asyncio.run(select_tools(self.specs, "weather", max_tools=len(self.builtin) + 2, config=CONFIG, embed=counting))
            asyncio.run(select_tools(self.specs, "weather again", max_tools=len(self.builtin) + 2, config=CONFIG, embed=counting))
        self.assertEqual(calls, [len(APPS), 1, 1])  # tools once, then only the two queries
        self.assertIn("Gmail", tool_text(APPS[0]))


if __name__ == "__main__":
    unittest.main()


class KeptSelectionTests(unittest.TestCase):
    """The selection holds from message to message (the prompt cache), and is made again after a compaction."""

    def setUp(self):
        tool_selection._tool_vectors.clear()
        self.builtin = available_tools(computer=False)
        self.specs = self.builtin + APPS
        self.limit = len(self.builtin) + 2

    def _run(self, query, kept, since="", mention=""):
        with mock.patch.object(retrieval, "rerank", return_value=None):
            return asyncio.run(tool_selection.select_tools_kept(
                self.specs, query, kept=kept, since=since, mention_text=mention, max_tools=self.limit, config=CONFIG, embed=fake_embed,
            ))

    def test_the_first_choice_holds_whatever_the_next_message_says(self):
        first, info, keep = self._run("what's the weather forecast for Dallas this weekend?", None)
        names = [s.name for s in first if s.connector]
        self.assertTrue(info.narrowed)
        self.assertEqual(keep["names"], names)
        second, _, keep2 = self._run("yes, go ahead", keep)
        self.assertEqual([s.name for s in second], [s.name for s in first])  # the same list, in the same order
        self.assertEqual(keep2, keep)

    def test_a_compaction_starts_a_new_choice(self):
        _, _, keep = self._run("what's the weather forecast for Dallas this weekend?", None, since="")
        after, _, keep2 = self._run("send the report by email to the team", keep, since="msg-compaction-1")
        self.assertEqual(keep2["since"], "msg-compaction-1")
        self.assertIn("gmail_send_email", [s.name for s in after])

    def test_an_app_named_by_the_user_or_the_bot_is_added(self):
        _, info, keep = self._run("what's the weather forecast for Dallas this weekend?", None)
        missing = info.dropped_apps[0]  # an app with none of its tools on offer
        name = next(s.connector["toolkit_name"] for s in APPS if s.connector["toolkit"] == missing)
        added, info2, keep2 = self._run("ok, do it", keep, mention=f"I need the {name} app for that.")
        self.assertTrue(any(s.connector and s.connector["toolkit"] == missing for s in added))
        self.assertTrue(set(keep["names"]) <= set(keep2["names"]))
        self.assertNotIn(missing, info2.dropped_apps)
        # Once offered, naming it again changes nothing.
        _, _, keep3 = self._run("again", keep2, mention=name)
        self.assertEqual(keep3, keep2)

    def test_nothing_is_kept_when_everything_fits(self):
        specs, info, keep = asyncio.run(tool_selection.select_tools_kept(self.specs, "hi", kept={"since": "", "limit": 500, "names": []}, since="", max_tools=500, config=CONFIG, embed=fake_embed))
        self.assertEqual((len(specs), keep), (len(self.specs), None))
        self.assertEqual(tool_selection.mentioned_toolkits("use google drive? no, googledrive", APPS), ["googledrive"])
