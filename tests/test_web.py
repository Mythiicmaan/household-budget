"""End-to-end tests through the HTTP layer (sample data only)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from budget.app import create_app
from budget.config import Paths
from budget.models import BillTemplate, Category, MonthBill, Paycheck, Person, Txn

CONFIG = """
[app]
account_name = "Joint"

[[seed.categories]]
name = "Groceries"
target = "600.00"

[[seed.categories]]
name = "Gas"
target = "200.00"

[[seed.categories]]
name = "Bills"

[[seed.people]]
name = "Alex"
pay_anchor = "2027-01-01"
default_check = "2000.00"

[[seed.people]]
name = "Sam"
pay_anchor = "2027-01-08"
default_check = "1500.00"

[[seed.per_check_lines]]
kind = "groceries"
label = "Groceries"
amount = "150.00"
day_offset = 0
"""


@pytest.fixture
def app(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    return create_app(Paths(tmp_path))


def local_client(app) -> TestClient:
    return TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))


def phone_client(app) -> TestClient:
    """Looks like a phone coming through `tailscale serve`."""
    c = TestClient(app, base_url="https://pc.example-tailnet.ts.net", client=("127.0.0.1", 50001))
    c.headers.update({"X-Forwarded-For": "100.64.0.7", "X-Forwarded-Proto": "https"})
    return c


@pytest.fixture
def client(app):
    with local_client(app) as c:
        yield c


def db(app):
    return app.state.sessionmaker()


def start_month(client, ym="2027-1", balance="1000"):
    return client.post("/month/new", data={"ym": ym, "bank_balance": balance})


def test_welcome_then_start_month(client, app):
    r = client.get("/")
    assert r.status_code == 200 and "No months yet" in r.text
    # Preview offers the current calendar month first; start a fixed month directly instead.
    with db(app) as s:
        from budget.services.months import start_month as svc_start

        svc_start(s, 2027, 1, bank_balance_cents=100000)
        s.commit()
    r = client.get("/")
    assert "January 2027" in r.text
    assert "$1,000.00" in r.text  # true starting balance


@pytest.fixture
def jan(client, app):
    with db(app) as s:
        people = {p.name: p for p in s.scalars(select(Person))}
        s.add(
            BillTemplate(
                name="Rent",
                default_amount_cents=120000,
                assigned_person_id=people["Alex"].id,
                assigned_check_ordinal=2,
            )
        )
        s.commit()
        from budget.services.months import start_month as svc_start

        svc_start(s, 2027, 1, bank_balance_cents=100000)
        s.commit()


def test_quick_add_and_duplicate_tap(client, app, jan):
    form = {
        "amount": "45.10",
        "direction": "out",
        "description": "Corner Market",
        "date": "2027-01-04",
        "token": "abc123",
    }
    with db(app) as s:
        groceries = s.scalar(select(Category).where(Category.name == "Groceries"))
        form["category_id"] = str(groceries.id)
    r1 = client.post("/add", data=form, follow_redirects=False)
    r2 = client.post("/add", data=form, follow_redirects=False)
    assert r1.status_code == r2.status_code == 303
    with db(app) as s:
        txns = list(s.scalars(select(Txn)))
        assert len(txns) == 1
        assert txns[0].amount_cents == -4510
    page = client.get("/").text
    assert "Saved: Corner Market" in page  # toast
    assert "$554.90 left" in page  # 600 - 45.10


def test_quick_add_money_in_and_bad_amount(client, app, jan):
    client.post("/add", data={"amount": "20", "direction": "in", "date": "2027-01-04"})
    r = client.post("/add", data={"amount": "abc", "date": "2027-01-04"}, follow_redirects=False)
    assert r.status_code == 303
    with db(app) as s:
        assert [t.amount_cents for t in s.scalars(select(Txn))] == [2000]


def test_bill_toggle_and_delete_entry_unmarks(client, app, jan):
    with db(app) as s:
        rent = s.scalar(select(MonthBill).where(MonthBill.name == "Rent"))
    client.post(f"/bill/{rent.id}/toggle")
    with db(app) as s:
        rent = s.get(MonthBill, rent.id)
        assert rent.paid and rent.paid_txn_id
        txn_id = rent.paid_txn_id
    page = client.get(f"/txn/{txn_id}").text
    assert "Deleting it will mark that bill unpaid" in page
    client.post(f"/txn/{txn_id}/delete")
    with db(app) as s:
        assert not s.get(MonthBill, rent.id).paid
    client.post(f"/txn/{txn_id}/restore")
    with db(app) as s:
        assert s.get(MonthBill, rent.id).paid


def test_check_edit_page_saves_lines(client, app, jan):
    with db(app) as s:
        a2 = s.scalar(select(Paycheck).where(Paycheck.label == "Alex check 2"))
        lines = {ln.kind: ln.id for ln in a2.lines}
    r = client.post(
        f"/check/{a2.id}",
        data={
            "label": "Alex check 2",
            "amount": "2100",
            f"line-{lines['groceries']}-amount": "175",
            f"line-{lines['bill']}-amount": "1200",
            "new-amount": "50",
            "new-label": "Savings",
            "new-kind": "savings",
        },
    )
    assert r.status_code == 200
    with db(app) as s:
        from budget.services.paychecks import remainder

        a2 = s.get(Paycheck, a2.id)
        assert remainder(a2) == 210000 - 17500 - 120000 - 5000
    assert "$675.00" in client.get("/checks").text


def test_received_check_counts_in_running_total(client, app, jan):
    with db(app) as s:
        s1 = s.scalar(select(Paycheck).where(Paycheck.label == "Sam check 1"))
    client.post(f"/check/{s1.id}/toggle")
    assert "+$1,500.00" in client.get("/ledger").text


def test_new_month_flow_with_pending(client, app, jan):
    client.post(
        "/add",
        data={"amount": "100", "description": "Childcare", "date": "2027-01-29", "pending": "on"},
    )
    page = client.get("/month/new").text
    assert "Start February 2027" in page and "Has this cleared yet?" in page
    r = start_month(client, ym="2027-2", balance="900")
    assert r.status_code == 200
    page = client.get("/").text
    assert "February 2027" in page
    assert "$800.00" in page  # 900 - 100 pending
    # January is now read-only.
    jan_page = client.get("/ledger?m=2027-01").text
    assert "read-only" in jan_page


def test_cards_flow(client, app, jan):
    client.post("/cards/add", data={"name": "Card A", "balance": "500"})
    with db(app) as s:
        from budget.models import Card

        card = s.scalar(select(Card))
    client.post("/add", data={"amount": "100", "card_id": str(card.id), "date": "2027-01-05"})
    client.post(f"/card/{card.id}/balance", data={"balance": "450", "note": "store order"})
    page = client.get("/cards").text
    assert "$450.00" in page and "store order" in page
    client.post(f"/card/{card.id}/archive", data={"archived": "on"})
    assert "Archived cards" in client.get("/cards").text
    client.post(f"/card/{card.id}/delete")
    assert "No cards yet" in client.get("/cards").text


def test_other_pages_render(client, app, jan):
    for path in ("/ledger", "/bills", "/checks", "/cards", "/history", "/settings", "/add"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/health").json()["status"] == "ok"
    api = client.get("/api/summary").json()
    assert api["month"] == "2027-01" and api["true_start_cents"] == 100000
    png = client.get("/apple-touch-icon.png")
    assert png.content.startswith(b"\x89PNG")
    csv = client.get("/export?scope=all&fmt=csv")
    assert csv.text.startswith("month,date,description")
    xlsx = client.get("/export?scope=month&fmt=xlsx")
    assert xlsx.content[:2] == b"PK"


def test_settings_category_target(client, app, jan):
    with db(app) as s:
        gas = s.scalar(select(Category).where(Category.name == "Gas"))
    client.post(f"/settings/category/{gas.id}", data={"name": "Fuel", "target": "250"})
    with db(app) as s:
        gas = s.get(Category, gas.id)
        assert gas.name == "Fuel" and gas.monthly_target_cents == 25000


# ---------------------------------------------------------------- security


def test_security_headers(client):
    r = client.get("/health")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_bad_host_rejected(app):
    with TestClient(app, base_url="http://evil.example.com", client=("127.0.0.1", 1)) as c:
        assert c.get("/").status_code == 400


def test_cross_site_post_blocked(client):
    r = client.post("/cards/add", data={"name": "X"}, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    r = client.post("/cards/add", data={"name": "X"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_phone_without_pin_set_is_refused(app):
    with phone_client(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 403
        assert c.get("/api/summary").status_code == 401
        assert c.get("/health").status_code == 200


def test_phone_pin_login_flow_and_rate_limit(app):
    with local_client(app) as desk:
        desk.post("/settings/pin", data={"new": "4321", "confirm": "4321"})
    with phone_client(app) as phone:
        r = phone.get("/ledger", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        bad = phone.post("/login", data={"pin": "0000", "next": "/ledger"}, follow_redirects=False)
        assert bad.status_code == 303 and "hb_session" not in bad.cookies
        ok = phone.post("/login", data={"pin": "4321", "next": "/ledger"}, follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/ledger"
        cookie = ok.headers["set-cookie"]
        assert (
            "HttpOnly" in cookie and "Secure" in cookie and re.search("samesite=lax", cookie, re.I)
        )
        assert phone.get("/settings").status_code == 200
    with phone_client(app) as attacker:
        for _ in range(5):
            attacker.post("/login", data={"pin": "1111"})
        r = attacker.post("/login", data={"pin": "4321"}, follow_redirects=False)
        assert "hb_session" not in r.headers.get("set-cookie", "")
        app.state.limiter.reset()


def test_open_redirect_blocked(app):
    with local_client(app) as desk:
        desk.post("/settings/pin", data={"new": "4321", "confirm": "4321"})
    with phone_client(app) as phone:
        r = phone.post(
            "/login", data={"pin": "4321", "next": "//evil.example.com"}, follow_redirects=False
        )
        assert r.headers["location"] == "/"


def test_localhost_skip_can_be_disabled(tmp_path):
    (tmp_path / "config.toml").write_text(
        CONFIG + "\n[auth]\nlocalhost_skip_pin = false\n", encoding="utf-8"
    )
    app = create_app(Paths(tmp_path))
    with local_client(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 403  # no PIN set yet


def test_today_used_for_default_date(client, app, jan):
    # Default date is today; with January 2027 as the only month, it lands there.
    client.post("/add", data={"amount": "1"})
    with db(app) as s:
        t = s.scalar(select(Txn))
        assert t.date == date.today()


def test_check_item_toggle_and_still_to_pay(client, app, jan):
    with db(app) as s:
        a1 = s.scalar(select(Paycheck).where(Paycheck.label == "Alex check 1"))
        savings_line = None
        from budget.services import paychecks as pc

        savings_line = pc.add_line(s, a1, kind="savings", label="Savings", amount_cents=-5000)
        s.commit()
        line_id = savings_line.id
    page = client.get("/").text
    assert "Still to pay this month" in page and "Savings" in page and "Rent" in page
    assert "How this adds up" in page
    r = client.post(f"/line/{line_id}/toggle")
    assert r.status_code == 200
    with db(app) as s:
        from budget.models import PaycheckLine

        line = s.get(PaycheckLine, line_id)
        assert line.paid and s.get(Txn, line.paid_txn_id).amount_cents == -5000
    assert "Mark paid" not in client.get("/checks").text.split("Savings")[1][:400]


def test_ledger_bad_filter_does_not_crash(client, jan):
    assert client.get("/ledger?cat=abc").status_code == 200


def test_saving_a_check_unchanged_leaves_its_bills_alone(client, app, jan):
    """Imported bill lines can be worded/priced differently from the bill list. Pressing
    Save on the check without touching them must not rewrite the bill."""
    with db(app) as s:
        rent = s.scalar(select(MonthBill).where(MonthBill.name == "Rent"))
        from budget.services import paychecks as pc

        line = pc.bill_line(s, rent)
        line.label, line.amount_cents = "Rent December 2026", -121000
        check = line.paycheck
        form = {"label": check.label, "amount": "2000.00"}
        for ln in check.lines:
            form[f"line-{ln.id}-label"] = ln.label
            form[f"line-{ln.id}-amount"] = f"{abs(ln.amount_cents) / 100:.2f}"
        s.commit()
        rent_id, check_id, line_id = rent.id, check.id, line.id
    client.post(f"/check/{check_id}", data=form)
    with db(app) as s:
        rent = s.get(MonthBill, rent_id)
        assert (rent.name, rent.amount_cents) == ("Rent", 120000)
    # Actually changing the line's amount does update the bill (they mirror each other).
    form[f"line-{line_id}-amount"] = "1250.00"
    client.post(f"/check/{check_id}", data=form)
    with db(app) as s:
        assert s.get(MonthBill, rent_id).amount_cents == 125000


def test_bills_page_daycare_paid_1x_and_undo(tmp_path):
    (tmp_path / "config.toml").write_text(
        CONFIG
        + """
