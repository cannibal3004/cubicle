"""Automatic memory: what a bot keeps from its conversations, replaced when facts change, and recalled."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import memories as memories_router
from app.services import auto_memory
from app.services import storage_service as storage_module
from app.services.llm_tools import available_tools, build_invocation, describe_tools
from app.services.storage_service import StorageService


def fake_model(reply, seen):
    async def stream(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
        seen.update(model=model, prompt=messages[-1]["content"], system=system_prompt, effort=getattr(config, "reasoning_effort", None))
        yield {"type": "content.delta", "delta": json.dumps(reply) if not isinstance(reply, str) else reply}
        yield {"type": "turn.completed", "ok": True}
    return stream


class AutoMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        # The model settings, read without the app's own storage (another test's may be gone by now).
        from app.services import llm_service

        self.config_patch = mock.patch.object(llm_service, "current_llm_config", lambda: llm_service.resolve_llm_config({}))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        bots = self.storage.get_bots()
        self.bot = bots[0]["id"]
        bots[0]["memory"] = "## The lab\n- NAS: nas.lan (TrueNAS)"
        self.storage.save_bots(bots)

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def _switch(self, value):
        bots = self.storage.get_bots()
        next(b for b in bots if b["id"] == self.bot)["auto_memory"] = value
        self.storage.save_bots(bots)

    def test_memories_are_kept_replaced_with_history_forgotten_and_edited(self):
        old = auto_memory.add_memory(self.bot, "mini.lan", "The Docker host mini.lan has the IP 192.168.1.40.")
        other = auto_memory.add_memory(self.bot, "backups", "Backups run nightly at 02:00 with restic.")
        new = auto_memory.replace_memory(self.bot, old["id"], "mini.lan", "mini.lan moved to 192.168.1.45 on 2026-10-30.")
        self.assertEqual([m["id"] for m in auto_memory.list_memories(self.bot)], [new["id"], other["id"]])
        history = {m["id"]: m for m in auto_memory.list_memories(self.bot, include_history=True)}
        self.assertEqual((history[old["id"]]["state"], history[old["id"]]["replaced_by"]), ("replaced", new["id"]))
        self.assertEqual(new["replaces"], old["id"])
        auto_memory.forget_memory(self.bot, other["id"], reason="wrong")
        self.assertEqual([m["id"] for m in auto_memory.list_memories(self.bot)], [new["id"]])
        edited = auto_memory.edit_memory(self.bot, new["id"], text="mini.lan is at 192.168.1.45.")
        self.assertTrue(edited["edited"])
        self.assertEqual(auto_memory.list_memories(self.bot, query="192.168.1.45")[0]["text"], "mini.lan is at 192.168.1.45.")
        auto_memory.delete_memory(self.bot, new["id"])
        self.assertNotIn(new["id"], [m["id"] for m in auto_memory.list_memories(self.bot, include_history=True)])
        with self.assertRaises(auto_memory.MemoryError):
            auto_memory.get_memory(self.bot, "mem-nope")

    def test_credentials_are_never_kept(self):
        for text in ("The iDRAC password is Winter2026!", "Login for node7: admin / Hunter2-Lab!", "The API key is sk-abc123def456",
                     "The token: ghp_abcdef123456"):
            self.assertTrue(auto_memory.looks_like_credential(text), text)
        for text in ("The iDRAC on esx01 is at 10.0.5.21.", "Passwords are kept in Vaultwarden.", "The API key is stored as the secret SHODAN_KEY."):
            self.assertFalse(auto_memory.looks_like_credential(text), text)

    def test_capture_reads_the_exchange_and_carries_out_what_the_model_decided(self):
        old = auto_memory.add_memory(self.bot, "mini.lan", "The Docker host mini.lan has the IP 192.168.1.40.")
        wrong = auto_memory.add_memory(self.bot, "proxy01", "proxy01 is the reverse proxy for the intranet.")
        seen = {}
        reply = {"memories": [
            {"op": "replace", "id": "1", "subject": "mini.lan", "text": "mini.lan moved to 192.168.1.45 on 2026-10-30."},
            {"op": "forget", "id": "2"},
            {"op": "add", "subject": "Dana", "text": "Dana from facilities handles badge access, extension 4412."},
            {"op": "add", "subject": "esx01", "text": "The iDRAC on esx01: login root / Winter2026!"},
            {"op": "replace", "id": "99", "subject": "x", "text": "An unknown target is added as new."},
        ]}
        # Off: nothing is read.
        self.assertIsNone(asyncio.run(auto_memory.capture(self.bot, self.bot, "I moved mini.lan to .45 yesterday", "Noted.", stream=fake_model(reply, seen))))
        self.assertEqual(seen, {})
        self._switch(True)
        with mock.patch.object(auto_memory, "nearest", lambda *a, **k: asyncio.sleep(0, result=[old, wrong])):
            outcome = asyncio.run(auto_memory.capture(self.bot, self.bot, "I moved mini.lan to 192.168.1.45 yesterday, and proxy01 is gone.",
                                                      "Noted both; Dana handles badges.", message_id="msg-1", stream=fake_model(reply, seen)))
        self.assertEqual(outcome, {"added": 2, "replaced": 1, "forgotten": 1, "skipped": 1})
        self.assertEqual(seen["effort"], "none")  # thinking off
        self.assertIn("nas.lan (TrueNAS)", seen["prompt"])  # the notes, so they are not kept twice
        self.assertIn("1. [mini.lan] The Docker host mini.lan has the IP 192.168.1.40.", seen["prompt"])
        self.assertIn("Never keep passwords", seen["system"])
        current = {m["subject"]: m for m in auto_memory.list_memories(self.bot)}
        self.assertEqual(sorted(current), ["Dana", "mini.lan", "x"])
        self.assertEqual(current["mini.lan"]["source"], {"thread_id": self.bot, "message_id": "msg-1"})
        self.assertNotIn("Winter2026", json.dumps(auto_memory.list_memories(self.bot, include_history=True)))
        # Small talk is not worth a model call.
        self.assertEqual(asyncio.run(auto_memory.capture(self.bot, self.bot, "thanks", "You're welcome!", stream=fake_model(reply, {})))["added"], 0)

    def test_recall_matches_words_without_the_search_index(self):
        auto_memory.add_memory(self.bot, "backups", "Backups go to the B2 bucket alex-lab-backups.")
        auto_memory.add_memory(self.bot, "Dana", "Dana handles badge access.")
        block = asyncio.run(auto_memory.recall_context(self.bot, "where do the backups go?"))
        self.assertIn("Things you remember", block)
        self.assertIn("- [backups] Backups go to the B2 bucket alex-lab-backups.", block)
        self.assertNotIn("Dana", block)
        self.assertEqual(asyncio.run(auto_memory.recall_context(self.bot, "what time is it")), "")

    def test_the_setting_the_default_and_the_tool(self):
        self.assertFalse(auto_memory.enabled_for(self.bot))
        self.storage.save_settings({"auto_memory_default": True})
        self.assertTrue(auto_memory.enabled_for(self.bot))
        self._switch(False)
        self.assertFalse(auto_memory.enabled_for(self.bot))  # the bot's own choice wins
        self.assertNotIn("memory_search", [s.name for s in available_tools(computer=False)])
        specs = available_tools(computer=False, auto_memory=True)
        self.assertIn("memory_search", [s.name for s in specs])
        self.assertIn("Automatic memory", describe_tools(specs))
        call = build_invocation("memory_search", {"query": "backups"}, self.bot)
        self.assertEqual((call.name, call.arguments["query"]), ("memory.search", "backups"))
        auto_memory.add_memory(self.bot, "backups", "Backups go to the B2 bucket alex-lab-backups.")
        found = asyncio.run(auto_memory.execute_search(call))
        self.assertEqual(found["memories"][0]["subject"], "backups")

    def test_only_the_owner_sees_and_changes_them(self):
        memory = auto_memory.add_memory(self.bot, "backups", "Backups go to B2.")
        listed = asyncio.run(memories_router.list_memories(self.bot, q="", history=False))
        self.assertEqual((listed["current"], listed["enabled"]), (1, False))
        asyncio.run(memories_router.edit_memory(self.bot, memory["id"], memories_router.MemoryEdit(text="Backups go to the B2 bucket.")))
        self.assertEqual(auto_memory.get_memory(self.bot, memory["id"])["text"], "Backups go to the B2 bucket.")
        with mock.patch.object(memories_router, "acting_for_owner", lambda: False):
            with self.assertRaises(HTTPException):
                asyncio.run(memories_router.list_memories(self.bot, q="", history=False))
        asyncio.run(memories_router.delete_memory(self.bot, memory["id"]))
        self.assertEqual(auto_memory.list_memories(self.bot), [])


if __name__ == "__main__":
    unittest.main()
