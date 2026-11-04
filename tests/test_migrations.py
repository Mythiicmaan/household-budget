from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from budget.models import Base


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"models changed without a migration: {diff}"
