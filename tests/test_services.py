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
    childcare_lines = 5 * 10000  # one unpaid childcare line per check
    budget_left = 60000 + 20000  # nothing spent yet on groceries or gas
    assert summ.projected_end_cents == (
        100000 + running + expected_checks - unpaid - childcare_lines - budget_left
    )
    assert summ.unpaid_lines_cents == childcare_lines
    assert summ.budget_left_cents == budget_left
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


# ---------------------------------------------------------------- planned check items


def test_check_line_paid_lifecycle_and_projection(s, jan):
    a1 = check_for(s, jan, "Alex", 1)
    childcare = next(ln for ln in a1.lines if ln.kind == "daycare")
    before = summary.month_summary(s, jan).projected_end_cents

    txn = paychecks.mark_line_paid(s, childcare)
    s.commit()
    assert childcare.paid and txn.amount_cents == -10000
    assert txn.pending  # childcare is paid by paper check, so it starts pending
    assert txn.date == date(2027, 1, 4)  # the Monday the line is for
    # Paying a planned item moves money from "planned" to "spent": projection unchanged.
    assert summary.month_summary(s, jan).projected_end_cents == before

    # Holiday week: childcare comes in lower; the entry follows the line.
    paychecks.update_line(s, childcare, amount_cents=-6000)
    s.commit()
    assert txn.amount_cents == -6000
    assert summary.month_summary(s, jan).projected_end_cents == before + 4000

    links = ledger.delete_txn(s, txn)
    assert links.line is childcare and not childcare.paid
    ledger.restore_txn(s, txn)
    assert childcare.paid
    paychecks.mark_line_unpaid(s, childcare)
    t2 = paychecks.mark_line_paid(s, childcare)
    s.commit()
    assert t2.id == txn.id and t2.deleted_at is None


def test_bill_and_grocery_lines_have_no_toggle(s, jan):
    a1 = check_for(s, jan, "Alex", 1)
    for ln in a1.lines:
        if ln.kind in ("bill", "groceries"):
            with pytest.raises(BudgetError):
                paychecks.mark_line_paid(s, ln)


def test_editing_one_weeks_line_leaves_other_weeks_alone(s, jan):
    a1, s1 = check_for(s, jan, "Alex", 1), check_for(s, jan, "Sam", 1)
    c1 = next(ln for ln in a1.lines if ln.kind == "daycare")
    paychecks.update_line(s, c1, amount_cents=-5000)
    s.commit()
    assert next(ln for ln in s1.lines if ln.kind == "daycare").amount_cents == -10000


def test_new_month_carries_recurring_check_items_forward(s, jan):
    a1 = check_for(s, jan, "Alex", 1)
    paychecks.add_line(s, a1, kind="savings", label="Savings", amount_cents=-5000)
    paid = paychecks.add_line(s, a1, kind="card_payment", label="Card A", amount_cents=-7500)
    paychecks.mark_line_paid(s, paid)
    paychecks.add_line(
        s, check_for(s, jan, "Alex", 3), kind="other", label="Extra", amount_cents=-100
    )
    feb = months.start_month(s, 2027, 2, bank_balance_cents=0)
    s.commit()
    f1 = check_for(s, feb, "Alex", 1)
    copied = [
        (ln.kind, ln.label, ln.amount_cents, ln.paid)
        for ln in f1.lines
        if ln.kind in ("savings", "card_payment")
    ]
    assert copied == [
        ("savings", "Savings", -5000, False),
        ("card_payment", "Card A", -7500, False),
    ]
    # Weekly lines come from Settings (not copied twice); February has no 3rd check.
    assert [ln.kind for ln in f1.lines].count("daycare") == 1
    assert all(ln.label != "Extra" for c in paychecks.checks_in_month(s, feb) for ln in c.lines)


# ---------------------------------------------------------------- editing bills


def set_line(s, bill, *, label, cents):
    """Make a bill's check line differ from the bill, as imported checks can."""
    line = paychecks.bill_line(s, bill)
    line.label, line.amount_cents = label, cents
    s.commit()
    return line


