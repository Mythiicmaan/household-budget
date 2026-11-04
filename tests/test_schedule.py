"""Pay schedule tests, using the fake sample household's anchors.

Alex: every other Friday from 2027-01-01. Sam: the opposite Fridays, from 2027-01-08.
So every Friday has exactly one check.
"""

from datetime import date

import pytest

from budget.schedule import (
    PerCheckLineTemplate,
    Schedule,
    checks_for_month,
    month_bounds,
    next_month,
    pay_dates_in_month,
    per_check_lines,
    prev_month,
)

ALEX = Schedule(person_id=1, anchor=date(2027, 1, 1))
SAM = Schedule(person_id=2, anchor=date(2027, 1, 8))


def fridays(year, month):
    first, last = month_bounds(year, month)
    d = first
    out = []
    while d <= last:
        if d.weekday() == 4:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_month_helpers():
    assert month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))
    assert next_month(2027, 12) == (2028, 1)
    assert prev_month(2027, 1) == (2026, 12)


def test_pay_dates_before_anchor_month():
    # Schedules extend backward from the anchor too.
    assert pay_dates_in_month(date(2027, 1, 1), 14, 2026, 12) == [
        date(2026, 12, 4),
        date(2026, 12, 18),
    ]


def test_pay_dates_anchor_on_first_and_last_day():
    assert pay_dates_in_month(date(2027, 1, 1), 14, 2027, 1)[0] == date(2027, 1, 1)
    assert pay_dates_in_month(date(2027, 12, 31), 14, 2027, 12)[-1] == date(2027, 12, 31)


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        pay_dates_in_month(date(2027, 1, 1), 0, 2027, 1)


@pytest.mark.parametrize("year,month", [(2027, m) for m in range(1, 13)] + [(2028, 2), (2026, 11)])
def test_every_friday_has_exactly_one_check(year, month):
    checks = checks_for_month([ALEX, SAM], year, month)
    assert [c.pay_date for c in checks] == fridays(year, month)
    assert len(checks) in (4, 5)
    # Each person gets 2 or 3 checks, numbered 1..n in date order.
    for pid in (1, 2):
        mine = [c for c in checks if c.person_id == pid]
        assert len(mine) in (2, 3)
        assert [c.ordinal for c in mine] == list(range(1, len(mine) + 1))


def test_five_friday_month_starting_on_the_first_alex_gets_third():
    # January 2027: Fridays 1, 8, 15, 22, 29.
    checks = checks_for_month([ALEX, SAM], 2027, 1)
    assert [(c.person_id, c.pay_date.day, c.ordinal) for c in checks] == [
        (1, 1, 1),
        (2, 8, 1),
        (1, 15, 2),
        (2, 22, 2),
        (1, 29, 3),
    ]


def test_five_friday_month_sam_gets_third():
    # April 2027: Fridays 2, 9, 16, 23, 30. Sam has the 2nd, 16th, 30th.
    checks = checks_for_month([ALEX, SAM], 2027, 4)
    sam = [c for c in checks if c.person_id == 2]
    assert [c.pay_date.day for c in sam] == [2, 16, 30]
    assert sam[-1].ordinal == 3


def test_five_friday_month_ending_on_the_31st():
    # December 2027: Fridays 3, 10, 17, 24, 31; the last check is on the last day.
    checks = checks_for_month([ALEX, SAM], 2027, 12)
    assert len(checks) == 5
    assert checks[-1].pay_date == date(2027, 12, 31)
    assert checks[-1].ordinal == 3


def test_four_friday_month():
    checks = checks_for_month([ALEX, SAM], 2027, 2)
    assert len(checks) == 4
    assert all(c.ordinal in (1, 2) for c in checks)


TEMPLATES = [
    PerCheckLineTemplate(kind="groceries", label="Groceries", amount_cents=15000, day_offset=0),
    PerCheckLineTemplate(kind="daycare", label="Childcare", amount_cents=10000, day_offset=3),
]


def test_per_check_lines_are_negative_and_dated():
    lines = per_check_lines(date(2027, 1, 15), TEMPLATES)
    assert [(ln.kind, ln.amount_cents, ln.line_date) for ln in lines] == [
        ("groceries", -15000, date(2027, 1, 15)),
        ("daycare", -10000, date(2027, 1, 18)),
    ]
    assert lines[1].label == "Childcare (Mon 1/18)"


def test_childcare_monday_can_fall_in_next_month():
    # Last Friday of January 2027 is the 29th; the Monday after is Feb 1.
    lines = per_check_lines(date(2027, 1, 29), TEMPLATES)
    assert lines[1].line_date == date(2027, 2, 1)
    assert lines[1].label == "Childcare (Mon 2/1)"
