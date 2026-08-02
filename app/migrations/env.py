"""Alembic environment.

URL resolution order, most specific first:

1. A URL set programmatically on the Config — how the test suite points a
   migration run at a throwaway database.
2. ``DATABASE_URL`` — the same variable the application reads, so migrations
   and the runtime cannot disagree about which database they are talking to.
3. The fallback in alembic.ini, which matches the application default.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.storage.schema import metadata

config = context.config

_url_from_environment = os.environ.get("DATABASE_URL")
if _url_from_environment and not config.attributes.get("url_set_by_caller"):
    config.set_main_option("sqlalchemy.url", _url_from_environment)

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites
            # the table instead, so the same migration runs on both engines.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
