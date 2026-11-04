"""Importer tests against a small FAKE workbook built in the test (never a real file)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from budget.importer import (
    ImportProblem,
    date_in_text,
    find_workbook,
    import_workbook,
    parse_tab_month,
)
from budget.models import BillTemplate, Category, Month, MonthBill, Person, Txn
from budget.services import months, paychecks, summary


def build_workbook(path):
    wb = Workbook()
    wb.remove(wb.active)

    # Old-style tab: dates in column H, a formula-only amount, a non-date "2/4".
    old = wb.create_sheet("11-26")
    old.sheet_state = "hidden"
    old["F1"] = "Running Tally"
    old["F2"], old["G2"], old["H2"] = "Impact", "Item", "Date"
    rows = [
        (2000, "Alex check 1", datetime(2026, 11, 6)),
        ("=100+50", "Sam check bonus part", datetime(2026, 11, 7)),
        (-45.1, "Corner Market 2/4", None),
        (-30, "Fuel Stop", datetime(2026, 11, 9)),
        ("?", "mystery", None),
    ]
    for i, (f, g, h) in enumerate(rows, start=3):
        old[f"F{i}"], old[f"G{i}"] = f, g
        if h:
            old[f"H{i}"] = h
    old["I15"], old["J15"] = "OUR ACCOUNT BALANCE ON FIRST DAY:", 900

    # Not a month tab.
    wb.create_sheet("BILLS")["A1"] = "template"

    # Live tab, newer style: dates inside the item text, H is a note.
    live = wb.create_sheet("December 2026")
    live["A1"], live["B1"], live["C1"], live["D1"] = "BILL NAME", "AMOUNT", "Paid?", "NOTE"
    bills = [
        ("Rent", 1200, "Alex Check 2", ""),
        ("Internet", 60, "Alex Check 1", ""),
        ("Phone Bill", 80, "Sa Check 1", ""),
        ("Streaming A", 10, "Alex Check 1", ""),
        ("Streaming B", 5, "Alex Check 1", ""),
        ("Childcare x 4", 400, "Weekly", "Mondays"),
        ("Water", 40, "Sam Check 2", "quarterly-ish"),
    ]
    for i, (a, b, c, d) in enumerate(bills, start=2):
        live[f"A{i}"], live[f"B{i}"], live[f"C{i}"], live[f"D{i}"] = a, b, c, d
    live["A10"], live["B10"] = "Special items", "?"
    live["A23"], live["B23"] = "Gas Budget:", "=200+F5"
    live["A24"], live["B24"] = "Groceries Budget:", "=600+F4+F6"

    live["F2"], live["G2"], live["H2"] = "Impact", "Item", "Note"
    ledger = [
        (-60, "Internet December 2026", None),  # pays the Internet bill
        (-50, "Corner Market 12-2-26", None),  # groceries via formula ref
        (-25, "Fuel Stop 12-3-26", "note here"),  # gas via formula ref
        (-20, "Farm Stand", None),  # groceries, no date -> carries previous date
        (-12, "Pizza 1/4", None),  # not referenced -> uncategorized in live month
    ]
    for i, (f, g, h) in enumerate(ledger, start=3):
        live[f"F{i}"], live[f"G{i}"] = f, g
        if h:
            live[f"H{i}"] = h
    live["I15"], live["J15"] = "OUR ACCOUNT BALANCE ON FIRST DAY:", 1500
    live["I20"], live["J20"] = '"Running" surplus/defecit: ', "=sum(F3:F1003)"

    def block(start, header, amount, lines, remainder):
        live[f"A{start}"], live[f"B{start}"], live[f"C{start}"] = header, amount, "Check Amount"
        r = start + 1
        for amt, label, note in lines:
            live[f"B{r}"], live[f"C{r}"] = amt, label
            if note:
                live[f"A{r}"] = note
            r += 1
        live[f"B{r}"], live[f"C{r}"] = f"=sum(B{start}:B{r - 1})", "To joint"
        return remainder

    block(
        26,
        "Alex Check 1 Calculation (12-4-2026)",
        2000,
        [
            (-60, "Internet December 2026", None),
            (-15, "Streaming A, B December 2026", None),  # combined line, two bills
            (-150, "Groceries 12-4-26", None),
            (-100, "Childcare 12-7-26", None),
            (-50, "Card A", None),
        ],
        1625,
    )
    block(
        34,
        "Sam Check 1 Calculation (12-11-26)",
        1500,
        [(-82, "Phone Bill December 2026", None), (-100, "Childcare 12-14-26", "estimate")],
        1318,
    )
    block(
        40,
        "Alex Check 2 Calculation (12-18-2026)",
        2000,
        [(-1200, "Rent December 2026", None), (-150, "Groceries 12-18-26", None)],
        650,
    )
    block(
        45,
        "Sam BONUS Calculation (12-18-2026)",
        500,
        [(-100, "Joint", None), (-50, "Savings", None)],
        350,
    )
    live["C48"] = "Gifts"  # the sheet's remainder label: what's left goes to gifts
    block(
        49,
        "Sam Check 2 Calculation (12-25-2026)",
        1500,
        [(-45, "Water", None)],
        1455,
    )
    # A stale block copied from an earlier month must be ignored.
    block(150, "Alex Check 1 Calculation (9-11-2026)", 2000, [(-1, "old", None)], 1999)

    # Cached values aren't written by openpyxl; formulas fall back to the safe parser,
    # except =sum(...) rows, which have no cached value here (treated as unknown).
    wb.save(path)


@pytest.fixture
def workbook(tmp_path):
    p = tmp_path / "Import" / "Fake Budget.xlsx"
    p.parent.mkdir()
    build_workbook(p)
    return p


@pytest.fixture
def imported(s, workbook):
    # The shared fixture seeds Alex & Sam plus bill templates; start from people/categories only.
    for t in s.scalars(select(BillTemplate)):
        s.delete(t)
    for p in s.scalars(select(Person)):
        s.delete(p)
    s.flush()
    result = import_workbook(
        s,
        workbook,
        live_key="2026-12",
        auto_tag={"pizza": "Eating Out", "fuel": "Gas", "market": "Groceries"},
        account_name="Joint",
    )
    s.commit()
    return result


def test_parse_tab_month():
    assert parse_tab_month("4-25") == (2025, 4)
    assert parse_tab_month("12-25") == (2025, 12)
    assert parse_tab_month("January 2026") == (2026, 1)
    assert parse_tab_month("Sept 2026") == (2026, 9)
    assert parse_tab_month("BILLS") is None
    assert parse_tab_month("13-25") is None


def test_date_in_text_requires_year():
    assert date_in_text("Groceries 10-2-26 (1 of 5)") == date(2026, 10, 2)
    assert date_in_text("Alex Check 1 (10-9-2026)") == date(2026, 10, 9)
    assert date_in_text("Pizza 2/4") is None
    assert date_in_text("DQ (11/10)") is None
    assert date_in_text("bad 13-45-26") is None


def test_find_workbook_needs_exactly_one(tmp_path):
    with pytest.raises(ImportProblem):
        find_workbook(tmp_path)
    (tmp_path / "a.xlsx").write_bytes(b"x")
    (tmp_path / "~$a.xlsx").write_bytes(b"x")  # Excel lock file is ignored
    assert find_workbook(tmp_path).name == "a.xlsx"
    (tmp_path / "b.xlsx").write_bytes(b"x")
    with pytest.raises(ImportProblem):
        find_workbook(tmp_path)


def test_history_month(s, imported):
    nov = s.scalar(select(Month).where(Month.year == 2026, Month.month == 11))
    assert nov.is_history and nov.locked
    assert nov.bank_balance_on_first_cents == 90000
    txns = list(s.scalars(select(Txn).where(Txn.month_id == nov.id).order_by(Txn.id)))
    assert [t.amount_cents for t in txns] == [200000, 15000, -4510, -3000]
    # "2/4" is not a date: the row without a date carries the previous row's date.
    assert txns[2].date == date(2026, 11, 7)
    # Keyword auto-tagging applies to history.
    cats = {c.id: c.name for c in s.scalars(select(Category))}
    assert cats[txns[2].category_id] == "Groceries"
    assert cats[txns[3].category_id] == "Gas"
    assert all(t.source == "import" for t in txns)
    assert any("ledger row 7" in line and "can't read" in line for line in imported.report)


def test_live_month_ledger_and_targets(s, imported):
    dec = imported.live_month
    assert (dec.year, dec.month, dec.locked, dec.is_history) == (2026, 12, False, False)
    assert dec.bank_balance_on_first_cents == 150000
    txns = {t.description: t for t in s.scalars(select(Txn).where(Txn.month_id == dec.id))}
    assert txns["Corner Market 12-2-26"].date == date(2026, 12, 2)
    assert txns["Farm Stand"].date == date(2026, 12, 3)
    assert txns["Fuel Stop 12-3-26"].note == "note here"
    # Live month: only the sheet's formula references categorize (no keyword guessing),
    # so "remaining" matches the sheet exactly.
    assert txns["Pizza 1/4"].category_id is None
    sm = summary.month_summary(s, dec, today=date(2026, 12, 1))
    remaining = {t.category.name: t.remaining_cents for t in sm.targets}
    assert remaining == {"Groceries": 60000 - 7000, "Gas": 20000 - 2500}
    assert sm.running_total_cents == -16700


def test_live_people_checks_and_remainders(s, imported):
    people = {p.name: p for p in s.scalars(select(Person))}
    assert people["Alex"].pay_anchor_date == date(2026, 12, 4)
    assert people["Sam"].pay_anchor_date == date(2026, 12, 11)
    assert people["Alex"].default_check_cents == 200000
    dec = imported.live_month
    checks = paychecks.checks_in_month(s, dec)
    assert [(c.label, c.pay_date.day, c.is_one_off) for c in checks] == [
        ("Alex check 1", 4, False),
        ("Sam check 1", 11, False),
        ("Alex check 2", 18, False),
        ("Sam BONUS", 18, True),
        ("Sam check 2", 25, False),
    ]
    rem = {c.label: paychecks.remainder(c) for c in checks}
    assert rem == {
        "Alex check 1": 162500,
        "Sam check 1": 131800,
        "Alex check 2": 65000,
        "Sam BONUS": 10000,
        "Sam check 2": 145500,
    }
    a1 = checks[0]
    kinds = [ln.kind for ln in a1.lines]
    assert kinds == ["bill", "bill", "bill", "groceries", "daycare", "card_payment"]
    assert a1.lines[4].line_date == date(2026, 12, 7)
    s1 = checks[1]
    assert s1.lines[1].label == "Childcare 12-14-26 (estimate)"
    # Stale September block skipped.
    assert any("stale block" in line for line in imported.report)


def test_live_bills_template_and_links(s, imported):
    dec = imported.live_month
    bills = {b.name: b for b in s.scalars(select(MonthBill).where(MonthBill.month_id == dec.id))}
    assert set(bills) == {"Rent", "Internet", "Phone Bill", "Streaming A", "Streaming B", "Water"}
    # Weekly item becomes per-check lines, not a bill.
    assert any("weekly item" in line for line in imported.report)
    # Internet: paid by a matching ledger entry, and its check line is linked.
    assert bills["Internet"].paid and bills["Internet"].paid_txn_id
    # "Sa" prefix maps to Sam; the line amount (82) is close to the bill (80) so it links.
    assert bills["Phone Bill"].assigned_check.label == "Sam check 1"
    assert paychecks.bill_line(s, bills["Phone Bill"]).amount_cents == -8200
    # Combined line "Streaming A, B" (15) = 10 + 5: split into one line per bill.
    a1 = bills["Streaming A"].assigned_check
    assert paychecks.bill_line(s, bills["Streaming A"]).amount_cents == -1000
    assert paychecks.bill_line(s, bills["Streaming B"]).amount_cents == -500
    assert paychecks.remainder(a1) == 162500  # unchanged by the split
    # Water: line amount (45) too far from the bill (40) -> left as on the sheet.
    assert paychecks.bill_line(s, bills["Water"]) is None
    tpl = s.scalar(select(BillTemplate).where(BillTemplate.name == "Rent"))
    assert tpl.assigned_check_ordinal == 2 and tpl.person.name == "Alex"
    assert not bills["Rent"].paid


def test_editing_unlinked_bill_does_not_add_a_duplicate_line(s, imported):
    from budget.services import bills as bill_svc

    dec = imported.live_month
    a1 = next(c for c in paychecks.checks_in_month(s, dec) if c.label == "Alex check 1")
    before = paychecks.remainder(a1)
    water = s.scalar(select(MonthBill).where(MonthBill.name == "Water"))
    s2 = water.assigned_check
    before_s2 = paychecks.remainder(s2)
    bill_svc.update_bill(s, water, amount_cents=4200)
    s.commit()
    s.refresh(s2)
    assert paychecks.remainder(s2) == before_s2
    assert paychecks.remainder(a1) == before


def test_next_month_after_import(s, imported):
    jan = months.start_month(s, 2027, 1, bank_balance_cents=100000)
    s.commit()
    checks = paychecks.checks_in_month(s, jan)
    # Alex from 12-4 -> Jan 1, 15, 29 ; Sam from 12-11 -> Jan 8, 22.
    assert [(c.label, c.pay_date.day) for c in checks] == [
        ("Alex check 1", 1),
        ("Sam check 1", 8),
        ("Alex check 2", 15),
        ("Sam check 2", 22),
        ("Alex check 3", 29),
    ]
    jbills = {b.name: b for b in s.scalars(select(MonthBill).where(MonthBill.month_id == jan.id))}
    assert jbills["Rent"].assigned_check.label == "Alex check 2"
    assert not any(b.paid for b in jbills.values())


def test_refuses_non_empty_database(s, imported, workbook):
    with pytest.raises(ImportProblem):
        import_workbook(s, workbook)


def test_name_matching():
    from budget.importer import _names_match

    assert _names_match("Water / Elect November 2026", "Water + Electric")
    assert _names_match("StreamBox November 2026", "Stream Box Extra")
    assert _names_match("Books/Music November 2026", "Books and Music")
    assert not _names_match("Card A", "Internet")


def test_joint_account_line_becomes_the_remainder(s, imported):
    bonus = next(c for c in paychecks.checks_in_month(s, imported.live_month) if c.is_one_off)
    assert [(ln.label, ln.amount_cents) for ln in bonus.lines] == [
        ("Gifts", -35000),
        ("Savings", -5000),
    ]
    assert paychecks.remainder(bonus) == 10000  # the 100 that went to the joint account
    assert any("re-pointed to the joint account" in line for line in imported.report)
