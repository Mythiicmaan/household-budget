"""initial schema

Revision ID: eba7117801f9
Revises:
Create Date: 2026-11-04 12:19:03.259961
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eba7117801f9"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("balance_cents", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("monthly_target_cents", sa.Integer(), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "month",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("bank_balance_on_first_cents", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("is_history", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "month"),
    )
    op.create_table(
        "person",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("pay_anchor_date", sa.Date(), nullable=True),
        sa.Column("pay_interval_days", sa.Integer(), nullable=False),
        sa.Column("default_check_cents", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "bill_template",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("default_amount_cents", sa.Integer(), nullable=False),
        sa.Column("assigned_person_id", sa.Integer(), nullable=True),
        sa.Column("assigned_check_ordinal", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_person_id"], ["person.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "paycheck",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("received", sa.Boolean(), nullable=False),
        sa.Column("received_txn_id", sa.Integer(), nullable=True),
        sa.Column("is_one_off", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["month_id"], ["month.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["received_txn_id"], ["txn.id"], ondelete="SET NULL", use_alter=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "txn",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("cleared_date", sa.Date(), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("client_token", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["category_id"], ["category.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["month_id"], ["month.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_token"),
    )
    with op.batch_alter_table("txn", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_txn_month_id"), ["month_id"], unique=False)

    op.create_table(
        "card_balance_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("balance_cents", sa.Integer(), nullable=False),
        sa.Column("delta_cents", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("txn_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["txn_id"], ["txn.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "month_bill",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month_id", sa.Integer(), nullable=False),
        sa.Column("bill_template_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("assigned_check_id", sa.Integer(), nullable=True),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("paid_txn_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_check_id"], ["paycheck.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["bill_template_id"], ["bill_template.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["month_id"], ["month.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paid_txn_id"], ["txn.id"], ondelete="SET NULL", use_alter=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pending_carryover",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_month_id", sa.Integer(), nullable=False),
        sa.Column("to_month_id", sa.Integer(), nullable=False),
        sa.Column("txn_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["from_month_id"], ["month.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_month_id"], ["month.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["txn_id"], ["txn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("to_month_id", "txn_id"),
    )
    op.create_table(
        "paycheck_line",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paycheck_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("line_date", sa.Date(), nullable=True),
        sa.Column("month_bill_id", sa.Integer(), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["month_bill_id"], ["month_bill.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paycheck_id"], ["paycheck.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("paycheck_line")
    op.drop_table("pending_carryover")
    op.drop_table("month_bill")
    op.drop_table("card_balance_event")
    with op.batch_alter_table("txn", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_txn_month_id"))

    op.drop_table("txn")
    op.drop_table("paycheck")
    op.drop_table("bill_template")
    op.drop_table("setting")
    op.drop_table("person")
    op.drop_table("month")
    op.drop_table("category")
    op.drop_table("card")
