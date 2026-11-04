"""Read-only numbers for the dashboard, history chart, and JSON API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from budget import calc
from budget.models import Category, Month, MonthBill, Paycheck, Txn
from budget.schedule import month_label
from budget.services import bills as bill_svc
from budget.services import ledger, months, paychecks
from budget.services.common import active_txns_query


@dataclass
class TargetStatus:
    category: Category
    target_cents: int
    remaining_cents: int
    fraction: float

    @property
    def over(self) -> bool:
        return self.remaining_cents < 0

    @property
    def bar_percent(self) -> int:
        return max(0, min(100, round(self.fraction * 100)))


@dataclass
class MonthSummary:
    month: Month
    label: str
    running_total_cents: int
    money_in_cents: int
    money_out_cents: int
    bank_balance_cents: int | None
    carryover_cents: int
    true_start_cents: int | None
    projected_end_cents: int
    targets: list[TargetStatus] = field(default_factory=list)
    next_check: Paycheck | None = None
    unpaid_bills: list[MonthBill] = field(default_factory=list)
    pending: list[Txn] = field(default_factory=list)
    checks_expected_cents: int = 0
    unpaid_bills_cents: int = 0


def month_summary(s: Session, month: Month, today: date | None = None) -> MonthSummary:
    today = today or date.today()
    txns = list(s.scalars(active_txns_query(month.id)))
    amounts = [t.amount_cents for t in txns]
    running = calc.running_total(amounts)
    carry = months.carryovers_into(s, month)
    true_start = months.true_starting_balance(s, month)

    checks = paychecks.checks_in_month(s, month)
    expected = [c.amount_cents for c in checks if not c.received]
    bills = bill_svc.bills_in_month(s, month)
    unpaid = [b for b in bills if not b.paid]
    projected = calc.projected_end_balance(
        true_start, running, expected, [b.amount_cents for b in unpaid]
    )

    targets = []
    cats = s.scalars(
        select(Category)
        .where(Category.monthly_target_cents.is_not(None), Category.hidden.is_(False))
        .order_by(Category.sort_order, Category.id)
    )
    for cat in cats:
        rem = calc.category_remaining(
            cat.monthly_target_cents, [t.amount_cents for t in txns if t.category_id == cat.id]
        )
        targets.append(
            TargetStatus(
                category=cat,
                target_cents=cat.monthly_target_cents,
                remaining_cents=rem,
                fraction=calc.category_spent_fraction(cat.monthly_target_cents, rem),
            )
        )

    upcoming = [c for c in checks if not c.received and c.pay_date >= today]
    not_received = [c for c in checks if not c.received]
    next_check = (upcoming or not_received or [None])[0]

    return MonthSummary(
        month=month,
        label=month_label(month.year, month.month),
        running_total_cents=running,
        money_in_cents=sum(a for a in amounts if a > 0),
        money_out_cents=sum(a for a in amounts if a < 0),
        bank_balance_cents=month.bank_balance_on_first_cents,
        carryover_cents=sum(c.amount_cents for c in carry),
        true_start_cents=true_start,
        projected_end_cents=projected,
        targets=targets,
        next_check=next_check,
        unpaid_bills=unpaid,
        pending=ledger.outstanding_pending(s, month),
        checks_expected_cents=sum(expected),
        unpaid_bills_cents=sum(b.amount_cents for b in unpaid),
    )


@dataclass
class HistoryRow:
    key: str
    label: str
    money_in_cents: int
    money_out_cents: int

    @property
    def net_cents(self) -> int:
        return self.money_in_cents + self.money_out_cents


def history(s: Session) -> list[HistoryRow]:
    rows = s.execute(
        select(
            Month.year,
            Month.month,
            func.coalesce(func.sum(case((Txn.amount_cents > 0, Txn.amount_cents), else_=0)), 0),
            func.coalesce(func.sum(case((Txn.amount_cents < 0, Txn.amount_cents), else_=0)), 0),
        )
        .join(Txn, (Txn.month_id == Month.id) & Txn.deleted_at.is_(None), isouter=True)
        .group_by(Month.id)
        .order_by(Month.year, Month.month)
    ).all()
    return [
        HistoryRow(
            key=f"{y:04d}-{m:02d}",
            label=month_label(y, m),
            money_in_cents=int(i),
            money_out_cents=int(o),
        )
        for y, m, i, o in rows
    ]