def test_saving_a_bill_without_changing_its_amount_leaves_the_check_line(s, jan):
    rent = bill_named(s, jan, "Rent")
    line = set_line(s, rent, label="Rent January 2027", cents=-125000)
    bills.update_bill(s, rent, name="Rent", amount_cents=120000, note="due on the 1st")
    s.commit()
    assert rent.note == "due on the 1st"
    assert (line.label, line.amount_cents) == ("Rent January 2027", -125000)


def test_changing_a_bills_amount_updates_its_check_line(s, jan):
    rent = bill_named(s, jan, "Rent")
    line = set_line(s, rent, label="Rent January 2027", cents=-125000)
    bills.update_bill(s, rent, name="Rent", amount_cents=130000)
    s.commit()
    assert line.amount_cents == -130000
    assert line.label == "Rent January 2027"  # only the amount changed
    assert summary.month_summary(s, jan).unpaid_bills_cents == 130000 + 6000 + 8000 + 4000


def test_renaming_a_bill_renames_its_check_line(s, jan):
    rent = bill_named(s, jan, "Rent")
    line = set_line(s, rent, label="Rent January 2027", cents=-125000)
    bills.update_bill(s, rent, name="Housing", amount_cents=120000)
    s.commit()
    assert (line.label, line.amount_cents) == ("Housing", -125000)


def test_moving_a_bill_keeps_its_line_amount(s, jan):
    rent = bill_named(s, jan, "Rent")
    set_line(s, rent, label="Rent January 2027", cents=-125000)
    a1 = check_for(s, jan, "Alex", 1)
    bills.update_bill(s, rent, amount_cents=120000, check=a1)
    s.commit()
    moved = paychecks.bill_line(s, rent)
    assert moved.paycheck_id == a1.id
    assert (moved.label, moved.amount_cents) == ("Rent January 2027", -125000)


def test_editing_a_paid_bill_only_touches_its_entry_when_needed(s, jan):
    rent = bill_named(s, jan, "Rent")
    txn = bills.mark_paid(s, rent, date(2027, 1, 15))
    txn.description = "Rent via bank transfer"
    s.commit()
    bills.update_bill(s, rent, name="Rent", amount_cents=120000, note="noted")
    s.commit()
    assert (txn.description, txn.amount_cents) == ("Rent via bank transfer", -120000)
    bills.update_bill(s, rent, name="Rent", amount_cents=121000)
    s.commit()
    assert (txn.description, txn.amount_cents) == ("Rent via bank transfer", -121000)


def test_changed_amount_carries_to_next_month_but_not_the_template(s, jan):
    internet = bill_named(s, jan, "Internet")
    bills.update_bill(s, internet, amount_cents=7000)
    feb = months.start_month(s, 2027, 2, bank_balance_cents=0)
    s.commit()
    assert internet.template.default_amount_cents == 6000
    assert bill_named(s, feb, "Internet").amount_cents == 7000


# ---------------------------------------------------------------- weekly rows on the bills page


def test_daycare_paid_1x_pays_the_next_week_at_that_weeks_amount(s, jan):
    holiday_check = check_for(s, jan, "Alex", 2)  # the third Friday of the month
    holiday = next(ln for ln in holiday_check.lines if ln.kind == "daycare")
    paychecks.update_line(s, holiday, amount_cents=-15000)
    s.commit()

    row = summary.bills_page(s, jan).daycare
    assert (row.total_cents, row.paid_count, row.week_count) == (55000, 0, 5)

    first = paychecks.pay_next_daycare(s, jan)
    second = paychecks.pay_next_daycare(s, jan)
    third = paychecks.pay_next_daycare(s, jan)
    s.commit()
    assert [first.date, second.date, third.date] == [
        date(2027, 1, 4),
        date(2027, 1, 11),
        date(2027, 1, 18),
    ]
    assert third.amount_cents == -15000  # the holiday week's own amount
    assert all(t.pending for t in (first, second, third))
    row = summary.bills_page(s, jan).daycare
    assert (row.paid_count, row.unpaid_cents) == (3, 20000)


