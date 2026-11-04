"""One-time import of the old monthly-tab budget spreadsheet.

Layout of each month tab (see the private plan for details):
- Bills: A-D from row 2 (name, amount, assigned check or paid flag, note),
  ending at a blank block or the "Gas Budget:" row.
- Soft-target rows: "Gas Budget:" / "Groceries Budget:" in column A with formulas
  like ``=TARGET+F14+F21`` that name the ledger rows belonging to that category.
- Ledger: F (impact), G (item), H (date, or a note in newer tabs), rows 3+.
- Starting balance: label in column I, value in J.
- Paycheck blocks in A-C: a header like "<Name> Check 1 Calculation (11-6-2026)"
  with the check amount in B, then lines (B amount, C label) until a ``=SUM`` row.

Months before the live month become read-only history (ledger + starting balance).
The live month also gets bills, the bill template, paychecks and their lines.

Never uses ``eval``: formulas without cached values go through ``money.evaluate``.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget.models import (
    BillTemplate,
    Category,
    Month,
    MonthBill,
    Paycheck,
    PaycheckLine,
    Person,
    Txn,
)
from budget.money import MoneyError, evaluate, to_cents

MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTH_ABBR = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}


class ImportProblem(RuntimeError):
    """Stops the import with a message for the owner."""


# ---------------------------------------------------------------- small parsers


def find_workbook(import_dir: Path) -> Path:
    files = sorted(
        p for p in import_dir.glob("*.xlsx") if p.is_file() and not p.name.startswith("~$")
    )
    if len(files) != 1:
        names = ", ".join(p.name for p in files) or "none"
        raise ImportProblem(
            f"Expected exactly one .xlsx file in {import_dir}, found {len(files)} ({names}). "
            "Leave only the one to import."
        )
    return files[0]


def parse_tab_month(title: str) -> tuple[int, int] | None:
    """'4-25' -> (2025, 4); 'January 2026' -> (2026, 1); 'Jan 2026' -> (2026, 1)."""
    t = title.strip()
    m = re.fullmatch(r"(\d{1,2})[-/](\d{2}|\d{4})", t)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        year = year + 2000 if year < 100 else year
        return (year, month) if 1 <= month <= 12 else None
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", t)
    if m:
        name = m.group(1).lower()
        month = MONTH_NAMES.get(name) or MONTH_ABBR.get(name[:3])
        return (int(m.group(2)), month) if month else None
    return None


_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](\d{4}|\d{2})(?!\d)")


def date_in_text(text: str) -> date | None:
    """Find a dated token like '10-2-26' or '11/6/2026'. A year is required, so
    '2/4' (meaning "2 of 4") is never mistaken for a date."""
    for m in _DATE_IN_TEXT.finditer(text or ""):
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = year + 2000 if year < 100 else year
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def token_list(text: str) -> list[str]:
    stop = {"and", "the", "bill", "monthly", "qtrly", "of", "x", "payment"}
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if w not in stop and not w.isdigit() and w not in MONTH_NAMES]


def norm_tokens(text: str) -> set[str]:
    return set(token_list(text))


def tokens_similar(a: str, b: str) -> bool:
    """'book' ~ 'books', 'elect' ~ 'electric' (shared start of at least 4 letters)."""
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def squash(text: str) -> str:
    return "".join(sorted(norm_tokens(text)))


# ---------------------------------------------------------------- cell access


@dataclass
class Sheet:
    """Cached values plus formulas for one worksheet."""

    values: Any
    formulas: Any

    def raw(self, coord: str) -> tuple[Any, Any]:
        return self.values[coord].value, self.formulas[coord].value

    def text(self, coord: str) -> str:
        v = self.values[coord].value
        return "" if v is None else str(v).strip()

    @property
    def max_row(self) -> int:
        return self.values.max_row


def cell_cents(sheet: Sheet, coord: str) -> tuple[int | None, str | None]:
    """(cents, problem). Prefers the cached value; falls back to the safe parser."""
    value, formula = sheet.raw(coord)
    if isinstance(value, bool):
        return None, f"{coord}: not a number ({value!r})"
    if isinstance(value, int | float):
        return to_cents(value), None
    if isinstance(value, str) and value.strip():
        try:
            return to_cents(evaluate(value.replace("$", "").replace(",", "").lstrip("="))), None
        except MoneyError:
            return None, f"{coord}: can't read amount {value!r}"
    if isinstance(formula, str) and formula.startswith("="):
        try:
            return to_cents(evaluate(formula[1:])), None
        except MoneyError:
            return None, f"{coord}: formula without a cached value ({formula!r})"
    return None, None


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# ---------------------------------------------------------------- parsed structures


@dataclass
class LedgerRow:
    row: int
    when: date
    cents: int
    description: str
    note: str
    category: str | None = None


@dataclass
class BillRow:
    row: int
    name: str
    cents: int
    person_hint: str | None
    ordinal: int | None
    paid: bool
    note: str


@dataclass
class CheckBlock:
    row: int
    who: str
    ordinal: int | None
    title: str
    pay_date: date
    cents: int
    lines: list[tuple[int, int, str]] = field(default_factory=list)  # row, cents, label
    sheet_remainder: int | None = None
    sum_label: str = ""  # what the sheet called the remainder, e.g. "To joint" or "Gifts"
    expected_remainder: int | None = None  # set when the remainder was re-pointed


@dataclass
class TabData:
    title: str
    year: int
    month: int
    ledger: list[LedgerRow] = field(default_factory=list)
    bills: list[BillRow] = field(default_factory=list)
    checks: list[CheckBlock] = field(default_factory=list)
    bank_balance: int | None = None
    target_refs: dict[str, set[int]] = field(default_factory=dict)  # category -> rows
    target_values: dict[str, int] = field(default_factory=dict)  # category -> sheet remaining
    target_amounts: dict[str, int] = field(default_factory=dict)  # category -> target
    sheet_running_total: int | None = None
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


TARGET_LABELS = {"gas budget": "Gas", "groceries budget": "Groceries"}
_HEADER = re.compile(r"^(?P<who>\S+)\s+(?P<mid>.*?)\s*calculation\s*\((?P<date>[^)]*)\)", re.I)


def parse_tab(sheet: Sheet, title: str, year: int, month: int, live: bool) -> TabData:
    tab = TabData(title=title, year=year, month=month)
    _parse_targets(sheet, tab)
    _parse_side_labels(sheet, tab)
    _parse_ledger(sheet, tab)
    if live:
        _parse_bills(sheet, tab)
        _parse_checks(sheet, tab)
    return tab


def _parse_targets(sheet: Sheet, tab: TabData) -> None:
    for r in range(1, min(sheet.max_row, 120) + 1):
        label = sheet.text(f"A{r}").rstrip(":").strip().lower()
        if label not in TARGET_LABELS:
            continue
        cat = TARGET_LABELS[label]
        value, formula = sheet.raw(f"B{r}")
        if isinstance(value, int | float):
            tab.target_values[cat] = to_cents(value)
        if isinstance(formula, str) and formula.startswith("="):
            tab.target_refs[cat] = {int(n) for n in re.findall(r"\bF\$?(\d+)\b", formula, re.I)}
            lead = re.match(r"=\s*(\d+(?:\.\d+)?)", formula)
            if lead:
                tab.target_amounts[cat] = to_cents(float(lead.group(1)))


def _parse_side_labels(sheet: Sheet, tab: TabData) -> None:
    for r in range(1, min(sheet.max_row, 80) + 1):
        label = sheet.text(f"I{r}").lower()
        if label.startswith("our account balance on first day"):
            cents, problem = cell_cents(sheet, f"J{r}")
            if cents is not None:
                tab.bank_balance = cents
            elif problem:
                tab.skipped.append(f"starting balance: {problem}")
        elif "running" in label and "surplus" in label:
            cents, _ = cell_cents(sheet, f"J{r}")
            tab.sheet_running_total = cents


def _parse_ledger(sheet: Sheet, tab: TabData) -> None:
    first = date(tab.year, tab.month, 1)
    last_date: date | None = None
    for r in range(3, sheet.max_row + 1):
        item = sheet.text(f"G{r}")
        f_value, f_formula = sheet.raw(f"F{r}")
        if f_value is None and f_formula is None:
            if item:
                tab.skipped.append(f"ledger row {r}: no amount ({item[:40]!r})")
            continue
        cents, problem = cell_cents(sheet, f"F{r}")
        if cents is None:
            tab.skipped.append(f"ledger row {r}: {problem or 'no amount'}")
            continue
        if isinstance(f_value, str) and f_value.strip().lower() in ("impact", "running tally"):
            continue
        h_value = sheet.values[f"H{r}"].value
        when = as_date(h_value)
        note = "" if when or h_value is None else str(h_value).strip()
        if when is None:
            when = date_in_text(item) or date_in_text(note)
        if when is None:
            when = last_date or first
        else:
            last_date = when
        category = None
        for cat, rows in tab.target_refs.items():
            if r in rows:
                category = cat
        tab.ledger.append(LedgerRow(r, when, cents, item, note, category))


def _parse_bills(sheet: Sheet, tab: TabData) -> None:
    blanks = 0
    for r in range(2, min(sheet.max_row, 120) + 1):
        name = sheet.text(f"A{r}")
        if name.rstrip(":").strip().lower() in TARGET_LABELS:
            break
        if not name:
            blanks += 1
            if blanks >= 2:
                break
            continue
        blanks = 0
        if name.lower() in ("bill name",):
            continue
        cents, problem = cell_cents(sheet, f"B{r}")
        if cents is None:
            tab.skipped.append(f"bill row {r} ({name[:40]}): {problem or 'no amount'}")
            continue
        assign = sheet.text(f"C{r}")
        note = sheet.text(f"D{r}")
        m = re.match(r"^\s*(\S+)\s+check\s*(\d+)", assign, re.I)
        if assign.lower() == "weekly":
            tab.notes.append(
                f"bill row {r} ({name}): weekly item, handled by the per-check lines instead of a bill"
            )
            continue
        if m:
            tab.bills.append(BillRow(r, name, abs(cents), m.group(1), int(m.group(2)), False, note))
        else:
            paid = assign.lower() in ("yes", "paid", "y", "x")
            extra = "" if paid or not assign else assign
            tab.bills.append(
                BillRow(
                    r, name, abs(cents), None, None, paid, " ".join(p for p in (note, extra) if p)
                )
            )


def _parse_header_date(text: str) -> date | None:
    m = re.fullmatch(r"\s*(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})\s*", text)
    if not m:
        return None
    year = int(m.group(3))
    year = year + 2000 if year < 100 else year
    try:
        return date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _parse_checks(sheet: Sheet, tab: TabData) -> None:
    r = 1
    while r <= sheet.max_row:
        m = _HEADER.match(sheet.text(f"A{r}"))
        if not m:
            r += 1
            continue
        header_row = r
        pay_date = _parse_header_date(m.group("date"))
        if pay_date is None:
            tab.skipped.append(f"check header row {r}: can't read date {m.group('date')!r}")
            r += 1
            continue
        if (pay_date.year, pay_date.month) != (tab.year, tab.month):
            tab.notes.append(
                f"check header row {r}: skipped stale block dated {pay_date.isoformat()}"
            )
            r += 1
            continue
        cents, problem = cell_cents(sheet, f"B{r}")
        if cents is None:
            tab.skipped.append(f"check header row {r}: {problem or 'no check amount'}")
            r += 1
            continue
        mid = m.group("mid").strip()
        om = re.search(r"check\s*(\d+)", mid, re.I)
        block = CheckBlock(
            row=r,
            who=m.group("who"),
            ordinal=int(om.group(1)) if om else None,
            title=f"{m.group('who')} {mid}".strip(),
            pay_date=pay_date,
            cents=cents,
        )
        r += 1
        found_sum = False
        while r <= sheet.max_row and r < header_row + 40:
            if _HEADER.match(sheet.text(f"A{r}")):
                break
            value, formula = sheet.raw(f"B{r}")
            if isinstance(formula, str) and formula.lower().startswith("=sum"):
                block.sheet_remainder, _ = cell_cents(sheet, f"B{r}")
                block.sum_label = sheet.text(f"C{r}")
                found_sum = True
                r += 1
                break
            label = sheet.text(f"C{r}")
            note = sheet.text(f"A{r}")
            if value is None and formula is None:
                if not label:
                    r += 1
                    continue
                tab.skipped.append(f"check line row {r}: no amount ({label[:40]!r})")
                r += 1
                continue
            line_cents, problem = cell_cents(sheet, f"B{r}")
            if line_cents is None:
                tab.skipped.append(f"check line row {r}: {problem}")
            else:
                full = f"{label} ({note})" if note else label
                block.lines.append((r, line_cents, full or "Line"))
            r += 1
        if not found_sum:
            tab.notes.append(f"check block row {header_row}: no =SUM row found; used lines above")
        tab.checks.append(block)


# ---------------------------------------------------------------- writing to the database


@dataclass
class ImportResult:
    report: list[str]
    live_month: Month | None
    problems: int


def _auto_tag(text: str, rules: dict[str, str], cats: dict[str, Category]) -> Category | None:
    low = (text or "").lower()
    for keyword, cat_name in rules.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}", low):
            return cats.get(cat_name.lower()) if cat_name else None
    return None


def _line_kind(label: str, rules: dict[str, str]) -> str:
    low = label.lower()
    if "daycare" in low or "childcare" in low:
        return "daycare"
    if "grocer" in low:
        return "groceries"
    mapped = next(
        (c for k, c in rules.items() if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}", low)),
        None,
    )
    if mapped and mapped.lower() == "groceries":
        return "groceries"
    if "saving" in low:
        return "savings"
    if re.search(r"\bcard\b|\bcc\b", low):
        return "card_payment"
    return "other"


def _match_person(hint: str, people: list[Person]) -> Person | None:
    h = hint.lower()
    for p in people:
        n = p.name.lower()
        if n == h or n.startswith(h) or h.startswith(n):
            return p
    return None


def _names_match(a: str, b: str) -> bool:
    la, lb = token_list(a), token_list(b)
    if not la or not lb:
        return False
    ja, jb = "".join(la), "".join(lb)
    if ja.startswith(jb) or jb.startswith(ja):  # "StreamBox" ~ "Stream Box Extra"
        return True
    small, large = (la, lb) if len(la) <= len(lb) else (lb, la)
    return all(any(tokens_similar(x, y) for y in large) for x in small)


def _shares_token(a: str, b: str) -> bool:
    return any(tokens_similar(x, y) for x in token_list(a) for y in token_list(b))


def link_bill_lines(check: Paycheck, bills: list[MonthBill]) -> dict[int, str]:
    """Connect a check's lines to the bills assigned to it. Returns bill id -> how.

    1. One line per bill when the names match and the amounts are within 10%.
    2. One combined line covering several bills ("A, B, C") when those bills' amounts add
       up to the line exactly: the line is split into one line per bill (same total).
    3. A line with the bill's name but a different amount: linked, amounts kept as-is.
    Check remainders never change.
    """
    how: dict[int, str] = {}
    open_bills = [b for b in bills if b.assigned_check_id == check.id]
    for b in open_bills:
        line = next(
            (
                ln
                for ln in check.lines
                if ln.month_bill_id is None
                and ln.kind not in ("groceries", "daycare")
                and _names_match(ln.label, b.name)
                and _close(ln.amount_cents, b.amount_cents)
            ),
            None,
        )
        if line:
            line.kind = "bill"
            line.month_bill_id = b.id
            how[b.id] = "line linked" + (
                ""
                if -line.amount_cents == b.amount_cents
                else f" (check line {_fmt(-line.amount_cents)})"
            )
    left = [b for b in open_bills if b.id not in how]
    for line in list(check.lines):
        if line.month_bill_id is not None or line.kind in ("groceries", "daycare") or not left:
            continue
        candidates = [b for b in left if _shares_token(line.label, b.name)]
        for size in range(2, min(5, len(candidates)) + 1):
            combo = next(
                (
                    c
                    for c in combinations(candidates, size)
                    if sum(b.amount_cents for b in c) == -line.amount_cents
                ),
                None,
            )
            if combo:
                pos = check.lines.index(line)
                check.lines.remove(line)
                for i, b in enumerate(combo):
                    check.lines.insert(
                        pos + i,
                        PaycheckLine(
                            kind="bill",
                            label=b.name,
                            amount_cents=-b.amount_cents,
                            month_bill_id=b.id,
                            sort_order=line.sort_order,
                        ),
                    )
                    how[b.id] = f"split out of the combined line {line.label!r}"
                left = [b for b in left if b.id not in how]
                break
    # 3. Same name, different amount (e.g. the check sets aside more than the bill):
    #    still the bill's line, so it isn't also counted as a separate planned item.
    #    The check keeps the sheet's amount; the bill keeps the bill list's amount.
    for b in list(left):
        line = next(
            (
                ln
                for ln in check.lines
                if ln.month_bill_id is None
                and ln.kind not in ("groceries", "daycare")
                and _names_match(ln.label, b.name)
            ),
            None,
        )
        if line:
            line.kind = "bill"
            line.month_bill_id = b.id
            how[b.id] = f"line linked (check sets aside {_fmt(-line.amount_cents)})"
            left.remove(b)
    for b in left:
        how[b.id] = "no matching line on the check; check lines kept as on the sheet"
    return how


def _close(a: int, b: int, pct: float = 0.10) -> bool:
    a, b = abs(a), abs(b)
    return abs(a - b) <= max(100, int(max(a, b) * pct))


def import_workbook(
    s: Session,
    workbook_path: Path,
    *,
    live_key: str | None = None,
    auto_tag: dict[str, str] | None = None,
    bills_category: str = "Bills",
    account_name: str | None = None,
) -> ImportResult:
    from openpyxl import load_workbook

    auto_tag = auto_tag or {}
    if s.scalar(select(Month.id).limit(1)) is not None:
        raise ImportProblem("The database already has months. Import into an empty database.")

    wb_values = load_workbook(workbook_path, data_only=True, read_only=False)
    wb_formulas = load_workbook(workbook_path, data_only=False, read_only=False)
    report: list[str] = [f"Import of {workbook_path.name}", ""]

    tabs: list[tuple[int, int, str]] = []
    for ws in wb_values.worksheets:
        ym = parse_tab_month(ws.title)
        if ym is None:
            report.append(f"Tab {ws.title!r}: not a month tab, ignored")
            continue
        tabs.append((ym[0], ym[1], ws.title))
    if not tabs:
        raise ImportProblem("No month tabs found.")
    tabs.sort()
    dupes = [k for k, v in _count((y, m) for y, m, _ in tabs).items() if v > 1]
    if dupes:
        raise ImportProblem(f"More than one tab for the same month: {dupes}")

    if live_key:
        ly, lm = (int(x) for x in live_key.split("-"))
    else:
        ly, lm = tabs[-1][0], tabs[-1][1]
    if (ly, lm) not in {(y, m) for y, m, _ in tabs}:
        raise ImportProblem(f"No tab found for the live month {ly}-{lm:02d}.")

    cats = {c.name.lower(): c for c in s.scalars(select(Category))}
    problems = 0
    live_month: Month | None = None
    live_tab: TabData | None = None

    for y, m, title in tabs:
        if (y, m) > (ly, lm):
            report.append(f"Tab {title!r}: after the live month, ignored")
            continue
        is_live = (y, m) == (ly, lm)
        sheet = Sheet(wb_values[title], wb_formulas[title])
        tab = parse_tab(sheet, title, y, m, live=is_live)
        month = Month(
            year=y,
            month=m,
            bank_balance_on_first_cents=tab.bank_balance,
            locked=not is_live,
            is_history=not is_live,
        )
        s.add(month)
        s.flush()
        tagged = 0
        for row in tab.ledger:
            cat = cats.get(row.category.lower()) if row.category else None
            if cat is None and not is_live:
                cat = _auto_tag(row.description, auto_tag, cats)
                tagged += cat is not None
            s.add(
                Txn(
                    month_id=month.id,
                    date=row.when,
                    amount_cents=row.cents,
                    description=row.description[:200],
                    note=row.note,
                    category_id=cat.id if cat else None,
                    source="import",
                )
            )
        s.flush()
        total = sum(r.cents for r in tab.ledger)
        report.append(
            f"{calendar.month_name[m]} {y} ({title!r}): {len(tab.ledger)} entries, "
            f"total {_fmt(total)}, starting balance {_fmt(tab.bank_balance)}"
            + (f", {tagged} auto-tagged" if tagged else "")
            + (", LIVE" if is_live else "")
        )
        if tab.sheet_running_total is not None and tab.sheet_running_total != total:
            report.append(f"    ! sheet running total is {_fmt(tab.sheet_running_total)}")
            problems += 1
        for msg in tab.skipped:
            report.append(f"    skipped {msg}")
        problems += len(tab.skipped)
        for msg in tab.notes:
            report.append(f"    note: {msg}")
        if is_live:
            live_month, live_tab = month, tab

    assert live_month is not None and live_tab is not None
    problems += _import_live(
        s, live_month, live_tab, report, cats, auto_tag, bills_category, account_name
    )
    report.append("")
    report.append(f"Done. {problems} item(s) need a look." if problems else "Done. No problems.")
    return ImportResult(report=report, live_month=live_month, problems=problems)


def _import_live(
    s: Session,
    month: Month,
    tab: TabData,
    report: list[str],
    cats: dict[str, Category],
    auto_tag: dict[str, str],
    bills_category: str,
    account_name: str | None = None,
) -> int:
    problems = 0
    label = f"{calendar.month_name[month.month]} {month.year}"
    report += ["", f"Live month: {label}"]

    # People and pay anchors come from the regular check blocks.
    people: dict[str, Person] = {p.name.lower(): p for p in s.scalars(select(Person))}
    for block in sorted(tab.checks, key=lambda b: b.pay_date):
        key = block.who.lower()
        person = people.get(key) or _match_person(block.who, list(people.values()))
        if person is None:
            person = Person(name=block.who, pay_interval_days=14)
            s.add(person)
            people[key] = person
        if block.ordinal and (
            person.pay_anchor_date is None or block.pay_date < person.pay_anchor_date
        ):
            person.pay_anchor_date = block.pay_date
            person.default_check_cents = block.cents
    s.flush()
    plist = list(people.values())
    for p in plist:
        report.append(
            f"  Person {p.name}: paid every {p.pay_interval_days} days from "
            f"{p.pay_anchor_date}, usual check {_fmt(p.default_check_cents)}"
        )

    # Paychecks and their lines.
    checks: list[tuple[CheckBlock, Paycheck]] = []
    for block in sorted(tab.checks, key=lambda b: (b.pay_date, b.ordinal is None)):
        person = _match_person(block.who, plist)
        check = Paycheck(
            month_id=month.id,
            person_id=person.id if person else None,
            label=f"{person.name if person else block.who} check {block.ordinal}"
            if block.ordinal
            else block.title,
            pay_date=block.pay_date,
            amount_cents=block.cents,
            ordinal=block.ordinal,
            is_one_off=block.ordinal is None,
        )
        for i, (_row, cents, line_label) in enumerate(block.lines):
            check.lines.append(
                PaycheckLine(
                    kind=_line_kind(line_label, auto_tag),
                    label=line_label[:160],
                    amount_cents=cents,
                    line_date=date_in_text(line_label),
                    sort_order=i,
                )
            )
        _repoint_remainder(block, check, account_name, report)
        s.add(check)
        checks.append((block, check))
    s.flush()

    # Bills, the bill template, and which check pays each bill.
    bills_cat = cats.get(bills_category.lower())
    bill_notes: list[tuple[MonthBill, str]] = []
    for i, b in enumerate(tab.bills):
        person = _match_person(b.person_hint, plist) if b.person_hint else None
        tpl = BillTemplate(
            name=b.name,
            default_amount_cents=b.cents,
            assigned_person_id=person.id if person else None,
            assigned_check_ordinal=b.ordinal if person else None,
            note=b.note,
            sort_order=i,
        )
        s.add(tpl)
        s.flush()
        bill = MonthBill(
            month_id=month.id,
            bill_template_id=tpl.id,
            name=b.name,
            amount_cents=b.cents,
            note=b.note,
            sort_order=i,
        )
        s.add(bill)
        s.flush()
        where = "no check"
        if person and b.ordinal:
            match = next(
                (
                    c
                    for blk, c in checks
                    if c.person_id == person.id and c.ordinal == b.ordinal and not c.is_one_off
                ),
                None,
            )
            if match is None:
                report.append(f"  ! bill {b.name}: no {person.name} check {b.ordinal} this month")
                problems += 1
            else:
                bill.assigned_check_id = match.id
                where = match.label
        # Already paid? Either the sheet says so, or a ledger entry matches it exactly.
        paid_txn = next(
            (
                t
                for t in s.scalars(
                    select(Txn).where(Txn.month_id == month.id, Txn.amount_cents == -b.cents)
                )
                if norm_tokens(t.description) & norm_tokens(b.name)
                and not s.scalar(select(MonthBill.id).where(MonthBill.paid_txn_id == t.id))
            ),
            None,
        )
        if paid_txn:
            bill.paid = True
            bill.paid_txn_id = paid_txn.id
            if bills_cat and paid_txn.category_id is None:
                paid_txn.category_id = bills_cat.id
            where += f"; PAID by ledger entry {paid_txn.description!r}"
        elif b.paid:
            bill.paid = True
            where += "; marked paid on the sheet (no matching ledger entry)"
        bill_notes.append((bill, where))
    s.flush()
    linked: dict[int, str] = {}
    for _blk, check in checks:
        linked.update(link_bill_lines(check, [b for b, _ in bill_notes]))
    s.flush()
    for bill, where in bill_notes:
        extra = f" ({linked[bill.id]})" if bill.id in linked else ""
        head, _, tail = where.partition(";")
        report.append(
            f"  Bill {bill.name} {_fmt(bill.amount_cents)} -> {head}{extra}"
            + (f";{tail}" if tail else "")
        )

    # Checks already received show up as positive ledger entries.
    for block, check in checks:
        txn = next(
            (
                t
                for t in s.scalars(
                    select(Txn).where(
                        Txn.month_id == month.id, Txn.amount_cents == check.amount_cents
                    )
                )
                if "check" in t.description.lower() or "bonus" in t.description.lower()
                if not s.scalar(select(Paycheck.id).where(Paycheck.received_txn_id == t.id))
            ),
            None,
        )
        if txn:
            check.received = True
            check.received_txn_id = txn.id
        remainder = check.amount_cents + sum(ln.amount_cents for ln in check.lines)
        expected = (
            block.expected_remainder
            if block.expected_remainder is not None
            else block.sheet_remainder
        )
        ok = expected is None or expected == remainder
        if not ok:
            problems += 1
        report.append(
            f"  Check {check.label} {check.pay_date}: amount {_fmt(check.amount_cents)}, "
            f"{len(check.lines)} lines, remainder {_fmt(remainder)} "
            + (
                (
                    "(matches sheet, re-pointed to the joint account)"
                    if block.expected_remainder is not None
                    else "(matches sheet)"
                )
                if expected is not None and ok
                else f"(! sheet says {_fmt(expected)})"
                if not ok
                else "(no sheet total)"
            )
            + (" RECEIVED" if check.received else "")
        )

    # Spot-check numbers against the sheet.
    txns = list(s.scalars(select(Txn).where(Txn.month_id == month.id)))
    report.append("")
    report.append("  Spot-check against the sheet:")
    running = sum(t.amount_cents for t in txns)
    report.append(
        f"    Running total: {_fmt(running)}"
        + (
            f" (sheet {_fmt(tab.sheet_running_total)})"
            if tab.sheet_running_total is not None
            else ""
        )
    )
    for cat_name, sheet_value in tab.target_values.items():
        cat = cats.get(cat_name.lower())
        if cat is None:
            report.append(f"    ! no {cat_name} category exists; add it in Settings")
            problems += 1
            continue
        target = cat.monthly_target_cents
        sheet_target = tab.target_amounts.get(cat_name)
        if target is None and sheet_target is not None:
            cat.monthly_target_cents = target = sheet_target
        spent = sum(t.amount_cents for t in txns if t.category_id == cat.id)
        remaining = (target or 0) + spent
        flag = "matches" if remaining == sheet_value else "! differs from"
        if remaining != sheet_value:
            problems += 1
        report.append(
            f"    {cat_name} remaining: {_fmt(remaining)} ({flag} sheet {_fmt(sheet_value)})"
        )
    return problems


def _repoint_remainder(
    block: CheckBlock, check: Paycheck, account_name: str | None, report: list[str]
) -> None:
    """Some checks (e.g. a bonus) list the joint account as a line and send the remainder
    somewhere else. The app's rule is "remainder goes to the joint account", so swap them:
    the joint-account line goes away and the remainder becomes a line under the sheet's
    label. The money in and out is unchanged; only which part is "left over" moves."""
    if not account_name or not block.sum_label:
        return
    left_over = (
        block.sheet_remainder
        if block.sheet_remainder is not None
        else block.cents + sum(c for _r, c, _l in block.lines)
    )
    name = account_name.strip().lower()

    def is_account(label: str) -> bool:
        return label.strip().strip("?").strip().lower() in (name, f"to {name}")

    if is_account(block.sum_label) or name in block.sum_label.lower():
        return
    line = next((ln for ln in check.lines if is_account(ln.label)), None)
    if line is None:
        return
    pos = check.lines.index(line)
    check.lines.remove(line)
    check.lines.insert(
        pos,
        PaycheckLine(
            kind="other",
            label=block.sum_label[:160],
            amount_cents=-left_over,
            sort_order=line.sort_order,
        ),
    )
    block.expected_remainder = -line.amount_cents
    report.append(
        f"  note: {check.label}: the sheet sends {_fmt(-line.amount_cents)} to "
        f"{account_name} as a line and leaves {_fmt(left_over)} for "
        f"{block.sum_label!r}; imported with {block.sum_label!r} as a line and "
        f"{_fmt(-line.amount_cents)} as the remainder to {account_name}."
    )


def _count(items) -> dict:
    out: dict = defaultdict(int)
    for i in items:
        out[i] += 1
    return out


def _fmt(cents: int | None) -> str:
    from budget.money import format_cents

    return "n/a" if cents is None else format_cents(cents)
