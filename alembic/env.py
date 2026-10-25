from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.models import Base, UtcDateTime

config = context.config
target_metadata = Base.metadata


def _url():
    """Engine URL from settings, never round-tripped through alembic.ini (it would mangle % in passwords)."""
    override = config.attributes.get("url")
    return override if override is not None else get_settings().sync_database_url()


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline():
    context.configure(
        url=_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=render_item,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
