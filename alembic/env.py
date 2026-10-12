"""Alembic configuration shared by the offline and PostgreSQL deployments."""

from alembic import context

from greek_news.config import get_settings
from greek_news.db import engine_for
from greek_news import models


database_url = get_settings().database_url
target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url, target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine_for(database_url).connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            compare_type=True, render_as_batch=database_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()