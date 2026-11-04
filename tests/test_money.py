from decimal import Decimal

import pytest

from budget.money import MoneyError, cents_to_input, evaluate, format_cents, parse_money, to_cents


@pytest.mark.parametrize(
    "text,cents",
    [
        ("12", 1200),
        ("12.5", 1250),
        ("12.50", 1250),
        ("$1,234.56", 123456),
        ("-12.34", -1234),
        ("  7.01 ", 701),
        (".5", 50),
        ("(12.00)", -1200),
        ("=100+50", 15000),
        ("=-(58.24+30.95)", -8919),
        ("10+2.5-0.5", 1200),
        ("3*4.25", 1275),
        ("10/4", 250),
        ("−20", -2000),  # unicode minus
        ("0.005", 1),  # half rounds away from zero
        ("-0.005", -1),
    ],
)
def test_parse_money(text, cents):
    assert parse_money(text) == cents


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "abc", "12..3", "1+", "(1+2", "__import__('os')", "1/0", "SUM(A1:A3)", "=", "2**3"],
)
def test_parse_money_rejects_garbage(bad):
    with pytest.raises(MoneyError):
        parse_money(bad)


def test_to_cents_types():
    assert to_cents(58.24) == 5824
    assert to_cents(0.1 + 0.2) == 30
    assert to_cents(19.995) == 2000
    assert to_cents(Decimal("1.234")) == 123
    assert to_cents(5) == 500
    with pytest.raises(MoneyError):
        to_cents(True)


def test_evaluate_precedence():
    assert evaluate("2+3*4") == Decimal(14)
    assert evaluate("(2+3)*4") == Decimal(20)
    assert evaluate("--5") == Decimal(5)


@pytest.mark.parametrize(
    "cents,text",
    [
        (0, "$0.00"),
        (5, "$0.05"),
        (123456, "$1,234.56"),
        (-100, "-$1.00"),
        (-123456789, "-$1,234,567.89"),
    ],
)
def test_format_cents(cents, text):
    assert format_cents(cents) == text


def test_format_cents_sign():
    assert format_cents(500, sign=True) == "+$5.00"
    assert format_cents(-500, sign=True) == "-$5.00"
    assert format_cents(0, sign=True) == "$0.00"


def test_cents_to_input_round_trips():
    for c in (0, 1, 99, 100, -250, 123456):
        assert parse_money(cents_to_input(c)) == c
    assert cents_to_input(None) == ""
