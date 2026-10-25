"""Production configuration, first-admin bootstrap, proxy headers, and migrations."""

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import ForwardedClient
from app.models import Base

CLOUD_SQL = {
    "environment": "production",
    "db_user": "cdnmanager",
    "db_password": "p@ss%w0rd/#:x?y",
    "db_name": "cdnmanager",
    "cloud_sql_instance": "proj:asia-south1:cdnmanager-pg",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ENVIRONMENT", "SEED_DEMO", "DEMO_TELEMETRY", "AUTO_CREATE_SCHEMA", "COOKIE_SECURE", "LOG_JSON",
                 "DATABASE_URL", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_HOST", "CLOUD_SQL_INSTANCE",
                 "BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD", "TRUSTED_PROXY_HOPS"):
        monkeypatch.delenv(name, raising=False)


def test_production_turns_off_demo_and_hardens_defaults():
    settings = Settings(_env_file=None, **CLOUD_SQL)
    assert settings.seed_demo is False
    assert settings.demo_telemetry is False
    assert settings.auto_create_schema is False
    assert settings.cookie_secure is True
    assert settings.log_json is True


def test_production_refuses_demo_seed():
    with pytest.raises(ValidationError, match="published passwords"):
        Settings(_env_file=None, seed_demo=True, **CLOUD_SQL)


def test_production_refuses_sqlite():
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(_env_file=None, environment="production")


def test_production_refuses_short_bootstrap_password():
    with pytest.raises(ValidationError, match="12 characters"):
        Settings(_env_file=None, bootstrap_admin_password="short", **CLOUD_SQL)


def test_cloud_sql_url_survives_special_characters_in_password():
    settings = Settings(_env_file=None, **CLOUD_SQL)
    url = settings.async_database_url()
    parsed = make_url(url.render_as_string(hide_password=False))
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.password == CLOUD_SQL["db_password"]
    assert parsed.database == "cdnmanager"
    assert parsed.host is None
    assert parsed.query["host"] == "/cloudsql/proj:asia-south1:cdnmanager-pg"
    assert settings.sync_database_url().drivername == "postgresql+psycopg"


def test_secret_trailing_newline_is_not_part_of_password():
    settings = Settings(_env_file=None, **{**CLOUD_SQL, "db_password": "abc123\n"}, bootstrap_admin_password="first-password-123\r\n")
    assert settings.db_password == "abc123"
    assert settings.bootstrap_admin_password == "first-password-123"


def test_tcp_database_parts():
    settings = Settings(_env_file=None, db_user="u", db_password="p", db_name="d", db_host="10.0.0.5", db_port=6432)
    url = settings.async_database_url()
    assert (url.host, url.port, url.database) == ("10.0.0.5", 6432, "d")


def _client_seen(hops: int, headers: list[tuple[bytes, bytes]]):
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
        seen["scheme"] = scope["scheme"]

    middleware = ForwardedClient(app, hops)
    scope = {"type": "http", "headers": headers, "client": ("169.254.1.1", 5000), "scheme": "http"}
    asyncio.run(middleware(scope, None, None))
    return seen


def test_forwarded_client_ignores_caller_supplied_entries():
    seen = _client_seen(1, [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9"), (b"x-forwarded-proto", b"https")])
    assert seen == {"client": "203.0.113.9", "scheme": "https"}


def test_forwarded_client_off_by_default():
    seen = _client_seen(0, [(b"x-forwarded-for", b"6.6.6.6")])
    assert seen["client"] == "169.254.1.1"


def test_bootstrap_admin_created_once(tmp_path, monkeypatch):
    db_file = tmp_path / "boot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("DEMO_TELEMETRY", "false")
    monkeypatch.setenv("BACKGROUND_TASKS", "false")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "owner")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "first-password-123")
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        response = client.post("/login", data={"username": "owner", "password": "first-password-123"}, follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/readyz").json() == {"status": "ok"}

    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "second-password-456")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        rejected = client.post("/login", data={"username": "owner", "password": "second-password-456"}, follow_redirects=False)
        assert rejected.status_code == 401
    get_settings.cache_clear()


def test_migrations_match_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config("alembic.ini")
    config.attributes["url"] = url
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == [], f"models changed without a migration: {diff}"
