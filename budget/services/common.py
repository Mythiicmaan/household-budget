"""Shared helpers for the service layer."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget.models import Category, Month, Setting, Txn
from budget.schedule import PerCheckLineTemplate

PER_CHECK_LINES_KEY = "per_check_lines"
BILLS_CATEGORY_KEY = "bills_category"
GROCERIES_CATEGORY_KEY = "groceries_category"


class BudgetError(ValueError):
    """A user-facing rule violation (shown as a message, not a crash)."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_setting(s: Session, key: str, default: str | None = None) -> str | None:
    row = s.get(Setting, key)
    return row.value if row else default


def set_setting(s: Session, key: str, value: str) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


def per_check_line_templates(s: Session) -> list[PerCheckLineTemplate]:
    raw = get_setting(s, PER_CHECK_LINES_KEY, "[]") or "[]"
    out = []
    for item in json.loads(raw):
        out.append(
            PerCheckLineTemplate(
                kind=str(item.get("kind", "other")),
                label=str(item.get("label", "")),
                amount_cents=int(item.get("amount_cents", 0)),
                day_offset=int(item.get("day_offset", 0)),
            )
        )
    return out


def save_per_check_line_templates(s: Session, templates: list[PerCheckLineTemplate]) -> None:
    set_setting(
        s,
        PER_CHECK_LINES_KEY,
        json.dumps(
            [
                {
                    "kind": t.kind,
                    "label": t.label,
                    "amount_cents": t.amount_cents,
                    "day_offset": t.day_offset,
                }
                for t in templates
            ]
        ),
    )


def bills_category(s: Session) -> Category | None:
    name = get_setting(s, BILLS_CATEGORY_KEY, "Bills") or "Bills"
    return s.scalar(select(Category).where(Category.name.ilike(name)))


def groceries_category(s: Session) -> Category | None:
    name = get_setting(s, GROCERIES_CATEGORY_KEY, "Groceries") or "Groceries"
    return s.scalar(select(Category).where(Category.name.ilike(name)))


def active_txns_query(month_id: int):
    return (
        select(Txn)
        .where(Txn.month_id == month_id, Txn.deleted_at.is_(None))
        .order_by(Txn.date.desc(), Txn.id.desc())
    )


def get_month(s: Session, year: int, month: int) -> Month | None:
    return s.scalar(select(Month).where(Month.year == year, Month.month == month))


def live_month(s: Session) -> Month | None:
    """The month being budgeted now: the latest non-history month (or latest of any)."""
    m = s.scalar(
        select(Month)
        .where(Month.is_history.is_(False))
        .order_by(Month.year.desc(), Month.month.desc())
    )
    if m:
        return m
    return s.scalar(select(Month).order_by(Month.year.desc(), Month.month.desc()))


def month_for_date(s: Session, d: date) -> Month | None:
    """Which month a new entry belongs to: the month of its date if that month exists and
    isn't locked; otherwise the live month (e.g. a late receipt from a closed month)."""
    m = get_month(s, d.year, d.month)
    if m and not m.locked:
        return m
    return live_month(s)


def ensure_editable(month: Month) -> None:
    if month.locked:
        raise BudgetError("This month is locked. Unlock it to make changes.")
