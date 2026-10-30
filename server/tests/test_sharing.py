"""Bots shared with teammates: who reaches what, private conversations, roles."""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import chat as chat_router
from app.routers import sharing as sharing_router
from app.schemas.contracts import TurnRequest
from app.services import secrets_service, sharing
from app.services import storage_service as storage_module
from app.services.accounts import AccountStore
from app.services.approval_broker import ApprovalBroker
from app.services.identity import LOCAL_USER_ID, actor_context, owner_context
from app.services.storage_service import StorageService, current_storage

PASSWORD = "correct horse battery"


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = StorageService(Path(self.temp.name))
        self.accounts = AccountStore(self.root.database, self.root.secret_store)
        self.accounts.complete_setup("jamie", PASSWORD)
        self.bob = self.accounts.create_user("bob", PASSWORD)
        self.carol = self.accounts.create_user("carol", PASSWORD)
        auth = SimpleNamespace(accounts=self.accounts)
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.root),
            mock.patch("app.services.auth_service.auth_service", auth),
            mock.patch.object(sharing_router, "auth_service", auth),
            mock.patch.object(chat_router.conversation_titles, "schedule", lambda thread_id: None),
        ]
        for p in self.patches:
            p.start()
        self.bot = self.root.get_bots()[0]["id"]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    @contextlib.contextmanager
    def request(self, user_id, method="GET", path="/api/v1/bots", query=None):
        """What the auth middleware does for one request: that person, routed to a shared bot's owner."""
        with owner_context(user_id), actor_context(user_id):
            refusal = sharing.route(method, path, query or {}, user_id)
            yield refusal

    def test_a_bot_reaches_only_the_people_it_is_shared_with(self):
        async def scenario():
            with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}") as refusal:
                self.assertIsNone(refusal)
                with self.assertRaises(HTTPException):
                    await bots_router.get_bot(self.bot)  # not shared yet: it does not exist for Bob
            sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
            with self.request(self.bob["id"]):
                listed = await bots_router.get_bots()
                shared = [b for b in listed if b.get("shared")]
                self.assertEqual([b["id"] for b in shared], [self.bot])
                self.assertEqual(shared[0]["shared"]["role"], "member")
                self.assertEqual(shared[0]["shared"]["owner_name"], "jamie")
            with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}"):
                seen = await bots_router.get_bot(self.bot)
                self.assertEqual(seen["shared"]["role"], "member")
                self.assertEqual(current_storage().owner_id, LOCAL_USER_ID)  # acting in Jamie's data
            with self.request(self.carol["id"], path=f"/api/v1/bots/{self.bot}"):
                self.assertEqual(current_storage().owner_id, self.carol["id"])

        asyncio.run(scenario())

    def test_the_turns_poll_names_the_bots_each_person_sees(self):
        """An open page polls /chat/turns; a bot shared meanwhile shows up there, so it joins the list."""
        async def scenario():
            with self.request(self.bob["id"], path="/api/v1/chat/turns"):
                await bots_router.get_bots()  # Bob's Cubie
                own = current_storage().bot_ids()
                self.assertEqual((await chat_router.list_turns())["bots"], own)
            sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
            with self.request(self.bob["id"], path="/api/v1/chat/turns"):
                self.assertEqual((await chat_router.list_turns())["bots"], own + [self.bot])
            with self.request(LOCAL_USER_ID, path="/api/v1/chat/turns"):
                self.assertEqual((await chat_router.list_turns())["bots"], [b["id"] for b in self.root.get_bots()])

        asyncio.run(scenario())

    def test_conversations_are_private_to_whoever_started_them(self):
        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        from app.routers import conversations as conversations_router

        async def scenario():
            with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}/conversations"):
                bobs = await conversations_router.list_conversations(self.bot)
            self.assertEqual(len(bobs), 1)  # his first conversation, made for him
            bob_thread = bobs[0]["id"]
            self.assertNotEqual(bob_thread, self.bot)
            self.assertEqual(self.root.get_conversation(bob_thread)["user_id"], self.bob["id"])
            with self.request(LOCAL_USER_ID, path=f"/api/v1/bots/{self.bot}/conversations"):
                jamies = await conversations_router.list_conversations(self.bot)
            self.assertNotIn(bob_thread, [c["id"] for c in jamies])
            self.assertIn(self.bot, [c["id"] for c in jamies])

            # Neither opens the other's; Bob writes in his, as himself, into Jamie's data.
            with self.request(self.bob["id"], path=f"/api/v1/chat/history/{self.bot}") as refusal:
                self.assertEqual(refusal.status, 404)
            with self.request(LOCAL_USER_ID, path=f"/api/v1/chat/history/{bob_thread}") as refusal:
                self.assertEqual(refusal.status, 404)
            with self.request(self.bob["id"], "POST", "/api/v1/chat/send"):
                await chat_router.send_message(TurnRequest(thread_id=bob_thread, bot_id=self.bot, user_text="Hello from Bob"))
                with self.assertRaises(HTTPException):
                    await chat_router.send_message(TurnRequest(thread_id=self.bot, bot_id=self.bot, user_text="Into Jamie's"))
            stored = [m for m in self.root.get_messages(thread_id=bob_thread) if m.get("sender") == "user"]
            self.assertEqual((stored[-1]["text"], stored[-1]["author_id"]), ("Hello from Bob", self.bob["id"]))
            with self.request(self.bob["id"], path=f"/api/v1/chat/history/{bob_thread}") as refusal:
                self.assertIsNone(refusal)
                self.assertEqual((await chat_router.get_history(bob_thread))[-1]["text"], "Hello from Bob")
            # The bot knows who it is talking to.
            self.accounts.update_profile(self.bob["id"], display_name="Bob Smith")
            with self.request(self.bob["id"], path=f"/api/v1/chat/turns/{bob_thread}"):
                described = chat_router.describe_user()
                self.assertIn("Bob Smith", described)
                self.assertIn("You belong to jamie", described)  # his notes are not about Bob

        asyncio.run(scenario())

    def test_what_each_role_may_do(self):
        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        with self.request(self.bob["id"], "DELETE", f"/api/v1/bots/{self.bot}") as refusal:
            self.assertEqual(refusal.status, 403)
        with self.request(self.bob["id"], "POST", f"/api/v1/computers/{self.bot}/reset") as refusal:
            self.assertEqual(refusal.status, 403)
        with self.request(self.bob["id"], "DELETE", f"/api/v1/bots/{self.bot}/secrets/TOKEN") as refusal:
            self.assertEqual(refusal.status, 403)
        with self.request(self.bob["id"], "PUT", f"/api/v1/bots/{self.bot}/secrets/TOKEN") as refusal:
            self.assertIsNone(refusal)
        with self.request(LOCAL_USER_ID, "DELETE", f"/api/v1/bots/{self.bot}") as refusal:
            self.assertIsNone(refusal)

        async def scenario():
            path = f"/api/v1/bots/{self.bot}/members"
            with self.request(self.bob["id"], "POST", path):
                with self.assertRaises(HTTPException) as ctx:
                    await sharing_router.add_member(self.bot, sharing_router.MemberInput(username="carol"))
                self.assertEqual(ctx.exception.status_code, 403)
                # A member changes the bot, but not the owner's pin or archive.
                updated = await bots_router.update_bot(self.bot, bots_router.BotUpdate(pinned=True, archived=True, description="Edited by Bob"))
                self.assertEqual(updated["description"], "Edited by Bob")
            mine = next(b for b in self.root.get_bots() if b["id"] == self.bot)
            self.assertEqual((mine["description"], mine.get("archived")), ("Edited by Bob", False))
            with self.request(LOCAL_USER_ID, "PATCH", path):
                await sharing_router.change_member(self.bot, self.bob["id"], sharing_router.MemberUpdate(role="admin"))
            with self.request(self.bob["id"], "POST", path):
                with self.assertRaises(HTTPException):
                    await sharing_router.add_member(self.bot, sharing_router.MemberInput(username="carol", role="admin"))
                listing = await sharing_router.add_member(self.bot, sharing_router.MemberInput(username="carol"))
            self.assertEqual([(m["username"], m["role"]) for m in listing["members"]], [("bob", "admin"), ("carol", "member")])
            self.assertEqual(listing["owner"]["username"], "jamie")
            with self.request(self.bob["id"], "POST", f"/api/v1/computers/{self.bot}/reset") as refusal:
                self.assertIsNone(refusal)  # bot admins may reset
            # Anyone may leave; the bot is then gone for them.
            with self.request(self.carol["id"], "DELETE", f"{path}/{self.carol['id']}"):
                self.assertEqual((await sharing_router.remove_member(self.bot, "me"))["status"], "left")
            self.assertEqual(sharing.memberships(self.carol["id"]), [])
            with self.request(self.bob["id"]):
                people = await sharing_router.list_people()
            self.assertEqual(sorted(p["username"] for p in people), ["carol", "jamie"])

        asyncio.run(scenario())

    def test_a_teammate_gets_the_bots_secrets_but_not_the_owners_account_ones(self):
        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        secrets_service.set_secret("app", "", "JAMIE_GMAIL_TOKEN", "account-wide", self.root)
        secrets_service.set_secret("bot", self.bot, "NEXTCLOUD_PASSWORD", "for-the-bot", self.root)
        with self.request(LOCAL_USER_ID, path=f"/api/v1/bots/{self.bot}"):
            self.assertEqual(set(secrets_service.environment_for(self.bot)), {"JAMIE_GMAIL_TOKEN", "NEXTCLOUD_PASSWORD"})
        with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}"):
            self.assertEqual(set(secrets_service.environment_for(self.bot)), {"NEXTCLOUD_PASSWORD"})

    def test_cards_are_answered_by_the_person_whose_turn_it_is(self):
        broker = ApprovalBroker()
        call = SimpleNamespace(name="x", summary="s", arguments_for_display={})

        async def scenario():
            # Opened in Bob's turn on Jamie's bot.
            with owner_context(LOCAL_USER_ID), actor_context(self.bob["id"]):
                card = broker.open("cv-bob", self.bot, call)
            with owner_context(self.carol["id"]), actor_context(self.carol["id"]):
                self.assertFalse(broker.resolve(card["request_id"], "allow"))
            # Bob answers from his own session (no bot in the request path).
            with owner_context(self.bob["id"]), actor_context(self.bob["id"]):
                self.assertTrue(broker.resolve(card["request_id"], "allow"))
            self.assertEqual(self.root.get_approvals()[-1]["decided_by_user"], self.bob["id"])

        asyncio.run(scenario())

    # ---- what teammates get (part 2) -------------------------------------------------------------

    def share(self, **settings):
        sharing.save_settings(LOCAL_USER_ID, self.bot, settings)

    def test_only_the_owner_chooses_what_teammates_get(self):
        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.ADMIN, LOCAL_USER_ID)
        secrets_service.set_secret("app", "", "JAMIE_GMAIL_TOKEN", "account-wide", self.root)

        async def scenario():
            path = f"/api/v1/bots/{self.bot}/sharing"
            with self.request(LOCAL_USER_ID, path=path), mock.patch("app.services.composio_service.composio_service.get_api_key", return_value=""):
                seen = await sharing_router.get_share_settings(self.bot)
                self.assertEqual(seen["settings"]["approvals"], sharing.APPROVERS_ADMINS)
                self.assertEqual(seen["options"]["secrets"], ["JAMIE_GMAIL_TOKEN"])
                saved = await sharing_router.save_share_settings(
                    self.bot, sharing_router.ShareSettings(apps={"gmail": "owner", "slack": "own"}, secrets=["JAMIE_GMAIL_TOKEN"], approvals="members")
                )
                self.assertEqual(saved["settings"]["apps"], {"gmail": "owner", "slack": "own"})
                with self.assertRaises(HTTPException) as ctx:
                    await sharing_router.save_share_settings(self.bot, sharing_router.ShareSettings(apps={"gmail": "everyone"}))
                self.assertEqual(ctx.exception.status_code, 422)
            with self.request(self.bob["id"], "PUT", path):
                seen = await sharing_router.get_share_settings(self.bot)
                self.assertIsNone(seen["options"])  # a bot admin sees the choice, not the owner's accounts
                self.assertEqual(seen["settings"]["approvals"], "members")
                with self.assertRaises(HTTPException) as ctx:
                    await sharing_router.save_share_settings(self.bot, sharing_router.ShareSettings())
                self.assertEqual(ctx.exception.status_code, 403)

        asyncio.run(scenario())

    def test_apps_use_the_owners_connection_or_each_persons_own(self):
        from app.services import composio_service as composio_module
        from app.services import connector_tools
        from app.services.action_gateway import ActionInvocation

        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        self.share(apps={"gmail": "owner", "slack": "own"})
        entity = lambda: composio_module.ComposioService(storage=self.root).user_id  # noqa: E731 - whose connections are in use
        jamie, bob = composio_module.DEFAULT_USER_ID, f"cubicle-{self.bob['id']}"

        class Composio:
            # Jamie has gmail, slack and github; Bob has slack.
            connected = {jamie: ["gmail", "slack", "github"], bob: ["slack"]}
            executed = []

            def get_api_key(self):
                return "key"

            async def list_connected_toolkits(self):
                return list(self.connected.get(entity(), []))

            async def allowed_tools(self, toolkit):
                return None

            async def list_tools(self, toolkit, important_only=True, limit=10, tool_slugs=None):
                return [{"slug": f"{toolkit.upper()}_READ", "toolkit": {"slug": toolkit}, "tags": ["readOnlyHint"], "description": "x"}]

            async def execute_tool(self, slug, payload, version=None):
                self.executed.append((slug, entity()))
                return {"ok": True}

        fake = Composio()

        async def scenario():
            with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}"):
                specs = await connector_tools.shared_connector_specs(fake, self.root.get_bots()[0])
                self.assertEqual(sorted(s.name for s in specs), ["gmail_read", "slack_read"])  # not github
                with mock.patch.object(connector_tools, "composio_service", fake):
                    for slug, toolkit in (("GMAIL_READ", "gmail"), ("SLACK_READ", "slack")):
                        call = ActionInvocation(name="x", arguments={"tool": slug, "arguments": {}}, target={"connector": toolkit, "bot_id": self.bot}, preview="")
                        await connector_tools.execute_composio_tool(call)
                    with self.assertRaises(Exception):
                        call = ActionInvocation(name="x", arguments={"tool": "GITHUB_READ", "arguments": {}}, target={"connector": "github", "bot_id": self.bot}, preview="")
                        await connector_tools.execute_composio_tool(call)
                self.assertEqual(fake.executed, [("GMAIL_READ", jamie), ("SLACK_READ", bob)])
                described = chat_router.describe_user(self.root.get_bots()[0])
                self.assertIn("use jamie's accounts: gmail", described)
                self.assertIn("own accounts: slack", described)
            with self.request(LOCAL_USER_ID, path=f"/api/v1/chat/turns/{self.bot}"):
                self.assertEqual(entity(), jamie)

        asyncio.run(scenario())

    def test_shared_account_secrets_and_mcp_servers_reach_teammates_by_name(self):
        from app.services.mcp_service import McpManager, McpStore

        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        secrets_service.set_secret("app", "", "SHARED_TOKEN", "shared-value", self.root)
        secrets_service.set_secret("app", "", "PRIVATE_TOKEN", "private-value", self.root)
        manager = McpManager(store=McpStore(), user_store=McpStore(scope="user"))
        with self.request(LOCAL_USER_ID):
            shared = manager.user_store.add({"name": "shared", "transport": "http", "url": "https://example.com/mcp"})["id"]
            manager.user_store.add({"name": "private", "transport": "http", "url": "https://example.com/other"})
            self.assertEqual(len(manager.records(self.bot)), 2)
        self.share(secrets=["SHARED_TOKEN"], mcp_servers=[shared])
        with self.request(self.bob["id"], path=f"/api/v1/bots/{self.bot}"):
            self.assertEqual(set(secrets_service.environment_for(self.bot)), {"SHARED_TOKEN"})
            self.assertEqual([r["id"] for r in manager.records(self.bot)], [shared])
            self.assertEqual(manager.records(None), [])

    def test_who_may_answer_a_teammates_card(self):
        broker = ApprovalBroker()
        call = SimpleNamespace(name="x", summary="s", arguments_for_display={})
        dave = self.accounts.create_user("dave", PASSWORD)
        for person, role in ((self.bob, sharing.MEMBER), (self.carol, sharing.ADMIN), (dave, sharing.MEMBER)):
            sharing.add_member(LOCAL_USER_ID, self.bot, person["id"], role, LOCAL_USER_ID)

        async def answers(mode):
            """Who of Jamie, Carol (bot admin) and Dave (member) may answer Bob's card under `mode`."""
            self.share(approvals=mode)
            with owner_context(LOCAL_USER_ID), actor_context(self.bob["id"]):
                card = broker.open("cv-bob", self.bot, call)
            who = {}
            for name, person_id in (("jamie", LOCAL_USER_ID), ("carol", self.carol["id"]), ("dave", dave["id"])):
                who[name] = any(c["request_id"] == card["request_id"] for c in broker.waiting_for(person_id))
            self.assertEqual(broker.waiting_for(self.bob["id"]), [])  # his own card is in his conversation
            with owner_context(self.bob["id"]), actor_context(self.bob["id"]):
                self.assertTrue(broker.resolve(card["request_id"], "deny"))
            return who

        async def scenario():
            self.assertEqual(await answers(sharing.APPROVERS_ADMINS), {"jamie": True, "carol": True, "dave": False})
            self.assertEqual(await answers(sharing.APPROVERS_MEMBERS), {"jamie": True, "carol": True, "dave": True})
            self.assertEqual(await answers(sharing.APPROVERS_STARTER), {"jamie": False, "carol": False, "dave": False})
            # A bot admin answers from their own session; the decision lands in Jamie's data.
            self.share(approvals=sharing.APPROVERS_ADMINS)
            with owner_context(LOCAL_USER_ID), actor_context(self.bob["id"]):
                card = broker.open("cv-bob", self.bot, call)
            with owner_context(dave["id"]), actor_context(dave["id"]):
                self.assertFalse(broker.resolve(card["request_id"], "allow"))
            with owner_context(self.carol["id"]), actor_context(self.carol["id"]):
                self.assertTrue(broker.resolve(card["request_id"], "allow"))
            self.assertEqual(self.root.get_approvals()[-1]["decided_by_user"], self.carol["id"])
            # Jamie's own conversations stay his: nobody else answers his cards.
            with owner_context(LOCAL_USER_ID), actor_context(LOCAL_USER_ID):
                card = broker.open(self.bot, self.bot, call)
            self.assertEqual(broker.waiting_for(self.carol["id"]), [])

        asyncio.run(scenario())

    def test_deleting_a_bot_unshares_it(self):
        sharing.add_member(LOCAL_USER_ID, self.bot, self.bob["id"], sharing.MEMBER, LOCAL_USER_ID)
        with self.request(LOCAL_USER_ID, "DELETE", f"/api/v1/bots/{self.bot}"):
            asyncio.run(bots_router.delete_bot(self.bot))
        self.assertEqual(sharing.memberships(self.bob["id"]), [])


if __name__ == "__main__":
    unittest.main()
