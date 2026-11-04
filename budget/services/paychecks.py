"""Paycheck plans: each check's lines and its remainder to the joint account."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget import calc
from budget.models import LINE_KINDS, Month, MonthBill, Paycheck, PaycheckLine, Person, Txn
from budget.schedule import Schedule, checks_for_month, per_check_lines
from budget.services import ledger
from budget.services.common import BudgetError, ensure_editable, per_check_line_templates


def remainder(check: Paycheck) -> int:
    return calc.check_remainder(check.amount_cents, [ln.amount_cents for ln in check.lines])


def checks_in_month(s: Session, month: Month) -> list[Paycheck]:
    return list(
        s.scalars(
            select(Paycheck)
            .where(Paycheck.month_id == month.id)
            .order_by(Paycheck.pay_date, Paycheck.is_one_off, Paycheck.id)
        )
    )


def schedules(s: Session) -> list[Schedule]:
    people = s.scalars(
        select(Person).where(Person.active.is_(True), Person.pay_anchor_date.is_not(None))
    )
    return [
        Schedule(person_id=p.id, anchor=p.pay_anchor_date, interval_days=p.pay_interval_days)
        for p in people
    ]


def generate_regular_checks(s: Session, month: Month) -> list[Paycheck]:
    """Create the month's regular checks from pay schedules, each with its per-check lines."""
    templates = per_check_line_templates(s)
    out = []
    for planned in checks_for_month(schedules(s), month.year, month.month):
        person = s.get(Person, planned.person_id)
        check = Paycheck(
            month_id=month.id,
            person_id=person.id,
            label=f"{person.name} check {planned.ordinal}",
            pay_date=planned.pay_date,
            amount_cents=person.default_check_cents,
            ordinal=planned.ordinal,
        )
        for i, ln in enumerate(per_check_lines(planned.pay_date, templates)):
            check.lines.append(
                PaycheckLine(
                    kind=ln.kind,
                    label=ln.label,
                    amount_cents=ln.amount_cents,
                    line_date=ln.line_date,
                    sort_order=i,
                )
            )
        s.add(check)
        out.append(check)
    s.flush()
    return out


def find_check(checks: list[Paycheck], person_id: int | None, ordinal: int | None):
    """The regular check for (person, ordinal). Falls back to that person's last check."""
    if person_id is None:
        return None
    mine = [c for c in checks if c.person_id == person_id and not c.is_one_off]
    if not mine:
        return None
    if ordinal:
        for c in mine:
            if c.ordinal == ordinal:
                return c
    return mine[-1]


def bill_line(s: Session, bill: MonthBill) -> PaycheckLine | None:
    return s.scalar(select(PaycheckLine).where(PaycheckLine.month_bill_id == bill.id))


def attach_bill(s: Session, bill: MonthBill, check: Paycheck | None) -> None:
    """Put a bill on a check (or take it off). Keeps the check's bill line in sync."""
    line = bill_line(s, bill)
    bill.assigned_check_id = check.id if check else None
    if check is None:
        if line:
            s.delete(line)
        return
    if line is None:
        line = PaycheckLine(kind="bill", month_bill_id=bill.id, sort_order=100 + bill.sort_order)
        check.lines.append(line)
    elif line.paycheck_id != check.id:
        line.paycheck.lines.remove(line)
        line = PaycheckLine(kind="bill", month_bill_id=bill.id, sort_order=100 + bill.sort_order)
        check.lines.append(line)
    line.label = bill.name
    line.amount_cents = -abs(bill.amount_cents)


def mark_received(s: Session, check: Paycheck, received_on: date | None = None) -> Txn:
    month = s.get(Month, check.month_id)
    ensure_editable(month)
    if check.received and check.received_txn_id:
        existing = s.get(Txn, check.received_txn_id)
        if existing and existing.deleted_at is None:
            return existing
    # Re-use the previous entry if it was deleted (keeps undo history tidy).
    if check.received_txn_id:
        old = s.get(Txn, check.received_txn_id)
        if old and old.deleted_at is not None:
            ledger.restore_txn(s, old)
            old.amount_cents = check.amount_cents
            return old
    txn = ledger.add_txn(
        s,
        month=month,
        date_=received_on or check.pay_date,
        amount_cents=check.amount_cents,
        description=check.label,
        source="paycheck",
    )
    check.received = True
    check.received_txn_id = txn.id
    return txn


def mark_not_received(s: Session, check: Paycheck) -> None:
    ensure_editable(s.get(Month, check.month_id))
    if check.received_txn_id:
        txn = s.get(Txn, check.received_txn_id)
        if txn:
            ledger.delete_txn(s, txn)
    check.received = False


def update_check(
    s: Session,
    check: Paycheck,
    *,
    label: str | None = None,
    amount_cents: int | None = None,
    pay_date: date | None = None,
) -> None:
    ensure_editable(s.get(Month, check.month_id))
    if label is not None and label.strip():
        check.label = label.strip()
    if amount_cents is not None:
        check.amount_cents = amount_cents
    if pay_date is not None:
        check.pay_date = pay_date
    if check.received and check.received_txn_id:
        txn = s.get(Txn, check.received_txn_id)
        if txn and txn.deleted_at is None:
            ledger.update_txn(s, txn, amount_cents=check.amount_cents, description=check.label)


