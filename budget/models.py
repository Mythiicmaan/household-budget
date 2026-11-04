"""Database schema (SQLAlchemy 2.x). Money columns are integer cents."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TXN_SOURCES = ("manual", "bill", "paycheck", "import")
LINE_KINDS = ("bill", "groceries", "daycare", "card_payment", "savings", "other")


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "category"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    monthly_target_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class Person(Base):
    __tablename__ = "person"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    pay_anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    pay_interval_days: Mapped[int] = mapped_column(Integer, default=14)
    default_check_cents: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BillTemplate(Base):
    __tablename__ = "bill_template"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    default_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    assigned_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), nullable=True
    )
    assigned_check_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    person: Mapped[Person | None] = relationship()


class Month(Base):
    __tablename__ = "month"
    __table_args__ = (UniqueConstraint("year", "month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    bank_balance_on_first_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_history: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


class Paycheck(Base):
    __tablename__ = "paycheck"
    id: Mapped[int] = mapped_column(primary_key=True)
    month_id: Mapped[int] = mapped_column(ForeignKey("month.id", ondelete="CASCADE"))
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(120))
    pay_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received: Mapped[bool] = mapped_column(Boolean, default=False)
    received_txn_id: Mapped[int | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    is_one_off: Mapped[bool] = mapped_column(Boolean, default=False)

    person: Mapped[Person | None] = relationship()
    lines: Mapped[list[PaycheckLine]] = relationship(
        back_populates="paycheck",
        cascade="all, delete-orphan",
        order_by="PaycheckLine.sort_order, PaycheckLine.id",
    )


class MonthBill(Base):
    __tablename__ = "month_bill"
    id: Mapped[int] = mapped_column(primary_key=True)
    month_id: Mapped[int] = mapped_column(ForeignKey("month.id", ondelete="CASCADE"))
    bill_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("bill_template.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)  # positive size of the bill
    assigned_check_id: Mapped[int | None] = mapped_column(
        ForeignKey("paycheck.id", ondelete="SET NULL"), nullable=True
    )
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_txn_id: Mapped[int | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    assigned_check: Mapped[Paycheck | None] = relationship(foreign_keys=[assigned_check_id])
    template: Mapped[BillTemplate | None] = relationship()


class PaycheckLine(Base):
    __tablename__ = "paycheck_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    paycheck_id: Mapped[int] = mapped_column(ForeignKey("paycheck.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20), default="other")
    label: Mapped[str] = mapped_column(String(160), default="")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)  # signed; outflows negative
    line_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    month_bill_id: Mapped[int | None] = mapped_column(
        ForeignKey("month_bill.id", ondelete="CASCADE"), nullable=True
    )
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("card.id", ondelete="SET NULL"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Planned items (childcare, card payments, savings...) are ticked off when they happen,
    # which creates their ledger entry. Bill lines use the bill's own paid flag instead,
    # and grocery lines are tracked through the Groceries budget.
    paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    paid_txn_id: Mapped[int | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL", name="fk_paycheck_line_paid_txn"), nullable=True
    )

    paycheck: Mapped[Paycheck] = relationship(back_populates="lines")

    @property
    def trackable(self) -> bool:
        """Has its own Paid toggle (not a bill line, not a grocery budget line)."""
        return self.kind not in ("bill", "groceries")

    month_bill: Mapped[MonthBill | None] = relationship()


class Txn(Base):
    """A ledger entry."""

    __tablename__ = "txn"
    id: Mapped[int] = mapped_column(primary_key=True)
    month_id: Mapped[int] = mapped_column(ForeignKey("month.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    cleared_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("card.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), default="manual")
    client_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    category: Mapped[Category | None] = relationship()
    card: Mapped[Card | None] = relationship()

    @property
    def is_outstanding(self) -> bool:
        return self.pending and self.cleared_date is None


class PendingCarryover(Base):
    __tablename__ = "pending_carryover"
    __table_args__ = (UniqueConstraint("to_month_id", "txn_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    from_month_id: Mapped[int] = mapped_column(ForeignKey("month.id", ondelete="CASCADE"))
    to_month_id: Mapped[int] = mapped_column(ForeignKey("month.id", ondelete="CASCADE"))
    txn_id: Mapped[int] = mapped_column(ForeignKey("txn.id", ondelete="CASCADE"))
    # Snapshot of the amount at carryover time, so the true starting balance
    # doesn't change if the original entry is edited later.
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)

    txn: Mapped[Txn] = relationship()


class Card(Base):
    __tablename__ = "card"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)  # amount owed
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    events: Mapped[list[CardBalanceEvent]] = relationship(
        back_populates="card",
        cascade="all, delete-orphan",
        order_by="CardBalanceEvent.id.desc()",
    )


class CardBalanceEvent(Base):
    __tablename__ = "card_balance_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id", ondelete="CASCADE"))
    balance_cents: Mapped[int] = mapped_column(Integer)  # balance after this event
    delta_cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    txn_id: Mapped[int | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    card: Mapped[Card] = relationship(back_populates="events")


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
