"""SQLite connection and migration helpers for local durable state."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Optional


SCHEMA_MIGRATIONS = {
    1: """
        CREATE TABLE IF NOT EXISTS bots (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS approvals (
            request_id TEXT PRIMARY KEY,
            thread_id TEXT,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_thread ON approvals(thread_id);
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_events(created_at);
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            bot_id TEXT,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            status TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS connections (
            id TEXT PRIMARY KEY,
            connector TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            bot_id TEXT,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS storage_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """,
    2: """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO users(id, username, role, created_at)
        VALUES ('local-user', 'local', 'owner', datetime('now'));
    """,
    # One row per action the auto-approval gate scored: what it was shown,
    # what it concluded, and what was finally decided (by it or the user).
    3: """
        CREATE TABLE IF NOT EXISTS gate_decisions (
            request_id TEXT PRIMARY KEY,
            thread_id TEXT,
            owner_id TEXT NOT NULL DEFAULT 'local-user',
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gate_decisions_created_at ON gate_decisions(created_at);
        CREATE INDEX IF NOT EXISTS idx_gate_decisions_owner ON gate_decisions(owner_id);
    """,
    # Named secrets (API keys and the like) for bot computers: app-wide
    # (bot_id '') or per bot. Values are Fernet-encrypted by the secret store.
    4: """
        CREATE TABLE IF NOT EXISTS secrets (
            owner_id TEXT NOT NULL DEFAULT 'local-user',
            scope TEXT NOT NULL,
            bot_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, scope, bot_id, name)
        );
    """,
    # Scheduled tasks: a prompt that runs in a bot's chat on a cadence. The
    # record is a JSON payload like a bot's; bot_id is a column for lookups.
    5: """
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT 'local-user',
            bot_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_bot ON schedules(owner_id, bot_id);
    """,
    # Password login: credentials and two-factor state on the user row,
    # server-side sessions behind the cookie, and API tokens for scripts.
    6: """
        ALTER TABLE users ADD COLUMN password_hash TEXT;
        ALTER TABLE users ADD COLUMN totp_secret TEXT;
        ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE users ADD COLUMN totp_pending TEXT;
        ALTER TABLE users ADD COLUMN recovery_codes TEXT;
        ALTER TABLE users ADD COLUMN totp_last_step INTEGER;
        ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE users ADD COLUMN updated_at TEXT;
        ALTER TABLE users ADD COLUMN last_login_at TEXT;
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            user_agent TEXT,
            address TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
    """,
    # Settings per owner: app-wide keys belong to the pseudo-owner 'system',
    # personal keys to each account. The old table had one row per key.
    7: """
        CREATE TABLE settings_v2 (
            owner_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            is_secret INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (owner_id, key)
        );
        INSERT INTO settings_v2(owner_id, key, value, is_secret)
            SELECT CASE WHEN key IN ('llm_provider', 'llm_api_key', 'llm_base_url', 'llm_reasoning_effort', 'muapi_api_key',
                                     'muapi_base_url', 'composio_api_key', 'composio_key', 'default_model', 'decider_url', 'mcp_servers')
                        THEN 'system' ELSE owner_id END,
                   key, value, is_secret FROM settings;
        DROP TABLE settings;
        ALTER TABLE settings_v2 RENAME TO settings;
    """,
    # The person behind the account, as the bots should address and reach them.
    8: """
        ALTER TABLE users ADD COLUMN display_name TEXT;
        ALTER TABLE users ADD COLUMN email TEXT;
    """,
    # Embeddings: one row per chunk of indexed text, per account (see services/retrieval.py).
    9: """
        CREATE TABLE IF NOT EXISTS vectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL,
            chunk INTEGER NOT NULL,
            text TEXT NOT NULL,
            vector BLOB NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS vectors_owner_kind_ref ON vectors(owner_id, kind, ref);
    """,
    # Undo/redo in a conversation: each row is one rewound span (services/rewind.py).
    10: """
        CREATE TABLE IF NOT EXISTS rewinds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS rewinds_owner_thread ON rewinds(owner_id, thread_id);
    """,
    # A bot's conversations (services/conversations.py) live in the threads table,
    # which was created early on and never used until now.
    11: """
        CREATE INDEX IF NOT EXISTS threads_owner_bot ON threads(owner_id, bot_id);
    """,
    # Projects (services/projects.py): a bot's pieces of work, each with work
    # notes and a log; and what a person wants every bot to know about them.
    12: """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS projects_owner_bot ON projects(owner_id, bot_id);
        CREATE TABLE IF NOT EXISTS project_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS project_log_owner_project ON project_log(owner_id, project_id);
        ALTER TABLE users ADD COLUMN about TEXT;
    """,
}

OWNER_TABLES = (
    "bots",
    "messages",
    "settings",
    "approvals",
    "audit_events",
    "threads",
    "tasks",
    "connections",
    "memories",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Small SQLite wrapper with WAL mode and explicit schema versions."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, sql in sorted(SCHEMA_MIGRATIONS.items()):
                if version in applied:
                    continue
                connection.executescript(sql)
                if version == 2:
                    self._add_owner_columns(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )

    @staticmethod
    def _add_owner_columns(connection: sqlite3.Connection) -> None:
        for table in OWNER_TABLES:
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "owner_id" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local-user'"
                )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner_id)"
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM storage_meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO storage_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
