"""Months: starting a new month, pending carryover, true starting balance, locking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget import calc
from budget.models import BillTemplate, Month, MonthBill, PendingCarryover, Txn
from budget.schedule import month_label, next_month, prev_month
from budget.services import ledger, paychecks
from budget.services.common import BudgetError, get_month, live_month, utcnow


def carryovers_into(s: Session, month: Month) -> list[PendingCarryover]:
    return list(
        s.scalars(
            select(PendingCarryover)
            .where(PendingCarryover.to_month_id == month.id)
            .order_by(PendingCarryover.id)
        )
    )


def true_starting_balance(s: Session, month: Month) -> int | None:
    """Bank balance on the 1st adjusted for items that hadn't cleared by then.

    Uses the carryover snapshot, so it does not change when those items clear later.
    """
    return calc.true_starting_balance(
        month.bank_balance_on_first_cents, [c.amount_cents for c in carryovers_into(s, month)]
    )


@dataclass
class NextMonthPreview:
    year: int
    month: int
    label: str
    previous: Month | None
    outstanding: list[Txn]


def next_month_preview(s: Session) -> NextMonthPreview:
    cur = live_month(s)
    if cur is None:
        today = date.today()
        y, m = today.year, today.month
    else:
        y, m = next_month(cur.year, cur.month)
    outstanding = ledger.outstanding_pending(s, cur) if cur else []
    return NextMonthPreview(y, m, month_label(y, m), cur, outstanding)


def start_month(
    s: Session,
    year: int,
    month: int,
    *,
    bank_balance_cents: int | None,
    cleared_txn_ids: set[int] | frozenset[int] = frozenset(),
    today: date | None = None,
    lock_previous: bool = True,
) -> Month:
    """Start a month: copy bills forward, generate checks, carry over pending items."""
    today = today or date.today()
    if get_month(s, year, month):
        raise BudgetError(f"{month_label(year, month)} has already been started.")
    py, pm = prev_month(year, month)
    prev = get_month(s, py, pm)

    new = Month(year=year, month=month, bank_balance_on_first_cents=bank_balance_cents)
    new.started_at = utcnow()
    s.add(new)
    s.flush()

    checks = paychecks.generate_regular_checks(s, new)
    _copy_bills(s, prev, new, checks)

    # Pending carryover: anything not yet cleared from earlier months.
    for txn in ledger.outstanding_pending(s, prev) if prev else []:
        if txn.id in cleared_txn_ids:
            ledger.set_cleared(txn, today)
            continue
        s.add(
            PendingCarryover(
                from_month_id=txn.month_id,
                to_month_id=new.id,
                txn_id=txn.id,
                amount_cents=txn.amount_cents,
            )
        )

    if prev and lock_previous:
        prev.locked = True
    s.flush()
    return new


def _copy_bills(s: Session, prev: Month | None, new: Month, checks) -> None:
    """Bills come from the template, using LAST month's amount when there was one
    (amounts like utilities vary). New bills start unpaid."""
    templates = list(
        s.scalars(
            select(BillTemplate)
            .where(BillTemplate.active.is_(True))
            .order_by(BillTemplate.sort_order, BillTemplate.id)
        )
    )
    last_amount: dict[int, int] = {}
    if prev:
        for b in s.scalars(select(MonthBill).where(MonthBill.month_id == prev.id)):
            if b.bill_template_id is not None:
                last_amount[b.bill_template_id] = b.amount_cents
    for i, tpl in enumerate(templates):
        bill = MonthBill(
            month_id=new.id,
            bill_template_id=tpl.id,
            name=tpl.name,
            amount_cents=last_amount.get(tpl.id, tpl.default_amount_cents),
            note=tpl.note,
            sort_order=i,
        )
        s.add(bill)
        s.flush()
        check = paychecks.find_check(checks, tpl.assigned_person_id, tpl.assigned_check_ordinal)
        if check:
            paychecks.attach_bill(s, bill, check)


def set_bank_balance(month: Month, cents: int | None) -> None:
    month.bank_balance_on_first_cents = cents


def set_locked(month: Month, locked: bool) -> None:
    month.locked = locked


def all_months(s: Session) -> list[Month]:
    return list(s.scalars(select(Month).order_by(Month.year, Month.month)))
