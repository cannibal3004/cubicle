import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.routers import admin as admin_router
from app.routers import chat as chat_router
from app.routers import settings as settings_router
from app.schemas.contracts import AppSettingsSchema
from app.services import composio_service as composio_module
from app.services import storage_service as storage_module
from app.services import user_questions
from app.services.accounts import AccountStore
from app.services.approval_broker import ApprovalBroker
from app.services.identity import LOCAL_USER_ID, SYSTEM_OWNER, get_current_owner, owner_context
from app.services.mcp_service import McpManager, McpStore
from app.services.scheduler import Scheduler
from app.services.storage_service import StorageService, current_storage
from app.services.turn_manager import TurnManager

PASSWORD = "correct horse battery"


def _request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user), headers={}, cookies={}, client=SimpleNamespace(host="10.0.0.1"))


OWNER = {"id": LOCAL_USER_ID, "username": "jamie", "role": "owner"}
MEMBER = {"id": "user-b", "username": "bob", "role": "member"}


class OwnerScopedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = StorageService(Path(self.temp.name))
        self.accounts = AccountStore(self.root.database, self.root.secret_store)
        self.accounts.complete_setup("jamie", PASSWORD)
        self.bob = self.accounts.create_user("bob", PASSWORD)

    def tearDown(self):
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_each_owner_sees_only_their_own_bots_and_messages(self):
        b = self.root.for_owner(self.bob["id"])
        self.assertIs(self.root.for_owner(LOCAL_USER_ID), self.root)
        self.assertIs(self.root.for_owner(self.bob["id"]), b)  # cached
        self.assertIs(b.database, self.root.database)
        # A new owner starts with the seeded bots; neither side sees the other's.
        self.assertTrue(b.get_bots())
        b.save_bots([{"id": "bot-b1", "name": "Bob's bot"}])
        self.assertEqual([x["id"] for x in b.get_bots()], ["bot-b1"])
        self.assertNotIn("bot-b1", [x["id"] for x in self.root.get_bots()])
        b.add_message({"id": "m1", "thread_id": "bot-b1", "bot_id": "bot-b1", "sender": "user", "text": "hi"})
        self.assertEqual(len(b.get_messages("bot-b1")), 1)
        self.assertEqual(self.root.get_messages("bot-b1"), [])
        self.assertEqual(self.root.list_owner_ids(), [LOCAL_USER_ID, self.bob["id"]])

    def test_app_settings_are_shared_and_personal_ones_are_not(self):
        b = self.root.for_owner(self.bob["id"])
        self.root.save_settings({"default_model": "shared-model", "composio_api_key": "key-1", "auto_approval": "on"})
        self.assertEqual(b.get_settings()["default_model"], "shared-model")
        self.assertEqual(b.get_settings()["composio_api_key"], "key-1")
        self.assertEqual(b.get_settings()["auto_approval"], "off")  # personal: Bob has not changed his
        b.save_settings({"auto_approval": "shadow", "disabled_toolkits": ["github"], "default_model": "bob-model"})
        self.assertEqual(b.get_settings()["auto_approval"], "shadow")
        self.assertEqual(self.root.get_settings()["auto_approval"], "on")
        self.assertEqual(self.root.get_settings().get("disabled_toolkits", []), [])
        # Bob's write to an app key landed on the shared row (the router stops non-admins).
        self.assertEqual(self.root.get_settings()["default_model"], "bob-model")
        self.assertEqual(set(self.root.get_app_settings()), {k for k in storage_module.APP_SETTING_KEYS if k in self.root.get_settings()})
        with self.root.database.connect() as connection:
            owners = {row[0] for row in connection.execute("SELECT DISTINCT owner_id FROM settings")}
        self.assertEqual(owners, {SYSTEM_OWNER, self.bob["id"], LOCAL_USER_ID})

    def test_the_owner_context_picks_the_store(self):
        with mock.patch.object(storage_module, "storage_service", self.root):
            self.assertEqual(get_current_owner(), LOCAL_USER_ID)
            self.assertIs(current_storage(), self.root)
            with owner_context(self.bob["id"]):
                self.assertEqual(get_current_owner(), self.bob["id"])
                self.assertEqual(current_storage().owner_id, self.bob["id"])
            self.assertIs(current_storage(), self.root)
            # A plain stand-in without owners is returned as it is (the tests use these).
            fake = SimpleNamespace(get_settings=lambda: {})
            with mock.patch.object(storage_module, "storage_service", fake):
                self.assertIs(current_storage(), fake)

    def test_composio_user_ids_and_mcp_scopes_follow_the_owner(self):
        service = composio_module.ComposioService(self.root)
        self.assertEqual(service.user_id, composio_module.DEFAULT_USER_ID)
        with owner_context(self.bob["id"]):
            self.assertEqual(service.user_id, f"cubicle-{self.bob['id']}")
        with mock.patch.object(storage_module, "storage_service", self.root):
            manager = McpManager(McpStore(scope="system"), secrets=lambda: {})
            manager.store.add({"name": "Shared docs", "transport": "http", "url": "https://mcp.example.com/mcp"})
            with owner_context(self.bob["id"]):
                manager.user_store.add({"name": "Bob's scanner", "command": "scan"})
                self.assertEqual([r["name"] for r in manager.records()], ["Shared docs", "Bob's scanner"])
                self.assertEqual([r["scope"] for r in manager.records()], ["system", "user"])
            self.assertEqual([r["name"] for r in manager.records()], ["Shared docs"])
            self.assertEqual(self.root.get_settings().get("user_mcp_servers", []), [])


