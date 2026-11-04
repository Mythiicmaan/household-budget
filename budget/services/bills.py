"""Monthly bills and the bill template."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget.models import BillTemplate, Month, MonthBill, Paycheck, Txn
from budget.services import ledger, paychecks
from budget.services.common import BudgetError, bills_category, ensure_editable


def bills_in_month(s: Session, month: Month) -> list[MonthBill]:
    return list(
        s.scalars(
            select(MonthBill)
            .where(MonthBill.month_id == month.id)
            .order_by(MonthBill.sort_order, MonthBill.id)
        )
    )


def mark_paid(s: Session, bill: MonthBill, paid_on: date) -> Txn:
    """Mark a bill paid and create its ledger entry (so nothing is entered twice)."""
    month = s.get(Month, bill.month_id)
    ensure_editable(month)
    if bill.paid_txn_id:
        txn = s.get(Txn, bill.paid_txn_id)
        if txn and txn.deleted_at is None:
            bill.paid = True
            return txn
        if txn:  # previously un-marked: bring the same entry back
            ledger.restore_txn(s, txn)
            ledger.update_txn(s, txn, amount_cents=-abs(bill.amount_cents))
            return txn
    cat = bills_category(s)
    txn = ledger.add_txn(
        s,
        month=month,
        date_=paid_on,
        amount_cents=-abs(bill.amount_cents),
        description=bill.name,
        category_id=cat.id if cat else None,
        source="bill",
    )
    bill.paid = True
    bill.paid_txn_id = txn.id
    return txn


def mark_unpaid(s: Session, bill: MonthBill) -> None:
    ensure_editable(s.get(Month, bill.month_id))
    if bill.paid_txn_id:
        txn = s.get(Txn, bill.paid_txn_id)
        if txn:
            ledger.delete_txn(s, txn)
    bill.paid = False


def _template_from_check(check: Paycheck | None) -> tuple[int | None, int | None]:
    if check is None or check.is_one_off:
        return None, None
    return check.person_id, check.ordinal


def add_bill(
    s: Session,
    month: Month,
    *,
    name: str,
    amount_cents: int,
    note: str = "",
    check: Paycheck | None = None,
    also_template: bool = False,
) -> MonthBill:
    ensure_editable(month)
    name = name.strip()
    if not name:
        raise BudgetError("Bill name is required.")
    order = (
        s.scalar(select(func.max(MonthBill.sort_order)).where(MonthBill.month_id == month.id)) or 0
    ) + 1
    template = None
    if also_template:
        person_id, ordinal = _template_from_check(check)
        template = BillTemplate(
            name=name,
            default_amount_cents=abs(amount_cents),
            assigned_person_id=person_id,
            assigned_check_ordinal=ordinal,
            note=note,
            sort_order=order,
        )
        s.add(template)
        s.flush()
    bill = MonthBill(
        month_id=month.id,
        bill_template_id=template.id if template else None,
        name=name,
        amount_cents=abs(amount_cents),
        note=note,
        sort_order=order,
    )
    s.add(bill)
    s.flush()
    if check:
        paychecks.attach_bill(s, bill, check)
    return bill


def update_bill(
    s: Session,
    bill: MonthBill,
    *,
    name: str | None = None,
    amount_cents: int | None = None,
    note: str | None = None,
    check: Paycheck | None | object = ...,
    also_template: bool = False,
) -> None:
    """Edit a bill for this month only, or (``also_template``) for future months too."""
    ensure_editable(s.get(Month, bill.month_id))
    if name is not None and name.strip():
        bill.name = name.strip()
    if amount_cents is not None:
        bill.amount_cents = abs(amount_cents)
    if note is not None:
        bill.note = note
    target_check = bill.assigned_check if check is ... else check
    paychecks.attach_bill(s, bill, target_check)
    if bill.paid and bill.paid_txn_id:
        txn = s.get(Txn, bill.paid_txn_id)
        if txn and txn.deleted_at is None:
            ledger.update_txn(s, txn, amount_cents=-bill.amount_cents, description=bill.name)
    if also_template:
        tpl = bill.template
        if tpl is None:
            tpl = BillTemplate(name=bill.name, sort_order=bill.sort_order)
            s.add(tpl)
            s.flush()
            bill.bill_template_id = tpl.id
            bill.template = tpl
        tpl.name = bill.name
        tpl.default_amount_cents = bill.amount_cents
        tpl.note = bill.note
        tpl.active = True
        tpl.assigned_person_id, tpl.assigned_check_ordinal = _template_from_check(target_check)


def remove_bill(s: Session, bill: MonthBill, *, also_template: bool = False) -> None:
    ensure_editable(s.get(Month, bill.month_id))
    if bill.paid_txn_id:
        txn = s.get(Txn, bill.paid_txn_id)
        if txn:
            ledger.delete_txn(s, txn)
    if also_template and bill.template:
        bill.template.active = False
    paychecks.attach_bill(s, bill, None)
    s.delete(bill)
