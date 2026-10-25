"""Application entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_sessionmaker, try_singleton_lock
from app.demo import push_sample
from app.ingest import mark_offline_nodes
from app.models import Node
from app.ops import align_estate, dispatch_webhooks, finalize_periods, run_retention
from app.seed import bootstrap_admin, seed_demo
from app.security import SessionStore
from app.web.api import router as api_router
from app.web.pages import router as pages_router

log = structlog.get_logger()

LOCK_STARTUP = 7301
LOCK_OFFLINE = 7302
LOCK_RETENTION = 7303
LOCK_DEMO = 7304
LOCK_BOOTSTRAP = 7305


def _cloud_severity(_logger, method_name, event_dict):
    event_dict["severity"] = event_dict.pop("level", method_name).upper()
    return event_dict


def configure_logging(settings: Settings) -> None:
    if settings.log_json:
        processors = [
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", key="time"),
            _cloud_severity,
            structlog.processors.EventRenamer("message"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [structlog.processors.TimeStamper(fmt="iso"), structlog.dev.ConsoleRenderer()]
    structlog.configure(processors=processors)
    logging.basicConfig(level=logging.INFO, format="%(message)s")


class ForwardedClient:
    """Take the client address from X-Forwarded-For, counting trusted proxy hops from the right.

    Entries to the left of the trusted hops are caller-supplied and can be forged, so the
    tenant IP allow list must never read them.
    """

    def __init__(self, app, hops: int):
        self.app = app
        self.hops = hops

    async def __call__(self, scope, receive, send):
        if self.hops and scope["type"] == "http":
            forwarded = [value.decode("latin-1") for name, value in scope["headers"] if name == b"x-forwarded-for"]
            chain = [part.strip() for part in ",".join(forwarded).split(",") if part.strip()]
            if len(chain) >= self.hops:
                scope = dict(scope, client=(chain[-self.hops], 0))
            proto = next((value.decode("latin-1") for name, value in scope["headers"] if name == b"x-forwarded-proto"), "")
            if proto in ("http", "https"):
                scope = dict(scope, scheme=proto)
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_sessionmaker(engine)
    store = SessionStore(settings.redis_url, settings.session_ttl_hours)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings)
        log.info("starting", environment=settings.environment, database=engine.dialect.name)
        if settings.auto_create_schema:
            await init_db(engine)
        backend = await store.connect()
        log.info("sessions", backend=backend)
        async with factory() as session:
            if await try_singleton_lock(session, LOCK_BOOTSTRAP):
                if await bootstrap_admin(session, settings):
                    log.info("bootstrap_admin_created", username=settings.bootstrap_admin_username)
                if settings.seed_demo:
                    await seed_demo(session, settings)
            await session.commit()
        try:
            await _startup_maintenance(factory)
        except Exception as exc:
            log.warning("startup_maintenance", error=str(exc))
        stop = asyncio.Event()
        tasks = []
        if settings.background_tasks:
            tasks.append(asyncio.create_task(_offline_loop(factory, settings.offline_after_s, stop)))
            tasks.append(asyncio.create_task(_retention_loop(factory, stop)))
            if settings.demo_telemetry:
                tasks.append(asyncio.create_task(_demo_loop(factory, settings, stop)))
        yield
        stop.set()
        for task in tasks:
            task.cancel()
        await engine.dispose()

    app = FastAPI(
        title="CDN Manager",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = factory
    app.state.store = store

    @app.middleware("http")
    async def db_transaction(request, call_next):
        async with factory() as session:
            request.state.db = session
            try:
                response = await call_next(request)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return response

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.add_middleware(ForwardedClient, hops=settings.trusted_proxy_hops)

    app.include_router(pages_router)
    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("readyz", error=str(exc))
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.exception_handler(404)
    async def _missing(_request, _exc):
        return JSONResponse({"error": "not found"}, status_code=404)

    return app


async def _startup_maintenance(factory) -> None:
    async with factory() as session:
        if not await try_singleton_lock(session, LOCK_STARTUP):
            return
        await align_estate(session)
        await run_retention(session)
        periods = await finalize_periods(session)
        await session.commit()
        for period in periods:
            await dispatch_webhooks(
                session,
                "usage.period_finalised",
                {"period_id": period.id, "tenant_id": period.tenant_id, "gap": period.gap, "body_bytes": period.body_bytes},
            )
        await session.commit()


async def _offline_loop(factory, offline_after_s: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with factory() as session:
                if await try_singleton_lock(session, LOCK_OFFLINE):
                    opened = await mark_offline_nodes(session, offline_after_s)
                    await session.commit()
                    for alert in opened:
                        await dispatch_webhooks(
                            session,
                            "alert.opened",
                            {"alert_id": alert.id, "type": alert.type, "severity": alert.severity, "message": alert.message},
                        )
                        await session.commit()
        except Exception as exc:
            log.warning("offline_loop", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            continue


async def _demo_loop(factory, settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with factory() as session:
                if await try_singleton_lock(session, LOCK_DEMO):
                    result = await session.execute(select(Node).where(Node.simulate.is_(True), Node.enabled.is_(True)))
                    for node in result.scalars():
                        status, body = await push_sample(
                            session,
                            settings,
                            node,
                            datetime.now(timezone.utc),
                            interval=15,
                            include_slow=(node.last_sequence % 4 == 0),
                        )
                        if status >= 400:
                            log.warning("demo_push", node=node.name, status=status, body=body)
                    await session.commit()
        except Exception as exc:
            log.warning("demo_loop", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            continue


async def _retention_loop(factory, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            async with factory() as session:
                if not await try_singleton_lock(session, LOCK_RETENTION):
                    continue
                await run_retention(session)
                periods = await finalize_periods(session)
                await session.commit()
                for period in periods:
                    await dispatch_webhooks(
                        session,
                        "usage.period_finalised",
                        {
                            "period_id": period.id,
                            "tenant_id": period.tenant_id,
                            "gap": period.gap,
                            "body_bytes": period.body_bytes,
                        },
                    )
                await session.commit()
        except Exception as exc:
            log.warning("retention_loop", error=str(exc))


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False, proxy_headers=False)
