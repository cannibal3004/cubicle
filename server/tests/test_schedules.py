import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import schedules as schedules_router
from app.services import schedule_service, scheduler as scheduler_module
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.context_builder import SCHEDULED_LEAD, build_history_messages
from app.services.llm_tools import SCHEDULE_TOOLS, ToolCallError, available_tools, build_invocation, describe_tools
from app.services.schedule_service import (
    ScheduleError,
    clean_cadence,
    create_schedule,
    describe_cadence,
    due_schedules,
    next_run,
    record_run,
    register_schedule_actions,
    scheduled_message,
    update_schedule,
)
from app.services.scheduler import Scheduler
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnBusyError, TurnManager
from app.services.workspace_service import WorkspaceService
from app.services import storage_service as storage_module

# A fixed zone keeps the maths the same on any host; Monday 2026-10-12 09:00.
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=TZ)


class _Audit:
    def add_audit_event(self, event):
        pass


class _Broker:
    def __init__(self, decision="allow"):
        self.decision = decision
        self.opened = []

    def open(self, thread_id, bot_id, call, request_id=None):
        self.opened.append(request_id)
        return {"request_id": request_id, "thread_id": thread_id, "bot_id": bot_id, "tool": call.name, "status": "pending"}

    async def wait(self, request_id):
        return self.decision


class CadenceTests(unittest.TestCase):
    def test_cadences_are_cleaned_and_described(self):
        self.assertEqual(clean_cadence({"kind": "once", "at": "2026-10-15 09:30"}, tz=TZ, now=NOW), {"kind": "once", "at": "2026-10-15 09:30"})
        self.assertEqual(clean_cadence({"kind": "once", "at": "2026-10-15T09:30"}, tz=TZ, now=NOW)["at"], "2026-10-15 09:30")
        self.assertEqual(clean_cadence({"kind": "once", "in_minutes": 90}, tz=TZ, now=NOW), {"kind": "once", "at": "2026-10-12 10:30"})
        self.assertEqual(clean_cadence({"kind": "every", "minutes": 30}, tz=TZ, now=NOW), {"kind": "every", "minutes": 30})
        self.assertEqual(clean_cadence({"kind": "every", "hours": 2}, tz=TZ, now=NOW), {"kind": "every", "minutes": 120})
        self.assertEqual(clean_cadence({"kind": "daily", "time": "8:05"}, tz=TZ, now=NOW), {"kind": "daily", "time": "08:05"})
        self.assertEqual(
            clean_cadence({"kind": "weekly", "days": ["mon", "Wednesday", 4, "4"], "time": "18:00"}, tz=TZ, now=NOW),
            {"kind": "weekly", "days": [0, 2, 4], "time": "18:00"},
        )
        self.assertEqual(describe_cadence({"kind": "every", "minutes": 90}), "every 90 minutes")
        self.assertEqual(describe_cadence({"kind": "every", "minutes": 120}), "every 2 hours")
        self.assertEqual(describe_cadence({"kind": "every", "minutes": 1440}), "every day")
        self.assertEqual(describe_cadence({"kind": "weekly", "days": [0, 4], "time": "09:00"}), "weekly on Mon, Fri at 09:00")
        self.assertEqual(describe_cadence({"kind": "once", "at": "2026-10-15 09:30"}), "once at 2026-10-15 09:30")

        for bad, message in (
            ({"kind": "hourly"}, "once, every, daily or weekly"),
            ({"kind": "once", "at": "tomorrow"}, "date and time"),
            ({"kind": "once", "at": "2026-10-12 08:00"}, "in the past"),
            ({"kind": "every", "minutes": 2}, "between 5"),
            ({"kind": "every", "minutes": "lots"}, "whole number"),
            ({"kind": "daily", "time": "25:00"}, "HH:MM"),
            ({"kind": "weekly", "days": [], "time": "09:00"}, "weekdays"),
            ({"kind": "weekly", "days": [7], "time": "09:00"}, "Unknown weekday"),
            ("daily", "cadence is required"),
        ):
            with self.assertRaisesRegex(ScheduleError, message):
                clean_cadence(bad, tz=TZ, now=NOW)

    def test_next_run_for_each_kind(self):
        self.assertEqual(next_run({"kind": "once", "at": "2026-10-15 09:30"}, after=NOW, tz=TZ), datetime(2026, 10, 15, 9, 30, tzinfo=TZ))
        self.assertIsNone(next_run({"kind": "once", "at": "2026-10-12 08:00"}, after=NOW, tz=TZ))
        # Every: counted from the anchor, skipping occurrences already past.
        self.assertEqual(next_run({"kind": "every", "minutes": 30}, after=NOW, anchor=NOW, tz=TZ), NOW + timedelta(minutes=30))
        self.assertEqual(
            next_run({"kind": "every", "minutes": 30}, after=NOW + timedelta(minutes=70), anchor=NOW, tz=TZ), NOW + timedelta(minutes=90)
        )
        # Daily: today if the time is still ahead, else tomorrow.
        self.assertEqual(next_run({"kind": "daily", "time": "09:00"}, after=NOW, tz=TZ), datetime(2026, 10, 13, 9, 0, tzinfo=TZ))
        self.assertEqual(next_run({"kind": "daily", "time": "17:30"}, after=NOW, tz=TZ), datetime(2026, 10, 12, 17, 30, tzinfo=TZ))
        # Weekly: Monday 09:00 has passed, so Wednesday; then next Monday.
        self.assertEqual(next_run({"kind": "weekly", "days": [0, 2], "time": "09:00"}, after=NOW, tz=TZ), datetime(2026, 10, 14, 9, 0, tzinfo=TZ))
        self.assertEqual(
            next_run({"kind": "weekly", "days": [0], "time": "09:00"}, after=NOW, tz=TZ), datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
        )
        # An instant in another zone is converted, not misread.
        utc_now = NOW.astimezone(timezone.utc)
        self.assertEqual(next_run({"kind": "daily", "time": "10:00"}, after=utc_now, tz=TZ), datetime(2026, 10, 12, 10, 0, tzinfo=TZ))


class ScheduleRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]["id"]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open a moment longer.

    def test_create_update_run_and_delete(self):
        created = create_schedule(self.bot, "  Morning  digest ", "Summarise overnight.\r\n", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
        self.assertTrue(created["id"].startswith("sch-"))
        self.assertEqual((created["name"], created["prompt"], created["cadence_text"]), ("Morning digest", "Summarise overnight.", "daily at 08:00"))
        self.assertEqual(created["next_run_at"], datetime(2026, 10, 13, 8, 0, tzinfo=TZ).isoformat(timespec="minutes"))
        self.assertTrue(created["enabled"])
        self.assertEqual(created["created_by"], "user")
        self.assertEqual([s["id"] for s in schedule_service.list_for_bot(self.bot, self.storage)], [created["id"]])

        # Not due yet; due once its time has come.
        self.assertEqual(due_schedules(NOW, storage=self.storage, tz=TZ), [])
        later = datetime(2026, 10, 13, 8, 0, tzinfo=TZ)
        self.assertEqual([s["id"] for s in due_schedules(later, storage=self.storage, tz=TZ)], [created["id"]])
        ran = record_run(created["id"], "completed", "", storage=self.storage, tz=TZ, now=later)
        self.assertEqual((ran["last_status"], ran["last_run_at"]), ("completed", later.isoformat(timespec="minutes")))
        self.assertEqual(ran["next_run_at"], datetime(2026, 10, 14, 8, 0, tzinfo=TZ).isoformat(timespec="minutes"))

        paused = update_schedule(created["id"], enabled=False, storage=self.storage, tz=TZ, now=later)
        self.assertFalse(paused["enabled"])
        self.assertEqual(due_schedules(later + timedelta(days=1), storage=self.storage, tz=TZ), [])
        changed = update_schedule(created["id"], cadence={"kind": "every", "minutes": 60}, name="Hourly", enabled=True, storage=self.storage, tz=TZ, now=later)
        self.assertEqual((changed["name"], changed["cadence_text"], changed["enabled"]), ("Hourly", "every hour", True))
        self.assertEqual(changed["next_run_at"], (later + timedelta(hours=1)).isoformat(timespec="minutes"))

        gone = schedule_service.delete_schedule(created["id"], storage=self.storage)
        self.assertEqual(gone["id"], created["id"])
        self.assertEqual(schedule_service.list_for_bot(self.bot, self.storage), [])
        with self.assertRaisesRegex(ScheduleError, "no schedule"):
            schedule_service.get_schedule(created["id"], self.storage)
        with self.assertRaisesRegex(ScheduleError, "does not exist"):
            create_schedule("nobody", "x", "y", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
        with self.assertRaisesRegex(ScheduleError, "needs a name"):
            create_schedule(self.bot, " ", "y", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
        with self.assertRaisesRegex(ScheduleError, "needs the prompt"):
            create_schedule(self.bot, "x", "", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)

    def test_a_once_schedule_is_finished_after_it_runs(self):
        created = create_schedule(self.bot, "Reminder", "Ping Jamie.", {"kind": "once", "in_minutes": 30}, storage=self.storage, tz=TZ, now=NOW)
        when = NOW + timedelta(minutes=30)
        self.assertEqual([s["id"] for s in due_schedules(when, storage=self.storage, tz=TZ)], [created["id"]])
        ran = record_run(created["id"], "completed", storage=self.storage, tz=TZ, now=when)
        self.assertFalse(ran["enabled"])
        self.assertIsNone(ran["next_run_at"])
        self.assertEqual(due_schedules(when + timedelta(days=1), storage=self.storage, tz=TZ), [])

    def test_the_per_bot_limit_and_the_message_a_run_posts(self):
        with mock.patch.object(schedule_service, "MAX_PER_BOT", 2):
            create_schedule(self.bot, "a", "A", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
            create_schedule(self.bot, "b", "B", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
            with self.assertRaisesRegex(ScheduleError, "at most 2"):
                create_schedule(self.bot, "c", "C", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
        record = self.storage.list_schedules(self.bot)[0]
        message = scheduled_message(record, message_id="msg-1", now=NOW)
        self.assertEqual((message["sender"], message["text"], message["item_type"], message["thread_id"]), ("user", "A", "user_text", self.bot))
        self.assertEqual(message["raw_payload"]["scheduled"]["name"], "a")
        self.assertEqual(message["raw_payload"]["scheduled"]["at"], "2026-10-12 09:00")
        # The model is told the message was scheduled, in place.
        context = build_history_messages([message], replay_tools=True)
        self.assertEqual(context[0]["content"], f"{SCHEDULED_LEAD.format(name='a', at='2026-10-12 09:00')}\n\nA")

    def test_deleting_a_bot_removes_its_schedules(self):
        create_schedule(self.bot, "a", "A", {"kind": "daily", "time": "08:00"}, storage=self.storage, tz=TZ, now=NOW)
        self.assertEqual(self.storage.delete_bot_schedules(self.bot), 1)
        self.assertEqual(self.storage.list_schedules(), [])


class ScheduleToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]["id"]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def test_tools_are_offered_and_create_needs_approval(self):
        specs = available_tools(computer=False)
        names = [s.name for s in specs]
        for name in ("schedule_list", "schedule_create", "schedule_delete"):
            self.assertIn(name, names)
        by_name = {s.name: s for s in SCHEDULE_TOOLS}
        self.assertFalse(by_name["schedule_list"].needs_approval)
        self.assertTrue(by_name["schedule_create"].needs_approval)
        self.assertTrue(by_name["schedule_delete"].needs_approval)
        self.assertIn("Schedules: schedule_create", describe_tools(specs))

        with mock.patch.object(schedule_service, "app_timezone", lambda: TZ), mock.patch.object(schedule_service, "now_local", lambda tz=None: NOW):
            create = build_invocation(
                "schedule_create",
                {"name": "Digest", "prompt": "Summarise the inbox.", "kind": "weekly", "days": [0, 2], "time": "09:00", "at": ""},
                self.bot,
            )
            self.assertEqual(create.name, "schedule.create")
            self.assertEqual(create.arguments["cadence"], {"kind": "weekly", "days": [0, 2], "time": "09:00"})
            self.assertIn("Schedule 'Digest' weekly on Mon, Wed at 09:00", create.preview)
            once = build_invocation("schedule_create", {"name": "Later", "prompt": "Check.", "kind": "once", "in_minutes": 15}, self.bot)
            self.assertEqual(once.arguments["cadence"], {"kind": "once", "at": "2026-10-12 09:15"})
            with self.assertRaisesRegex(ToolCallError, "between 5"):
                build_invocation("schedule_create", {"name": "x", "prompt": "y", "kind": "every", "minutes": 1}, self.bot)
            with self.assertRaises(ToolCallError):
                build_invocation("schedule_create", {"name": "x", "kind": "daily", "time": "09:00"}, self.bot)
        listing = build_invocation("schedule_list", {}, self.bot)
        self.assertEqual(listing.name, "schedule.list")
        delete = build_invocation("schedule_delete", {"id": "sch-1"}, self.bot)
        self.assertEqual(delete.arguments, {"bot_id": self.bot, "id": "sch-1"})

    def test_gateway_actions(self):
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp_dir.name)), approvals=_Broker(), audit=_Audit())
        register_schedule_actions(gateway)
        with mock.patch.object(storage_module, "storage_service", self.storage), mock.patch.object(
            schedule_service, "app_timezone", lambda: TZ
        ), mock.patch.object(schedule_service, "now_local", lambda tz=None: NOW):
            create = build_invocation("schedule_create", {"name": "Digest", "prompt": "Summarise.", "kind": "daily", "time": "08:00"}, self.bot)
            request, approval = gateway.open("t", self.bot, create)
            self.assertIsNotNone(approval)
            self.assertEqual(asyncio.run(gateway.wait_for_decision(request)), "allow")
            result = asyncio.run(gateway.execute(request))
            self.assertEqual(result.status, "completed")
            schedule = result.result["schedule"]
            self.assertEqual((schedule["name"], schedule["created_by"], schedule["cadence_text"]), ("Digest", "bot", "daily at 08:00"))
            self.assertEqual(result.result["now"], "2026-10-12 09:00")
            self.assertEqual(result.result["weekday"], "Mon")

            listing = build_invocation("schedule_list", {}, self.bot)
            request, approval = gateway.open("t", self.bot, listing)
            self.assertIsNone(approval)
            listed = asyncio.run(gateway.execute(request)).result
            self.assertEqual([s["id"] for s in listed["schedules"]], [schedule["id"]])

            # Another bot cannot delete it.
            other = ActionInvocation(name="schedule.delete", arguments={"bot_id": "bot-other", "id": schedule["id"]}, target={}, preview="x")
            request, _ = gateway.open("t", "bot-other", other)
            asyncio.run(gateway.wait_for_decision(request))
            failed = asyncio.run(gateway.execute(request))
            self.assertNotEqual(failed.status, "completed")
            self.assertIn("another bot", str(failed.error))

            delete = build_invocation("schedule_delete", {"id": schedule["id"]}, self.bot)
            request, approval = gateway.open("t", self.bot, delete)
            self.assertIsNotNone(approval)
            asyncio.run(gateway.wait_for_decision(request))
            self.assertEqual(asyncio.run(gateway.execute(request)).result["deleted"]["id"], schedule["id"])
            self.assertEqual(self.storage.list_schedules(self.bot), [])

    def test_router_crud_and_ownership(self):
        with mock.patch.object(storage_module, "storage_service", self.storage), mock.patch.object(
            storage_module, "storage_service", self.storage
        ), mock.patch.object(schedule_service, "app_timezone", lambda: TZ), mock.patch.object(schedule_service, "now_local", lambda tz=None: NOW):
            created = asyncio.run(
                schedules_router.create_schedule(self.bot, schedules_router.ScheduleInput(name="Digest", prompt="Go.", cadence={"kind": "daily", "time": "08:00"}))
            )["schedule"]
            listed = asyncio.run(schedules_router.list_schedules(self.bot))
            self.assertEqual([s["id"] for s in listed["schedules"]], [created["id"]])
            self.assertEqual(listed["now"], "2026-10-12 09:00")
            changed = asyncio.run(schedules_router.update_schedule(self.bot, created["id"], schedules_router.ScheduleUpdate(enabled=False)))["schedule"]
            self.assertFalse(changed["enabled"])
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(schedules_router.update_schedule(self.bot, created["id"], schedules_router.ScheduleUpdate(cadence={"kind": "every", "minutes": 1})))
            self.assertEqual(caught.exception.status_code, 422)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(schedules_router.delete_schedule("bot-other", created["id"]))
            self.assertEqual(caught.exception.status_code, 404)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(schedules_router.list_schedules("bot-missing"))
            self.assertEqual(caught.exception.status_code, 404)
            deleted = asyncio.run(schedules_router.delete_schedule(self.bot, created["id"]))
            self.assertEqual(deleted["deleted"]["id"], created["id"])


class _FakeTurn:
    def __init__(self, status="completed", error=None, delay=0.0):
        self.turn_id = "turn-fake"
        self.status = "running"
        self.error = error
        self.pending_approval = None
        self.finished = False
        self._final = status
        self.task = asyncio.get_running_loop().create_task(self._run(delay))

    async def _run(self, delay):
        await asyncio.sleep(delay)
        self.status = self._final
        self.finished = True


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]["id"]
        self.manager = TurnManager()
        self.started = []

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    async def _run_turn(self, bot_id, message, owner_id=None):
        self.started.append((bot_id, message))
        self.storage.add_message(message)
        return _FakeTurn()

    async def test_due_schedules_run_and_a_busy_bot_is_skipped(self):
        with mock.patch.object(storage_module, "storage_service", self.storage), mock.patch.object(
            schedule_service, "app_timezone", lambda: TZ
        ), mock.patch.object(schedule_service, "now_local", lambda tz=None: NOW), mock.patch.object(scheduler_module, "turn_manager", self.manager):
            daily = create_schedule(self.bot, "Digest", "Summarise.", {"kind": "daily", "time": "09:30"}, storage=self.storage, tz=TZ, now=NOW)
            once = create_schedule(self.bot, "Ping", "Ping.", {"kind": "once", "in_minutes": 10}, storage=self.storage, tz=TZ, now=NOW)
            scheduler = Scheduler(tick_seconds=60)
            scheduler.start(self._run_turn)
            await scheduler.stop()  # The loop is not needed; ticks are driven by hand.

            self.assertEqual(await scheduler.tick(NOW), 0)
            self.assertEqual(self.started, [])

            at_once = NOW + timedelta(minutes=10)
            self.assertEqual(await scheduler.tick(at_once), 1)
            await asyncio.sleep(0.05)
            self.assertEqual([m["text"] for _, m in self.started], ["Ping."])
            self.assertEqual(self.started[0][1]["raw_payload"]["scheduled"]["name"], "Ping")
            self.assertEqual(self.storage.get_messages(self.bot)[-1]["text"], "Ping.")
            finished = schedule_service.get_schedule(once["id"], self.storage)
            self.assertEqual((finished["last_status"], finished["enabled"], finished["next_run_at"]), ("completed", False, None))

            # The daily one is due at 09:30; a running turn makes it a skip.
            at_daily = NOW + timedelta(minutes=30)
            self.manager.start(self.bot, self.bot, "m", lambda turn: asyncio.sleep(0.2))
            self.assertEqual(await scheduler.tick(at_daily), 1)
            skipped = schedule_service.get_schedule(daily["id"], self.storage)
            self.assertEqual((skipped["last_status"], skipped["last_note"]), ("skipped", "a reply was still in progress"))
            self.assertEqual(skipped["next_run_at"], datetime(2026, 10, 13, 9, 30, tzinfo=TZ).isoformat(timespec="minutes"))
            self.assertEqual(len(self.started), 1)
            self.manager.cancel(self.bot)

    async def test_a_turn_that_cannot_start_is_recorded(self):
        async def busy(bot_id, message, owner_id=None):
            raise TurnBusyError(mock.Mock())

        with mock.patch.object(storage_module, "storage_service", self.storage), mock.patch.object(
            schedule_service, "app_timezone", lambda: TZ
        ), mock.patch.object(schedule_service, "now_local", lambda tz=None: NOW), mock.patch.object(scheduler_module, "turn_manager", self.manager):
            created = create_schedule(self.bot, "Ping", "Ping.", {"kind": "every", "minutes": 5}, storage=self.storage, tz=TZ, now=NOW)
            scheduler = Scheduler(tick_seconds=60)
            scheduler.start(busy)
            await scheduler.stop()
            self.assertEqual(await scheduler.tick(NOW + timedelta(minutes=5)), 1)
            await asyncio.sleep(0.05)
            record = schedule_service.get_schedule(created["id"], self.storage)
            self.assertEqual(record["last_status"], "skipped")
            self.assertEqual(record["next_run_at"], (NOW + timedelta(minutes=10)).isoformat(timespec="minutes"))

            # Run now goes through the same path, immediately.
            self.started.clear()
            scheduler._run_turn = self._run_turn
            self.assertTrue(await scheduler.run_now(record))
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.started), 1)


if __name__ == "__main__":
    unittest.main()
