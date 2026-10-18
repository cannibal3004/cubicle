"""Who owns what: the identifiers and the per-request owner context shared by
the auth and storage layers.

Kept apart so storage can name the owner of its rows without importing the
authentication machinery, which in turn needs storage.
"""

import contextlib
from contextvars import ContextVar
from typing import Iterator, Optional

# The first account. Everything stored before accounts existed belongs to it,
# and the setup page turns it into the owner's named account.
LOCAL_USER_ID = "local-user"
# App-wide settings (model server, connector key, system MCP servers) are
# kept under this pseudo-owner; admins change them, everyone uses them.
SYSTEM_OWNER = "system"
SESSION_COOKIE = "cubicle_session"
ADMIN_ROLES = {"owner", "admin"}

# The account the current request (or scheduled run) acts for. Set by the
# auth middleware for HTTP requests, by the WebSocket routes after they check
# the session, and by the scheduler around each run. asyncio copies the
# context into tasks it creates, so a turn started by a request keeps its
# owner for its whole life.
_current_owner: ContextVar[Optional[str]] = ContextVar("cubicle_owner", default=None)


def get_current_owner() -> str:
    """The owner id in effect; the first account when nothing set one (tests,
    scripts, one-user installs)."""
    return _current_owner.get() or LOCAL_USER_ID


@contextlib.contextmanager
def owner_context(owner_id: Optional[str]) -> Iterator[None]:
    token = _current_owner.set(owner_id or LOCAL_USER_ID)
    try:
        yield
    finally:
        _current_owner.reset(token)


def set_current_owner(owner_id: Optional[str]):
    """Set the owner for the rest of the current context; returns the token for reset."""
    return _current_owner.set(owner_id or LOCAL_USER_ID)


def reset_current_owner(token) -> None:
    _current_owner.reset(token)


def is_admin(user: Optional[dict]) -> bool:
    return bool(user) and str(user.get("role") or "") in ADMIN_ROLES
