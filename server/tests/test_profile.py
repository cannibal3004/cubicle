"""The account's display name and email: stored, served, and told to the bots."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.routers import auth as auth_router
from app.routers import chat as chat_router
from app.services import auth_service as auth_module
from app.services.accounts import AccountError, AccountStore
from app.services.auth_service import AuthService
from app.services.database import Database
from app.services.identity import LOCAL_USER_ID, SESSION_COOKIE
from app.services.secret_store import SecretStore

PASSWORD = "correct horse battery"


def _request(cookie=None):
    return SimpleNamespace(headers={"user-agent": "tests"}, cookies={SESSION_COOKIE: cookie} if cookie else {}, client=SimpleNamespace(host="10.0.0.5"))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = AccountStore(Database(root / "test.sqlite3"), SecretStore(root))
        self.store.complete_setup("jamie", PASSWORD)
        self.service = AuthService(self.store)
        self.patches = [mock.patch.object(auth_router, "auth_service", self.service), mock.patch.object(auth_module, "auth_service", self.service)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_profile_is_stored_validated_and_served_with_the_session(self):
        public = self.store.update_profile(LOCAL_USER_ID, "Jamie  Harrison", "jamie@example.com")
        self.assertEqual((public["display_name"], public["email"]), ("Jamie Harrison", "jamie@example.com"))
        with self.assertRaises(AccountError):
            self.store.update_profile(LOCAL_USER_ID, email="not-an-address")
        self.assertEqual(self.store.update_profile(LOCAL_USER_ID, email="")["email"], "")  # cleared
        self.assertEqual(self.store.update_profile(LOCAL_USER_ID, display_name=None)["display_name"], "Jamie Harrison")  # untouched

        async def scenario():
            session = self.store.create_session(LOCAL_USER_ID)
            me = await auth_router.update_profile(auth_router.ProfileUpdate(display_name="Jamie", email="jamie@example.com"), _request(cookie=session))
            self.assertEqual((me["user"]["display_name"], me["user"]["email"]), ("Jamie", "jamie@example.com"))
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.update_profile(auth_router.ProfileUpdate(email="nope"), _request(cookie=session))
            self.assertEqual(ctx.exception.status_code, 400)
            # The session payload carries it, so the sidebar can greet by name.
            session_payload = await auth_router.establish_session(_request(cookie=session))
            self.assertEqual(session_payload["user"]["display_name"], "Jamie")
            self.assertEqual((await auth_router.me(_request(cookie=session)))["email"], "jamie@example.com")

        asyncio.run(scenario())

    def test_the_bot_is_told_who_it_works_for(self):
        self.assertEqual(chat_router.describe_user(), "")  # nothing set: nothing said
        self.store.update_profile(LOCAL_USER_ID, "Jamie", "jamie@example.com")
        line = chat_router.describe_user()
        self.assertIn("The person you work for is Jamie (account jamie); address them as Jamie.", line)
        self.assertIn("Their email address is jamie@example.com; use it when they ask to be copied", line)
        self.assertTrue(line.endswith("\n\n"))
        self.store.update_profile(LOCAL_USER_ID, "", "jamie@example.com")
        self.assertIn("has the account jamie", chat_router.describe_user())


if __name__ == "__main__":
    unittest.main()
