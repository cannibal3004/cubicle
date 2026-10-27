"""Demonstrations: a recording becomes steps the model reads, frames it sees once, and a procedure the bot keeps."""

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import chat as chat_router
from app.routers import computers as computers_router
from app.schemas.contracts import TurnRequest
from app.services import demonstrations, procedures, retrieval
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.computer_provider import FakeComputerProvider
from app.services.context_builder import build_history_messages
from app.services.llm_config import LLMConfig
from app.services.llm_tools import available_tools, build_invocation, describe_tools
from app.services.retrieval import RetrievalConfig, toy_embedding
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services.workspace_service import WorkspaceService

from test_chat_router import NoComposio, _fake_context_window, _fake_stream, _start_and_stream

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1", top_k=6, chunk_chars=2000)


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


def frame_png(width=400, height=300):
    from PIL import Image

    image = Image.new("RGB", (width, height), (40, 40, 40))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def a_recording():
    return {
        "started_at": "2026-10-21T10:00:00",
        "duration_ms": 42000,
        "width": 400,
        "height": 300,
        "steps": [
            {"n": 1, "at_ms": 0, "type": "start", "url": "https://portal.example/login"},
            {"n": 2, "at_ms": 1200, "type": "click", "url": "https://portal.example/login", "target": {"role": "text field", "name": "Email", "selector": "#email"}, "x": 100, "y": 80, "screen_x": 110, "screen_y": 150, "button": "left"},
            {"n": 3, "at_ms": 2500, "type": "type", "url": "https://portal.example/login", "target": {"role": "text field", "name": "Email"}, "value": "jamie@example.com"},
            {"n": 4, "at_ms": 4000, "type": "type", "url": "https://portal.example/login", "target": {"role": "password field", "name": "Password", "secret": True}, "value": ""},
            {"n": 5, "at_ms": 5000, "type": "key", "url": "https://portal.example/login", "target": {"role": "password field", "name": "Password", "secret": True}, "key": "Enter"},
            {"n": 6, "at_ms": 6500, "type": "navigate", "url": "https://portal.example/home"},
            {"n": 7, "at_ms": 8000, "type": "select", "url": "https://portal.example/home", "target": {"role": "dropdown", "name": "Request type"}, "value": "Time off"},
            {"n": 8, "at_ms": 9000, "type": "check", "url": "https://portal.example/home", "target": {"role": "checkbox", "name": "Half day"}, "checked": True},
            {"n": 9, "at_ms": 9500, "type": "click", "url": "https://portal.example/home", "target": {"role": "button", "name": "Submit"}, "x": 10, "y": 10, "screen_x": 20, "screen_y": 90, "button": "left"},
        ],
        "keyframes": [
            {"step": 2, "data": frame_png(), "width": 400, "height": 300},
            {"step": 5, "data": frame_png(), "width": 400, "height": 300},
            {"step": 9, "data": frame_png(), "width": 400, "height": 300},
            {"step": None, "data": frame_png(), "width": 400, "height": 300},
        ],
        "dropped_steps": 0,
    }


class StepTextTests(unittest.TestCase):
    def test_steps_read_as_what_the_user_did_and_passwords_stay_out(self):
        text = demonstrations.render_steps(a_recording())
        lines = text.split("\n")
        self.assertEqual(lines[0], "1. Started on https://portal.example/login.")
        self.assertEqual(lines[1], '2. Clicked the text field "Email".')
        self.assertEqual(lines[2], '3. Typed "jamie@example.com" into the text field "Email".')
        self.assertEqual(lines[3], '4. Typed a password into the password field "Password" (not recorded).')
        self.assertEqual(lines[4], '5. Pressed Enter in the password field "Password".')
        self.assertEqual(lines[5], "6. The page became https://portal.example/home.")
        self.assertEqual(lines[6], '7. Chose "Time off" in the dropdown "Request type".')
        self.assertEqual(lines[7], '8. Checked the checkbox "Half day".')
        self.assertEqual(lines[8], '9. Clicked the button "Submit".')
        self.assertNotIn("hunter2", text)

    def test_the_page_is_named_when_it_changes_between_steps(self):
        steps = [
            {"n": 1, "type": "click", "url": "https://a.example/", "target": {"role": "link", "name": "Reports"}},
            {"n": 2, "type": "click", "url": "https://a.example/reports", "target": {"role": "button", "name": "Export"}},
            {"n": 3, "type": "click", "url": "https://a.example/reports", "target": {"role": "button", "name": "CSV"}},
        ]
        text = demonstrations.render_steps({"steps": steps})
        self.assertIn('1. Clicked the link "Reports" on https://a.example/.', text)
        self.assertIn('2. Clicked the button "Export" on https://a.example/reports.', text)
        self.assertIn('3. Clicked the button "CSV".', text)


class SavedRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(demonstrations.settings, "DATA_DIR", Path(self.temp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_a_recording_is_saved_with_marked_scaled_frames_and_found_only_for_its_bot(self):
        record = demonstrations.save_recording("bot-a", a_recording(), max_width=200)
        self.assertTrue(record["id"].startswith("rec-"))
        self.assertEqual(len(record["steps"]), 9)
        self.assertEqual([f["step"] for f in record["keyframes"]], [2, 5, 9, None])
        self.assertEqual((record["keyframes"][0]["width"], record["keyframes"][0]["height"]), (200, 150))
        folder = demonstrations.recordings_dir("bot-a") / record["id"]
        self.assertTrue((folder / "recording.json").exists())
        self.assertEqual(sorted(p.name for p in folder.glob("*.jpg")), ["frame-001.jpg", "frame-002.jpg", "frame-003.jpg", "frame-004.jpg"])
        # The click point is circled in red on the step's frame (scaled by half: 110,150 -> 55,75; ring radius 22 -> 11).
        from PIL import Image

        image = Image.open(folder / "frame-001.jpg").convert("RGB")
        r, g, b = image.getpixel((55, 75 - 11))
        self.assertGreater(r, 150)
        self.assertLess(g, 100)
        self.assertEqual(demonstrations.load_recording("bot-a", record["id"])["id"], record["id"])
        self.assertIsNone(demonstrations.load_recording("bot-b", record["id"]))
        self.assertIsNone(demonstrations.load_recording("bot-a", "../../etc/passwd"))
        summary = demonstrations.summary(record)
        self.assertEqual((summary["steps"], summary["keyframes"]), (9, 4))
        self.assertIn('Clicked the button "Submit"', summary["text"])

    def test_the_model_gets_a_bounded_set_of_frames_with_the_final_screen(self):
        payload = a_recording()
        payload["steps"] = [{"n": n, "type": "click", "url": "https://x.example/", "target": {"role": "button", "name": f"B{n}"}, "screen_x": 5, "screen_y": 5} for n in range(1, 31)]
        payload["keyframes"] = [{"step": n, "data": frame_png(20, 10)} for n in range(1, 31)] + [{"step": None, "data": frame_png(20, 10)}]
        record = demonstrations.save_recording("bot-a", payload, max_width=0)
        message = demonstrations.keyframe_message("bot-a", {"id": record["id"]}, limit=6)
        images = [p for p in message["content"] if p["type"] == "image_url"]
        captions = [p["text"] for p in message["content"] if p["type"] == "text"]
        self.assertEqual(len(images), 6)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("Step 1:", captions[1])
        self.assertIn("The screen when the user finished.", captions[-1])
        self.assertIn("6 of 31 frames", captions[0])
        self.assertIsNone(demonstrations.keyframe_message("bot-a", {"id": "rec-00000000"}))

    def test_the_steps_follow_the_users_words_in_the_history(self):
        record = demonstrations.save_recording("bot-a", a_recording())
        history = [{
            "id": "m1", "sender": "user", "text": "This is how I file time off.", "created_at": "2026-10-21T10:01:00",
            "raw_payload": {"demonstration": demonstrations.summary(record)},
        }]
        messages = build_history_messages(history, replay_tools=True)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["content"].startswith("This is how I file time off.\n\n[While recording, the user showed this"))
        self.assertIn('9. Clicked the button "Submit".', messages[0]["content"])
        self.assertEqual(demonstrations.pending_demonstration(history)["id"], record["id"])
        self.assertIsNone(demonstrations.pending_demonstration(history + [{"sender": "bot", "text": "ok"}]))


class ProcedureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        self.bot = self.storage.get_bots()[0]

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_save_replaces_by_title_read_finds_by_part_and_the_prompt_lists_titles(self):
        saved = procedures.save_procedure(self.bot["id"], "File a PTO request in Workday", "1. Open Workday.\n2. Click Time off.")
        again = procedures.save_procedure(self.bot["id"], "file a pto request in workday", "1. Open Workday.\n2. Click Absence.\n3. Request.")
        self.assertEqual(saved["id"], again["id"])
        procedures.save_procedure(self.bot["id"], "Export the sales report", "1. Reports.\n2. Export CSV.")
        listed = procedures.list_procedures(self.bot["id"])
        self.assertEqual([p["title"] for p in listed], ["File a PTO request in Workday", "Export the sales report"])
        self.assertEqual(procedures.find_procedure(self.bot["id"], "pto request")["steps"], "1. Open Workday.\n2. Click Absence.\n3. Request.")
        self.assertIsNone(procedures.find_procedure(self.bot["id"], "report the"))  # no such substring
        block = procedures.prompt_block(next(b for b in self.storage.get_bots() if b["id"] == self.bot["id"]))
        self.assertIn('"File a PTO request in Workday"; "Export the sales report"', block)
        self.assertIn("procedure_read", block)
        self.assertEqual(procedures.prompt_block({"procedures": []}), "")
        self.assertTrue(procedures.delete_procedure(self.bot["id"], saved["id"]))
        self.assertFalse(procedures.delete_procedure(self.bot["id"], saved["id"]))
        with self.assertRaises(procedures.ProcedureError):
            procedures.save_procedure(self.bot["id"], "  ", "steps")
        with self.assertRaises(procedures.ProcedureError):
            procedures.save_procedure(self.bot["id"], "Too long", "x" * (procedures.STEPS_MAX + 1))

    def test_the_tools_and_actions_round_trip(self):
        specs = available_tools(computer=True)
        names = [s.name for s in specs]
        self.assertIn("procedure_save", names)
        self.assertIn("procedure_read", names)
        self.assertNotIn("procedure_save", [s.name for s in available_tools(computer=False)])
        self.assertIn("Procedures: the user can show you", describe_tools(specs))
        call = build_invocation("procedure_save", {"title": "Export the sales report", "steps": "1. Reports.\n2. Export."}, self.bot["id"])
        self.assertEqual(call.name, "procedure.save")
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp.name) / "ws"), approvals=None, audit=self.storage)
        procedures.register_procedure_actions(gateway)
        result = asyncio.run(gateway.executors["procedure.save"](call)) if hasattr(gateway, "executors") else asyncio.run(procedures.execute_save(call))
        self.assertEqual((result["saved"], result["title"], result["count"]), (True, "Export the sales report", 1))
        read = asyncio.run(procedures.execute_read(build_invocation("procedure_read", {"title": "sales"}, self.bot["id"])))
        self.assertEqual(read["steps"], "1. Reports.\n2. Export.")
        with self.assertRaisesRegex(procedures.ProcedureError, "No procedure called"):
            asyncio.run(procedures.execute_read(build_invocation("procedure_read", {"title": "nothing"}, self.bot["id"])))

    def test_the_editor_can_replace_the_list_and_the_index_follows(self):
        saved = procedures.save_procedure(self.bot["id"], "Export the sales report", "1. Reports.\n2. Export CSV.")
        with mock.patch.object(procedures, "schedule_unindex") as unindex, mock.patch.object(procedures, "schedule_index") as index:
            updated = asyncio.run(bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(procedures=[])))
            self.assertEqual(updated["procedures"] if isinstance(updated, dict) else updated.procedures, [])
            unindex.assert_called_once_with(self.bot["id"], saved["id"])
            index.assert_not_called()
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(procedures=[{"title": "", "steps": "x"}])))

    def test_matching_procedures_are_retrieved_in_full(self):
        saved = procedures.save_procedure(self.bot["id"], "File a PTO request in Workday", "1. Open Workday.\n2. Click Absence.\n3. Request time off and submit.")
        other = procedures.save_procedure(self.bot["id"], "Export the sales report", "1. Open Reports.\n2. Export the sales figures as CSV.")
        for procedure in (saved, other):
            asyncio.run(procedures.index_procedure(self.bot["id"], procedure, config=CONFIG, storage=self.storage, embed=fake_embed))
        with mock.patch.object(retrieval, "rerank", return_value=None):
            block = asyncio.run(procedures.procedure_context(self.bot["id"], "please request time off in workday for friday", limit=1, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertIn("--- File a PTO request in Workday ---", block)
        self.assertIn("3. Request time off and submit.", block)
        self.assertNotIn("sales", block)
        self.assertEqual(asyncio.run(procedures.procedure_context("bot-none", "anything", config=CONFIG, storage=self.storage, embed=fake_embed)), "")


class RecordingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.provider = FakeComputerProvider()
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(computers_router, "computer_provider", self.provider),
            mock.patch.object(demonstrations.settings, "DATA_DIR", Path(self.temp.name) / "data"),
        ]
        for p in self.patches:
            p.start()
        self.bot = self.storage.get_bots()[0]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_start_hands_the_desktop_to_the_user_and_stop_saves_the_recording(self):
        status = self.provider.get_or_create(self.bot["id"])
        asyncio.run(self.provider.start(status.computer_id))
        started = asyncio.run(computers_router.start_recording(self.bot["id"]))
        self.assertEqual(started["status"], "recording")
        self.assertEqual(started["computer"]["controlled_by"], "user")
        self.assertTrue(asyncio.run(computers_router.recording_status(self.bot["id"]))["active"])
        self.provider.recordings[status.computer_id]["steps"].append({"n": 1, "type": "click", "url": "https://x.example/", "target": {"role": "button", "name": "Go"}})
        stopped = asyncio.run(computers_router.stop_recording(self.bot["id"]))
        self.assertEqual(stopped["status"], "saved")
        self.assertEqual(stopped["recording"]["steps"], 1)
        self.assertIn('Clicked the button "Go"', stopped["recording"]["text"])
        self.assertFalse(asyncio.run(computers_router.recording_status(self.bot["id"]))["active"])
        with self.assertRaises(HTTPException):
            asyncio.run(computers_router.stop_recording(self.bot["id"]))
        with self.assertRaises(HTTPException):
            asyncio.run(computers_router.start_recording("bot-nobody"))
        # The recording is the bot's own; another bot's send cannot pick it up.
        self.assertIsNone(demonstrations.load_recording("bot-other", stopped["recording"]["id"]))


class DemonstrationTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://localhost:11434/v1", reasoning_effort="", default_model="default-model")
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(demonstrations.settings, "DATA_DIR", Path(self.temp.name) / "data"),
        ]
        for p in self.patches:
            p.start()
        self.bot = self.storage.get_bots()[0]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_the_hand_back_message_carries_the_steps_and_the_turn_gets_the_frames_once(self):
        record = demonstrations.save_recording(self.bot["id"], a_recording(), max_width=0)
        sent = asyncio.run(chat_router.send_message(TurnRequest(thread_id=self.bot["id"], bot_id=self.bot["id"], user_text="", recording_id=record["id"])))
        self.assertEqual(sent["message"]["text"], "I showed you how to do something on your computer.")
        self.assertEqual(sent["message"]["raw_payload"]["demonstration"]["steps"], 9)
        with self.assertRaises(HTTPException):
            asyncio.run(chat_router.send_message(TurnRequest(thread_id=self.bot["id"], bot_id=self.bot["id"], user_text="x", recording_id="rec-ffffffff")))

        recorder = {}
        patches = [
            mock.patch.object(chat_router.llm_service, "current_llm_config", return_value=self.config),
            mock.patch.object(chat_router.llm_service, "stream_chat_completion", _fake_stream(recorder)),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", TurnManager()),
            mock.patch.object(chat_router, "_context_window_for", _fake_context_window),
            mock.patch.object(chat_router.conversation_titles, "schedule", lambda thread_id: None),
            mock.patch.object(chat_router.settings, "COMPUTER_PROVIDER", "docker"),  # the computer tools, and with them the procedures
        ]
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(self.bot["id"]))
        finally:
            for p in reversed(patches):
                p.stop()
        kinds = [e.get("type") for e in events]
        self.assertIn("demonstration.frames", kinds)
        frames = next(e for e in events if e.get("type") == "demonstration.frames")
        self.assertEqual(frames["count"], 4)
        sent_messages = recorder["messages"][-2:]  # after the greeting: the user's message, then the frames
        # The user's message (with the steps) then one message carrying the frames, before the model's rounds.
        self.assertIn('9. Clicked the button "Submit".', sent_messages[0]["content"])
        self.assertIsInstance(sent_messages[1]["content"], list)
        self.assertEqual(sum(1 for p in sent_messages[1]["content"] if p["type"] == "image_url"), 4)
        self.assertIn("procedure_save", recorder["system_prompt"])

        # A later turn sees the steps in the history but is not sent the frames again.
        self.storage.add_message({"id": "m-follow", "thread_id": self.bot["id"], "bot_id": self.bot["id"], "sender": "user", "text": "Thanks, now do it for next Friday.", "created_at": "2026-10-21T11:00:00", "item_type": "user_text"})
        recorder2 = {}
        patches[1] = mock.patch.object(chat_router.llm_service, "stream_chat_completion", _fake_stream(recorder2))
        patches[3] = mock.patch.object(chat_router, "turn_manager", TurnManager())
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(self.bot["id"]))
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertNotIn("demonstration.frames", [e.get("type") for e in events])
        self.assertFalse(any(isinstance(m.get("content"), list) for m in recorder2["messages"]))
        self.assertTrue(any('9. Clicked the button "Submit".' in str(m.get("content")) for m in recorder2["messages"]))


if __name__ == "__main__":
    unittest.main()
