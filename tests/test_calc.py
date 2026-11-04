from budget import calc


def test_running_total_mixed_signs():
    assert calc.running_total([]) == 0
    assert calc.running_total([200000, -15000, -4599, 1000]) == 181401


def test_true_starting_balance_subtracts_outstanding_checks():
    # Bank shows $1,000 but a $100 childcare check hasn't cashed yet.
    assert calc.true_starting_balance(100000, [-10000]) == 90000


def test_true_starting_balance_pending_deposit_adds():
    assert calc.true_starting_balance(100000, [-10000, 2500]) == 92500


def test_true_starting_balance_no_pending_and_missing_balance():
    assert calc.true_starting_balance(50000, []) == 50000
    assert calc.true_starting_balance(None, [-100]) is None


def test_projected_end_balance():
    # start 900 + running -50 + expected checks 2000 + 1500 - unpaid bills (300 + 45.50)
    got = calc.projected_end_balance(90000, -5000, [200000, 150000], [30000, 4550])
    assert got == 90000 - 5000 + 350000 - 34550


def test_projected_end_balance_without_start():
    assert calc.projected_end_balance(None, 1000, [], [500]) == 500


def test_projected_end_subtracts_planned_check_items_and_budget_left():
    # start 900 + running -50 + checks due 2000 - bills 300
    # - planned check items not yet paid (childcare 100, card payment 250, as negative lines)
    # - what's left of soft budgets (gas 80 left, groceries 400 left)
    got = calc.projected_end_balance(
        90000,
        -5000,
        [200000],
        [30000],
        unpaid_line_amounts=[-10000, -25000],
        budget_left=[8000, 40000],
    )
    assert got == 90000 - 5000 + 200000 - 30000 - 10000 - 25000 - 8000 - 40000


def test_budget_left_ignores_overspent_categories():
    # An over-budget category (negative remaining) is already in the running total.
    assert calc.budget_left([8000, -4000, 0]) == [8000, 0, 0]


def test_category_remaining_positive_and_negative():
    assert calc.category_remaining(60000, [-10000, -2550]) == 47450
    # Over budget: remaining goes negative, never clamps.
    assert calc.category_remaining(20000, [-15000, -9000]) == -4000
    # Refund adds back.
    assert calc.category_remaining(20000, [-5000, 1000]) == 16000
    assert calc.category_remaining(20000, []) == 20000


def test_category_spent_fraction():
    assert calc.category_spent_fraction(20000, 20000) == 0.0
    assert calc.category_spent_fraction(20000, 5000) == 0.75
    assert calc.category_spent_fraction(20000, -4000) == 1.2
    assert calc.category_spent_fraction(0, 0) == 0.0


def test_check_remainder():
    assert calc.check_remainder(200000, [-15000, -10000, -50000]) == 125000
    assert calc.check_remainder(150000, []) == 150000
    # A check that's fully spoken for (or over-committed).
    assert calc.check_remainder(100000, [-100000]) == 0
    assert calc.check_remainder(100000, [-110000]) == -10000


def test_card_balance_after_payment():
    assert calc.card_balance_after_payment(50000, -10000) == 40000
    assert calc.card_balance_after_payment(5000, -10000) == -5000  # overpaid = credit
