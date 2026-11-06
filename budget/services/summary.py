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
from budget.services.common import (
    active_txns_query,
    groceries_category,
    per_check_line_templates,
)


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
    unpaid_lines_cents: int = 0  # positive size of planned check items not yet paid
    budget_left_cents: int = 0  # what's still left to spend in soft budgets
    to_pay: list[ToPay] = field(default_factory=list)


@dataclass
class ToPay:
    """One thing still planned for the month: an unpaid bill or check item."""

    kind: str  # "bill" or "line"
    id: int
    label: str
    amount_cents: int  # positive
    when: date | None
    check_label: str


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
    open_lines = paychecks.unpaid_planned_lines(checks)

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

    left = calc.budget_left(t.remaining_cents for t in targets)
    projected = calc.projected_end_balance(
        true_start,
        running,
        expected,
        [b.amount_cents for b in unpaid],
        unpaid_line_amounts=[ln.amount_cents for ln in open_lines],
        budget_left=left,
    )
    to_pay = [
        ToPay(
            "bill",
            b.id,
            b.name,
            b.amount_cents,
            b.assigned_check.pay_date if b.assigned_check else None,
            b.assigned_check.label if b.assigned_check else "",
        )
        for b in unpaid
    ] + [
        ToPay(
            "line",
            ln.id,
            ln.label,
            -ln.amount_cents,
            ln.line_date or ln.paycheck.pay_date,
            ln.paycheck.label,
        )
        for ln in open_lines
    ]
    to_pay.sort(key=lambda x: (x.when or date.max, x.kind, x.label))

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
        unpaid_lines_cents=-sum(ln.amount_cents for ln in open_lines),
        budget_left_cents=sum(left),
        to_pay=to_pay,
    )


@dataclass
class GroceriesRow:
    """Follows ledger entries in the Groceries category, the same number as Home."""

    label: str
    budget_cents: int
    left_cents: int  # negative when over budget

    @property
    def spent_cents(self) -> int:
        return self.budget_cents - self.left_cents

    @property
    def over(self) -> bool:
        return self.left_cents < 0


@dataclass
class DaycareRow:
    """Reads the weekly daycare lines on the month's checks."""

    label: str
    total_cents: int
    unpaid_cents: int
    paid_count: int
    week_count: int
    next_when: date | None  # the next unpaid week, if any


@dataclass
class BillsPage:
    bills: list[MonthBill]
    groceries: GroceriesRow | None
    daycare: DaycareRow | None
    total_cents: int
    unpaid_cents: int


def bills_page(s: Session, month: Month) -> BillsPage:
    """The bills list plus the two weekly rows. The rows read existing data rather than
    storing bills of their own, so nothing is counted twice."""
    items = bill_svc.bills_in_month(s, month)
    groceries = None
    daycare = None
    if not month.is_history:
        cat = groceries_category(s)
        if cat is not None and cat.monthly_target_cents is not None:
            entries = [
                t.amount_cents
                for t in s.scalars(active_txns_query(month.id))
                if t.category_id == cat.id
            ]
            groceries = GroceriesRow(
                label=cat.name,
                budget_cents=cat.monthly_target_cents,
                left_cents=calc.category_remaining(cat.monthly_target_cents, entries),
            )
        lines = paychecks.daycare_lines(paychecks.checks_in_month(s, month))
        if lines:
            unpaid = [ln for ln in lines if not ln.paid]
            label = next(
                (t.label for t in per_check_line_templates(s) if t.kind == "daycare"), "Daycare"
            )
            daycare = DaycareRow(
                label=label,
                total_cents=-sum(ln.amount_cents for ln in lines),
                unpaid_cents=-sum(ln.amount_cents for ln in unpaid),
                paid_count=len(lines) - len(unpaid),
                week_count=len(lines),
                next_when=(unpaid[0].line_date or unpaid[0].paycheck.pay_date) if unpaid else None,
            )

    total = sum(b.amount_cents for b in items)
    still_unpaid = sum(b.amount_cents for b in items if not b.paid)
    if groceries:
        total += groceries.budget_cents
        still_unpaid += sum(calc.budget_left([groceries.left_cents]))
    if daycare:
        total += daycare.total_cents
        still_unpaid += daycare.unpaid_cents
    return BillsPage(items, groceries, daycare, total, still_unpaid)


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
