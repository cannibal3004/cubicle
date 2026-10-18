import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyotp
from fastapi import HTTPException, Response

from app.routers import auth as auth_router
from app.services import accounts as accounts_module
from app.services.accounts import AccountError, AccountStore
from app.services.auth_service import AuthService
from app.services.database import Database
from app.services.identity import LOCAL_USER_ID, SESSION_COOKIE
from app.services.secret_store import SecretStore

PASSWORD = "correct horse battery"


def _request(cookie=None, authorization="", host="10.0.0.5"):
    headers = {"user-agent": "tests"}
    if authorization:
        headers["authorization"] = authorization
    cookies = {SESSION_COOKIE: cookie} if cookie else {}
    return SimpleNamespace(headers=headers, cookies=cookies, client=SimpleNamespace(host=host))


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = AccountStore(Database(root / "test.sqlite3"), SecretStore(root))

    def tearDown(self):
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_setup_turns_the_first_account_into_the_owner(self):
        self.assertTrue(self.store.setup_required())
        with self.assertRaises(AccountError):
            self.store.complete_setup("jamie", "short")
        with self.assertRaises(AccountError):
            self.store.complete_setup("j", PASSWORD)
        user = self.store.complete_setup("jamie", PASSWORD)
        self.assertEqual((user["id"], user["username"], user["role"]), (LOCAL_USER_ID, "jamie", "owner"))
        self.assertFalse(self.store.setup_required())
        with self.assertRaises(AccountError):
            self.store.complete_setup("again", PASSWORD)
        # Case-insensitive lookup, password check, no hash leaks in the public view.
        self.assertEqual(self.store.verify_login("JAMIE", PASSWORD)["id"], LOCAL_USER_ID)
        self.assertNotIn("password_hash", self.store.public_user(user))

    def test_wrong_passwords_lock_the_username(self):
        self.store.complete_setup("jamie", PASSWORD)
        for _ in range(accounts_module.LOCKOUT_FAILURES):
            with self.assertRaises(AccountError) as ctx:
                self.store.verify_login("jamie", "nope nope nope")
            self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(AccountError) as ctx:
            self.store.verify_login("jamie", PASSWORD)  # even the right password waits
        self.assertEqual(ctx.exception.code, "locked")
        self.assertGreater(self.store.locked_for("jamie"), 0)
        # An unknown username counts too, and does not say whether it exists.
        with self.assertRaises(AccountError) as ctx:
            self.store.verify_login("nobody", PASSWORD)
        self.assertEqual(ctx.exception.code, "invalid")

    def test_sessions_expire_and_are_revoked_by_password_changes(self):
        user = self.store.complete_setup("jamie", PASSWORD)
        first = self.store.create_session(user["id"], "browser A")
        second = self.store.create_session(user["id"], "browser B")
        self.assertEqual(self.store.session_user(first)["id"], user["id"])
        self.assertIsNone(self.store.session_user("not-a-session"))
        listed = self.store.list_sessions(user["id"], current=first)
        self.assertEqual([s["current"] for s in listed].count(True), 1)
        self.assertEqual(len(listed), 2)
        # Changing the password keeps the session that did it and drops the rest.
        with self.assertRaises(AccountError):
            self.store.change_password(user["id"], "wrong", "another long password", keep_session=first)
        self.store.change_password(user["id"], PASSWORD, "another long password", keep_session=first)
        self.assertIsNotNone(self.store.session_user(first))
        self.assertIsNone(self.store.session_user(second))
        self.store.delete_session(first)
        self.assertIsNone(self.store.session_user(first))
        # Expiry.
        with mock.patch.object(accounts_module.settings, "AUTH_SESSION_MAX_AGE", 0):
            short = self.store.create_session(user["id"])
        self.assertIsNone(self.store.session_user(short))

    def test_api_tokens_are_shown_once_and_stored_hashed(self):
        user = self.store.complete_setup("jamie", PASSWORD)
        record, token = self.store.create_token(user["id"], "scripts")
        self.assertTrue(token.startswith("cbt_"))
        self.assertEqual(self.store.token_user(token)["id"], user["id"])
        self.assertIsNone(self.store.token_user("cbt_wrong"))
        self.assertIsNone(self.store.token_user(""))
        with self.store.database.connect() as connection:
            stored = connection.execute("SELECT token_hash FROM api_tokens").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual([t["name"] for t in self.store.list_tokens(user["id"])], ["scripts"])
        self.assertTrue(self.store.delete_token(user["id"], record["id"]))
        self.assertIsNone(self.store.token_user(token))
        self.assertFalse(self.store.delete_token(user["id"], record["id"]))

    def test_totp_enrolment_login_replay_and_recovery(self):
        user = self.store.complete_setup("jamie", PASSWORD)
        started = self.store.start_totp(user["id"])
        self.assertIn("otpauth://totp/Cubicle:jamie?", started["otpauth_url"])
        self.assertTrue(started["qr_svg"].startswith("data:image/svg+xml;base64,"))
        totp = pyotp.TOTP(started["secret"])
        with self.assertRaises(AccountError):
            self.store.confirm_totp(user["id"], "000000")
        self.assertFalse(self.store.get_user(user["id"])["totp_enabled"])
        codes = self.store.confirm_totp(user["id"], totp.now())
        self.assertEqual(len(codes), accounts_module.RECOVERY_CODE_COUNT)
        self.assertTrue(self.store.get_user(user["id"])["totp_enabled"])
        # Login now needs a code; the one used at confirmation cannot be replayed.
        with self.assertRaises(AccountError) as ctx:
            self.store.verify_login("jamie", PASSWORD)
        self.assertEqual(ctx.exception.code, "totp_required")
        with self.assertRaises(AccountError) as ctx:
            self.store.verify_login("jamie", PASSWORD, totp.now())
        self.assertEqual(ctx.exception.code, "totp_invalid")
        # A code for the next time step is accepted (within the one-step window).
        next_code = totp.at(int(time.time()) + totp.interval)
        self.assertEqual(self.store.verify_login("jamie", PASSWORD, next_code)["id"], user["id"])
        # Recovery codes work once each.
        self.assertEqual(self.store.verify_login("jamie", PASSWORD, codes[0].upper())["id"], user["id"])
        with self.assertRaises(AccountError):
            self.store.verify_login("jamie", PASSWORD, codes[0])
        self.assertEqual(self.store.recovery_codes_left(self.store.get_user(user["id"])), accounts_module.RECOVERY_CODE_COUNT - 1)
        # The secret is encrypted at rest.
        with self.store.database.connect() as connection:
            raw = connection.execute("SELECT totp_secret, recovery_codes FROM users WHERE id = ?", (user["id"],)).fetchone()
        self.assertNotIn(started["secret"], raw[0])
        self.assertNotIn(codes[1], raw[1] or "")
        # Disabling needs the password.
        with self.assertRaises(AccountError):
            self.store.disable_totp(user["id"], "wrong")
        self.store.disable_totp(user["id"], PASSWORD)
        self.assertEqual(self.store.verify_login("jamie", PASSWORD)["id"], user["id"])

    def test_disabled_accounts_cannot_sign_in_or_use_sessions(self):
        user = self.store.complete_setup("jamie", PASSWORD)
        session = self.store.create_session(user["id"])
        with self.store.database.connect() as connection:
            connection.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user["id"],))
        with self.assertRaises(AccountError) as ctx:
            self.store.verify_login("jamie", PASSWORD)
        self.assertEqual(ctx.exception.code, "disabled")
        self.assertIsNone(self.store.session_user(session))


