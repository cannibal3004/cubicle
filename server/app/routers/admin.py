"""Account administration for admins: list, create, change and disable users."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.accounts import AccountError
from app.services.auth_service import auth_service
from app.services.identity import LOCAL_USER_ID, is_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ROLES = ("owner", "member")


class UserCreate(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    role: str = "member"


class UserUpdate(BaseModel):
    role: Optional[str] = None
    disabled: Optional[bool] = None
    password: Optional[str] = None  # a reset: signs the user out everywhere


def require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Only an admin can do that.")
    return user


def _account_error(exc: AccountError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/users")
async def list_users(request: Request):
    require_admin(request)
    return {"users": auth_service.accounts.list_users()}


@router.post("/users")
async def create_user(body: UserCreate, request: Request):
    require_admin(request)
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {', '.join(ROLES)}.")
    try:
        user = auth_service.accounts.create_user(body.username, body.password, role=body.role)
    except AccountError as exc:
        raise _account_error(exc)
    return {"user": auth_service.accounts.public_user(user)}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, request: Request):
    admin = require_admin(request)
    accounts = auth_service.accounts
    user = accounts.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {', '.join(ROLES)}.")
        if user_id in (admin["id"], LOCAL_USER_ID) and body.role != "owner":
            raise HTTPException(status_code=400, detail="The first account and your own stay admins.")
    if body.disabled is not None and user_id in (admin["id"], LOCAL_USER_ID) and body.disabled:
        raise HTTPException(status_code=400, detail="The first account and your own cannot be disabled.")
    try:
        with accounts.database.connect() as connection:
            if body.role is not None:
                connection.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
            if body.disabled is not None:
                connection.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if body.disabled else 0, user_id))
        if body.disabled:
            accounts.delete_user_sessions(user_id)
        if body.password:
            accounts.set_password(user_id, body.password)
    except AccountError as exc:
        raise _account_error(exc)
    return {"user": accounts.public_user(accounts.get_user(user_id))}
