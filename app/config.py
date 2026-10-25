"""Runtime settings, read from the environment and an optional .env file."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./cdnmanager.db"
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""
    db_host: str = ""
    db_port: int = 5432
    cloud_sql_instance: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 5
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-only-change-me"
    port: int = 8080
    seed_demo: bool = True
    demo_telemetry: bool = True
    background_tasks: bool = True
    auto_create_schema: bool = True
    cookie_secure: bool = False
    log_json: bool = False
    trusted_proxy_hops: int = 0
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    session_ttl_hours: int = 8
    telemetry_max_skew_s: int = 300
    telemetry_max_inflated_bytes: int = 8_388_608
    telemetry_max_raw_bytes: int = 2_000_000
    offline_after_s: int = 45
    usage_bucket_s: int = 300

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @model_validator(mode="after")
    def _production_defaults(self):
        if not self.is_production:
            return self
        explicit = self.model_fields_set
        for name, value in (("seed_demo", False), ("demo_telemetry", False), ("auto_create_schema", False), ("cookie_secure", True), ("log_json", True)):
            if name not in explicit:
                setattr(self, name, value)
        if self.seed_demo or self.demo_telemetry:
            raise ValueError("SEED_DEMO and DEMO_TELEMETRY create accounts with published passwords; they cannot be on in production")
        if self.async_database_url().get_backend_name() == "sqlite":
            raise ValueError("production needs PostgreSQL; set DB_USER, DB_PASSWORD, DB_NAME and CLOUD_SQL_INSTANCE or DB_HOST")
        if self.bootstrap_admin_password and len(self.bootstrap_admin_password) < 12:
            raise ValueError("BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters")
        return self

    def async_database_url(self) -> URL:
        """DB_* parts win over DATABASE_URL, so a password never has to be URL-escaped by hand."""
        if self.db_name and (self.cloud_sql_instance or self.db_host):
            query = {"host": f"/cloudsql/{self.cloud_sql_instance}"} if self.cloud_sql_instance else {}
            return URL.create(
                "postgresql+asyncpg",
                username=self.db_user or None,
                password=self.db_password or None,
                host=None if self.cloud_sql_instance else self.db_host,
                port=None if self.cloud_sql_instance else self.db_port,
                database=self.db_name,
                query=query,
            )
        return make_url(self.database_url)

    def sync_database_url(self) -> URL:
        """Alembic uses a synchronous driver."""
        url = self.async_database_url()
        driver = {"sqlite+aiosqlite": "sqlite", "postgresql+asyncpg": "postgresql+psycopg"}.get(url.drivername, url.drivername)
        return url.set(drivername=driver)


@lru_cache
def get_settings() -> Settings:
    return Settings()
