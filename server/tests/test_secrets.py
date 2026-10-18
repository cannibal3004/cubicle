import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import secrets as secrets_router
from app.services import secrets_service
from app.services.llm_tools import available_tools, describe_tools
from app.services.secrets_service import (
    Redactor,
    SecretError,
    environment_for,
    list_for_bot,
    names_for,
    redactor_for,
    set_secret,
    validate_name,
    validate_value,
)
from app.services.storage_service import StorageService
from app.services import storage_service as storage_module


class SecretsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]["id"]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open a moment longer.

    def test_names_and_values_are_validated(self):
        self.assertEqual(validate_name(" github_token "), "GITHUB_TOKEN")
        for bad in ("", "1ABC", "has-dash", "a" * 65, None, "PATH", "COMPUTER_TOKEN"):
            with self.assertRaises(SecretError):
                validate_name(bad)
        self.assertEqual(validate_value("  tok  "), "tok")
        for bad in ("", "   ", "x" * 9000, "nul\x00", 42):
            with self.assertRaises(SecretError):
                validate_value(bad)

    def test_app_and_bot_scopes_merge_with_the_bot_on_top(self):
        set_secret("app", "", "openai_key", "sk-app-value-1234", self.storage)
        set_secret("app", "", "shared", "shared-value", self.storage)
        set_secret("bot", self.bot, "openai_key", "sk-bot-value-5678", self.storage)
        set_secret("bot", self.bot, "only_mine", "mine-value", self.storage)

        # Names and dates only, never values.
        listed = self.storage.list_secrets("app")
        self.assertEqual([e["name"] for e in listed], ["OPENAI_KEY", "SHARED"])
        self.assertNotIn("value", listed[0])

        merged = list_for_bot(self.bot, self.storage)
        self.assertEqual(
            [(e["name"], e["source"]) for e in merged],
            [("ONLY_MINE", "bot"), ("OPENAI_KEY", "bot"), ("SHARED", "app")],
        )
        self.assertTrue(next(e for e in merged if e["name"] == "OPENAI_KEY")["overrides_app"])
        self.assertFalse(next(e for e in merged if e["name"] == "SHARED")["overridden"])

        self.assertEqual(
            environment_for(self.bot, self.storage),
            {"OPENAI_KEY": "sk-bot-value-5678", "SHARED": "shared-value", "ONLY_MINE": "mine-value"},
        )
        self.assertEqual(environment_for("other-bot", self.storage), {"OPENAI_KEY": "sk-app-value-1234", "SHARED": "shared-value"})
        self.assertEqual(names_for(self.bot, self.storage), ["ONLY_MINE", "OPENAI_KEY", "SHARED"])

        # Values are encrypted at rest.
        with self.storage.database.connect() as connection:
            raw = connection.execute("SELECT value FROM secrets WHERE name = 'SHARED'").fetchone()[0]
        self.assertNotIn("shared-value", raw)

        # Replace keeps created_at, delete removes, deleting a bot drops its own only.
        first = self.storage.list_secrets("app")[1]
        set_secret("app", "", "shared", "new-shared-value", self.storage)
        again = self.storage.list_secrets("app")[1]
        self.assertEqual(again["created_at"], first["created_at"])
        self.assertTrue(secrets_service.delete_secret("app", "", "shared", self.storage))
        self.assertFalse(secrets_service.delete_secret("app", "", "shared", self.storage))
        self.assertEqual(self.storage.delete_bot_secrets(self.bot), 2)
        self.assertEqual(environment_for(self.bot, self.storage), {"OPENAI_KEY": "sk-app-value-1234"})

    def test_redactor_replaces_values_longest_first_and_in_json(self):
        redactor = Redactor({"A": "secret-value", "B": "secret-value-longer", "SHORT": "ab", "Q": 'pa"ss'})
        self.assertTrue(redactor)
        self.assertEqual(redactor("x secret-value-longer y secret-value z"), "x [secret B] y [secret A] z")
        self.assertEqual(redactor("ab is too short to matter"), "ab is too short to matter")
        # The JSON-escaped form of a value is caught inside an encoded result.
        self.assertEqual(redactor('{"out": "pa\\"ss"}'), '{"out": "[secret Q]"}')
        self.assertEqual(redactor.apply({"stdout": "secret-value", "n": 3, "list": ["secret-value", 1]}), {"stdout": "[secret A]", "n": 3, "list": ["[secret A]", 1]})
        self.assertEqual(redactor(None), None)
        self.assertFalse(Redactor({}))
        self.assertEqual(Redactor({}).apply({"k": "v"}), {"k": "v"})

        set_secret("app", "", "token", "tok-123456", self.storage)
        self.assertEqual(redactor_for(self.bot, self.storage)("echo tok-123456"), "echo [secret TOKEN]")

    def test_redactor_catches_the_encodings_a_shell_prints(self):
        redactor = Redactor({"KEY": "sk-live-abc123/+="})
        # What `echo $KEY | base64` and `echo -n $KEY | base64` print.
        self.assertEqual(redactor("c2stbGl2ZS1hYmMxMjMvKz0K"), "[secret KEY as base64]")
        self.assertEqual(redactor("out: c2stbGl2ZS1hYmMxMjMvKz0="), "out: [secret KEY as base64]")
        # URL-safe base64, hex as xxd and od print it, and a percent-encoded query string.
        self.assertEqual(redactor("c2stbGl2ZS1hYmMxMjMvKz0=".replace("/", "_").replace("+", "-")), "[secret KEY as base64]")
        self.assertEqual(redactor("736b2d6c6976652d616263313233 2f2b3d".replace(" ", "")), "[secret KEY as hex]")
        self.assertEqual(redactor("736B2D6C6976652D6162633132332F2B3D"), "[secret KEY as hex]")
        self.assertEqual(redactor("GET /v1?key=sk-live-abc123%2F%2B%3D HTTP/1.1"), "GET /v1?key=[secret KEY as url-encoded] HTTP/1.1")
        # The plain value still wins its own label, and unrelated text is untouched.
        self.assertEqual(redactor("sk-live-abc123/+="), "[secret KEY]")
        self.assertEqual(redactor("c2stbGl2ZS1hYmMxMjM="), "c2stbGl2ZS1hYmMxMjM=")
        # A value with no special characters has no separate url-encoded form, and
        # another secret whose plain value equals an encoding keeps its own name.
        both = Redactor({"PLAIN": "YWJjZGVm", "RAW": "abcdef"})
        self.assertEqual(both("YWJjZGVm abcdef"), "[secret PLAIN] [secret RAW]")

    def test_the_model_learns_names_only(self):
        specs = available_tools(computer=True)
        prompt = describe_tools(specs, secret_names=["GITHUB_TOKEN", "OPENAI_KEY"])
        self.assertIn("GITHUB_TOKEN, OPENAI_KEY", prompt)
        self.assertIn("never print", prompt)
        self.assertNotIn("Secrets:", describe_tools(specs))
        self.assertNotIn("Secrets:", describe_tools(available_tools(computer=False), secret_names=["X"]))

    def test_router_sets_lists_and_deletes_without_returning_values(self):
        with mock.patch.object(storage_module, "storage_service", self.storage), mock.patch.object(
            storage_module, "storage_service", self.storage
        ):
            created = asyncio.run(secrets_router.set_app_secret("github_token", secrets_router.SecretValue(value="ghp_x")))
            self.assertEqual((created["name"], created["source"]), ("GITHUB_TOKEN", "app"))
            self.assertNotIn("value", created)
            listed = asyncio.run(secrets_router.list_app_secrets())["secrets"]
            self.assertEqual([e["name"] for e in listed], ["GITHUB_TOKEN"])

            with self.assertRaises(HTTPException) as caught:
                asyncio.run(secrets_router.set_app_secret("bad name", secrets_router.SecretValue(value="x")))
            self.assertEqual(caught.exception.status_code, 422)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(secrets_router.set_bot_secret("missing-bot", "X", secrets_router.SecretValue(value="x")))
            self.assertEqual(caught.exception.status_code, 404)

            asyncio.run(secrets_router.set_bot_secret(self.bot, "github_token", secrets_router.SecretValue(value="ghp_mine")))
            mine = asyncio.run(secrets_router.list_bot_secrets(self.bot))["secrets"]
            self.assertEqual([(e["name"], e["source"]) for e in mine], [("GITHUB_TOKEN", "bot")])
            self.assertEqual(asyncio.run(secrets_router.delete_bot_secret(self.bot, "github_token"))["status"], "ok")
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(secrets_router.delete_bot_secret(self.bot, "github_token"))
            self.assertEqual(caught.exception.status_code, 404)
            inherited = asyncio.run(secrets_router.list_bot_secrets(self.bot))["secrets"]
            self.assertEqual([(e["name"], e["source"]) for e in inherited], [("GITHUB_TOKEN", "app")])
            self.assertEqual(asyncio.run(secrets_router.delete_app_secret("GITHUB_TOKEN"))["name"], "GITHUB_TOKEN")


if __name__ == "__main__":
    unittest.main()