def test_daycare_undo_reverses_the_most_recent_week(s, jan):
    first = paychecks.pay_next_daycare(s, jan)
    second = paychecks.pay_next_daycare(s, jan)
    paychecks.undo_last_daycare(s, jan)
    s.commit()
    assert second.deleted_at is not None and first.deleted_at is None
    assert summary.bills_page(s, jan).daycare.paid_count == 1
    # Paying again brings that same week back, not a duplicate entry.
    again = paychecks.pay_next_daycare(s, jan)
    s.commit()
    assert again.id == second.id and again.deleted_at is None


def test_daycare_all_paid_and_nothing_to_undo(s, jan):
    with pytest.raises(BudgetError):
        paychecks.undo_last_daycare(s, jan)
    for _ in range(5):
        paychecks.pay_next_daycare(s, jan)
    with pytest.raises(BudgetError):
        paychecks.pay_next_daycare(s, jan)


def test_daycare_row_matches_mark_paid_from_other_pages(s, jan):
    line = next(ln for ln in check_for(s, jan, "Sam", 1).lines if ln.kind == "daycare")
    paychecks.mark_line_paid(s, line)
    s.commit()
    assert summary.bills_page(s, jan).daycare.paid_count == 1


def test_groceries_row_follows_ledger_entries(s, jan):
    groceries = cat(s, "Groceries")
    ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 3), amount_cents=-4510, category_id=groceries.id
    )
    ledger.add_txn(s, month=jan, date_=date(2027, 1, 3), amount_cents=-999)  # no category
    s.commit()
    row = summary.bills_page(s, jan).groceries
    assert (row.budget_cents, row.spent_cents, row.left_cents) == (60000, 4510, 55490)
    assert not row.over
    home = next(t for t in summary.month_summary(s, jan).targets if t.category.name == "Groceries")
    assert home.remaining_cents == row.left_cents  # the Bills page and Home always agree


def test_bills_page_totals_include_both_rows(s, jan):
    page = summary.bills_page(s, jan)
    the_bills = 120000 + 6000 + 8000 + 4000
    assert page.total_cents == the_bills + 60000 + 50000
    assert page.unpaid_cents == the_bills + 60000 + 50000

    paychecks.pay_next_daycare(s, jan)
    bills.mark_paid(s, bill_named(s, jan, "Internet"), date(2027, 1, 2))
    groceries = cat(s, "Groceries")
    ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 3), amount_cents=-10000, category_id=groceries.id
    )
    s.commit()
    page = summary.bills_page(s, jan)
    assert page.total_cents == the_bills + 60000 + 50000  # the month's plan does not shrink
    assert page.unpaid_cents == (the_bills - 6000) + 50000 + 40000


def test_groceries_over_budget_adds_nothing_to_unpaid(s, jan):
    groceries = cat(s, "Groceries")
    ledger.add_txn(
        s, month=jan, date_=date(2027, 1, 3), amount_cents=-70000, category_id=groceries.id
    )
    s.commit()
    page = summary.bills_page(s, jan)
    assert page.groceries.over and page.groceries.left_cents == -10000
    assert page.unpaid_cents == (120000 + 6000 + 8000 + 4000) + 50000


def test_projection_unchanged_by_paying_daycare_from_the_bills_page(s, jan):
    before = summary.month_summary(s, jan).projected_end_cents
    paychecks.pay_next_daycare(s, jan)
    s.commit()
    assert summary.month_summary(s, jan).projected_end_cents == before


def test_history_months_have_no_weekly_rows(s, jan):
    old = Month(year=2026, month=6, is_history=True, locked=True)
    s.add(old)
    s.commit()
    page = summary.bills_page(s, old)
    assert page.groceries is None and page.daycare is None