class OwnershipRouteTests(unittest.TestCase):
    """What one account's requests can reach of another's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = StorageService(Path(self.temp.name))
        self.accounts = AccountStore(self.root.database, self.root.secret_store)
        self.accounts.complete_setup("jamie", PASSWORD)
        self.bob = self.accounts.create_user("bob", PASSWORD)
        self.patch = mock.patch.object(storage_module, "storage_service", self.root)
        self.patch.start()
        self.a_bot = self.root.get_bots()[0]["id"]
        with owner_context(self.bob["id"]):
            self.b_bot = current_storage().get_bots()[0]["id"]

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_threads_turns_and_approvals_belong_to_their_owner(self):
        async def scenario():
            # History: mine is readable, a stranger's is not found.
            self.assertEqual(len(await chat_router.get_history(self.a_bot)), 1)  # the seeded greeting
            with owner_context(self.bob["id"]):
                with self.assertRaises(HTTPException) as ctx:
                    await chat_router.get_history(self.a_bot)
                self.assertEqual(ctx.exception.status_code, 404)
                self.assertEqual(len(await chat_router.get_history(self.b_bot)), 1)
                with self.assertRaises(HTTPException):
                    await chat_router.turn_status(self.a_bot)
                with self.assertRaises(HTTPException):
                    await chat_router.cancel_turn(self.a_bot)
            # The turn overview only lists the caller's bots.
            manager = TurnManager()

            async def runner(turn):
                await asyncio.Event().wait()

            with mock.patch.object(chat_router, "turn_manager", manager):
                turn = manager.start(self.a_bot, self.a_bot, "m", runner)
                self.assertEqual([t["thread_id"] for t in (await chat_router.list_turns())["turns"]], [self.a_bot])
                with owner_context(self.bob["id"]):
                    self.assertEqual((await chat_router.list_turns())["turns"], [])
                turn.task.cancel()

            # Approval cards and question cards answer only to their owner.
            broker = ApprovalBroker()
            call = SimpleNamespace(name="x", summary="s", arguments_for_display={})
            approval = broker.open(self.a_bot, self.a_bot, call)
            with owner_context(self.bob["id"]):
                self.assertFalse(broker.resolve(approval["request_id"], "allow"))
            self.assertTrue(broker.resolve(approval["request_id"], "allow"))
            questions = user_questions.QuestionBroker()
            questions.open("q-1")
            with owner_context(self.bob["id"]):
                self.assertFalse(questions.answer("q-1", "x"))
            self.assertTrue(questions.answer("q-1", "x"))

        asyncio.run(scenario())

    def test_only_admins_change_app_settings(self):
        async def scenario():
            with owner_context(self.bob["id"]):
                with self.assertRaises(HTTPException) as ctx:
                    await settings_router.save_settings(AppSettingsSchema(default_model="mine"), _request(MEMBER))
                self.assertEqual(ctx.exception.status_code, 403)
                saved = await settings_router.save_settings(AppSettingsSchema(auto_approval="shadow"), _request(MEMBER))
                self.assertEqual(saved.auto_approval, "shadow")
            saved = await settings_router.save_settings(AppSettingsSchema(default_model="ours"), _request(OWNER))
            self.assertEqual(saved.default_model, "ours")
            with owner_context(self.bob["id"]):
                self.assertEqual((await settings_router.get_settings()).default_model, "ours")
                self.assertEqual((await settings_router.get_settings()).auto_approval, "shadow")
            self.assertEqual((await settings_router.get_settings()).auto_approval, "off")

        asyncio.run(scenario())

    def test_admin_user_management(self):
        service = mock.Mock()
        service.accounts = self.accounts
        with mock.patch.object(admin_router, "auth_service", service):
            async def scenario():
                with self.assertRaises(HTTPException) as ctx:
                    await admin_router.list_users(_request(MEMBER))
                self.assertEqual(ctx.exception.status_code, 403)
                users = (await admin_router.list_users(_request(OWNER)))["users"]
                self.assertEqual([u["username"] for u in users], ["jamie", "bob"])
                created = (await admin_router.create_user(admin_router.UserCreate(username="carol", password=PASSWORD, role="member"), _request(OWNER)))["user"]
                self.assertEqual((created["username"], created["role"]), ("carol", "member"))
                with self.assertRaises(HTTPException):
                    await admin_router.create_user(admin_router.UserCreate(username="carol", password=PASSWORD), _request(OWNER))
                with self.assertRaises(HTTPException):
                    await admin_router.create_user(admin_router.UserCreate(username="dave", password=PASSWORD, role="god"), _request(OWNER))
                # Disable, promote, reset a password; never the first account or yourself.
                updated = (await admin_router.update_user(created["id"], admin_router.UserUpdate(disabled=True), _request(OWNER)))["user"]
                self.assertTrue(updated["disabled"])
                updated = (await admin_router.update_user(created["id"], admin_router.UserUpdate(disabled=False, role="owner", password="another long password"), _request(OWNER)))["user"]
                self.assertEqual((updated["disabled"], updated["role"]), (False, "owner"))
                self.assertEqual(self.accounts.verify_login("carol", "another long password")["id"], created["id"])
                with self.assertRaises(HTTPException):
                    await admin_router.update_user(LOCAL_USER_ID, admin_router.UserUpdate(disabled=True), _request(OWNER))
                with self.assertRaises(HTTPException):
                    await admin_router.update_user(LOCAL_USER_ID, admin_router.UserUpdate(role="member"), _request(OWNER))
                with self.assertRaises(HTTPException) as ctx:
                    await admin_router.update_user("user-nope", admin_router.UserUpdate(disabled=True), _request(OWNER))
                self.assertEqual(ctx.exception.status_code, 404)

            asyncio.run(scenario())

    def test_scheduled_runs_act_as_their_owner(self):
        past = "2000-01-01T00:00:00+00:00"
        self.root.save_schedule({"id": "sch-a", "bot_id": self.a_bot, "name": "A", "prompt": "Ping A.", "enabled": True, "next_run_at": past, "cadence": {"kind": "once", "at": past}, "created_at": past})
        with owner_context(self.bob["id"]):
            current_storage().save_schedule({"id": "sch-b", "bot_id": self.b_bot, "name": "B", "prompt": "Ping B.", "enabled": True, "next_run_at": past, "cadence": {"kind": "once", "at": past}, "created_at": past})
        due = Scheduler._due()
        self.assertEqual({(d["id"], d["owner_id"]) for d in due}, {("sch-a", LOCAL_USER_ID), ("sch-b", self.bob["id"])})

        seen = []

        async def run_turn(bot_id, message, owner_id=None):
            seen.append((bot_id, owner_id, get_current_owner(), current_storage().owner_id))
            return SimpleNamespace(task=None, status="completed", error=None, pending_approval=None, turn_id="t")

        async def scenario():
            scheduler = Scheduler()
            scheduler._run_turn = run_turn  # one tick, without the background loop
            await scheduler.tick()
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertEqual(sorted(seen), sorted([(self.a_bot, LOCAL_USER_ID, LOCAL_USER_ID, LOCAL_USER_ID), (self.b_bot, self.bob["id"], self.bob["id"], self.bob["id"])]))


if __name__ == "__main__":
    unittest.main()
