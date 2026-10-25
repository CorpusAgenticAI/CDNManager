"""Passwords, session handles, HMAC, and login sessions.

Redis holds the live session when it is reachable. The login_sessions table is
always written, so a preview without Redis still signs in.
"""

import base64
import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginSession, User, as_utc, utcnow

COOKIE = "cdn_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_hex(32)


def pseudonym(salt: str, subscriber_code: str) -> str:
    """12-character base64url HMAC. Stable per subscriber, different per tenant salt."""
    digest = hmac.new(salt.encode(), subscriber_code.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:12]


def sign_body(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def signature_ok(secret: str, raw: bytes, header: str) -> bool:
    if not header.startswith("sha256=") or len(header) != 7 + 64:
        return False
    expected = sign_body(secret, raw)
    return hmac.compare_digest(expected, header)


def ip_allowed(allowlist: str, ip: str) -> bool:
    """Empty allow list means no IP restriction. Otherwise the address must match an entry."""
    items = [part.strip() for part in (allowlist or "").split(",") if part.strip()]
    if not items:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in items:
        try:
            if "/" in item and address in ipaddress.ip_network(item, strict=False):
                return True
            if "/" not in item and address == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def sign_url_token(secret: str, path: str, expires: str, ip: str) -> str:
    raw = f"{path}|{expires}|{ip}".encode()
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def truncate_ip(ip: str) -> str:
    if ":" in ip:
        pieces = [p for p in ip.split(":") if p]
        return ":".join(pieces[:3]) + "::/48" if pieces else ip
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["0"])
    return ip


class SessionStore:
    def __init__(self, redis_url: str, ttl_hours: int):
        self.redis_url = redis_url
        self.ttl = timedelta(hours=ttl_hours)
        self._redis = None
        self.backend = "database"

    async def connect(self) -> str:
        if not self.redis_url:
            return self.backend
        try:
            import redis.asyncio as redis

            client = redis.from_url(self.redis_url, socket_connect_timeout=0.4)
            await client.ping()
            self._redis = client
            self.backend = "redis"
        except Exception:
            self._redis = None
            self.backend = "database"
        return self.backend

    async def put(self, db: AsyncSession, user_id: str) -> LoginSession:
        row = LoginSession(
            token=new_token(),
            user_id=user_id,
            csrf_token=new_token(),
            expires_at=utcnow() + self.ttl,
        )
        db.add(row)
        await db.flush()
        if self._redis is not None:
            try:
                await self._redis.setex(
                    f"cdn:sess:{row.token}",
                    int(self.ttl.total_seconds()),
                    user_id,
                )
            except Exception:
                self._redis = None
                self.backend = "database"
        return row

    async def get_user(self, db: AsyncSession, token: str | None) -> tuple[User | None, LoginSession | None]:
        if not token:
            return None, None
        now = datetime.now(timezone.utc)
        result = await db.execute(select(LoginSession).where(LoginSession.token == token))
        row = result.scalar_one_or_none()
        if row is None or as_utc(row.expires_at) < now:
            return None, None
        user = await db.get(User, row.user_id)
        if user is None or not user.active:
            return None, None
        return user, row

    async def delete(self, db: AsyncSession, token: str) -> None:
        row = await db.get(LoginSession, token)
        if row is not None:
            await db.delete(row)
        if self._redis is not None:
            try:
                await self._redis.delete(f"cdn:sess:{token}")
            except Exception:
                self._redis = None
                self.backend = "database"
