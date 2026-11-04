from datetime import date

import pytest
from sqlalchemy import select

from budget.models import BillTemplate, Category, Month, Paycheck, Person, Txn
from budget.services import bills, cards, ledger, months, paychecks, summary
from budget.services.common import BudgetError, month_for_date


def cat(s, name):
    return s.scalar(select(Category).where(Category.name == name))


def check_for(s, month, person_name, ordinal):
    person = s.scalar(select(Person).where(Person.name == person_name))
    return s.scalar(
        select(Paycheck).where(
            Paycheck.month_id == month.id,
            Paycheck.person_id == person.id,
            Paycheck.ordinal == ordinal,
        )
    )


def bill_named(s, month, name):
    return next(b for b in bills.bills_in_month(s, month) if b.name == name)


# ---------------------------------------------------------------- new month


def test_start_month_generates_five_checks_with_lines_and_bills(s, jan):
    checks = paychecks.checks_in_month(s, jan)
    assert [(c.label, c.pay_date.day) for c in checks] == [
        ("Alex check 1", 1),
        ("Sam check 1", 8),
        ("Alex check 2", 15),
        ("Sam check 2", 22),
        ("Alex check 3", 29),
    ]
    a1 = check_for(s, jan, "Alex", 1)
    assert [(ln.kind, ln.amount_cents) for ln in a1.lines] == [
        ("groceries", -15000),
        ("daycare", -10000),
        ("bill", -6000),
    ]
    assert paychecks.remainder(a1) == 200000 - 15000 - 10000 - 6000
    # Rent is essentially the whole 2nd check.
    assert paychecks.remainder(check_for(s, jan, "Alex", 2)) == 200000 - 25000 - 120000
    # The 3rd check (5-Friday month) has only the per-check lines.
    a3 = check_for(s, jan, "Alex", 3)
    assert [ln.kind for ln in a3.lines] == ["groceries", "daycare"]
    assert paychecks.remainder(a3) == 200000 - 25000


def test_month_boundary_childcare_belongs_to_checks_month(s, jan):
    a3 = check_for(s, jan, "Alex", 3)  # Friday Jan 29
    daycare = next(ln for ln in a3.lines if ln.kind == "daycare")
    assert daycare.line_date == date(2027, 2, 1)
    assert a3.month_id == jan.id


def test_start_month_twice_is_rejected(s, jan):
    with pytest.raises(BudgetError):
        months.start_month(s, 2027, 1, bank_balance_cents=0)


def test_bills_copy_last_months_amounts_and_one_month_edit_keeps_template(s, jan):
    internet = bill_named(s, jan, "Internet")
    bills.update_bill(s, internet, amount_cents=6550)  # this month only
    s.commit()
    tpl = s.scalar(select(BillTemplate).where(BillTemplate.name == "Internet"))
    assert tpl.default_amount_cents == 6000
    # The check line follows the bill.
    a1 = check_for(s, jan, "Alex", 1)
    assert next(ln for ln in a1.lines if ln.kind == "bill").amount_cents == -6550

    feb = months.start_month(s, 2027, 2, bank_balance_cents=50000)
    s.commit()
    assert bill_named(s, feb, "Internet").amount_cents == 6550  # last month's amount
    assert not bill_named(s, feb, "Internet").paid
    assert jan.locked and not feb.locked


def test_update_bill_also_template_and_reassign_check(s, jan):
    water = bill_named(s, jan, "Water")
    a1 = check_for(s, jan, "Alex", 1)
    bills.update_bill(s, water, amount_cents=4500, check=a1, also_template=True)
    s.commit()
    tpl = water.template
    assert tpl.default_amount_cents == 4500
    assert tpl.assigned_check_ordinal == 1
    assert tpl.assigned_person_id == a1.person_id
    s2 = check_for(s, jan, "Sam", 2)
    assert all(ln.month_bill_id != water.id for ln in s2.lines)
    assert any(ln.month_bill_id == water.id for ln in a1.lines)


def test_add_and_remove_bill(s, jan):
    s1 = check_for(s, jan, "Sam", 1)
    b = bills.add_bill(s, jan, name="Quarterly fee", amount_cents=3000, check=s1, note="every 3 mo")
    s.commit()
    assert paychecks.remainder(s1) == 150000 - 25000 - 8000 - 3000
    bills.remove_bill(s, b)
    s.commit()
    s.refresh(s1)
    assert paychecks.remainder(s1) == 150000 - 25000 - 8000


# ---------------------------------------------------------------- pending carryover


