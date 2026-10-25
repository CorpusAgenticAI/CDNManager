"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncIterator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base


def make_engine(settings: Settings):
    url = settings.async_database_url()
    options = {"pool_pre_ping": True}
    if url.get_backend_name() == "postgresql":
        options.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_recycle=1800)
    engine = create_async_engine(url, **options)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_fk(dbapi_conn, _connection_record):
        if engine.dialect.name == "sqlite":
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _column_type(dialect: str, kind: str) -> str:
    if kind == "bool":
        return "BOOLEAN NOT NULL DEFAULT true" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 1"
    if kind == "text":
        return "TEXT NOT NULL DEFAULT ''"
    return "VARCHAR(36)"


def _ensure_columns(connection) -> None:
    """Add columns introduced after the first local database was created."""
    dialect = connection.dialect.name
    existing_tables = set(inspect(connection).get_table_names())
    additions = {
        "tenants": {
            "signed_urls": _column_type(dialect, "bool"),
            "ip_allowlist": _column_type(dialect, "text"),
        },
        "nodes": {
            "origin_role": "VARCHAR(16) NOT NULL DEFAULT ''",
            "paired_node_id": "VARCHAR(36)",
        },
        "channels": {
            "protocol": "VARCHAR(16) NOT NULL DEFAULT 'hls'",
            "kind": "VARCHAR(16) NOT NULL DEFAULT 'live'",
        },
    }
    for table, columns in additions.items():
        if table not in existing_tables:
            continue
        present = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, ddl in columns.items():
            if name not in present:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)


async def try_singleton_lock(session: AsyncSession, key: int) -> bool:
    """Transaction-scoped Postgres advisory lock, so one Cloud Run instance runs each background tick."""
    if session.bind.dialect.name != "postgresql":
        return True
    result = await session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(result.scalar_one())


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
