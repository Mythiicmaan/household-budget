"""Money helpers. All money is integer cents: positive = in, negative = out.

Includes a tiny, safe arithmetic parser (no ``eval``) used by the spreadsheet
importer and by amount fields, so "12.50+3" or "=-(58.24+30.95)" work.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a value can't be read as money."""


def to_cents(value: Decimal | int | float | str) -> int:
    """Convert a dollar value to integer cents, rounding half away from zero.

    Floats are converted through ``repr`` so 58.24 stays 58.24 (not 58.2399...).
    """
    if isinstance(value, bool):
        raise MoneyError("booleans are not money")
    if isinstance(value, float):
        value = Decimal(repr(value))
    elif isinstance(value, int):
        value = Decimal(value)
    elif isinstance(value, str):
        return parse_money(value)
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_money(text: str) -> int:
    """Parse user or spreadsheet text into cents.

    Accepts "$1,234.56", "-12", "(12.00)" (accounting negative), "1.5k" is NOT accepted.
    Also accepts simple arithmetic: "12.50+3", "=-(58.24+30.95)".
    """
    if text is None:
        raise MoneyError("no value")
    s = str(text).strip()
    if not s:
        raise MoneyError("empty value")
    s = s.replace("$", "").replace(",", "").replace("−", "-").replace(" ", "")
    if s.startswith("="):
        s = s[1:]
    if not s:
        raise MoneyError("empty value")
    return to_cents(evaluate(s))


def format_cents(cents: int, *, sign: bool = False) -> str:
    """Format cents as "$1,234.56" / "-$1,234.56". ``sign=True`` adds "+" for positives."""
    neg = cents < 0
    whole, frac = divmod(abs(int(cents)), 100)
    body = f"${whole:,}.{frac:02d}"
    if neg:
        return "-" + body
    if sign and cents > 0:
        return "+" + body
    return body


def cents_to_input(cents: int | None) -> str:
    """Render cents for an editable input field ("12.50"), no symbols."""
    if cents is None:
        return ""
    neg = cents < 0
    whole, frac = divmod(abs(int(cents)), 100)
    return ("-" if neg else "") + f"{whole}.{frac:02d}"


# --------------------------------------------------------------------------
# Safe arithmetic: numbers, + - * /, parentheses, unary +/-. Nothing else.
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|(.))")


def _tokenize(expr: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:  # pragma: no cover - regex always matches a char
            raise MoneyError(f"can't read {expr!r}")
        num, op = m.groups()
        if num is not None:
            tokens.append(num)
        elif op in "+-*/()":
            tokens.append(op)
        elif op.isspace():
            pass
        else:
            raise MoneyError(f"unsupported character {op!r} in {expr!r}")
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise MoneyError("unexpected end of expression")
        self.i += 1
        return tok

    def expr(self) -> Decimal:
        value = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self) -> Decimal:
        value = self.unary()
        while self.peek() in ("*", "/"):
            op = self.take()
            rhs = self.unary()
            if op == "*":
                value = value * rhs
            else:
                if rhs == 0:
                    raise MoneyError("division by zero")
                value = value / rhs
        return value

    def unary(self) -> Decimal:
        if self.peek() in ("+", "-"):
            op = self.take()
            v = self.unary()
            return -v if op == "-" else v
        return self.atom()

    def atom(self) -> Decimal:
        tok = self.take()
        if tok == "(":
            v = self.expr()
            if self.take() != ")":
                raise MoneyError("missing )")
            return v
        try:
            return Decimal(tok)
        except InvalidOperation as exc:
            raise MoneyError(f"not a number: {tok!r}") from exc


def evaluate(expr: str) -> Decimal:
    """Evaluate a simple arithmetic expression safely. Raises MoneyError."""
    s = expr.strip()
    # Accounting style "(12.00)" means -12.00 when it is the whole value.
    if re.fullmatch(r"\(\s*\d[\d.]*\s*\)", s):
        s = "-" + s
    tokens = _tokenize(s)
    if not tokens:
        raise MoneyError("empty expression")
    p = _Parser(tokens)
    value = p.expr()
    if p.peek() is not None:
        raise MoneyError(f"unexpected {p.peek()!r}")
    return value
