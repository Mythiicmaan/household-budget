"""Credit card balance tracker (manual; no bank connections)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget.models import Card, CardBalanceEvent
from budget.services.common import BudgetError, utcnow


def add_card(s: Session, name: str, balance_cents: int) -> Card:
    name = name.strip()
    if not name:
        raise BudgetError("Card name is required.")
    card = Card(name=name, balance_cents=balance_cents, updated_at=utcnow())
    s.add(card)
    s.flush()
    s.add(
        CardBalanceEvent(
            card_id=card.id,
            balance_cents=balance_cents,
            delta_cents=balance_cents,
            note="Starting balance",
            created_at=utcnow(),
        )
    )
    return card


def set_balance(s: Session, card: Card, new_balance_cents: int, note: str = "") -> CardBalanceEvent:
    delta = new_balance_cents - card.balance_cents
    return _apply(s, card, delta, note or "Balance update", txn_id=None)


def apply_txn_delta(s: Session, card: Card, delta_cents: int, note: str, txn_id: int | None):
    """Change a card balance because of a ledger entry (payment, edit, delete, undo)."""
    if delta_cents == 0:
        return None
    return _apply(s, card, delta_cents, note, txn_id=txn_id)


def _apply(s: Session, card: Card, delta: int, note: str, txn_id: int | None) -> CardBalanceEvent:
    card.balance_cents += delta
    card.updated_at = utcnow()
    ev = CardBalanceEvent(
        card_id=card.id,
        balance_cents=card.balance_cents,
        delta_cents=delta,
        note=note,
        txn_id=txn_id,
        created_at=utcnow(),
    )
    s.add(ev)
    return ev


def archive(card: Card, archived: bool = True) -> None:
    card.archived = archived
    card.updated_at = utcnow()


def delete_card(s: Session, card: Card) -> None:
    s.delete(card)


def active_cards(s: Session) -> list[Card]:
    return list(s.scalars(select(Card).where(Card.archived.is_(False)).order_by(Card.name)))


def archived_cards(s: Session) -> list[Card]:
    return list(s.scalars(select(Card).where(Card.archived.is_(True)).order_by(Card.name)))


def total_active_balance(s: Session) -> int:
    return sum(c.balance_cents for c in active_cards(s))
