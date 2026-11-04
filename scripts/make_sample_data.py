"""Create a FAKE sample household for development, tests, and screenshots.

    python scripts/make_sample_data.py                 # -> ./data/sample/
    python scripts/make_sample_data.py --force         # rebuild it
    $env:BUDGET_DATA_DIR = "$PWD\\data\\sample"; uvicorn budget.app:app --reload --port 8765

The household is "Alex" and "Sam" with "Card A" and "Card B" and round,
invented amounts. It never touches the real data folder.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from budget.config import DEFAULT_DATA_DIR, Paths, load_config  # noqa: E402
from budget.db import make_engine, make_sessionmaker, migrate, session_scope  # noqa: E402
from budget.models import BillTemplate, Category, Month, Person  # noqa: E402
from budget.schedule import month_bounds, next_month, prev_month  # noqa: E402
from budget.services import bills, cards, ledger, months, paychecks  # noqa: E402
from budget.services.seed import seed_if_empty  # noqa: E402

BILLS = [
    # name, amount (cents), person, check ordinal
    ("Rent", 120000, "Alex", 2),
    ("Internet", 6000, "Alex", 1),
    ("Electric", 9000, "Alex", 1),
    ("Streaming", 1500, "Alex", 1),
    ("Phone", 8000, "Sam", 1),
    ("Water", 4000, "Sam", 2),
    ("Car insurance", 11000, "Sam", 2),
]
STORES = {
    "Groceries": ["Corner Market", "Big Grocer", "Farm Stand"],
    "Gas": ["Fuel Stop", "Gas Station"],
    "Eating Out": ["Pizza Place", "Taco Truck", "Diner"],
    "Kids": ["School supplies", "Toy shop"],
    "Shopping": ["Online order"],
}


def refuse_real_dir(target: Path) -> None:
    real = Path(DEFAULT_DATA_DIR)
    if target.resolve() == real.resolve() or (target / "denylist.txt").exists():
        sys.exit(
            f"Refusing to write sample data into {target}: that looks like the real data folder."
        )


def fill_month(s, month: Month, rng: random.Random, cats: dict[str, Category], upto: date) -> None:
    """Receive checks, pay their bills, and add everyday spending up to ``upto``."""
    for check in paychecks.checks_in_month(s, month):
        if check.pay_date > upto:
            continue
        paychecks.mark_received(s, check)
        for bill in bills.bills_in_month(s, month):
            if bill.assigned_check_id == check.id and not bill.paid:
                bills.mark_paid(s, bill, check.pay_date)
    first, last = month_bounds(month.year, month.month)
    d = first
    while d <= min(last, upto):
        for cat_name, chance, lo, hi in (
            ("Groceries", 0.28, 40, 160),
            ("Gas", 0.15, 30, 60),
            ("Eating Out", 0.10, 20, 60),
            ("Kids", 0.05, 10, 50),
            ("Shopping", 0.06, 15, 80),
        ):
            if rng.random() < chance:
                ledger.add_txn(
                    s,
                    month=month,
                    date_=d,
                    amount_cents=-rng.randrange(lo, hi, 5) * 100,
                    description=rng.choice(STORES[cat_name]),
                    category_id=cats[cat_name].id,
                    check_locked=False,
                )
        d += timedelta(days=1)


def build(data_dir: Path, today: date, history_months: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "config.example.toml", data_dir / "config.toml")
    cfg = load_config(Paths(data_dir))
    engine = make_engine(cfg.paths.db)
    migrate(engine)
    rng = random.Random(20270101)
    with session_scope(make_sessionmaker(engine)) as s:
        seed_if_empty(s, cfg.seed)
        s.flush()
        people = {p.name: p for p in s.query(Person)}
        for i, (name, amt, who, ordinal) in enumerate(BILLS):
            s.add(
                BillTemplate(
                    name=name,
                    default_amount_cents=amt,
                    assigned_person_id=people[who].id,
                    assigned_check_ordinal=ordinal,
                    sort_order=i,
                )
            )
        s.flush()
        cats = {c.name: c for c in s.query(Category)}

        y, m = today.year, today.month
        for _ in range(history_months):
            y, m = prev_month(y, m)
        balance = 150000
        while (y, m) != (today.year, today.month):
            month = months.start_month(s, y, m, bank_balance_cents=balance, today=today)
            fill_month(s, month, rng, cats, upto=month_bounds(y, m)[1])
            balance = 250000 + rng.randrange(-5, 6) * 10000
            y, m = next_month(y, m)

        live = months.start_month(s, y, m, bank_balance_cents=balance, today=today)
        fill_month(s, live, rng, cats, upto=today)
        ledger.add_txn(
            s,
            month=live,
            date_=today - timedelta(days=1),
            amount_cents=-10000,
            description="Childcare",
            pending=True,
        )
        card_a = cards.add_card(s, "Card A", 150000)
        cards.add_card(s, "Card B", 40000)
        s.flush()
        ledger.add_txn(
            s,
            month=live,
            date_=today,
            amount_cents=-20000,
            description="Card A payment",
            card_id=card_a.id,
        )
    engine.dispose()


def _month_txns(month_id: int):
    from budget.services.common import active_txns_query

    return active_txns_query(month_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "sample"))
    parser.add_argument("--force", action="store_true", help="replace an existing sample database")
    parser.add_argument("--today", help="pretend today is YYYY-MM-DD (default: real today)")
    parser.add_argument("--history-months", type=int, default=6)
    args = parser.parse_args(argv)

    target = Path(args.data_dir)
    refuse_real_dir(target)
    db = target / "budget.db"
    if db.exists():
        if not args.force:
            print(f"{db} already exists. Use --force to rebuild it.")
            return 1
        for p in target.glob("budget.db*"):
            p.unlink()
    today = date.fromisoformat(args.today) if args.today else date.today()
    build(target, today, args.history_months)
    print(f"Sample household written to {target}")
    print(f'Run it:  $env:BUDGET_DATA_DIR = "{target}"; uvicorn budget.app:app --port 8765')
    return 0


if __name__ == "__main__":
    sys.exit(main())
