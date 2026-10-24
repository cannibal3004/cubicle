"""Fixes from the Server Manager bot's run: live secrets, background jobs, card timeouts, the loop guard."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import approval_broker as broker_module
from app.services import storage_service as storage_module
from app.services.approval_broker import card_timeout
from app.services.docker_computer_provider import DockerComputerProvider
from app.services.llm_tools import build_invocation
from app.services.storage_service import StorageService

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream
from test_docker_computer_provider import FakeDockerCommand


class LiveSecretsAndBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_command_carries_the_secrets_as_they_are_now(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        secrets = {"GITHUB_TOKEN": "tok-1", "COMPUTER_TOKEN": "hijack", "PATH": "/evil"}
        docker = FakeDockerCommand()
        provider = DockerComputerProvider(
            image="cubicle-computer:test", workspace_root=root / "computers", seccomp_profile=root / "none.json",
            start_timeout=0.5, environment=lambda bot_id: dict(secrets), docker_command=docker, shared_dir="",
        )
        status = provider.get_or_create("bot-env")

        async def ready(_record):
            return {"status": "healthy"}

        provider._wait_until_ready = ready
        await provider.start(status.computer_id)
        seen = {}

        async def capture(_record, method, route, payload=None, timeout=None):
            seen.update({"payload": payload, "timeout": timeout})
            return {"operation": "terminal.exec", "exit_code": 0, "stdout": "", "stderr": ""}

        provider._request = capture
        await provider.terminal_execute(status.computer_id, "printenv GITHUB_TOKEN")
        self.assertEqual(seen["payload"]["env"], {"GITHUB_TOKEN": "tok-1"})
        self.assertNotIn("unset", seen["payload"])
        # The user adds one secret and deletes another while the computer runs.
        secrets["WINRM_PASS"] = "pw"
        del secrets["GITHUB_TOKEN"]
        await provider.terminal_execute(status.computer_id, "printenv")
        self.assertEqual(seen["payload"]["env"], {"WINRM_PASS": "pw"})
        self.assertEqual(seen["payload"]["unset"], ["GITHUB_TOKEN"])
        # A background job returns at once: the request does not wait for the command.
        await provider.terminal_execute(status.computer_id, "rsync -a a b", 600, background=True)
        self.assertTrue(seen["payload"]["background"])
        self.assertEqual(seen["timeout"], 10.0)

    def test_the_tool_takes_background(self):
        call = build_invocation("computer_terminal_execute", {"command": "rsync -a /a /b", "background": True}, "bot-1")
        self.assertTrue(call.arguments["background"])
        self.assertIn("background job", call.preview)
        plain = build_invocation("computer_terminal_execute", {"command": "ls"}, "bot-1")
        self.assertNotIn("background", plain.arguments)


class CardTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_the_users_setting_decides_and_zero_waits_until_answered(self):
        self.assertEqual(card_timeout(), float(broker_module.settings.APPROVAL_TIMEOUT_SECONDS))
        self.storage.save_settings({"approval_timeout_seconds": 3600})
        self.assertEqual(card_timeout(), 3600.0)
        self.storage.save_settings({"approval_timeout_seconds": 0})
        self.assertIsNone(card_timeout())

    def test_a_card_with_no_limit_waits_for_its_answer(self):
        self.storage.save_settings({"approval_timeout_seconds": 0})
        broker = broker_module.ApprovalBroker()

        async def scenario():
            future = asyncio.get_running_loop().create_future()
            broker.pending["req-1"] = type("Pending", (), {"future": future})()
            waiter = asyncio.create_task(broker.wait("req-1"))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            future.set_result("allow")
            return await waiter

        self.assertEqual(asyncio.run(scenario()), "allow")


def _looping_round(line="- Get-VMReplication.", times=40):
    return [{"type": "reasoning.delta", "delta": "Let me list the cmdlets.\n"}] + [
        {"type": "reasoning.delta", "delta": f"{line}\n"} for _ in range(times)
    ] + [{"type": "content.delta", "delta": "never reached"}, {"type": "turn.completed", "ok": True}]


class LoopGuardTests(chat_harness.ChatStreamRouteTests):
    def _bot(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "which cmdlets do you need?"})
        return bot

    def test_a_loop_is_cut_and_the_model_is_told_to_carry_on(self):
        bot = self._bot()
        recorder = {}
        stream = _scripted_stream(recorder, [
            _looping_round(),
            [{"type": "content.delta", "delta": "Get-VM, Get-VMHost and Get-VMReplication."}, {"type": "turn.completed", "ok": True}],
        ])
        _, events = self._run_turn(bot["id"], stream=stream)
        loops = [e for e in events if e["type"] == "turn.loop"]
        self.assertEqual([(e["where"], e["sample"], e["count"]) for e in loops], [("thinking", "- Get-VMReplication.", 1)])
        nudge = recorder["rounds"][1]["messages"][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("fell into a loop", nudge["content"])
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual(saved["text"], "Get-VM, Get-VMHost and Get-VMReplication.")
        self.assertEqual(saved["raw_payload"]["loop_cuts"], 1)
        self.assertEqual(saved["raw_payload"]["reasoning"].count("- Get-VMReplication."), 1)

    def test_a_second_loop_ends_the_turn_with_a_note(self):
        bot = self._bot()
        recorder = {}
        stream = _scripted_stream(recorder, [_looping_round(), _looping_round("I will check again.", 60)])
        _, events = self._run_turn(bot["id"], stream=stream)
        self.assertEqual([e["count"] for e in events if e["type"] == "turn.loop"], [1, 2])
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertIn("kept repeating itself", saved["text"])
        self.assertEqual(len(recorder["rounds"]), 2)


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in LoopGuardTests.__dict__:
        setattr(LoopGuardTests, _name, None)


if __name__ == "__main__":
    unittest.main()