class AuthRouterTests(unittest.TestCase):
    """The HTTP layer: setup, login with and without 2FA, cookies, tokens."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = AccountStore(Database(root / "test.sqlite3"), SecretStore(root))
        self.service = AuthService(self.store)
        self.patch = mock.patch.object(auth_router, "auth_service", self.service)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def _cookie(self, response):
        header = response.headers.get("set-cookie", "")
        self.assertTrue(header.startswith(f"{SESSION_COOKIE}="), header)
        self.assertIn("HttpOnly", header)
        return header.split(";", 1)[0].split("=", 1)[1]

    def test_setup_then_login_then_logout(self):
        async def scenario():
            status = await auth_router.auth_status(_request())
            self.assertEqual((status["authenticated"], status["setup_required"]), (False, True))
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD), _request(), Response())
            self.assertEqual(ctx.exception.status_code, 409)

            response = Response()
            payload = await auth_router.complete_setup(auth_router.SetupRequest(username="jamie", password=PASSWORD), _request(), response)
            self.assertEqual(payload["user"]["username"], "jamie")
            cookie = self._cookie(response)
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.complete_setup(auth_router.SetupRequest(username="x", password=PASSWORD), _request(), Response())
            self.assertEqual(ctx.exception.status_code, 409)

            # The cookie identifies the user; a made-up one does not.
            self.assertEqual(self.service.authenticate_request(_request(cookie=cookie))["id"], LOCAL_USER_ID)
            self.assertIsNone(self.service.authenticate_request(_request(cookie="forged")))
            session = await auth_router.establish_session(_request(cookie=cookie))
            self.assertTrue(session["authenticated"])
            with self.assertRaises(HTTPException):
                await auth_router.establish_session(_request())

            # Wrong password, then right one.
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.login(auth_router.LoginRequest(username="jamie", password="nope nope nope"), _request(), Response())
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(ctx.exception.detail["reason"], "invalid")
            response = Response()
            await auth_router.login(auth_router.LoginRequest(username="Jamie", password=PASSWORD), _request(), response)
            second = self._cookie(response)
            me = await auth_router.me(_request(cookie=second))
            self.assertEqual((me["username"], me["totp_enabled"], len(me["sessions"])), ("jamie", False, 2))

            # Logout revokes that session only.
            await auth_router.logout(_request(cookie=second), Response())
            self.assertIsNone(self.service.authenticate_request(_request(cookie=second)))
            self.assertIsNotNone(self.service.authenticate_request(_request(cookie=cookie)))
            # Sign out the others from the remaining one.
            await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD), _request(), Response())
            result = await auth_router.sign_out_others(_request(cookie=cookie))
            self.assertEqual(result["signed_out"], 1)

        asyncio.run(scenario())

    def test_two_factor_login_and_api_tokens(self):
        async def scenario():
            response = Response()
            await auth_router.complete_setup(auth_router.SetupRequest(username="jamie", password=PASSWORD), _request(), response)
            cookie = self._cookie(response)
            started = await auth_router.totp_start(_request(cookie=cookie))
            totp = pyotp.TOTP(started["secret"])
            confirmed = await auth_router.totp_confirm(auth_router.TotpCode(code=totp.now()), _request(cookie=cookie))
            self.assertEqual(len(confirmed["recovery_codes"]), accounts_module.RECOVERY_CODE_COUNT)

            with self.assertRaises(HTTPException) as ctx:
                await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD), _request(), Response())
            self.assertEqual((ctx.exception.status_code, ctx.exception.detail["reason"]), (401, "totp_required"))
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD, code="123456"), _request(), Response())
            self.assertEqual(ctx.exception.detail["reason"], "totp_invalid")
            response = Response()
            code = totp.at(int(time.time()) + totp.interval)  # the confirmation used the current step
            payload = await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD, code=code), _request(), response)
            self.assertTrue(payload["authenticated"])

            # API tokens: shown once, usable as a bearer header, listed, revoked.
            created = await auth_router.create_token(auth_router.TokenCreate(name="live tests"), _request(cookie=cookie))
            token = created["token"]
            self.assertEqual(self.service.authenticate_request(_request(authorization=f"Bearer {token}"))["username"], "jamie")
            self.assertIsNotNone(self.service.authenticate_websocket({"authorization": f"Bearer {token}"}, {}))
            self.assertIsNone(self.service.authenticate_websocket({}, {SESSION_COOKIE: "forged"}))
            listed = await auth_router.list_tokens(_request(cookie=cookie))
            self.assertEqual([t["name"] for t in listed["tokens"]], ["live tests"])
            self.assertNotIn("token", listed["tokens"][0])
            await auth_router.delete_token(created["id"], _request(cookie=cookie))
            self.assertIsNone(self.service.authenticate_request(_request(authorization=f"Bearer {token}")))
            with self.assertRaises(HTTPException):
                await auth_router.delete_token(created["id"], _request(cookie=cookie))

            # Disable 2FA with the password; login goes back to password only.
            await auth_router.totp_disable(auth_router.TotpDisable(password=PASSWORD), _request(cookie=cookie))
            await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD), _request(), Response())

        asyncio.run(scenario())

    def test_lockout_is_reported_as_too_many_requests(self):
        async def scenario():
            await auth_router.complete_setup(auth_router.SetupRequest(username="jamie", password=PASSWORD), _request(), Response())
            for _ in range(accounts_module.LOCKOUT_FAILURES):
                with self.assertRaises(HTTPException):
                    await auth_router.login(auth_router.LoginRequest(username="jamie", password="wrong wrong wrong"), _request(), Response())
            with self.assertRaises(HTTPException) as ctx:
                await auth_router.login(auth_router.LoginRequest(username="jamie", password=PASSWORD), _request(), Response())
            self.assertEqual((ctx.exception.status_code, ctx.exception.detail["reason"]), (429, "locked"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
