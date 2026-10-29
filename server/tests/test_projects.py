"""Projects: work notes in two tiers with the bot's notes, the log, conversations joining and leaving."""

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from app.routers import chat as chat_router
from app.routers import conversations as conversations_router
from app.routers import projects as projects_router
from app.services import conversations, projects
from app.services import storage_service as storage_module
from app.services.context_builder import build_history_messages, summary_messages
from app.services.llm_tools import build_invocation

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream


def _run(name, arguments, bot_id, thread_id):
    """A project tool call as the gateway would run it."""
    invocation = build_invocation(name, arguments, bot_id, thread_id=thread_id)
    executor = dict((action, run) for action, _, _, _, _, run in projects.ACTIONS)[invocation.name]
    return asyncio.run(executor(invocation))


class ProjectTests(chat_harness.ChatStreamRouteTests):
    def setUp(self):
        super().setUp()
        self.bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(self.bot)
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def _say(self, thread_id, text="hello"):
        self.storage.add_message({"id": f"m-{thread_id}-{len(self.storage.get_messages(thread_id))}", "thread_id": thread_id, "bot_id": self.bot, "sender": "user", "text": text})

    def test_work_notes_belong_to_the_project_or_to_the_conversation(self):
        self._say(self.bot)
        # In no project, the work notes are the conversation's own, and there is no log.
        _run("work_notes_write", {"text": "Waiting on the vendor."}, self.bot, self.bot)
        self.assertEqual(conversations.get(self.bot)["work_notes"], "Waiting on the vendor.")
        with self.assertRaises(projects.ProjectError):
            _run("project_log", {"entry": "x"}, self.bot, self.bot)

        created = _run("project_create", {"name": "Proxy decommission", "work_notes": "Phase 1: inventory."}, self.bot, self.bot)
        self.assertEqual(created["created"], "Proxy decommission")
        with self.assertRaises(projects.ProjectError):  # names are unique
            _run("project_create", {"name": "proxy  DECOMMISSION"}, self.bot, self.bot)
        joined = _run("project_join", {"name": "proxy decommission"}, self.bot, self.bot)
        self.assertEqual(joined["work_notes"], "Phase 1: inventory.")
        rows = [m for m in self.storage.get_messages(self.bot) if m.get("item_type") == "work_notes"]
        self.assertEqual(rows[-1]["raw_payload"]["by"], "joined")
        self.assertIn("This conversation is now part of project Proxy decommission", str(build_history_messages(self.storage.get_messages(self.bot), replay_tools=True)))

        _run("work_notes_edit", {"old_text": "Phase 1: inventory.", "new_text": "Phase 2: cut over."}, self.bot, self.bot)
        self.assertEqual(_run("work_notes_read", {}, self.bot, self.bot)["work_notes"], "Phase 2: cut over.")
        self.assertEqual(conversations.get(self.bot)["work_notes"], "Waiting on the vendor.")  # its own, kept for later
        listed = _run("projects_list", {}, self.bot, self.bot)
        self.assertEqual(listed["this_conversation_is_in"], "Proxy decommission")
        self.assertEqual(listed["projects"][0]["status"], "Phase 2: cut over.")

        left = _run("project_join", {"name": ""}, self.bot, self.bot)
        self.assertEqual(left["work_notes"], "Waiting on the vendor.")
        self.assertEqual([m for m in self.storage.get_messages(self.bot) if m.get("item_type") == "work_notes"][-1]["raw_payload"]["by"], "left")

    def test_a_change_is_announced_once_in_the_projects_other_conversations(self):
        project = projects.create(self.bot, "Nextcloud", "Version 29.")
        first = conversations.create(self.bot, project_id=project["id"])
        second = conversations.create(self.bot, project_id=project["id"])
        outside = conversations.create(self.bot)
        for thread in (first["id"], second["id"], outside["id"]):
            self._say(thread)
        # A conversation started in a project begins with its work notes.
        start = [m for m in self.storage.get_messages(first["id"]) if m.get("item_type") == "work_notes"][0]
        self.assertEqual((start["raw_payload"]["by"], start["text"]), ("start", "Version 29."))
        self.assertIn("This conversation is part of project Nextcloud", str(build_history_messages(self.storage.get_messages(first["id"]), replay_tools=True)))

        def told(thread):
            return [m for m in self.storage.get_messages(thread) if (m.get("raw_payload") or {}).get("by") == "elsewhere" and m.get("item_type") == "work_notes"]

        _run("work_notes_write", {"text": "Version 30 planned."}, self.bot, first["id"])
        _run("work_notes_write", {"text": "Version 30 on Friday."}, self.bot, first["id"])
        self.assertEqual((len(told(second["id"])), told(first["id"]), told(outside["id"])), (1, [], []))
        _run("work_notes_read", {}, self.bot, second["id"])
        _run("work_notes_write", {"text": "Version 30 done."}, self.bot, first["id"])
        self.assertEqual(len(told(second["id"])), 2)
        # A tool call can name another project.
        _run("work_notes_write", {"text": "Not started.", "project": "Nextcloud"}, self.bot, outside["id"])
        self.assertEqual(projects.get(project["id"])["notes"], "Not started.")
        self.assertEqual(conversations.get(outside["id"]).get("work_notes", ""), "")

    def test_the_log_is_kept_out_of_the_context_and_found_again(self):
        project = projects.create(self.bot, "Backups")
        thread = conversations.create(self.bot, project_id=project["id"])["id"]
        for entry in ("Moved the NAS backup to 02:00.", "Pruned snapshots older than 30 days.", "Restored a test file from Tuesday."):
            _run("project_log", {"entry": entry}, self.bot, thread)
        read = _run("project_log_read", {"limit": 2}, self.bot, thread)
        self.assertEqual(read["entries"], 3)
        self.assertIn("Restored a test file", read["recent"])
        self.assertNotIn("Moved the NAS backup", read["recent"])
        self.assertEqual([e["text"] for e in asyncio.run(projects_router.project_log(project["id"]))["entries"]][0], "Restored a test file from Tuesday.")
        with self.assertRaises(projects.ProjectError):
            _run("project_log", {"entry": "x" * 5000}, self.bot, thread)

        # A message that matches the log gets the matching entries with it, once.
        self._say(thread, "when do the backups run?")

        async def matching(project_id, query, storage=None):
            return "Entries from the project log that match this message:\n--- from the log of Backups, 2026-10-27 (part 1) ---\nMoved the NAS backup to 02:00."

        recorder = {}
        stream = _scripted_stream(recorder, [[{"type": "content.delta", "delta": "At 02:00."}, {"type": "turn.completed", "ok": True}]])
        with mock.patch.object(chat_router.projects, "log_context", matching):
            self._run_turn(thread, stream=stream)
        sent = recorder["rounds"][0]["messages"][-1]["content"]
        self.assertIn("Moved the NAS backup to 02:00.", sent)
        stored = [m for m in self.storage.get_messages(thread) if m.get("sender") == "user"][-1]
        self.assertEqual(stored["raw_payload"]["turn_context"]["log_entries"], 1)

    def test_compaction_summaries_carry_the_work_notes(self):
        rendered = summary_messages({"text": "Did things.", "raw_payload": {"work_notes": "Cut over on Friday.", "project": "Proxy"}})
        self.assertIn("Your work notes at this point (project Proxy):", rendered[0]["content"])
        self.assertIn("Cut over on Friday.", rendered[0]["content"])

    def test_the_api_moves_conversations_edits_notes_and_deletes_projects(self):
        made = asyncio.run(projects_router.create_project(self.bot, projects_router.ProjectInput(name="Printers", notes="Two left.")))
        thread = asyncio.run(conversations_router.start_conversation(self.bot, conversations_router.ConversationInput(project_id=made["id"])))
        self.assertEqual(thread["project_id"], made["id"])
        self._say(thread["id"])
        asyncio.run(projects_router.update_project(made["id"], projects_router.ProjectUpdate(notes="One left.")))
        last = self.storage.get_messages(thread["id"])[-1]
        self.assertEqual((last["item_type"], last["raw_payload"]["by"], last["text"]), ("work_notes", "user", "One left."))

        other = conversations.create(self.bot)
        self._say(other["id"])
        moved = asyncio.run(conversations_router.update_conversation(other["id"], conversations_router.ConversationUpdate(project_id=made["id"])))
        self.assertEqual(moved["project_id"], made["id"])
        self.assertEqual(sorted(asyncio.run(projects_router.get_project(made["id"]))["conversations"]), sorted([thread["id"], other["id"]]))
        own = asyncio.run(conversations_router.update_conversation(self.bot, conversations_router.ConversationUpdate(work_notes="My own status.")))
        self.assertEqual(own["work_notes"], "My own status.")
        with self.assertRaises(HTTPException):
            asyncio.run(projects_router.create_project(self.bot, projects_router.ProjectInput(name="printers")))

        result = asyncio.run(projects_router.delete_project(made["id"]))
        self.assertEqual(result["conversations_moved_out"], 2)
        self.assertIsNone(conversations.get(other["id"]).get("project_id"))
        self.assertEqual(self.storage.get_messages(other["id"])[-1]["raw_payload"]["by"], "left")
        with self.assertRaises(HTTPException):
            asyncio.run(projects_router.get_project(made["id"]))

    def test_each_project_has_a_folder_that_keeps_its_name(self):
        from pathlib import Path

        from app.services import file_browser

        made = projects.create(self.bot, "Proxy decommission!")
        self.assertEqual(made["folder"], "proxy-decommission")
        self.assertEqual(projects.create(self.bot, "Proxy / Decommission")["folder"], "proxy-decommission-2")
        projects.update(made["id"], {"name": "Proxy retirement"})
        self.assertEqual(asyncio.run(projects_router.get_project(made["id"]))["folder"], "projects/proxy-decommission")
        # A project from before folders gets one when first asked.
        legacy = {**projects.create(self.bot, "Printers"), "folder": None}
        self.storage.save_project(legacy)
        self.assertEqual(projects.workspace_path(projects.get(legacy["id"])), "/workspace/projects/printers")
        self.assertEqual(projects.get(legacy["id"])["folder"], "printers")
        # The bot is told where it is.
        self._say(self.bot)
        self.assertEqual(_run("project_join", {"name": "Proxy retirement"}, self.bot, self.bot)["folder"], "/workspace/projects/proxy-decommission")
        # Opening it from the project page makes it, parents and all, and again is fine.
        root = Path(self.temp_dir.name) / "computer"
        root.mkdir()
        with mock.patch.object(file_browser, "source_root", lambda source, bot_id: root):
            self.assertEqual(asyncio.run(projects_router.open_project_folder(made["id"]))["path"], "projects/proxy-decommission")
            asyncio.run(projects_router.open_project_folder(made["id"]))
        self.assertTrue((root / "projects" / "proxy-decommission").is_dir())

    def test_what_a_person_wants_known_is_shown_to_their_bots(self):
        user = {"id": "u", "username": "jamie", "display_name": "Jamie", "email": "", "about": "I prefer short answers."}
        with mock.patch("app.services.auth_service.auth_service.accounts.get_user", return_value=user):
            described = chat_router.describe_user()
        self.assertIn("What they want you to know about them:\nI prefer short answers.", described)


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in ProjectTests.__dict__:
        setattr(ProjectTests, _name, None)


if __name__ == "__main__":
    unittest.main()
