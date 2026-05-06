from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from src.storage.database import fetch_user
from src.storage.models import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def current_user(request: Request) -> User | None:
    """Return the logged-in User or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return await fetch_user(int(user_id))


async def require_user(request: Request) -> User:
    """Dependency that requires a logged-in user; raises a redirect to /login."""
    user = await current_user(request)
    if not user:
        raise _LoginRequired()
    request.state.user = user
    return user


async def require_admin(request: Request) -> User:
    user = await require_user(request)
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


class _LoginRequired(Exception):
    pass


def install_login_redirect(app) -> None:
    """Convert _LoginRequired into a 303 redirect to /login."""
    @app.exception_handler(_LoginRequired)
    async def _handler(request: Request, exc: _LoginRequired):  # type: ignore[unused-argument]
        return RedirectResponse("/login", status_code=303)
