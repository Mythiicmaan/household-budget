"""First-run seeding from config.toml's [seed] section."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget.models import Category, Person
from budget.money import parse_money
from budget.schedule import PerCheckLineTemplate
from budget.services.common import (
    PER_CHECK_LINES_KEY,
    get_setting,
    save_per_check_line_templates,
)


def _cents(value: Any) -> int:
    return parse_money(str(value))


def seed_if_empty(s: Session, seed: dict[str, Any]) -> bool:
    """Seed categories, people, and per-check lines when the database is brand new.

    Returns True if anything was seeded. Never overwrites existing data.
    """
    did = False
    if not s.scalar(select(func.count(Category.id))):
        for i, c in enumerate(seed.get("categories", [])):
            target = c.get("target")
            s.add(
                Category(
                    name=str(c["name"]),
                    sort_order=i,
                    monthly_target_cents=_cents(target) if target not in (None, "") else None,
                )
            )
        did = True
    if not s.scalar(select(func.count(Person.id))):
        for p in seed.get("people", []):
            anchor = p.get("pay_anchor")
            if isinstance(anchor, str):
                anchor = date.fromisoformat(anchor)
            s.add(
                Person(
                    name=str(p["name"]),
                    pay_anchor_date=anchor,
                    pay_interval_days=int(p.get("interval_days", 14)),
                    default_check_cents=_cents(p.get("default_check", "0")),
                )
            )
        did = True
    if get_setting(s, PER_CHECK_LINES_KEY) is None:
        save_per_check_line_templates(
            s,
            [
                PerCheckLineTemplate(
                    kind=str(t.get("kind", "other")),
                    label=str(t.get("label", "")),
                    amount_cents=_cents(t.get("amount", "0")),
                    day_offset=int(t.get("day_offset", 0)),
                )
                for t in seed.get("per_check_lines", [])
            ],
        )
        did = True
    s.flush()
    return did
