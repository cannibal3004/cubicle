"""Sign-in, first-run setup, and the signed-in user's own account."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services.accounts import AccountError
from app.services.auth_service import SESSION_COOKIE, auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SetupRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    code: Optional[str] = None  # authenticator or recovery code, when 2FA is on


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class TotpCode(BaseModel):
    code: str


class TotpDisable(BaseModel):
    password: str


class TokenCreate(BaseModel):
    name: str = ""


def _account_error(exc: AccountError) -> HTTPException:
    status = {"locked": 429, "totp_required": 401, "totp_invalid": 401, "invalid": 401, "disabled": 403}.get(exc.code, 400)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(status_code=status, detail={"message": str(exc), "reason": exc.code}, headers=headers)


def _current_user(request: Request):
    user = auth_service.authenticate_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication is required.", headers={"WWW-Authenticate": "Bearer"})
    return user


def _session_payload(user) -> dict:
    return {"authenticated": True, "user": user}


@router.get("/status")
async def auth_status(request: Request):
    user = auth_service.authenticate_request(request)
    return {
        "auth_required": True,
        "authenticated": bool(user),
        "setup_required": auth_service.setup_required(),
        "user": user,
    }


@router.get("/session")
async def establish_session(request: Request):
    """The client's first call: who the cookie belongs to, or 401."""
    return _session_payload(_current_user(request))


@router.post("/setup")
async def complete_setup(body: SetupRequest, request: Request, response: Response):
    """Create the owner's account on first run. Refused once an account exists."""
    if not auth_service.setup_required():
        raise HTTPException(status_code=409, detail="Setup has already been completed; sign in instead.")
    try:
        opened = auth_service.complete_setup(body.username, body.password, request)
    except AccountError as exc:
        raise _account_error(exc)
    auth_service.set_session_cookie(response, opened["session_id"])
    return _session_payload(opened["user"])


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    if auth_service.setup_required():
        raise HTTPException(status_code=409, detail="No account exists yet; complete setup first.")
    try:
        opened = auth_service.login(body.username, body.password, body.code, request)
    except AccountError as exc:
        raise _account_error(exc)
    auth_service.set_session_cookie(response, opened["session_id"])
    return _session_payload(opened["user"])


@router.post("/logout")
async def logout(request: Request, response: Response):
    auth_service.logout(request)
    auth_service.clear_session_cookie(response)
    return {"authenticated": False}


# ---- the signed-in user's account -----------------------------------------------------

@router.get("/me")
async def me(request: Request):
    user = _current_user(request)
    record = auth_service.accounts.get_user(user["id"])
    return {
        **auth_service.accounts.public_user(record),
        "recovery_codes_left": auth_service.accounts.recovery_codes_left(record),
        "sessions": auth_service.accounts.list_sessions(user["id"], request.cookies.get(SESSION_COOKIE)),
        "tokens": auth_service.accounts.list_tokens(user["id"]),
    }


@router.post("/password")
async def change_password(body: PasswordChange, request: Request):
    user = _current_user(request)
    try:
        auth_service.accounts.change_password(user["id"], body.current_password, body.new_password, keep_session=request.cookies.get(SESSION_COOKIE))
    except AccountError as exc:
        raise _account_error(exc)
    return {"status": "ok", "note": "Other sessions have been signed out."}


@router.post("/sessions/sign-out-others")
async def sign_out_others(request: Request):
    user = _current_user(request)
    removed = auth_service.accounts.delete_user_sessions(user["id"], keep=request.cookies.get(SESSION_COOKIE))
    return {"status": "ok", "signed_out": removed}


@router.post("/totp/start")
async def totp_start(request: Request):
    user = _current_user(request)
    return auth_service.accounts.start_totp(user["id"])


@router.post("/totp/confirm")
async def totp_confirm(body: TotpCode, request: Request):
    user = _current_user(request)
    try:
        codes = auth_service.accounts.confirm_totp(user["id"], body.code)
    except AccountError as exc:
        raise _account_error(exc)
    return {"status": "ok", "recovery_codes": codes}


@router.post("/totp/disable")
async def totp_disable(body: TotpDisable, request: Request):
    user = _current_user(request)
    try:
        auth_service.accounts.disable_totp(user["id"], body.password)
    except AccountError as exc:
        raise _account_error(exc)
    return {"status": "ok"}


@router.get("/tokens")
async def list_tokens(request: Request):
    user = _current_user(request)
    return {"tokens": auth_service.accounts.list_tokens(user["id"])}


@router.post("/tokens")
async def create_token(body: TokenCreate, request: Request):
    user = _current_user(request)
    record, token = auth_service.accounts.create_token(user["id"], body.name)
    # The only time the token is shown; the store keeps a hash.
    return {"token": token, **record}


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: str, request: Request):
    user = _current_user(request)
    if not auth_service.accounts.delete_token(user["id"], token_id):
        raise HTTPException(status_code=404, detail="No such token.")
    return {"status": "ok"}
