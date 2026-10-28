"""Bots shared with teammates.

A bot keeps one owner, who can add other accounts as members or bot admins.
Everyone then uses the same bot: its settings, notes, computer, files,
schedules and projects all stay in the owner's data. A teammate's request
about a shared bot is carried out in the owner's data (the owner context)
while still acting as themselves (the actor context): their conversations
are their own, the bot sees their profile, and they answer their own cards.

Conversations are private to whoever started them, the owner included: a
record with a `user_id` belongs to that teammate, one without belongs to the
owner. Schedules and Cubie's tasks run as the owner.

Until the owner chooses otherwise (next stage), a teammate's turns get none
of the owner's connected apps, account MCP servers or account-wide secrets;
the bot's own secrets and MCP servers are part of the bot and are shared.

What each role may do:

    owner   everything
    admin   everything but deleting the bot and making bot admins
    member  chat, change the bot's settings, notes and schedules, add or
            replace its secrets; not delete secrets, reset the computer,
            manage members or delete the bot

The membership rows live in `bot_members` (migration 13), outside any one
owner's data, since the member needs to find them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import storage_service as storage_module
from app.services.identity import get_current_actor, get_current_owner, set_current_owner

OWNER = "owner"
ADMIN = "admin"
MEMBER = "member"
ROLES = (MEMBER, ADMIN)


class SharingError(ValueError):
    """A sharing change that cannot be made."""


@dataclass(frozen=True)
class Access:
    """How the acting person reaches a bot: as its owner, or through a membership."""

    owner_id: str
    bot_id: str
    role: str

    @property
    def shared(self) -> bool:
        return self.role != OWNER

    @property
    def manages(self) -> bool:
        return self.role in (OWNER, ADMIN)


def _store():
    return storage_module.storage_service


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- membership rows ---------------------------------------------------------------------------


def members(owner_id: str, bot_id: str) -> List[Dict[str, Any]]:
    with _store().database.connect() as connection:
        rows = connection.execute(
            "SELECT member_id, role, added_by, added_at FROM bot_members WHERE owner_id = ? AND bot_id = ? ORDER BY added_at",
            (owner_id, bot_id),
        ).fetchall()
    return [{"user_id": row[0], "role": row[1], "added_by": row[2], "added_at": row[3]} for row in rows]


def memberships(user_id: str) -> List[Dict[str, Any]]:
    """The bots shared with this person: [{owner_id, bot_id, role}]."""
    with _store().database.connect() as connection:
        rows = connection.execute("SELECT owner_id, bot_id, role FROM bot_members WHERE member_id = ? ORDER BY added_at", (user_id,)).fetchall()
    return [{"owner_id": row[0], "bot_id": row[1], "role": row[2]} for row in rows]


def add_member(owner_id: str, bot_id: str, member_id: str, role: str, added_by: str) -> Dict[str, Any]:
    if role not in ROLES:
        raise SharingError("The role is member or admin.")
    if member_id == owner_id:
        raise SharingError("That is the bot's owner.")
    with _store().database.connect() as connection:
        connection.execute(
            "INSERT INTO bot_members(owner_id, bot_id, member_id, role, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, bot_id, member_id) DO UPDATE SET role = excluded.role",
            (owner_id, bot_id, member_id, role, added_by, _now()),
        )
    return next(m for m in members(owner_id, bot_id) if m["user_id"] == member_id)


def remove_member(owner_id: str, bot_id: str, member_id: str) -> bool:
    with _store().database.connect() as connection:
        cursor = connection.execute("DELETE FROM bot_members WHERE owner_id = ? AND bot_id = ? AND member_id = ?", (owner_id, bot_id, member_id))
    return cursor.rowcount > 0


def forget_bot(owner_id: str, bot_id: str) -> None:
    """A deleted bot is shared with nobody."""
    with _store().database.connect() as connection:
        connection.execute("DELETE FROM bot_members WHERE owner_id = ? AND bot_id = ?", (owner_id, bot_id))


# ---- who reaches what --------------------------------------------------------------------------


def _bot_exists(owner_id: str, bot_id: str) -> bool:
    return any(b.get("id") == bot_id for b in _store().for_owner(owner_id).get_bots())


def bot_access(user_id: str, bot_id: str) -> Optional[Access]:
    if not bot_id:
        return None
    if _bot_exists(user_id, bot_id):
        return Access(user_id, bot_id, OWNER)
    for membership in memberships(user_id):
        if membership["bot_id"] == bot_id and _bot_exists(membership["owner_id"], bot_id):
            return Access(membership["owner_id"], bot_id, membership["role"])
    return None


def thread_access(user_id: str, thread_id: str) -> Optional[Access]:
    """The bot behind a conversation the person may open (their own, or the owner's for the owner)."""
    from app.services import conversations

    if not thread_id:
        return None
    candidates = [(user_id, None, OWNER)] + [(m["owner_id"], m["bot_id"], m["role"]) for m in memberships(user_id)]
    for owner_id, bot_id, role in candidates:
        storage = _store().for_owner(owner_id)
        found = conversations.bot_id_of(thread_id, storage)
        if found is None or (bot_id is not None and found != bot_id):
            continue
        record = conversations.get(thread_id, storage) or {}
        if not visible(record, user_id, owner_id):
            return None  # someone else's conversation: it does not exist for them
        return Access(owner_id, found, role)
    return None


def project_access(user_id: str, project_id: str) -> Optional[Access]:
    if not project_id:
        return None
    candidates = [(user_id, None, OWNER)] + [(m["owner_id"], m["bot_id"], m["role"]) for m in memberships(user_id)]
    for owner_id, bot_id, role in candidates:
        project = _store().for_owner(owner_id).get_project(project_id)
        if project is None or (bot_id is not None and project.get("bot_id") != bot_id):
            continue
        if not _bot_exists(owner_id, project.get("bot_id")):
            continue
        return Access(owner_id, project["bot_id"], role)
    return None


def visible(conversation: Dict[str, Any], actor_id: str, owner_id: str) -> bool:
    """A conversation is seen only by whoever started it; the owner's have no user_id."""
    return (conversation.get("user_id") or owner_id) == actor_id


def enter(access: Optional[Access]) -> Optional[Access]:
    """Act in the bot owner's data for the rest of this request (or task)."""
    if access is not None and access.owner_id != get_current_owner():
        set_current_owner(access.owner_id)
    return access


def enter_bot(bot_id: str) -> Optional[Access]:
    return enter(bot_access(get_current_actor(), bot_id))


def enter_thread(thread_id: str) -> Optional[Access]:
    return enter(thread_access(get_current_actor(), thread_id))


def current_access(bot_id: str) -> Access:
    """How the acting person reaches a bot of the current owner (after entering it)."""
    owner_id, actor_id = get_current_owner(), get_current_actor()
    if owner_id == actor_id:
        return Access(owner_id, bot_id, OWNER)
    role = next((m["role"] for m in memberships(actor_id) if m["owner_id"] == owner_id and m["bot_id"] == bot_id), None)
    return Access(owner_id, bot_id, role or MEMBER)


# ---- requests ------------------------------------------------------------------------------------

_BOT_PATH = re.compile(r"^/api/v1/(?:bots|computers|mcp/bots)/([^/]+)")
_THREAD_PATH = re.compile(r"^/api/v1/(?:chat/(?:history|compact|rewind|redo|turns|stream)|conversations)/([^/]+)")
_PROJECT_PATH = re.compile(r"^/api/v1/projects/([^/]+)")

# What a teammate may not do, by role: (method, path pattern, roles allowed).
_LIMITED = (
    ("DELETE", re.compile(r"^/api/v1/bots/[^/]+$"), (OWNER,)),
    ("POST", re.compile(r"^/api/v1/computers/[^/]+/reset$"), (OWNER, ADMIN)),
    ("DELETE", re.compile(r"^/api/v1/bots/[^/]+/secrets/[^/]+$"), (OWNER, ADMIN)),
)


@dataclass(frozen=True)
class Refusal:
    status: int
    detail: str


def route(method: str, path: str, query: Dict[str, str], user_id: str) -> Optional[Refusal]:
    """Point a request about a shared bot at its owner's data, or refuse it.

    Requests naming a bot, conversation or project in the path (or a bot in
    the query) are resolved here; ones that carry the id in their body enter
    it themselves (enter_bot / enter_thread). Nothing found means the route
    answers as it always has (usually 404).
    """
    access: Optional[Access] = None
    match = _THREAD_PATH.match(path)
    if match:
        thread_id = match.group(1)
        access = thread_access(user_id, thread_id)
        if access is None:
            return Refusal(404, "Conversation not found") if _thread_exists_for(user_id, thread_id) else None
    elif _PROJECT_PATH.match(path):
        access = project_access(user_id, _PROJECT_PATH.match(path).group(1))
    elif _BOT_PATH.match(path) and path != "/api/v1/bots":
        access = bot_access(user_id, _BOT_PATH.match(path).group(1))
    elif path.startswith("/api/v1/files") and query.get("bot_id"):
        access = bot_access(user_id, query["bot_id"])
    if access is None:
        return None
    for limited_method, pattern, roles in _LIMITED:
        if method == limited_method and pattern.match(path) and access.role not in roles:
            return Refusal(403, "Only the bot's owner or a bot admin can do that." if ADMIN in roles else "Only the bot's owner can do that.")
    enter(access)
    return None


def _thread_exists_for(user_id: str, thread_id: str) -> bool:
    """Whether the conversation is in data the person reaches, but is someone else's."""
    from app.services import conversations

    owners = {user_id} | {m["owner_id"] for m in memberships(user_id)}
    return any(conversations.bot_id_of(thread_id, _store().for_owner(owner)) is not None for owner in owners)


# ---- what the bot list shows -----------------------------------------------------------------------


def shared_bots(user_id: str) -> List[Dict[str, Any]]:
    """The bots shared with this person, each marked with its owner and the person's role."""
    from app.services.auth_service import auth_service

    found: List[Dict[str, Any]] = []
    for membership in memberships(user_id):
        bot = next((b for b in _store().for_owner(membership["owner_id"]).get_bots() if b.get("id") == membership["bot_id"]), None)
        if bot is None:
            continue
        owner = auth_service.accounts.get_user(membership["owner_id"]) or {}
        found.append({**bot, "pinned": False, "shared": {"owner_id": membership["owner_id"], "owner_name": person_name(owner), "role": membership["role"]}})
    return found


def person_name(user: Dict[str, Any]) -> str:
    return (user.get("display_name") or "").strip() or user.get("username") or "someone"