def test_pending_carryover_and_true_starting_balance(s, jan):
    childcare = ledger.add_txn(
        s,
        month=jan,
        date_=date(2027, 1, 25),
        amount_cents=-10000,
        description="Childcare",
        pending=True,
    )
    cleared = ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 28), amount_cents=-5000, description="Check", pending=True
    )
    s.commit()
    preview = months.next_month_preview(s)
    assert (preview.year, preview.month) == (2027, 2)
    assert {t.id for t in preview.outstanding} == {childcare.id, cleared.id}

    feb = months.start_month(
        s, 2027, 2, bank_balance_cents=100000, cleared_txn_ids={cleared.id}, today=date(2027, 2, 1)
    )
    s.commit()
    # Bank shows $1,000 but the $100 childcare check hasn't cashed: true start is $900.
    assert months.true_starting_balance(s, feb) == 90000
    assert cleared.cleared_date == date(2027, 2, 1)
    assert [t.id for t in summary.month_summary(s, feb).pending] == [childcare.id]

    # It clears after the new month started: the starting balance stays put,
    # and it drops off the pending list.
    ledger.set_cleared(childcare, date(2027, 2, 3))
    s.commit()
    assert months.true_starting_balance(s, feb) == 90000
    assert summary.month_summary(s, feb).pending == []


def test_still_pending_carries_into_following_month_too(s, jan):
    t = ledger.add_txn(s, month=jan, date_=date(2027, 1, 30), amount_cents=-2000, pending=True)
    feb = months.start_month(s, 2027, 2, bank_balance_cents=1000)
    mar = months.start_month(s, 2027, 3, bank_balance_cents=1000)
    s.commit()
    assert [c.txn_id for c in months.carryovers_into(s, feb)] == [t.id]
    assert [c.txn_id for c in months.carryovers_into(s, mar)] == [t.id]


# ---------------------------------------------------------------- bills <-> ledger


def test_mark_bill_paid_creates_entry_and_deleting_it_unmarks(s, jan):
    rent = bill_named(s, jan, "Rent")
    txn = bills.mark_paid(s, rent, date(2027, 1, 15))
    s.commit()
    assert rent.paid and txn.amount_cents == -120000 and txn.source == "bill"
    assert txn.category_id == cat(s, "Bills").id

    links = ledger.delete_txn(s, txn)
    s.commit()
    assert links.bill is rent and not rent.paid
    assert summary.month_summary(s, jan).running_total_cents == 0

    ledger.restore_txn(s, txn)  # undo
    s.commit()
    assert rent.paid
    assert summary.month_summary(s, jan).running_total_cents == -120000


def test_mark_unpaid_then_paid_reuses_entry(s, jan):
    phone = bill_named(s, jan, "Phone")
    t1 = bills.mark_paid(s, phone, date(2027, 1, 8))
    bills.mark_unpaid(s, phone)
    t2 = bills.mark_paid(s, phone, date(2027, 1, 9))
    s.commit()
    assert t1.id == t2.id and t2.deleted_at is None and phone.paid
    assert len(list(s.scalars(select(Txn).where(Txn.month_id == jan.id)))) == 1


# ---------------------------------------------------------------- checks <-> ledger


def test_mark_received_and_one_off_same_day(s, jan):
    a2 = check_for(s, jan, "Alex", 2)
    bonus = paychecks.add_one_off(
        s, jan, label="Alex bonus", pay_date=a2.pay_date, amount_cents=50000, person_id=a2.person_id
    )
    paychecks.add_line(s, bonus, kind="savings", label="Savings", amount_cents=-20000)
    s.commit()
    same_day = [c for c in paychecks.checks_in_month(s, jan) if c.pay_date == a2.pay_date]
    assert [c.is_one_off for c in same_day] == [False, True]
    assert paychecks.remainder(bonus) == 30000

    paychecks.mark_received(s, a2)
    paychecks.mark_received(s, bonus)
    s.commit()
    summ = summary.month_summary(s, jan, today=date(2027, 1, 15))
    assert summ.running_total_cents == 250000
    assert summ.money_in_cents == 250000

    paychecks.mark_not_received(s, bonus)
    s.commit()
    assert summary.month_summary(s, jan).running_total_cents == 200000
    assert not bonus.received


def test_editing_received_check_amount_updates_entry(s, jan):
    s1 = check_for(s, jan, "Sam", 1)
    txn = paychecks.mark_received(s, s1)
    paychecks.update_check(s, s1, amount_cents=151000)
    s.commit()
    assert txn.amount_cents == 151000


def test_delete_line_on_bill_unassigns_bill(s, jan):
    a1 = check_for(s, jan, "Alex", 1)
    line = next(ln for ln in a1.lines if ln.kind == "bill")
    paychecks.delete_line(s, line)
    s.commit()
    assert bill_named(s, jan, "Internet").assigned_check_id is None
    assert paychecks.remainder(a1) == 200000 - 25000


# ---------------------------------------------------------------- dashboard numbers


