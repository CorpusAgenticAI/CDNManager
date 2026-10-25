"""HTTP dependencies shared by the console."""

import hmac

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginSession, User
from app.security import COOKIE


async def db_session(request: Request) -> AsyncSession:
    return request.state.db


async def load_user(request: Request, db: AsyncSession) -> tuple[User | None, LoginSession | None]:
    token = request.cookies.get(COOKIE)
    return await request.app.state.store.get_user(db, token)


def csrf_ok(request: Request, login: LoginSession, form: dict) -> bool:
    sent = str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
    if not sent:
        return False
    return hmac.compare_digest(sent, login.csrf_token)
