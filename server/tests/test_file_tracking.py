"""Files a bot read that change under it: reported with the call that changed them or as changed elsewhere, and
not overwritten whole unseen."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_chat_router as chat_harness
from app.config import settings
from app.services import file_tracking
from app.services.action_gateway import ActionDefinition
from app.services.computer_provider import computer_id_for_bot


class Roots:
    """The computer's folders and the shared folder in a temporary directory, as on a bind-mount install."""

    def __init__(self, base: Path, bot_id: str):
        self.computer = base / "computers" / computer_id_for_bot(bot_id)
        self.shared = base / "shared"
        self.computer.mkdir(parents=True)
        self.shared.mkdir()
        self.patches = [
            mock.patch.object(settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", base / "computers"),
            mock.patch.object(settings, "WORKSPACE_ROOT", self.shared),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.roots = Roots(Path(self.temp.name), "bot-1").__enter__()
        self.addCleanup(self.roots.__exit__)
        file_tracking.forget("t")

    def write(self, name, text):
        target = self.roots.computer / name
        target.write_bytes(text.encode())
        return target

    def test_a_small_change_is_shown_and_then_known(self):
        target = self.write("deploy.sh", "#!/bin/sh\necho one\necho two\n")
        file_tracking.note_seen("t", "bot-1", "/workspace/deploy.sh")
        self.assertEqual(file_tracking.changes("t"), [])
        target.write_bytes(b"#!/bin/sh\necho one\necho 2\n")
        found = file_tracking.changes("t")
        self.assertEqual((found[0]["path"], found[0]["change"]), ("/workspace/deploy.sh", "changed"))
        self.assertIn("-echo two\n+echo 2", found[0]["diff"])
        self.assertNotIn("note", found[0])
        self.assertEqual(file_tracking.changes("t"), [])  # reported once
        self.assertIsNone(file_tracking.overwrite_refusal("t", "bot-1", "/workspace/deploy.sh"))  # shown in full: known

    def test_a_big_or_unseen_change_must_be_read_before_an_overwrite(self):
        target = self.write("notes.md", "\n".join(f"line {n}" for n in range(100)))
        file_tracking.note_seen("t", "bot-1", file_tracking.computer_path("notes.md"))  # relative: under /workspace
        target.write_bytes("\n".join(f"LINE {n}" for n in range(100)).encode())
        found = file_tracking.changes("t")
        self.assertIn("[… more changes]", found[0]["diff"])
        self.assertIn("Read it again", found[0]["note"])
        self.assertIn("overwrite: true", file_tracking.overwrite_refusal("t", "bot-1", "/workspace/notes.md"))
        file_tracking.note_seen("t", "bot-1", "/workspace/notes.md")  # read again
        self.assertIsNone(file_tracking.overwrite_refusal("t", "bot-1", "/workspace/notes.md"))
        # Another conversation's view is its own.
        self.assertIsNone(file_tracking.overwrite_refusal("other", "bot-1", "/workspace/notes.md"))

    def test_touched_deleted_shared_and_out_of_reach(self):
        target = self.write("a.txt", "same")
        file_tracking.note_seen("t", "bot-1", "/workspace/a.txt")
        target.write_bytes(b"same")  # a new time, the same text
        self.assertEqual(file_tracking.changes("t"), [])
        target.unlink()
        self.assertEqual(file_tracking.changes("t"), [{"path": "/workspace/a.txt", "change": "deleted"}])
        (self.roots.shared / "plan.md").write_bytes(b"one")
        file_tracking.note_seen("t", "bot-1", file_tracking.shared_path("plan.md"))
        (self.roots.shared / "plan.md").write_bytes(b"two and three")
        self.assertEqual(file_tracking.changes("t")[0]["path"], "/shared/plan.md")
        for path in ("/tmp/x", "/etc/passwd", "/workspace/../etc/passwd"):
            self.assertIsNone(file_tracking.host_path("bot-1", file_tracking.computer_path(path)), path)


class TurnTests(chat_harness.ChatStreamRouteTests):
    def test_the_bot_hears_what_changed_and_does_not_overwrite_unseen(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "fix the script"})
        file_tracking.forget(bot["id"])
        gateway, _ = self._gateway("allow")
        with Roots(Path(self.temp_dir.name) / "files", bot["id"]) as roots:
            script = roots.computer / "deploy.sh"
            script.write_bytes(b"#!/bin/sh\necho one\necho two\n")

            async def read(call):
                return {"path": call.arguments["path"], "content": script.read_text()}

            async def terminal(call):
                script.write_bytes(b"#!/bin/sh\necho one\necho 2\n")  # the bot's own sed
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            async def write(call):
                script.write_bytes(call.arguments["content"].encode())
                return {"path": call.arguments["path"], "created": False}

            for name, run in (("files_read", read), ("terminal_execute", terminal), ("files_write", write)):
                gateway.register_action(ActionDefinition(name=f"computer.{name}", tool="computer", action=name, intent=name, risk="external", requires_approval=False), run)

            recorder = {"rounds": []}
            calls = [
                [{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "deploy.sh"})}],
                [{"id": "c2", "name": "computer_terminal_execute", "arguments": json.dumps({"command": "sed -i s/two/2/ deploy.sh"})}],
                [{"id": "c3", "name": "write_file", "arguments": json.dumps({"path": "/workspace/deploy.sh", "content": "#!/bin/sh\necho mine\n"})}],
            ]

            async def stream(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
                index = len(recorder["rounds"])
                recorder["rounds"].append({"messages": [dict(m) for m in messages]})
                if index == 2:
                    script.write_bytes(("#!/bin/sh\n" + "".join(f"echo {n}\n" for n in range(80))).encode())  # the user, meanwhile
                if index < len(calls):
                    yield {"type": "tool_calls", "calls": calls[index]}
                else:
                    yield {"type": "content.delta", "delta": "Done."}
                yield {"type": "turn.completed", "ok": True}

            with mock.patch.object(chat_harness.chat_router.settings, "COMPUTER_PROVIDER", "docker"):
                self._run_turn(bot["id"], stream=stream, gateway=gateway)

        after_command = json.loads(recorder["rounds"][2]["messages"][-1]["content"])
        self.assertIn("-echo two\n+echo 2", after_command["files_changed_by_this_call"][0]["diff"])
        refused = json.loads(recorder["rounds"][3]["messages"][-1]["content"])
        self.assertIn("changed since you last read it", refused["error"])
        self.assertEqual(refused["files_changed_outside_this_conversation"][0]["path"], "/workspace/deploy.sh")
        self.assertIn("echo 79", script.read_text())  # the user's change survived


# Only the TurnTests above run under this name, not the whole inherited suite again.
for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in TurnTests.__dict__:
        setattr(TurnTests, _name, None)


if __name__ == "__main__":
    unittest.main()