def test_category_remaining_goes_negative(s, jan):
    gas = cat(s, "Gas")
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 3), amount_cents=-12000, category_id=gas.id)
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 9), amount_cents=-9000, category_id=gas.id)
    s.commit()
    t = next(t for t in summary.month_summary(s, jan).targets if t.category.name == "Gas")
    assert t.remaining_cents == -1000
    assert t.over and t.bar_percent == 100


def test_projected_end_and_next_check(s, jan):
    bills.mark_paid(s, bill_named(s, jan, "Internet"), date(2027, 1, 1))
    paychecks.mark_received(s, check_for(s, jan, "Alex", 1))
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 2), amount_cents=-4000)
    s.commit()
    summ = summary.month_summary(s, jan, today=date(2027, 1, 5))
    running = 200000 - 6000 - 4000
    expected_checks = 150000 + 200000 + 150000 + 200000
    unpaid = 120000 + 8000 + 4000
    assert summ.running_total_cents == running
    assert summ.projected_end_cents == 100000 + running + expected_checks - unpaid
    assert summ.next_check.label == "Sam check 1"
    assert {b.name for b in summ.unpaid_bills} == {"Rent", "Phone", "Water"}


def test_history_rows(s, jan):
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 2), amount_cents=1000)
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 2), amount_cents=-300)
    months.start_month(s, 2027, 2, bank_balance_cents=0)
    s.commit()
    rows = summary.history(s)
    assert [(r.key, r.money_in_cents, r.money_out_cents, r.net_cents) for r in rows] == [
        ("2027-01", 1000, -300, 700),
        ("2027-02", 0, 0, 0),
    ]


# ---------------------------------------------------------------- ledger rules


def test_duplicate_submit_returns_same_entry(s, jan):
    a = ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 2), amount_cents=-500, client_token="tok-1"
    )
    b = ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 2), amount_cents=-500, client_token="tok-1"
    )
    s.commit()
    assert a.id == b.id
    assert len(list(s.scalars(select(Txn)))) == 1


def test_locked_month_rejects_changes(s, jan):
    months.set_locked(jan, True)
    with pytest.raises(BudgetError):
        ledger.add_txn(s, month=jan, date_=date(2027, 1, 2), amount_cents=-500)
    with pytest.raises(BudgetError):
        bills.mark_paid(s, bill_named(s, jan, "Rent"), date(2027, 1, 2))


def test_month_for_date_falls_back_to_live_month(s, jan):
    feb = months.start_month(s, 2027, 2, bank_balance_cents=0)
    s.commit()
    assert month_for_date(s, date(2027, 1, 30)).id == feb.id  # January is locked
    assert month_for_date(s, date(2027, 3, 2)).id == feb.id  # March not started yet
    assert month_for_date(s, date(2027, 2, 2)).id == feb.id


def test_live_month_ignores_history(s, jan):
    s.add(Month(year=2030, month=1, is_history=True, locked=True))
    s.commit()
    from budget.services.common import live_month

    assert live_month(s).id == jan.id


# ---------------------------------------------------------------- cards


def test_card_payment_lifecycle(s, jan):
    card = cards.add_card(s, "Card A", 50000)
    s.flush()
    txn = ledger.add_txn(
        s,
        month=jan,
        date_=date(2027, 1, 5),
        amount_cents=-10000,
        description="Pay A",
        card_id=card.id,
    )
    assert card.balance_cents == 40000
    ledger.update_txn(s, txn, amount_cents=-15000)
    assert card.balance_cents == 35000
    ledger.delete_txn(s, txn)
    assert card.balance_cents == 50000
    ledger.restore_txn(s, txn)
    assert card.balance_cents == 35000
    card_b = cards.add_card(s, "Card B", 20000)
    s.flush()
    ledger.update_txn(s, txn, card_id=card_b.id)
    assert (card.balance_cents, card_b.balance_cents) == (50000, 5000)
    ledger.update_txn(s, txn, card_id=None)
    assert card_b.balance_cents == 20000
    s.commit()


def test_card_payment_must_be_money_out(s, jan):
    card = cards.add_card(s, "Card A", 100)
    s.flush()
    with pytest.raises(BudgetError):
        ledger.add_txn(s, month=jan, date_=date(2027, 1, 5), amount_cents=500, card_id=card.id)


def test_card_update_archive_delete(s):
    card = cards.add_card(s, "Card A", 30000)
    cards.add_card(s, "Card B", 10000)
    s.flush()
    cards.set_balance(s, card, 32500, "store order")
    s.commit()
    assert card.events[0].delta_cents == 2500 and card.events[0].note == "store order"
    assert cards.total_active_balance(s) == 42500
    cards.archive(card)
    s.commit()
    assert cards.total_active_balance(s) == 10000
    assert [c.name for c in cards.archived_cards(s)] == ["Card A"]
    cards.delete_card(s, card)
    s.commit()
    assert cards.archived_cards(s) == []
