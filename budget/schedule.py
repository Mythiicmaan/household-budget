"""Pay schedules: which checks land in a month, and the lines every check covers."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_label(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def pay_dates_in_month(anchor: date, interval_days: int, year: int, month: int) -> list[date]:
    """Every pay date in the month for a schedule repeating every ``interval_days`` from ``anchor``.

    Works for months before or after the anchor.
    """
    if interval_days <= 0:
        raise ValueError("interval_days must be positive")
    first, last = month_bounds(year, month)
    # Smallest k with anchor + k*interval >= first (k may be negative).
    k = -((anchor - first).days // interval_days)
    d = anchor + timedelta(days=k * interval_days)
    if d < first:  # pragma: no cover - defensive; math above guarantees d >= first
        d += timedelta(days=interval_days)
    out = []
    while d <= last:
        out.append(d)
        d += timedelta(days=interval_days)
    return out


@dataclass(frozen=True)
class Schedule:
    person_id: int
    anchor: date
    interval_days: int = 14


@dataclass(frozen=True)
class PlannedCheck:
    person_id: int
    pay_date: date
    ordinal: int  # 1st, 2nd, 3rd check for this person in this month


def checks_for_month(schedules: list[Schedule], year: int, month: int) -> list[PlannedCheck]:
    """All regular checks in a month, sorted by date (ties by person id)."""
    out: list[PlannedCheck] = []
    for s in schedules:
        for i, d in enumerate(pay_dates_in_month(s.anchor, s.interval_days, year, month), start=1):
            out.append(PlannedCheck(person_id=s.person_id, pay_date=d, ordinal=i))
    out.sort(key=lambda c: (c.pay_date, c.person_id))
    return out


@dataclass(frozen=True)
class PerCheckLineTemplate:
    kind: str
    label: str
    amount_cents: int  # positive size; stored on the check as a negative line
    day_offset: int = 0


@dataclass(frozen=True)
class PlannedLine:
    kind: str
    label: str
    amount_cents: int  # signed (negative = out)
    line_date: date


def per_check_lines(pay_date: date, templates: list[PerCheckLineTemplate]) -> list[PlannedLine]:
    """Lines every check covers, e.g. that Friday's groceries and the following Monday's childcare.

    A line's date may fall in the NEXT month (a check on the last Friday covers
    the Monday after); the line still belongs to the check, and so to the check's month.
    """
    out = []
    for t in templates:
        d = pay_date + timedelta(days=t.day_offset)
        label = f"{t.label} ({d.strftime('%a')} {d.month}/{d.day})"
        out.append(
            PlannedLine(kind=t.kind, label=label, amount_cents=-abs(t.amount_cents), line_date=d)
        )
    return out
