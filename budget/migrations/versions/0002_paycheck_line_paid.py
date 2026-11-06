"""paycheck line paid status

Revision ID: 1bb4194803b4
Revises: eba7117801f9
Create Date: 2026-11-04 13:03:53.991552
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1bb4194803b4"
down_revision: str | None = "eba7117801f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("paycheck_line", schema=None) as batch_op:
        batch_op.add_column(sa.Column("paid", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("paid_txn_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_paycheck_line_paid_txn", "txn", ["paid_txn_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("paycheck_line", schema=None) as batch_op:
        batch_op.drop_constraint("fk_paycheck_line_paid_txn", type_="foreignkey")
        batch_op.drop_column("paid_txn_id")
        batch_op.drop_column("paid")
