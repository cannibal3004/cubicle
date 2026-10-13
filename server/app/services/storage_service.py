"""Durable local storage for bots, conversations, approvals, audit, and settings."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.auth_service import LOCAL_USER_ID
from app.services.database import Database
from app.services.secret_store import SecretStore, SecretStoreError


# `muapi_*` keys are the pre-provider-abstraction names. They are still
# accepted so legacy JSON imports and old databases keep working, and are
# copied into the `llm_*` keys once by `_migrate_legacy_llm_settings`.
LEGACY_LLM_KEYS = {"muapi_api_key", "muapi_base_url"}
SECRET_KEYS = {"llm_api_key", "muapi_api_key", "composio_api_key", "composio_key"}
SETTING_KEYS = {
    "llm_provider",
    "llm_api_key",
    "llm_base_url",
    "llm_reasoning_effort",
    "muapi_api_key",
    "muapi_base_url",
    "composio_api_key",
    "composio_key",
    "default_model",
    "theme",
    "disabled_toolkits",
    "auto_approval",
    "decider_url",
    "decider_threshold",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageService:
    """SQLite-backed storage with a one-time import from the legacy JSON files."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = (data_dir or settings.DATA_DIR).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.owner_id = LOCAL_USER_ID

        # Keep these paths for the one-time migration and as human-readable
        # recovery copies. Normal reads and writes use SQLite below.
        self.bots_file = self.data_dir / "bots.json"
        self.messages_file = self.data_dir / "messages.json"
        self.settings_file = self.data_dir / "settings.json"
        self.approvals_file = self.data_dir / "approvals.json"
        self.audit_file = self.data_dir / "audit.json"

        self.db_path = self.data_dir / "cubicle.sqlite3"
        self._adopt_legacy_database()
        self.database = Database(self.db_path)
        self.secret_store = SecretStore(self.data_dir)
        self._migrate_legacy_json()
        self._migrate_legacy_llm_settings()
        self._ensure_defaults()

    @staticmethod
    def _default_settings() -> Dict[str, Any]:
        return {
            "llm_provider": settings.LLM_PROVIDER,
            "llm_api_key": settings.LLM_API_KEY,
            "llm_base_url": settings.LLM_BASE_URL,
            "llm_reasoning_effort": settings.LLM_REASONING_EFFORT,
            "composio_api_key": settings.COMPOSIO_API_KEY,
            "default_model": settings.DEFAULT_MODEL,
            "theme": "dark",
            "auto_approval": settings.AUTO_APPROVAL if settings.AUTO_APPROVAL in {"off", "shadow", "on"} else "off",
            "decider_url": settings.DECIDER_URL,
            "decider_threshold": settings.DECIDER_THRESHOLD,
        }

    def _stored_setting_keys(self) -> set:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT key FROM settings WHERE owner_id = ?", (self.owner_id,)
            ).fetchall()
        return {row[0] for row in rows}

    def _migrate_legacy_llm_settings(self) -> None:
        """Copy a saved MUAPI key into the provider-neutral settings once.

        A database that only has `muapi_api_key` belongs to someone who was
        using MUAPI, so the provider is pinned to `muapi` for them. Fresh
        installs never enter this branch and default to OpenAI-compatible.
        """
        if self.database.get_meta("llm_settings_migrated") == "1":
            return
        stored = self._stored_setting_keys()
        if "llm_api_key" not in stored and "muapi_api_key" in stored:
            current = self.get_settings()
            legacy_key = str(current.get("muapi_api_key") or "")
            if legacy_key:
                migrated = {
                    "llm_provider": "muapi",
                    "llm_api_key": legacy_key,
                    "llm_base_url": str(
                        current.get("muapi_base_url") or "https://api.muapi.ai/api/v1"
                    ),
                }
                self.save_settings(migrated)
        self.database.set_meta("llm_settings_migrated", "1")

    @staticmethod
    def _write_json(path: Path, data: Any):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)

    @staticmethod
    def _read_json(path: Path, default: Any):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return default

    def _count(self, table: str) -> int:
        if table not in {
            "bots",
            "messages",
            "settings",
            "approvals",
            "audit_events",
        }:
            raise ValueError(f"Unsupported storage table: {table}")
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE owner_id = ?", (self.owner_id,)
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _decode_payload(payload: str) -> Optional[Any]:
        try:
            return json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return None

    def _migrate_legacy_json(self) -> None:
        if self.database.get_meta("legacy_json_imported") == "1":
            return

        legacy_bots = self._read_json(self.bots_file, [])
        if self._count("bots") == 0 and isinstance(legacy_bots, list) and legacy_bots:
            self.save_bots(legacy_bots)

        legacy_messages = self._read_json(self.messages_file, [])
        if (
            self._count("messages") == 0
            and isinstance(legacy_messages, list)
            and legacy_messages
        ):
            self.save_messages(legacy_messages)

        legacy_settings = self._read_json(self.settings_file, {})
        if (
            self._count("settings") == 0
            and isinstance(legacy_settings, dict)
            and legacy_settings
        ):
            self.save_settings(legacy_settings)

        legacy_approvals = self._read_json(self.approvals_file, [])
        if (
            self._count("approvals") == 0
            and isinstance(legacy_approvals, list)
            and legacy_approvals
        ):
            for approval in legacy_approvals:
                if isinstance(approval, dict):
                    self.add_approval(approval)

        legacy_audit = self._read_json(self.audit_file, [])
        if (
            self._count("audit_events") == 0
            and isinstance(legacy_audit, list)
            and legacy_audit
        ):
            for event in legacy_audit:
                if isinstance(event, dict):
                    self.add_audit_event(event)

        # The old settings file may contain plaintext credentials. Retain it
        # as a migration copy, but scrub only the credential fields after the
        # encrypted database copy has been written.
        if isinstance(legacy_settings, dict) and self.settings_file.exists():
            sanitized_settings = dict(legacy_settings)
            for key in SECRET_KEYS:
                if key in sanitized_settings:
                    sanitized_settings[key] = ""
            self._write_json(self.settings_file, sanitized_settings)
            os.chmod(self.settings_file, 0o600)

        for legacy_file in (
            self.bots_file,
            self.messages_file,
            self.approvals_file,
            self.audit_file,
        ):
            if legacy_file.exists():
                os.chmod(legacy_file, 0o600)

        self.database.set_meta("legacy_json_imported", "1")

    def _ensure_defaults(self) -> None:
        # MUAPI has its own slugs for these personas. Any other provider seeds
        # every bot with the configured default so the first chat works.
        if settings.LLM_PROVIDER == "muapi":
            seed_models = {"claude": "claude-sonnet-4-5", "codex": "gpt-5-mini"}
        else:
            seed_models = {"claude": settings.DEFAULT_MODEL, "codex": settings.DEFAULT_MODEL}

        if self._count("bots") == 0:
            self.save_bots(
                [
                    {
                        "id": "bot-grok-1",
                        "name": "Grok 4.5 Analyst",
                        "role": "Real-time Reasoning & Intelligence",
                        "description": f"Powered by the {settings.DEFAULT_MODEL} model. Deep analysis, real-time web intelligence, & precise technical problem solving.",
                        "avatar": "🚀",
                        "model": settings.DEFAULT_MODEL,
                        "accent_color": "#3b82f6",
                        "system_prompt": "You are Grok 4.5, an advanced AI analyst. Provide direct, highly accurate, and helpful answers with real-time insight.",
                        "tools": ["web_search", "code_interpreter", "computer_preview"],
                        "pinned": True,
                        "unread_count": 0,
                        "created_at": datetime.now().isoformat(),
                    },
                    {
                        "id": "bot-claude-1",
                        "name": "Claude Architect",
                        "role": "Full-Stack Code & Refactoring",
                        "description": "Specialized in software design, clean architectural patterns, and elegant React/Next.js code.",
                        "avatar": "⚡",
                        "model": seed_models["claude"],
                        "accent_color": "#d97757",
                        "system_prompt": "You are Claude Architect, an expert software engineer. Write clean, modular, modern code.",
                        "tools": ["file_editor", "terminal"],
                        "pinned": True,
                        "unread_count": 0,
                        "created_at": datetime.now().isoformat(),
                    },
                    {
                        "id": "bot-codex-1",
                        "name": "Codex Builder",
                        "role": "Automated Execution & Workflows",
                        "description": "Executes shell tasks, runs automated builds, and manages local computer environment.",
                        "avatar": "💻",
                        "model": seed_models["codex"],
                        "accent_color": "#10b981",
                        "system_prompt": "You are Codex Builder, a devops and execution specialist. Help run commands safely.",
                        "tools": ["terminal", "approval_broker"],
                        "pinned": False,
                        "unread_count": 0,
                        "created_at": datetime.now().isoformat(),
                    },
                    {
                        "id": "bot-supa-1",
                        "name": "Grok Connector Assistant",
                        "role": "General Assistant & App Connector",
                        "description": "Friendly assistant connected to the app marketplace (Slack, Gmail, GitHub).",
                        "avatar": "🐭",
                        "model": settings.DEFAULT_MODEL,
                        "accent_color": "#a855f7",
                        "system_prompt": "You are a super-powered assistant. Be friendly, fast, and proactive.",
                        "tools": ["composio_apps", "dictation"],
                        "pinned": False,
                        "unread_count": 0,
                        "created_at": datetime.now().isoformat(),
                    },
                ]
            )

        if self._count("messages") == 0:
            self.save_messages(
                [
                    {
                        "id": "msg-welcome-1",
                        "thread_id": "bot-grok-1",
                        "bot_id": "bot-grok-1",
                        "sender": "bot",
                        "text": "Hello! I am **Grok 4.5 Analyst**. Ask me anything, or give me a task to analyze!",
                        "created_at": datetime.now().isoformat(),
                        "model": settings.DEFAULT_MODEL,
                        "item_type": "assistant_text",
                    }
                ]
            )

        if self._count("settings") == 0:
            self.save_settings(self._default_settings())

    def _adopt_legacy_database(self) -> None:
        """Rename an open-grok-bot.sqlite3 left by the pre-rename app."""
        if self.db_path.exists():
            return
        legacy = self.data_dir / "open-grok-bot.sqlite3"
        if not legacy.exists():
            return
        for suffix in ("", "-wal", "-shm"):
            source = legacy.with_name(legacy.name + suffix)
            if source.exists():
                source.rename(self.db_path.with_name(self.db_path.name + suffix))

    def get_bots(self) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM bots WHERE owner_id = ? ORDER BY rowid",
                (self.owner_id,),
            ).fetchall()
        return [payload for row in rows if isinstance((payload := self._decode_payload(row[0])), dict)]

    def save_bots(self, bots: List[Dict[str, Any]]):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM bots WHERE owner_id = ?", (self.owner_id,))
            for bot in bots:
                if not isinstance(bot, dict) or not bot.get("id"):
                    continue
                connection.execute(
                    "INSERT OR REPLACE INTO bots(id, owner_id, payload) VALUES (?, ?, ?)",
                    (str(bot["id"]), self.owner_id, json.dumps(bot)),
                )

    def get_messages(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            if thread_id:
                rows = connection.execute(
                    "SELECT payload FROM messages WHERE owner_id = ? AND thread_id = ? ORDER BY rowid",
                    (self.owner_id, thread_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM messages WHERE owner_id = ? ORDER BY rowid",
                    (self.owner_id,),
                ).fetchall()
        return [payload for row in rows if isinstance((payload := self._decode_payload(row[0])), dict)]

    def add_message(self, message: Dict[str, Any]):
        if not isinstance(message, dict):
            return
        message_id = str(message.get("id") or f"msg-{uuid.uuid4().hex}")
        thread_id = str(message.get("thread_id") or "")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO messages(id, thread_id, owner_id, payload) VALUES (?, ?, ?, ?)",
                (message_id, thread_id, self.owner_id, json.dumps(message)),
            )

    # ---- named secrets for bot computers --------------------------------

    def list_secrets(self, scope: str, bot_id: str = "") -> List[Dict[str, Any]]:
        """Names and timestamps only; values stay encrypted in the row."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name, created_at, updated_at FROM secrets WHERE owner_id = ? AND scope = ? AND bot_id = ? ORDER BY name",
                (self.owner_id, scope, bot_id),
            ).fetchall()
        return [{"name": row[0], "created_at": row[1], "updated_at": row[2]} for row in rows]

    def set_secret(self, scope: str, bot_id: str, name: str, value: str) -> Dict[str, Any]:
        now = _now()
        encrypted = self.secret_store.encrypt(value)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO secrets(owner_id, scope, bot_id, name, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_id, scope, bot_id, name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (self.owner_id, scope, bot_id, name, encrypted, now, now),
            )
        return {"name": name, "updated_at": now}

    def delete_secret(self, scope: str, bot_id: str, name: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM secrets WHERE owner_id = ? AND scope = ? AND bot_id = ? AND name = ?",
                (self.owner_id, scope, bot_id, name),
            )
        return cursor.rowcount > 0

    def delete_bot_secrets(self, bot_id: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM secrets WHERE owner_id = ? AND scope = 'bot' AND bot_id = ?", (self.owner_id, bot_id)
            )
        return cursor.rowcount

    def secret_values(self, bot_id: str = "") -> Dict[str, str]:
        """Decrypted app secrets with the bot's own on top (same name: the bot's wins)."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT scope, name, value FROM secrets WHERE owner_id = ? AND (scope = 'app' OR (scope = 'bot' AND bot_id = ?)) "
                "ORDER BY CASE scope WHEN 'app' THEN 0 ELSE 1 END, name",
                (self.owner_id, bot_id),
            ).fetchall()
        values: Dict[str, str] = {}
        for _scope, name, raw in rows:
            try:
                values[name] = self.secret_store.decrypt(raw)
            except SecretStoreError:
                continue  # A rotated key: the secret is unusable until replaced.
        return values

    def delete_messages(self, thread_id: str) -> int:
        """Remove every message in a thread; returns how many were deleted."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE owner_id = ? AND thread_id = ?",
                (self.owner_id, thread_id),
            )
            return cursor.rowcount if cursor.rowcount is not None else 0

    def save_messages(self, messages: List[Dict[str, Any]]):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM messages WHERE owner_id = ?", (self.owner_id,))
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("id") or f"msg-{uuid.uuid4().hex}")
                thread_id = str(message.get("thread_id") or "")
                connection.execute(
                    "INSERT OR REPLACE INTO messages(id, thread_id, owner_id, payload) VALUES (?, ?, ?, ?)",
                    (message_id, thread_id, self.owner_id, json.dumps(message)),
                )

    def get_settings(self) -> Dict[str, Any]:
        values = self._default_settings()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT key, value, is_secret FROM settings WHERE owner_id = ? ORDER BY key",
                (self.owner_id,),
            ).fetchall()

        for row in rows:
            key, raw_value, is_secret = row
            if is_secret:
                try:
                    values[key] = self.secret_store.decrypt(raw_value)
                except SecretStoreError:
                    # A rotated or missing key should not prevent the app from
                    # starting. The user can replace the unavailable secret.
                    values[key] = ""
                continue
            decoded = self._decode_payload(raw_value)
            values[key] = raw_value if decoded is None else decoded
        return values

    def get_public_settings(self) -> Dict[str, Any]:
        values = self.get_settings()
        for key in SECRET_KEYS:
            values[f"{key}_configured"] = bool(values.get(key))
            values[key] = ""
        return values

    def save_settings(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            return
        with self.database.connect() as connection:
            for key in SETTING_KEYS.intersection(data):
                value = data[key]
                if key in SECRET_KEYS:
                    # An empty write is the UI's "keep existing value" signal.
                    if value is None or str(value).strip() == "":
                        continue
                    stored_value = self.secret_store.encrypt(str(value))
                    is_secret = 1
                else:
                    stored_value = json.dumps(value)
                    is_secret = 0
                connection.execute(
                    "INSERT INTO settings(key, owner_id, value, is_secret) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET owner_id = excluded.owner_id, "
                    "value = excluded.value, is_secret = excluded.is_secret",
                    (key, self.owner_id, stored_value, is_secret),
                )

    def get_approvals(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            if thread_id:
                rows = connection.execute(
                    "SELECT payload FROM approvals WHERE owner_id = ? AND thread_id = ? ORDER BY rowid",
                    (self.owner_id, thread_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM approvals WHERE owner_id = ? ORDER BY rowid",
                    (self.owner_id,),
                ).fetchall()
        return [payload for row in rows if isinstance((payload := self._decode_payload(row[0])), dict)]

    def add_approval(self, approval: Dict[str, Any]):
        if not isinstance(approval, dict) or not approval.get("request_id"):
            return
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO approvals(request_id, thread_id, owner_id, payload) VALUES (?, ?, ?, ?)",
                (
                    str(approval["request_id"]),
                    approval.get("thread_id"),
                    self.owner_id,
                    json.dumps(approval),
                ),
            )

    def update_approval(self, request_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM approvals WHERE owner_id = ? AND request_id = ?",
                (self.owner_id, request_id),
            ).fetchone()
            if not row:
                return None
            approval = self._decode_payload(row[0])
            if not isinstance(approval, dict):
                return None
            approval.update(updates)
            connection.execute(
                "UPDATE approvals SET thread_id = ?, owner_id = ?, payload = ? "
                "WHERE owner_id = ? AND request_id = ?",
                (
                    approval.get("thread_id"),
                    self.owner_id,
                    json.dumps(approval),
                    self.owner_id,
                    request_id,
                ),
            )
        return approval

    def get_audit_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM audit_events WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
                (self.owner_id, bounded_limit),
            ).fetchall()
        return [
            payload
            for row in reversed(rows)
            if isinstance((payload := self._decode_payload(row[0])), dict)
        ]

    def add_audit_event(self, event: Dict[str, Any]):
        if not isinstance(event, dict):
            return
        created_at = str(event.get("created_at") or _now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(created_at, owner_id, payload) VALUES (?, ?, ?)",
                (created_at, self.owner_id, json.dumps(event)),
            )

    def add_gate_decision(self, decision: Dict[str, Any]):
        """Record what the auto-approval gate was shown and concluded."""
        if not isinstance(decision, dict) or not decision.get("request_id"):
            return
        created_at = str(decision.get("created_at") or _now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO gate_decisions(request_id, thread_id, owner_id, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(decision["request_id"]), decision.get("thread_id"), self.owner_id, created_at, json.dumps(decision)),
            )

    def update_gate_decision(self, request_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM gate_decisions WHERE owner_id = ? AND request_id = ?",
                (self.owner_id, request_id),
            ).fetchone()
            if not row:
                return None
            decision = self._decode_payload(row[0])
            if not isinstance(decision, dict):
                return None
            decision.update(updates)
            connection.execute(
                "UPDATE gate_decisions SET payload = ? WHERE owner_id = ? AND request_id = ?",
                (json.dumps(decision), self.owner_id, request_id),
            )
        return decision

    def get_gate_decisions(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Most recent gate decisions, oldest first."""
        bounded_limit = max(1, min(limit, 5000))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM gate_decisions WHERE owner_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (self.owner_id, bounded_limit),
            ).fetchall()
        return [
            payload
            for row in reversed(rows)
            if isinstance((payload := self._decode_payload(row[0])), dict)
        ]


storage_service = StorageService()
