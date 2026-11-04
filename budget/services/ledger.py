"""Ledger entries: add, edit, delete (soft, with undo), clear pending."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget.models import Card, Month, MonthBill, Paycheck, PaycheckLine, Txn
from budget.services import cards as card_svc
from budget.services.common import BudgetError, ensure_editable, utcnow

_UNSET = object()


def _card_delta(txn: Txn) -> int:
    """How much a ledger entry changes its linked card's balance (payments reduce it)."""
    if txn.card_id is None or txn.deleted_at is not None:
        return 0
    return txn.amount_cents


def add_txn(
    s: Session,
    *,
    month: Month,
    date_: date,
    amount_cents: int,
    description: str = "",
    category_id: int | None = None,
    pending: bool = False,
    card_id: int | None = None,
    source: str = "manual",
    client_token: str | None = None,
    note: str = "",
    check_locked: bool = True,
) -> Txn:
    """Create a ledger entry. Re-submitting the same ``client_token`` returns the original
    entry instead of creating a duplicate (protects against double taps)."""
    if client_token:
        existing = s.scalar(select(Txn).where(Txn.client_token == client_token))
        if existing:
            return existing
    if check_locked:
        ensure_editable(month)
    if card_id is not None and amount_cents >= 0:
        raise BudgetError("A card payment must be money out.")
    txn = Txn(
        month_id=month.id,
        date=date_,
        amount_cents=amount_cents,
        description=description.strip(),
        category_id=category_id,
        pending=pending,
        card_id=card_id,
        source=source,
        client_token=client_token or None,
        note=note,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    s.add(txn)
    s.flush()
    if card_id is not None:
        card = s.get(Card, card_id)
        card_svc.apply_txn_delta(s, card, txn.amount_cents, f"Payment: {txn.description}", txn.id)
    return txn


def update_txn(
    s: Session,
    txn: Txn,
    *,
    date_: date | None = None,
    amount_cents: int | None = None,
    description: str | None = None,
    category_id=_UNSET,
    pending: bool | None = None,
    card_id=_UNSET,
    note: str | None = None,
) -> Txn:
    month = s.get(Month, txn.month_id)
    ensure_editable(month)
    old_card_id, old_delta = txn.card_id, _card_delta(txn)
    if date_ is not None:
        txn.date = date_
    if amount_cents is not None:
        txn.amount_cents = amount_cents
    if description is not None:
        txn.description = description.strip()
    if category_id is not _UNSET:
        txn.category_id = category_id
    if pending is not None:
        txn.pending = pending
        if not pending:
            txn.cleared_date = None
    if card_id is not _UNSET:
        txn.card_id = card_id
    if note is not None:
        txn.note = note
    if txn.card_id is not None and txn.amount_cents >= 0:
        raise BudgetError("A card payment must be money out.")
    txn.updated_at = utcnow()

    # Re-sync card balances: undo the old effect, apply the new one.
    new_delta = _card_delta(txn)
    if old_card_id == txn.card_id:
        if old_card_id is not None and new_delta != old_delta:
            card_svc.apply_txn_delta(
                s, s.get(Card, old_card_id), new_delta - old_delta, "Payment edited", txn.id
            )
    else:
        if old_card_id is not None:
            card_svc.apply_txn_delta(
                s, s.get(Card, old_card_id), -old_delta, "Payment moved", txn.id
            )
        if txn.card_id is not None:
            card_svc.apply_txn_delta(s, s.get(Card, txn.card_id), new_delta, "Payment", txn.id)
    return txn


@dataclass
class Links:
    bill: MonthBill | None
    paycheck: Paycheck | None
    line: PaycheckLine | None = None


def links_for(s: Session, txn: Txn) -> Links:
    bill = s.scalar(select(MonthBill).where(MonthBill.paid_txn_id == txn.id))
    check = s.scalar(select(Paycheck).where(Paycheck.received_txn_id == txn.id))
    line = s.scalar(select(PaycheckLine).where(PaycheckLine.paid_txn_id == txn.id))
    return Links(bill=bill, paycheck=check, line=line)


def delete_txn(s: Session, txn: Txn, *, check_locked: bool = True) -> Links:
    """Soft-delete an entry so it can be undone. Un-marks a bill it paid / check it received
    and reverses any card payment."""
    if txn.deleted_at is not None:
        return links_for(s, txn)
    if check_locked:
        ensure_editable(s.get(Month, txn.month_id))
    delta = _card_delta(txn)
    txn.deleted_at = utcnow()
    if delta:
        card_svc.apply_txn_delta(s, s.get(Card, txn.card_id), -delta, "Payment removed", txn.id)
    links = links_for(s, txn)
    if links.bill:
        links.bill.paid = False
    if links.paycheck:
        links.paycheck.received = False
    if links.line:
        links.line.paid = False
    return links


def restore_txn(s: Session, txn: Txn) -> Links:
    """Undo a delete."""
    if txn.deleted_at is None:
        return links_for(s, txn)
    ensure_editable(s.get(Month, txn.month_id))
    txn.deleted_at = None
    delta = _card_delta(txn)
    if delta:
        card_svc.apply_txn_delta(s, s.get(Card, txn.card_id), delta, "Payment restored", txn.id)
    links = links_for(s, txn)
    if links.bill:
        links.bill.paid = True
    if links.paycheck:
        links.paycheck.received = True
    if links.line:
        links.line.paid = True
    return links


def set_cleared(txn: Txn, cleared_on: date | None) -> None:
    """Mark a pending entry as cleared (or not). Doesn't change any month's starting balance."""
    txn.cleared_date = cleared_on
    txn.updated_at = utcnow()


def outstanding_pending(s: Session, before_or_in_month: Month | None = None) -> list[Txn]:
    """Pending entries that haven't cleared, optionally limited to months up to ``month``."""
    q = select(Txn).where(
        Txn.pending.is_(True), Txn.cleared_date.is_(None), Txn.deleted_at.is_(None)
    )
    if before_or_in_month is not None:
        q = q.join(Month, Month.id == Txn.month_id).where(
            (Month.year * 100 + Month.month)
            <= (before_or_in_month.year * 100 + before_or_in_month.month)
        )
    return list(s.scalars(q.order_by(Txn.date, Txn.id)))


def recent_descriptions(s: Session, limit: int = 200) -> list[tuple[str, int | None]]:
    """Distinct recent descriptions with their most recent category, for autocomplete."""
    rows = s.execute(
        select(Txn.description, Txn.category_id, func.max(Txn.id))
        .where(Txn.deleted_at.is_(None), Txn.description != "")
        .group_by(func.lower(Txn.description))
        .order_by(func.max(Txn.id).desc())
        .limit(limit)
    ).all()
    return [(r[0], r[1]) for r in rows]
