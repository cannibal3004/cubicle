"""Background jobs a bot waits on: the conversation is told when one ends."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import job_watch
from app.services import storage_service as storage_module
from app.services.job_watch import JobWatcher
from app.services.llm_tools import build_invocation
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnBusyError


class JobWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.storage = StorageService(root / "data")
        self.bot = self.storage.get_bots()[0]["id"]
        self.home = root / "computers" / "computer-x.home" / ".cubicle-jobs"
        self.home.mkdir(parents=True)
        self.state = "running"
        self.running_turn = None
        self.started = []
        self.patches = [
            mock.patch.object(job_watch.settings, "DATA_DIR", root / "data"),
            mock.patch.object(job_watch.settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(job_watch.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(job_watch.settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", root / "computers"),
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch("app.services.computer_provider.computer_provider", SimpleNamespace(describe=lambda bot_id: SimpleNamespace(state=self.state))),
            mock.patch("app.services.turn_manager.turn_manager", SimpleNamespace(current=lambda thread_id: self.running_turn)),
        ]
        for p in self.patches:
            p.start()
        self.watcher = JobWatcher()

        async def start_turn(thread_id, message, owner_id):
            if self.running_turn is not None and not self.running_turn.finished:
                raise TurnBusyError(self.running_turn)
            self.started.append((thread_id, message))

        self.watcher._start_turn = start_turn

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def _watch(self, job_id, command="rsync -a /mnt/a /mnt/b"):
        result = {"job_id": job_id, "pid": 4242, "log": f"/home/pwuser/.cubicle-jobs/{job_id}.log", "exit_file": f"/home/pwuser/.cubicle-jobs/{job_id}.exit"}
        return self.watcher.watch(owner_id="local-user", bot_id=self.bot, thread_id=self.bot, computer_id="computer-x", result=result, command=command)

    async def test_an_ended_job_starts_a_turn_with_its_exit_code_and_log(self):
        told = self._watch("job-1")
        self.assertIn("You will get a message", told["notify"])
        await self.watcher.poll()
        self.assertEqual(self.started, [])  # still running
        (self.home / "job-1.log").write_text("copied 412 files\nall done\n")
        (self.home / "job-1.exit").write_text("0\n")
        await self.watcher.poll()
        self.assertEqual(len(self.started), 1)
        thread_id, message = self.started[0]
        self.assertEqual(thread_id, self.bot)
        self.assertIn("`rsync -a /mnt/a /mnt/b` (job-1) finished (exit code 0)", message["text"])
        self.assertIn("all done", message["text"])
        self.assertEqual(message["raw_payload"]["jobs"], [{"id": "job-1", "command": "rsync -a /mnt/a /mnt/b", "exit_code": 0}])
        self.assertEqual(self.watcher.pending(), [])  # told once

    async def test_while_a_reply_runs_it_waits_and_jobs_that_end_together_make_one_message(self):
        self._watch("job-a", "make build")
        self._watch("job-b", "pytest -q")
        for job, code in (("job-a", "0"), ("job-b", "2")):
            (self.home / f"{job}.log").write_text(f"{job} output\n")
            (self.home / f"{job}.exit").write_text(code)
        self.running_turn = SimpleNamespace(finished=False)
        await self.watcher.poll()
        self.assertEqual(self.started, [])
        self.assertEqual(len(self.watcher.pending()), 2)  # kept, ended
        self.running_turn = SimpleNamespace(finished=True)
        await self.watcher.poll()
        self.assertEqual(len(self.started), 1)
        text = self.started[0][1]["text"]
        self.assertIn("[2 background jobs you started have ended.]", text)
        self.assertIn("`pytest -q` (job-b) failed with exit code 2", text)

    async def test_a_job_whose_computer_stopped_is_reported_lost(self):
        self._watch("job-x")
        self.state = "stopped"
        await self.watcher.poll()
        self.assertIn("ended without an exit code", self.started[0][1]["text"])
        self.assertIn("the computer stopped", self.started[0][1]["text"])

    async def test_a_killed_job_is_noticed_by_the_alive_check(self):
        self._watch("job-k")
        watches = job_watch._load()
        watches[0]["checked_alive_at"] = time.time() - job_watch.ALIVE_CHECK_SECONDS - 1
        job_watch._save(watches)
        with mock.patch.object(JobWatcher, "_run", staticmethod(lambda watch, command: asyncio.sleep(0, result="gone\n"))):
            await self.watcher.poll()
        self.assertIn("killed", self.started[0][1]["text"])

    def test_the_tool_takes_notify(self):
        call = build_invocation("computer_terminal_execute", {"command": "python serve.py", "background": True, "notify": False}, "bot-1")
        self.assertIs(call.arguments["notify"], False)
        default = build_invocation("computer_terminal_execute", {"command": "make", "background": True}, "bot-1")
        self.assertNotIn("notify", default.arguments)


if __name__ == "__main__":
    unittest.main()
