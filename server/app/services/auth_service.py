"""Who is making a request: a session cookie or a bearer API token.

Accounts, sessions and tokens live in the account store; this layer turns a
request (or a WebSocket's headers and cookies) into a user and manages the
session cookie.
"""

from typing import Any, Dict, Mapping, Optional

from fastapi import Request, Response

from app.config import settings
from app.services.accounts import AccountError, AccountStore
from app.services.identity import LOCAL_USER_ID, SESSION_COOKIE  # noqa: F401 - re-exported
from app.services.storage_service import storage_service


class AuthService:
    def __init__(self, accounts: Optional[AccountStore] = None):
        self.accounts = accounts or AccountStore(storage_service.database, storage_service.secret_store)

    @staticmethod
    def _public(user: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": user["id"], "username": user["username"], "role": user["role"], "display_name": user.get("display_name") or "", "email": user.get("email") or ""}

    def authenticate_credentials(self, authorization: str, session_cookie: Optional[str]) -> Optional[Dict[str, Any]]:
        """The user behind a bearer API token or a session cookie, as the
        request carries it; None when neither is valid."""
        scheme, _, bearer = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and bearer.strip():
            user = self.accounts.token_user(bearer.strip())
            if user:
                return self._public(user)
        user = self.accounts.session_user(session_cookie)
        return self._public(user) if user else None

    def authenticate_request(self, request: Request) -> Optional[Dict[str, Any]]:
        return self.authenticate_credentials(request.headers.get("authorization", ""), request.cookies.get(SESSION_COOKIE))

    def authenticate_websocket(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> Optional[Dict[str, Any]]:
        return self.authenticate_credentials(headers.get("authorization", ""), cookies.get(SESSION_COOKIE))

    def setup_required(self) -> bool:
        return self.accounts.setup_required()

    def login(self, username: str, password: str, code: Optional[str], request: Request) -> Dict[str, Any]:
        """Check the credentials and open a session; raises AccountError with
        a code (invalid, locked, totp_required, totp_invalid, disabled)."""
        user = self.accounts.verify_login(username, password, code)
        return self._open_session(user, request)

    def complete_setup(self, username: str, password: str, request: Request) -> Dict[str, Any]:
        user = self.accounts.complete_setup(username, password)
        return self._open_session(user, request)

    def _open_session(self, user: Dict[str, Any], request: Request) -> Dict[str, Any]:
        session_id = self.accounts.create_session(
            user["id"],
            user_agent=request.headers.get("user-agent", ""),
            address=request.client.host if request.client else "",
        )
        return {"user": self._public(user), "session_id": session_id}

    def logout(self, request: Request) -> None:
        self.accounts.delete_session(request.cookies.get(SESSION_COOKIE))

    def set_session_cookie(self, response: Response, session_id: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=settings.AUTH_SESSION_MAX_AGE,
            httponly=True,
            secure=settings.AUTH_COOKIE_SECURE,
            samesite="lax",
        )

    @staticmethod
    def clear_session_cookie(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax")


auth_service = AuthService()

__all__ = ["AccountError", "AuthService", "auth_service", "LOCAL_USER_ID", "SESSION_COOKIE"]