[[seed.per_check_lines]]
kind = "daycare"
label = "Childcare"
amount = "100.00"
day_offset = 3
""",
        encoding="utf-8",
    )
    fresh = create_app(Paths(tmp_path))
    with local_client(fresh) as c:
        with db(fresh) as s:
            from budget.services.months import start_month as svc_start

            month_id = svc_start(s, 2027, 1, bank_balance_cents=100000).id
            s.commit()
        page = c.get("/bills").text
        assert "Childcare" in page and "0 of 5 paid" in page and "Paid 1x" in page
        assert "$600.00" in page  # the Groceries budget, same as Home

        r = c.post(f"/month/{month_id}/daycare/pay", follow_redirects=False)
        assert r.status_code == 303
        assert "1 of 5 paid" in c.get("/bills").text
        with db(fresh) as s:
            t = s.scalar(select(Txn))
            assert t.amount_cents == -10000 and t.pending

        c.post(f"/month/{month_id}/daycare/undo")
        assert "0 of 5 paid" in c.get("/bills").text


def test_editing_bill_amount_on_the_bills_page(client, app, jan):
    with db(app) as s:
        rent = s.scalar(select(MonthBill).where(MonthBill.name == "Rent"))
        rent_id, check_id = rent.id, rent.assigned_check_id
    r = client.post(
        f"/bill/{rent_id}",
        data={"name": "Rent", "amount": "1250", "note": "", "check_id": str(check_id)},
    )
    assert r.status_code == 200
    page = client.get("/bills").text
    assert "$1,250.00" in page and "Edit" in page
    assert "1250" in client.get("/checks").text
