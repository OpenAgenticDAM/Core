"""Alembic environment: URL comes from openagenticdam.config, never from alembic.ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from openagenticdam.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

URL = Settings().sqlalchemy_url


def run_migrations_offline() -> None:
    context.configure(url=URL, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