def add_one_off(
    s: Session,
    month: Month,
    *,
    label: str,
    pay_date: date,
    amount_cents: int,
    person_id: int | None = None,
) -> Paycheck:
    ensure_editable(month)
    if not label.strip():
        raise BudgetError("Give the check a name, e.g. 'Bonus'.")
    check = Paycheck(
        month_id=month.id,
        person_id=person_id,
        label=label.strip(),
        pay_date=pay_date,
        amount_cents=amount_cents,
        is_one_off=True,
    )
    s.add(check)
    s.flush()
    return check


def delete_check(s: Session, check: Paycheck) -> None:
    ensure_editable(s.get(Month, check.month_id))
    if check.received_txn_id:
        txn = s.get(Txn, check.received_txn_id)
        if txn:
            ledger.delete_txn(s, txn)
    for bill in s.scalars(select(MonthBill).where(MonthBill.assigned_check_id == check.id)):
        bill.assigned_check_id = None
    s.delete(check)


def add_line(
    s: Session,
    check: Paycheck,
    *,
    kind: str,
    label: str,
    amount_cents: int,
    card_id: int | None = None,
) -> PaycheckLine:
    ensure_editable(s.get(Month, check.month_id))
    if kind not in LINE_KINDS or kind == "bill":
        kind = "other"
    line = PaycheckLine(
        kind=kind,
        label=label.strip() or kind.replace("_", " ").title(),
        amount_cents=amount_cents,
        card_id=card_id,
        sort_order=len(check.lines),
    )
    check.lines.append(line)
    s.flush()
    return line


def update_line(
    s: Session, line: PaycheckLine, *, label: str | None = None, amount_cents: int | None = None
) -> None:
    check = line.paycheck
    ensure_editable(s.get(Month, check.month_id))
    if line.month_bill_id:
        # A bill line mirrors its bill; edit the bill instead so both stay in sync.
        bill = s.get(MonthBill, line.month_bill_id)
        if amount_cents is not None and amount_cents != line.amount_cents:
            bill.amount_cents = abs(amount_cents)
            line.amount_cents = -abs(amount_cents)
            if bill.paid and bill.paid_txn_id:
                txn = s.get(Txn, bill.paid_txn_id)
                if txn and txn.deleted_at is None:
                    ledger.update_txn(s, txn, amount_cents=-bill.amount_cents)
        if label is not None and label.strip() and label.strip() != line.label:
            bill.name = label.strip()
            line.label = bill.name
        return
    if label is not None:
        line.label = label.strip()
    if amount_cents is not None:
        line.amount_cents = amount_cents
    if line.paid and line.paid_txn_id:
        txn = s.get(Txn, line.paid_txn_id)
        if txn and txn.deleted_at is None:
            ledger.update_txn(s, txn, amount_cents=line.amount_cents, description=line.label)


def mark_line_paid(s: Session, line: PaycheckLine, paid_on: date | None = None) -> Txn:
    """Tick off a planned check item (childcare, card payment, savings...): creates its
    ledger entry. Childcare is usually paid by paper check, so it starts out pending."""
    check = line.paycheck
    month = s.get(Month, check.month_id)
    ensure_editable(month)
    if not line.trackable:
        raise BudgetError("Bills are marked paid on the Bills page; groceries use the budget.")
    if line.paid_txn_id:
        txn = s.get(Txn, line.paid_txn_id)
        if txn and txn.deleted_at is None:
            line.paid = True
            return txn
        if txn:  # un-ticked earlier: bring the same entry back
            ledger.restore_txn(s, txn)
            ledger.update_txn(s, txn, amount_cents=line.amount_cents, description=line.label)
            return txn
    txn = ledger.add_txn(
        s,
        month=month,
        date_=paid_on or line.line_date or check.pay_date,
        amount_cents=line.amount_cents,
        description=line.label,
        pending=line.kind == "daycare",
        source="paycheck",
    )
    line.paid = True
    line.paid_txn_id = txn.id
    return txn


def mark_line_unpaid(s: Session, line: PaycheckLine) -> None:
    ensure_editable(s.get(Month, line.paycheck.month_id))
    if line.paid_txn_id:
        txn = s.get(Txn, line.paid_txn_id)
        if txn:
            ledger.delete_txn(s, txn)
    line.paid = False


def unpaid_planned_lines(checks: list[Paycheck]) -> list[PaycheckLine]:
    """Planned check items that haven't happened yet (for the projection)."""
    return [ln for c in checks for ln in c.lines if ln.trackable and not ln.paid]


def delete_line(s: Session, line: PaycheckLine) -> None:
    check = line.paycheck
    ensure_editable(s.get(Month, check.month_id))
    if line.month_bill_id:
        bill = s.get(MonthBill, line.month_bill_id)
        if bill:
            bill.assigned_check_id = None
    check.lines.remove(line)
