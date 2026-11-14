"""Alembic runs every migration as the owner role, with all the app's models loaded.

The owner role owns every schema and table. The app itself never connects as the owner,
because owners skip row-level security (see db/init/01-roles.sh).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import ask_teach_chat.models
import eval_lab.models
import go_live.models
import knowledge_brain.models
import self_improve.models
from core.config import get_settings
from core.models import SCHEMAS, Base

# Import every feature's models so Base.metadata knows every table
_ = (knowledge_brain.models, ask_teach_chat.models, eval_lab.models, self_improve.models)
_ = go_live.models

config = context.config
if config.config_file_name is not None:
    # Keep loggers that already exist (pytest's, uvicorn's) working
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Compare only our own schemas; `public` holds the extensions and the job queue."""
    if type_ == "schema":
        return name in SCHEMAS
    return True


def run_migrations(connection: Connection) -> None:
    # Alembic keeps its version table in `core`, so that schema must exist before anything
    # else; the first migration creates the other schemas
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS core")
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table_schema="core",
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # search_path=public makes Postgres print every object with its schema (core.users, not
    # users), so the models and the database compare by full name
    engine = create_async_engine(
        get_settings().database_url("owner"),
        connect_args={"options": "-c search_path=public"},
    )
    async with engine.connect() as connection:
        # Alembic's API is synchronous, so it runs inside the async connection's run_sync
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Migrations run against a live database only; --sql mode isn't set up.")

asyncio.run(run_async_migrations())
