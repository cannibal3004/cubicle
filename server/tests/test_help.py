"""Cubicle's help: the docs pages served to the Help view, and searched by the cubicle_help tool."""

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import help as help_router
from app.services import conversations, help, projects
from app.services.llm_tools import available_tools, build_invocation, describe_tools
from app.services.retrieval import RetrievalConfig

import test_chat_router as chat_harness

WORDS_ONLY = RetrievalConfig(enabled=False, embedding_model="", rerank_model="", base_url="")


class HelpPagesTests(unittest.TestCase):
    def test_the_index_lists_the_pages_in_groups_and_pages_stay_inside_docs(self):
        groups = help.index()
        self.assertEqual([g["group"] for g in groups][:2], ["Using Cubicle", "Your bots"])
        paths = [p["path"] for g in groups for p in g["pages"]]
        self.assertIn("approvals.md", paths)
        self.assertIn("admin/install.md", paths)
        # Every page the index names exists and has a title.
        for path in paths:
            self.assertTrue(help.page(path)["title"])
        self.assertEqual(help.page("approvals")["title"], "Tools and approvals")
        for outside in ("../server/app/config.py", "../../README.md", "/etc/passwd"):
            with self.assertRaises(help.HelpError):
                help.page(outside)
        with self.assertRaises(HTTPException):
            asyncio.run(help_router.help_page("../server/run.py"))
        self.assertTrue(help.image("overview.png").is_file())
        with self.assertRaises(help.HelpError):
            help.image("../README.md")

    def test_search_finds_the_section_that_answers(self):
        found = asyncio.run(help.search("how do I let a bot run a command without asking", 3, config=WORDS_ONLY))
        self.assertEqual((found[0].page, found[0].heading), ("approvals.md", "Letting things run without asking"))
        self.assertEqual(found[0].link, "/help/approvals#letting-things-run-without-asking")
        self.assertEqual(asyncio.run(help.search("", config=WORDS_ONLY)), [])

    def test_search_by_meaning_when_the_server_embeds(self):
        def embed_for(target):
            async def embed(texts, config=None):
                # The section about schedules points the same way as the question; everything else does not.
                return [[1.0, 0.0] if ("Schedules" in t and "Making one" in t) or t.startswith("remind") else [0.0, 1.0] for t in texts]
            return embed

        config = RetrievalConfig(enabled=True, embedding_model="e", rerank_model="", base_url="http://model.test/v1")
        with mock.patch.object(help.retrieval, "rerank", return_value=None):
            found = asyncio.run(help.search("remind me every morning", 1, config=config, embed=embed_for("schedules")))
        self.assertEqual((found[0].page, found[0].heading), ("schedules.md", "Making one"))

    def test_images_become_urls_the_chat_can_show(self):
        self.assertEqual(help.with_image_urls("![A card](images/approval-card.png) and ![x](../images/overview.png)"),
                         "![A card](/api/v1/help/images/approval-card.png) and ![x](/api/v1/help/images/overview.png)")


class HelpToolTests(chat_harness.ChatStreamRouteTests):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.object(chat_harness.storage_module, "storage_service", self.storage)
        self.patch.start()
        self.bot = self.storage.get_bots()[0]["id"]

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_every_bot_can_look_things_up_and_hears_how_it_is_set_up(self):
        specs = available_tools(computer=False)
        self.assertIn("cubicle_help", [s.name for s in specs])
        self.assertIn("cubicle_help looks it up", describe_tools(specs))

        asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(trusted_tools=["todo_add"], allowed_commands=["df -h"], auto_approval="shadow")))
        project = projects.create(self.bot, "Backups")
        conversations.update(self.bot, {"project_id": project["id"]})
        invocation = build_invocation("cubicle_help", {"question": "how do I let a command run without asking"}, self.bot, thread_id=self.bot)
        self.assertEqual((invocation.name, invocation.arguments["thread_id"]), ("help.search", self.bot))
        with mock.patch.object(help.retrieval, "current_config", return_value=WORDS_ONLY):
            result = asyncio.run(help.execute_search(invocation))
        self.assertEqual(result["sections"][0]["link"], "/help/approvals#letting-things-run-without-asking")
        self.assertIn("how_to_answer", result)
        this_bot = result["this_bot"]
        self.assertEqual((this_bot["trusted_tools"], this_bot["always_allowed_commands"]), (["todo_add"], ["df -h"]))
        self.assertTrue(this_bot["auto_approval"].startswith("shadow (this bot)"))
        self.assertEqual(this_bot["this_conversation_project"], "Backups")

        with mock.patch.object(help.retrieval, "current_config", return_value=WORDS_ONLY):
            pictured = asyncio.run(help.execute_search(build_invocation("cubicle_help", {"question": "approval card"}, self.bot)))
        self.assertTrue(any("/api/v1/help/images/" in s["text"] for s in pictured["sections"]))


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in HelpToolTests.__dict__:
        setattr(HelpToolTests, _name, None)


if __name__ == "__main__":
    unittest.main()
