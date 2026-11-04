"""Alembic environment. The engine is passed in by budget.db.migrate()."""

from alembic import context
from sqlalchemy import create_engine

from budget.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations() -> None:
    engine = config.attributes.get("engine")
    if engine is None:
        engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


run_migrations()
