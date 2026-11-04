"""Core money rules, as pure functions over integer cents.

Sign convention everywhere: positive = money in, negative = money out.
Bill amounts are the one exception: a bill's ``amount_cents`` is the positive
size of the bill (as written on a bill list); paying it creates a negative entry.
"""

from __future__ import annotations

from collections.abc import Iterable


def running_total(entry_amounts: Iterable[int]) -> int:
    """Running surplus/deficit for a month = sum of the month's ledger entries."""
    return sum(entry_amounts)


def true_starting_balance(
    bank_balance_cents: int | None, pending_carryover_amounts: Iterable[int]
) -> int | None:
    """Bank balance on the 1st, adjusted for items that hadn't cleared yet.

    Pending items are signed: an uncashed $100 check is -10000, so the true
    balance is 10000 lower than the bank shows. (Equivalently: bank balance minus
    the outstanding amount.) Returns None when no bank balance was entered.
    """
    if bank_balance_cents is None:
        return None
    return bank_balance_cents + sum(pending_carryover_amounts)


def projected_end_balance(
    true_start_cents: int | None,
    running_total_cents: int,
    checks_not_received: Iterable[int],
    unpaid_bill_amounts: Iterable[int],
) -> int:
    """Projected end-of-month balance.

    = true starting balance + running total + checks still expected
      - unpaid planned bills (bill amounts are positive sizes).
    A missing starting balance counts as 0 so the projection still shows the trend.
    """
    start = true_start_cents or 0
    return start + running_total_cents + sum(checks_not_received) - sum(unpaid_bill_amounts)


def category_remaining(target_cents: int, category_entry_amounts: Iterable[int]) -> int:
    """Soft-target remaining = target + sum of the category's entries (outflows are negative).

    Can go negative (over budget). Refunds (positive entries) add back.
    """
    return target_cents + sum(category_entry_amounts)


def category_spent_fraction(target_cents: int, remaining_cents: int) -> float:
    """Fraction of a soft target used, for progress bars. 0 when there's no target.

    Can exceed 1.0 when over budget; callers clamp for display.
    """
    if target_cents <= 0:
        return 0.0
    return (target_cents - remaining_cents) / target_cents


def check_remainder(check_amount_cents: int, line_amounts: Iterable[int]) -> int:
    """What's left of a paycheck for the joint account = check amount + its (negative) lines."""
    return check_amount_cents + sum(line_amounts)


def card_balance_after_payment(balance_cents: int, txn_amount_cents: int) -> int:
    """A card balance is what's owed (positive). A ledger payment (negative) reduces it."""
    return balance_cents + txn_amount_cents
