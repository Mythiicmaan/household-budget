"""Shared fixtures. Everything here uses the FAKE sample household."""

from __future__ import annotations

from datetime import date

import pytest

from budget.db import make_engine, make_sessionmaker, migrate
from budget.models import BillTemplate, Person
from budget.services.seed import seed_if_empty

SAMPLE_SEED = {
    "categories": [
        {"name": "Groceries", "target": "600.00"},
        {"name": "Gas", "target": "200.00"},
        {"name": "Eating Out"},
        {"name": "Bills"},
        {"name": "Other"},
    ],
    "people": [
        {"name": "Alex", "pay_anchor": "2027-01-01", "default_check": "2000.00"},
        {"name": "Sam", "pay_anchor": "2027-01-08", "default_check": "1500.00"},
    ],
    "per_check_lines": [
        {"kind": "groceries", "label": "Groceries", "amount": "150.00", "day_offset": 0},
        {"kind": "daycare", "label": "Childcare", "amount": "100.00", "day_offset": 3},
    ],
}

# (name, amount, person, check ordinal)
SAMPLE_BILLS = [
    ("Rent", 120000, "Alex", 2),
    ("Internet", 6000, "Alex", 1),
    ("Phone", 8000, "Sam", 1),
    ("Water", 4000, "Sam", 2),
]


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path / "budget.db")
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def s(engine):
    factory = make_sessionmaker(engine)
    session = factory()
    seed_if_empty(session, SAMPLE_SEED)
    people = {p.name: p for p in session.query(Person)}
    for i, (name, amt, who, ordinal) in enumerate(SAMPLE_BILLS):
        session.add(
            BillTemplate(
                name=name,
                default_amount_cents=amt,
                assigned_person_id=people[who].id,
                assigned_check_ordinal=ordinal,
                sort_order=i,
            )
        )
    session.commit()
    yield session
    session.close()


@pytest.fixture
def jan(s):
    from budget.services.months import start_month

    m = start_month(s, 2027, 1, bank_balance_cents=100000, today=date(2027, 1, 1))
    s.commit()
    return m
