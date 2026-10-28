"""Accounts: usernames and passwords, sessions, API tokens and optional TOTP.

Passwords are argon2 hashes. A session is a random id in an HttpOnly cookie
with the record (owner, expiry, last use) kept here, so logging out or
changing a password revokes it server side. API tokens are for scripts and
integrations: shown once, stored as a hash, sent as a bearer header. TOTP
secrets and recovery codes are encrypted with the app's secret store key.
"""

import base64
import hashlib
import io
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import settings
from app.services.database import Database
from app.services.identity import LOCAL_USER_ID
from app.services.secret_store import SecretStore

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 200
USERNAME_MIN_LENGTH = 2
USERNAME_MAX_LENGTH = 40
ABOUT_MAX_CHARS = 2000
TOKEN_PREFIX = "cbt_"
RECOVERY_CODE_COUNT = 8
# Lockout: this many failed passwords for one username within the window
# locks that username for the lock period.
LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 5 * 60
# How often a session's or token's "last used" stamp is refreshed.
TOUCH_INTERVAL_SECONDS = 60


class AccountError(ValueError):
    """A credential or account problem; `code` says which."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not USERNAME_MIN_LENGTH <= len(name) <= USERNAME_MAX_LENGTH:
        raise AccountError(f"Usernames are {USERNAME_MIN_LENGTH} to {USERNAME_MAX_LENGTH} characters.", "username")
    if not all(ch.isalnum() or ch in "._-@" for ch in name):
        raise AccountError("Usernames may contain letters, digits, dots, dashes, underscores and @.", "username")
    return name


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise AccountError(f"Passwords need at least {PASSWORD_MIN_LENGTH} characters.", "password")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise AccountError(f"Passwords are at most {PASSWORD_MAX_LENGTH} characters.", "password")
    return password


class AccountStore:
    def __init__(self, database: Database, secret_store: SecretStore):
        self.database = database
        self.secrets = secret_store
        self.hasher = PasswordHasher()
        self._failures: Dict[str, List[float]] = {}
        self._locked_until: Dict[str, float] = {}

    # ---- users ------------------------------------------------------------------

    @staticmethod
    def _row_to_user(row: Any) -> Dict[str, Any]:
        keys = ("id", "username", "role", "created_at", "password_hash", "totp_secret", "totp_enabled",
                "totp_pending", "recovery_codes", "totp_last_step", "disabled", "updated_at", "last_login_at",
                "display_name", "email", "about")
        return dict(zip(keys, row))

    _USER_COLUMNS = ("id, username, role, created_at, password_hash, totp_secret, totp_enabled, totp_pending, "
                     "recovery_codes, totp_last_step, disabled, updated_at, last_login_at, display_name, email, about")

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT {self._USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE lower(username) = lower(?)", ((username or "").strip(),)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(f"SELECT {self._USER_COLUMNS} FROM users ORDER BY created_at").fetchall()
        return [self.public_user(self._row_to_user(row)) for row in rows]

    @staticmethod
    def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "totp_enabled": bool(user.get("totp_enabled")),
            "disabled": bool(user.get("disabled")),
            "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
            "display_name": user.get("display_name") or "",
            "email": user.get("email") or "",
            "about": user.get("about") or "",
        }

    def setup_required(self) -> bool:
        """True until some account has a password: the setup page runs then."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM users WHERE password_hash IS NOT NULL AND password_hash != ''").fetchone()
        return int(row[0]) == 0

    def complete_setup(self, username: str, password: str) -> Dict[str, Any]:
        """Turn the first account into the owner's: everything stored so far is theirs."""
        if not self.setup_required():
            raise AccountError("Setup has already been completed.", "setup_done")
        name = validate_username(username)
        validate_password(password)
        now = _iso(_now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users(id, username, role, created_at) VALUES (?, ?, 'owner', ?)",
                (LOCAL_USER_ID, name, now),
            )
            connection.execute(
                "UPDATE users SET username = ?, role = 'owner', password_hash = ?, disabled = 0, updated_at = ? WHERE id = ?",
                (name, self.hasher.hash(password), now, LOCAL_USER_ID),
            )
        return self.get_user(LOCAL_USER_ID)

    def create_user(self, username: str, password: str, role: str = "member") -> Dict[str, Any]:
        name = validate_username(username)
        validate_password(password)
        if self.get_user_by_username(name):
            raise AccountError("That username is taken.", "username")
        user_id = f"user-{secrets.token_hex(6)}"
        now = _iso(_now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users(id, username, role, created_at, password_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name, role, now, self.hasher.hash(password), now),
            )
        return self.get_user(user_id)

    def update_profile(self, user_id: str, display_name: Optional[str] = None, email: Optional[str] = None, about: Optional[str] = None) -> Dict[str, Any]:
        """The name the person wants to be called, the address to reach them at, and what every bot should know about them."""
        changes = {}
        if about is not None:
            changes["about"] = str(about).replace("\r\n", "\n").strip()[:ABOUT_MAX_CHARS]
        if display_name is not None:
            name = " ".join(str(display_name).split())[:80]
            changes["display_name"] = name
        if email is not None:
            address = str(email).strip()[:200]
            if address and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
                raise AccountError("That does not look like an email address.", "email")
            changes["email"] = address
        if changes:
            with self.database.connect() as connection:
                sets = ", ".join(f"{key} = ?" for key in changes)
                connection.execute(f"UPDATE users SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), _iso(_now()), user_id))
        return self.public_user(self.get_user(user_id))

    def set_password(self, user_id: str, password: str) -> None:
        """Set a password outright (setup, admin reset) and sign the account out everywhere."""
        validate_password(password)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (self.hasher.hash(password), _iso(_now()), user_id),
            )
        self.delete_user_sessions(user_id)

    def change_password(self, user_id: str, current: str, new: str, keep_session: Optional[str] = None) -> None:
        user = self.get_user(user_id)
        if user is None or not self._password_ok(user, current):
            raise AccountError("The current password is wrong.", "password")
        validate_password(new)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (self.hasher.hash(new), _iso(_now()), user_id),
            )
        self.delete_user_sessions(user_id, keep=keep_session)

    def _password_ok(self, user: Dict[str, Any], password: str) -> bool:
        stored = user.get("password_hash") or ""
        if not stored or not isinstance(password, str) or not password:
            return False
        try:
            self.hasher.verify(stored, password)
        except VerifyMismatchError:
            return False
        except Exception:
            return False
        return True

    # ---- login with lockout --------------------------------------------------------

    def _lock_key(self, username: str) -> str:
        return (username or "").strip().lower()

    def locked_for(self, username: str) -> int:
        """Seconds the username stays locked, 0 when it is not."""
        until = self._locked_until.get(self._lock_key(username), 0.0)
        return max(0, int(until - time.time()))

    def _record_failure(self, username: str) -> None:
        key = self._lock_key(username)
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < LOCKOUT_WINDOW_SECONDS]
        recent.append(now)
        self._failures[key] = recent
        if len(recent) >= LOCKOUT_FAILURES:
            self._locked_until[key] = now + LOCKOUT_SECONDS
            self._failures[key] = []

    def _clear_failures(self, username: str) -> None:
        key = self._lock_key(username)
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def verify_login(self, username: str, password: str, code: Optional[str] = None) -> Dict[str, Any]:
        """The user for a username, password and (when enabled) TOTP or recovery code."""
        remaining = self.locked_for(username)
        if remaining:
            raise AccountError(f"Too many failed sign-ins. Try again in {max(1, remaining // 60 + 1)} minute(s).", "locked")
        user = self.get_user_by_username(username)
        if user is None or not self._password_ok(user, password):
            self._record_failure(username)
            raise AccountError("Wrong username or password.", "invalid")
        if user.get("disabled"):
            raise AccountError("This account is disabled.", "disabled")
        if user.get("totp_enabled"):
            if not code:
                raise AccountError("Enter the code from your authenticator app.", "totp_required")
            if not self.verify_totp(user, code):
                self._record_failure(username)
                raise AccountError("That code was not accepted.", "totp_invalid")
        self._clear_failures(username)
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_iso(_now()), user["id"]))
        return user

    # ---- sessions -------------------------------------------------------------------

    def create_session(self, user_id: str, user_agent: str = "", address: str = "") -> str:
        session_id = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(seconds=settings.AUTH_SESSION_MAX_AGE)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(id, user_id, created_at, expires_at, last_seen_at, user_agent, address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_sha256(session_id), user_id, _iso(now), _iso(expires), _iso(now), (user_agent or "")[:300], (address or "")[:100]),
            )
        return session_id

    def session_user(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        key = _sha256(session_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT user_id, expires_at, last_seen_at FROM sessions WHERE id = ?", (key,)).fetchone()
            if row is None:
                return None
            user_id, expires_at, last_seen_at = row
            expires = _parse(expires_at)
            if expires is None or expires <= _now():
                connection.execute("DELETE FROM sessions WHERE id = ?", (key,))
                return None
            seen = _parse(last_seen_at)
            if seen is None or (_now() - seen).total_seconds() > TOUCH_INTERVAL_SECONDS:
                connection.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (_iso(_now()), key))
        user = self.get_user(user_id)
        if user is None or user.get("disabled") or not user.get("password_hash"):
            return None
        return user

    def delete_session(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE id = ?", (_sha256(session_id),))

    def delete_user_sessions(self, user_id: str, keep: Optional[str] = None) -> int:
        with self.database.connect() as connection:
            if keep:
                cursor = connection.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, _sha256(keep)))
            else:
                cursor = connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return cursor.rowcount

    def list_sessions(self, user_id: str, current: Optional[str] = None) -> List[Dict[str, Any]]:
        current_key = _sha256(current) if current else None
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))
            rows = connection.execute(
                "SELECT id, created_at, expires_at, last_seen_at, user_agent, address FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {"created_at": r[1], "expires_at": r[2], "last_seen_at": r[3], "user_agent": r[4], "address": r[5], "current": r[0] == current_key}
            for r in rows
        ]

    # ---- API tokens ------------------------------------------------------------------

    def create_token(self, user_id: str, name: str) -> Tuple[Dict[str, Any], str]:
        label = (name or "").strip()[:80] or "token"
        token = TOKEN_PREFIX + secrets.token_urlsafe(30)
        token_id = f"tok-{secrets.token_hex(5)}"
        now = _iso(_now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO api_tokens(id, user_id, name, token_hash, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (token_id, user_id, label, _sha256(token), now),
            )
        return {"id": token_id, "name": label, "created_at": now, "last_used_at": None}, token

    def token_user(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        key = _sha256(token)
        with self.database.connect() as connection:
            row = connection.execute("SELECT user_id, last_used_at FROM api_tokens WHERE token_hash = ?", (key,)).fetchone()
            if row is None:
                return None
            seen = _parse(row[1])
            if seen is None or (_now() - seen).total_seconds() > TOUCH_INTERVAL_SECONDS:
                connection.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?", (_iso(_now()), key))
        user = self.get_user(row[0])
        if user is None or user.get("disabled"):
            return None
        return user

    def list_tokens(self, user_id: str) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2], "last_used_at": r[3]} for r in rows]

    def delete_token(self, user_id: str, token_id: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM api_tokens WHERE user_id = ? AND id = ?", (user_id, token_id))
            return cursor.rowcount > 0

    # ---- TOTP ---------------------------------------------------------------------------

    def start_totp(self, user_id: str, issuer: str = "Cubicle") -> Dict[str, Any]:
        """A fresh secret, pending until a code from the app confirms it."""
        user = self.get_user(user_id)
        if user is None:
            raise AccountError("No such user.", "invalid")
        secret = pyotp.random_base32()
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET totp_pending = ? WHERE id = ?", (self.secrets.encrypt(secret), user_id))
        url = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name=issuer)
        return {"secret": secret, "otpauth_url": url, "qr_svg": _qr_svg(url)}

    def confirm_totp(self, user_id: str, code: str) -> List[str]:
        """Enable TOTP once a code proves the app has the pending secret; returns the recovery codes."""
        user = self.get_user(user_id)
        if user is None or not user.get("totp_pending"):
            raise AccountError("Start two-factor setup first.", "totp_not_started")
        secret = self.secrets.decrypt(user["totp_pending"])
        step = _totp_step(secret, code)
        if step is None:
            raise AccountError("That code was not accepted. Check the app's time and try the next code.", "totp_invalid")
        codes = [secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT)]
        stored = json.dumps([_sha256(c) for c in codes])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET totp_secret = ?, totp_enabled = 1, totp_pending = NULL, recovery_codes = ?, totp_last_step = ?, updated_at = ? WHERE id = ?",
                (user["totp_pending"], self.secrets.encrypt(stored), step, _iso(_now()), user_id),
            )
        return codes

    def disable_totp(self, user_id: str, password: str) -> None:
        user = self.get_user(user_id)
        if user is None or not self._password_ok(user, password):
            raise AccountError("The password is wrong.", "password")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_pending = NULL, recovery_codes = NULL, totp_last_step = NULL, updated_at = ? WHERE id = ?",
                (_iso(_now()), user_id),
            )

    def verify_totp(self, user: Dict[str, Any], code: str) -> bool:
        """A current authenticator code (each time step accepted once) or an unused recovery code."""
        cleaned = (code or "").strip().replace(" ", "").replace("-", "")
        if not cleaned or not user.get("totp_secret"):
            return False
        secret = self.secrets.decrypt(user["totp_secret"])
        step = _totp_step(secret, cleaned)
        if step is not None:
            if user.get("totp_last_step") is not None and int(user["totp_last_step"]) >= step:
                return False  # a code already used for this (or a later) time step
            with self.database.connect() as connection:
                connection.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
            return True
        try:
            hashes = json.loads(self.secrets.decrypt(user["recovery_codes"])) if user.get("recovery_codes") else []
        except Exception:
            hashes = []
        digest = _sha256(cleaned.lower())
        if digest in hashes:
            hashes.remove(digest)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE users SET recovery_codes = ? WHERE id = ?", (self.secrets.encrypt(json.dumps(hashes)), user["id"])
                )
            return True
        return False

    def recovery_codes_left(self, user: Dict[str, Any]) -> int:
        try:
            return len(json.loads(self.secrets.decrypt(user["recovery_codes"]))) if user.get("recovery_codes") else 0
        except Exception:
            return 0


def _totp_step(secret: str, code: str) -> Optional[int]:
    """The time step a code belongs to (now, or one either side), else None."""
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (0, -1, 1):
        at = now + offset * totp.interval
        if totp.verify(code, for_time=at, valid_window=0):
            return at // totp.interval
    return None


def _qr_svg(url: str) -> str:
    """The QR code as an SVG data URL, for the enrolment screen."""
    try:
        import segno
    except ImportError:  # pragma: no cover - the dependency is pinned
        return ""
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="svg", scale=5, dark="#111114", light="#ffffff", border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
