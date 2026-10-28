"""Sharing a bot with teammates: who has it, adding and removing people, their role."""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import sharing
from app.services.auth_service import auth_service
from app.services.identity import get_current_actor, get_current_owner
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1", tags=["sharing"])


class MemberInput(BaseModel):
    username: str = Field(max_length=200)
    role: str = Field(default=sharing.MEMBER, max_length=20)


class MemberUpdate(BaseModel):
    role: str = Field(max_length=20)


def _person(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"user_id": user["id"], "username": user["username"], "name": sharing.person_name(user)}


def _access(bot_id: str) -> sharing.Access:
    """The acting person's access to a bot of the current owner (the request has entered a shared one)."""
    if not any(bot.get("id") == bot_id for bot in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")
    return sharing.current_access(bot_id)


def _listing(bot_id: str, access: sharing.Access) -> Dict[str, Any]:
    owner = auth_service.accounts.get_user(access.owner_id) or {"id": access.owner_id, "username": access.owner_id}
    people: List[Dict[str, Any]] = []
    for member in sharing.members(access.owner_id, bot_id):
        user = auth_service.accounts.get_user(member["user_id"])
        if user is not None:
            people.append({**_person(user), "role": member["role"], "added_at": member["added_at"]})
    return {"owner": _person(owner), "members": people, "you": {"user_id": get_current_actor(), "role": access.role}}


@router.get("/bots/{bot_id}/members")
async def list_members(bot_id: str):
    return _listing(bot_id, _access(bot_id))


@router.post("/bots/{bot_id}/members")
async def add_member(bot_id: str, body: MemberInput):
    """Share the bot with someone: the owner may add members and bot admins, a bot admin members."""
    access = _access(bot_id)
    if not access.manages:
        raise HTTPException(status_code=403, detail="Only the bot's owner or a bot admin can share it.")
    if body.role not in sharing.ROLES:
        raise HTTPException(status_code=422, detail="The role is member or admin.")
    if body.role == sharing.ADMIN and access.role != sharing.OWNER:
        raise HTTPException(status_code=403, detail="Only the bot's owner can make someone a bot admin.")
    user = auth_service.accounts.get_user_by_username(body.username)
    if user is None or user.get("disabled"):
        raise HTTPException(status_code=404, detail=f"There is no account named {body.username.strip()!r}.")
    existing = next((m for m in sharing.members(access.owner_id, bot_id) if m["user_id"] == user["id"]), None)
    if existing is not None and existing["role"] == sharing.ADMIN and access.role != sharing.OWNER:
        raise HTTPException(status_code=403, detail="Only the bot's owner can change a bot admin.")
    try:
        sharing.add_member(access.owner_id, bot_id, user["id"], body.role, get_current_actor())
    except sharing.SharingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _listing(bot_id, access)


@router.patch("/bots/{bot_id}/members/{user_id}")
async def change_member(bot_id: str, user_id: str, body: MemberUpdate):
    access = _access(bot_id)
    if access.role != sharing.OWNER:
        raise HTTPException(status_code=403, detail="Only the bot's owner can change someone's role.")
    if body.role not in sharing.ROLES:
        raise HTTPException(status_code=422, detail="The role is member or admin.")
    if not any(m["user_id"] == user_id for m in sharing.members(access.owner_id, bot_id)):
        raise HTTPException(status_code=404, detail="That person does not have this bot.")
    sharing.add_member(access.owner_id, bot_id, user_id, body.role, get_current_actor())
    return _listing(bot_id, access)


@router.delete("/bots/{bot_id}/members/{user_id}")
async def remove_member(bot_id: str, user_id: str):
    """Stop sharing the bot with someone; anyone may leave a bot shared with them."""
    access = _access(bot_id)
    if user_id == "me":
        user_id = get_current_actor()
    member = next((m for m in sharing.members(access.owner_id, bot_id) if m["user_id"] == user_id), None)
    if member is None:
        raise HTTPException(status_code=404, detail="That person does not have this bot.")
    leaving = user_id == get_current_actor()
    allowed = leaving or access.role == sharing.OWNER or (access.role == sharing.ADMIN and member["role"] == sharing.MEMBER)
    if not allowed:
        raise HTTPException(status_code=403, detail="Only the bot's owner can remove a bot admin." if access.manages else "Only the bot's owner or a bot admin can do that.")
    sharing.remove_member(access.owner_id, bot_id, user_id)
    if leaving:
        return {"status": "left", "bot_id": bot_id}
    return _listing(bot_id, access)


class ShareSettings(BaseModel):
    apps: Dict[str, str] = Field(default_factory=dict)
    mcp_servers: List[str] = Field(default_factory=list)
    secrets: List[str] = Field(default_factory=list)
    approvals: str = Field(default=sharing.APPROVERS_ADMINS, max_length=20)


async def _options() -> Dict[str, Any]:
    """The owner's things a shared bot could use: connected apps, own MCP servers, account secrets."""
    from app.services.composio_service import COMPOSIO_TOOLKIT, composio_service
    from app.services.mcp_service import mcp_manager

    apps: List[str] = []
    if composio_service.get_api_key():
        try:
            apps = sorted(slug for slug in await composio_service.list_connected_toolkits() if slug != COMPOSIO_TOOLKIT)
        except Exception:
            apps = []
    servers = [{"id": record["id"], "name": record.get("name") or record["id"]} for record in mcp_manager.user_store.list()]
    secrets = sorted(entry["name"] for entry in current_storage().list_secrets("app", ""))
    return {"apps": apps, "mcp_servers": servers, "secrets": secrets}


@router.get("/bots/{bot_id}/sharing")
async def get_share_settings(bot_id: str):
    """What teammates' conversations with the bot may use; the owner also gets what there is to choose from."""
    access = _access(bot_id)
    settings = sharing.settings_of(access.owner_id, bot_id)
    if access.role != sharing.OWNER:
        return {"settings": settings, "options": None}
    return {"settings": settings, "options": await _options()}


@router.put("/bots/{bot_id}/sharing")
async def save_share_settings(bot_id: str, body: ShareSettings):
    access = _access(bot_id)
    if access.role != sharing.OWNER:
        raise HTTPException(status_code=403, detail="Only the bot's owner chooses what teammates may use.")
    try:
        settings = sharing.save_settings(access.owner_id, bot_id, body.model_dump())
    except sharing.SharingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"settings": settings}


@router.get("/people")
async def list_people():
    """Accounts on this server one could share a bot with (not oneself, not disabled)."""
    me = get_current_actor()
    return [_person(user) for user in auth_service.accounts.list_users() if user["id"] != me and not user.get("disabled")]
