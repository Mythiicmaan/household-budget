"""Draw the README's app screens as SVG, from the FAKE sample household.

    python scripts/make_readme_screens.py

The repo's privacy guardrails block image files, so these are SVG: plain text that the
private-data check can read word by word. Every number comes from the sample data.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from budget.config import Paths, load_config  # noqa: E402
from budget.db import make_engine, make_sessionmaker  # noqa: E402
from budget.money import format_cents as money  # noqa: E402
from budget.schedule import month_label  # noqa: E402
from budget.services import paychecks, summary  # noqa: E402
from budget.services.common import live_month  # noqa: E402

TODAY = date(2027, 1, 12)
W, H = 375, 700
PAD = 16
BG, CARD, TEXT, MUTED, LINE = "#f4f6f8", "#ffffff", "#0f172a", "#64748b", "#e2e8f0"
ACCENT, GOOD, BAD, WARN, CHIP = "#2563eb", "#15803d", "#b91c1c", "#b45309", "#eef2f7"
FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif"


class Screen:
    def __init__(self, title: str):
        self.parts: list[str] = []
        self.rect(0, 0, W, H, fill=BG)
        self.rect(0, 0, W, 44, fill=BG)
        self.text(PAD, 28, title, 15, weight=700)
        self.text(W - PAD, 28, "History · Settings", 13, fill=ACCENT, anchor="end")
        self.line(0, 44, W, 44)

    def rect(self, x, y, w, h, *, r=0, fill=CARD, stroke=None):
        s = f' stroke="{stroke}"' if stroke else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{s}/>'
        )

    def line(self, x1, y1, x2, y2, stroke=LINE):
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}"/>')

    def text(self, x, y, s, size=14, *, weight=400, fill=TEXT, anchor="start"):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}">{escape(str(s))}</text>'
        )

    def card(self, y, h, x=PAD, w=W - 2 * PAD):
        self.rect(x, y, w, h, r=14, stroke=LINE)

    def button(self, right, y, label, *, primary=True, w=None):
        w = w or len(label) * 7 + 20
        self.rect(
            right - w,
            y,
            w,
            30,
            r=10,
            fill=ACCENT if primary else CARD,
            stroke=None if primary else LINE,
        )
        self.text(
            right - w / 2,
            y + 20,
            label,
            12,
            weight=600,
            fill="#fff" if primary else TEXT,
            anchor="middle",
        )
        return right - w

    def field(self, x, y, w, value, *, h=40, anchor="start", size=14, weight=400):
        self.rect(x, y, w, h, r=10, stroke=LINE)
        tx = x + 12 if anchor == "start" else x + w / 2
        self.text(tx, y + h / 2 + 5, value, size, weight=weight, anchor=anchor)

    def fab(self):
        cx, cy = W - 48, H - 106
        self.parts.append(f'<circle cx="{cx}" cy="{cy}" r="32" fill="{ACCENT}"/>')
        self.rect(cx - 13, cy - 2, 26, 4, r=2, fill="#fff")
        self.rect(cx - 2, cy - 13, 4, 26, r=2, fill="#fff")

    def nav(self, active: str):
        self.rect(0, H - 60, W, 60)
        self.line(0, H - 60, W, H - 60)
        items = ["Home", "Ledger", "Bills", "Checks", "Cards"]
        for i, label in enumerate(items):
            on = label == active
            self.text(
                (i + 0.5) * W / 5,
                H - 24,
                label,
                12,
                weight=700 if on else 400,
                fill=ACCENT if on else MUTED,
                anchor="middle",
            )

    def svg(self) -> str:
        body = "\n".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
            f'height="{H}" font-family="{FONT}">\n{body}\n</svg>\n'
        )


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def home(sm) -> str:
    sc = Screen(sm.label)
    sc.card(56, 104)
    sc.text(32, 82, "RUNNING SURPLUS / DEFICIT", 11, fill=MUTED)
    color = GOOD if sm.running_total_cents >= 0 else BAD
    sc.text(32, 124, money(sm.running_total_cents), 36, weight=800, fill=color)
    sc.text(
        32, 147, f"In {money(sm.money_in_cents)} · Out {money(sm.money_out_cents)}", 12, fill=MUTED
    )
    half = (W - 2 * PAD - 12) / 2
    sc.card(172, 84, w=half)
    sc.text(28, 196, "TRUE STARTING BALANCE", 10, fill=MUTED)
    sc.text(28, 226, money(sm.true_start_cents), 19, weight=700)
    x2 = PAD + half + 12
    sc.card(172, 84, x=x2, w=half)
    sc.text(x2 + 12, 196, "END OF MONTH, ALL PAID", 10, fill=MUTED)
    sc.text(x2 + 12, 226, money(sm.projected_end_cents), 19, weight=700, fill=GOOD)
    sc.card(268, 136)
    y = 294
    for t in sm.targets[:2]:
        sc.text(32, y, t.category.name, 14, weight=700)
        sc.text(W - 32, y, f"{money(t.remaining_cents)} left", 14, weight=600, anchor="end")
        sc.rect(32, y + 10, W - 64, 9, r=4, fill=CHIP)
        width = (W - 64) * t.bar_percent / 100
        sc.rect(32, y + 10, width, 9, r=4, fill=WARN if t.over else GOOD)
        sc.text(32, y + 36, f"of {money(t.target_cents)}", 11, fill=MUTED)
        y += 58
    sc.card(416, 84)
    sc.text(32, 442, "Next paycheck", 15, weight=700)
    c = sm.next_check
    sc.text(
        32, 466, f"{c.label} · {c.pay_date.strftime('%a')} {c.pay_date.month}/{c.pay_date.day}", 14
    )
    sc.text(W - 32, 466, money(c.amount_cents), 14, weight=600, anchor="end")
    covers = ", ".join(ln.label for ln in c.lines)
    sc.text(32, 486, clip(f"Covers: {covers}", 52), 11, fill=MUTED)
    sc.card(512, 60)
    sc.text(32, 538, "Still to pay this month", 15, weight=700)
    sc.text(32, 558, f"{len(sm.to_pay)} items planned", 11, fill=MUTED)
    sc.fab()
    sc.nav("Home")
    return sc.svg()


def bills(page, label) -> str:
    sc = Screen(f"Bills · {label}")
    sc.card(56, 72)
    sc.text(32, 80, "BILLS THIS MONTH", 10, fill=MUTED)
    sc.text(32, 110, money(page.total_cents), 19, weight=700)
    sc.text(W - 32, 80, "STILL UNPAID", 10, fill=MUTED, anchor="end")
    sc.text(W - 32, 110, money(page.unpaid_cents), 19, weight=700, fill=WARN, anchor="end")
    sc.card(140, H - 216)
    y = 140

    def row(name, sub, amount, button=None, primary=True, extra=None):
        nonlocal y
        sc.text(32, y + 26, name, 14, weight=700)
        sc.text(32, y + 46, clip(sub, 30), 11, fill=MUTED)
        right = W - 32
        if extra:
            right = sc.button(right, y + 15, extra, primary=False) - 6
        if button:
            right = sc.button(right, y + 15, button, primary=primary) - 8
        sc.text(right, y + 34, amount, 13, weight=600, anchor="end")
        y += 62
        sc.line(16, y, W - 16, y)

    g, d = page.groceries, page.daycare
    row(
        g.label, f"{money(g.spent_cents)} spent · {money(g.left_cents)} left", money(g.budget_cents)
    )
    row(
        d.label,
        f"{d.paid_count} of {d.week_count} paid",
        money(d.total_cents),
        "Paid 1x",
        True,
        "Undo" if d.paid_count else None,
    )
    for b in page.bills[:5]:
        check = b.assigned_check.label if b.assigned_check else "No check"
        row(
            b.name,
            check,
            money(b.amount_cents),
            "Undo" if b.paid else "Paid",
            not b.paid,
        )
    sc.fab()
    sc.nav("Bills")
    return sc.svg()


def checks(check, remainder, label) -> str:
    sc = Screen(f"Checks · {label}")
    shown = check.lines[:6]
    sc.card(56, 168 + 62 * len(shown))
    sc.text(32, 84, check.label, 16, weight=700)
    d = check.pay_date
    sc.text(W - 32, 84, f"{d.strftime('%a')} {d.month}/{d.day}", 12, fill=MUTED, anchor="end")
    sc.text(32, 116, money(check.amount_cents), 15, weight=600)
    sc.button(W - 32, 96, "Received ✓", primary=False)
    y = 142
    for ln in shown:
        sc.field(32, y, 176, clip(ln.label, 20), h=38, size=13)
        sc.field(214, y, 84, f"{abs(ln.amount_cents) / 100:g}", h=38, size=13)
        sc.rect(304, y, 38, 38, r=10, fill=CARD, stroke=BAD)
        sc.text(323, y + 25, "✕", 14, weight=700, fill=BAD, anchor="middle")
        y += 44
        status = {"bill": "bill", "groceries": "tracked by the Groceries budget"}.get(
            ln.kind, ln.kind.replace("_", " ")
        )
        sc.text(34, y + 8, status, 10, fill=MUTED)
        y += 18
    sc.text(32, y + 20, "Remainder → Joint", 14)
    sc.text(W - 32, y + 20, money(remainder), 16, weight=800, anchor="end")
    sc.button(92, y + 36, "Save", w=60)
    sc.nav("Checks")
    return sc.svg()


def quick_add(categories, label) -> str:
    sc = Screen(label)
    sc.rect(0, 0, W, H, fill="#0206178c")
    top = 225
    sc.rect(0, top, W, H - top + 20, r=20)
    sc.text(PAD, top + 34, "Add entry", 16, weight=700)
    sc.text(W - PAD, top + 34, "Close", 14, weight=600, fill=ACCENT, anchor="end")
    sc.rect(PAD, top + 50, W - 2 * PAD, 44, r=12, fill=CHIP)
    half = (W - 2 * PAD) / 2
    sc.rect(PAD + 4, top + 54, half - 8, 36, r=9)
    sc.text(PAD + half / 2, top + 77, "Money out", 13, weight=600, fill=BAD, anchor="middle")
    sc.text(PAD + half * 1.5, top + 77, "Money in", 13, weight=600, anchor="middle")
    sc.field(PAD, top + 106, W - 2 * PAD, "45.10", h=62, anchor="middle", size=28, weight=700)
    sc.field(PAD, top + 180, W - 2 * PAD, "Corner Market", h=44)
    x, y = PAD, top + 238
    for i, name in enumerate(categories):
        w = len(name) * 8 + 26
        if x + w > W - PAD:
            x, y = PAD, y + 44
        sc.rect(x, y, w, 36, r=18, fill=ACCENT if i == 0 else CHIP)
        sc.text(x + w / 2, y + 23, name, 13, fill="#fff" if i == 0 else TEXT, anchor="middle")
        x += w + 8
    y += 58
    sc.text(PAD, y, "Date", 12, fill=MUTED)
    sc.text(PAD + half + 6, y, "Payment to card →", 12, fill=MUTED)
    sc.field(PAD, y + 8, half - 6, "Jan 12, 2027", h=44)
    sc.field(PAD + half + 6, y + 8, half - 6, "None", h=44)
    y += 66
    sc.rect(PAD, y, W - 2 * PAD, 46, r=12, fill=ACCENT)
    sc.text(W / 2, y + 29, "Save", 15, weight=700, fill="#fff", anchor="middle")
    return sc.svg()


def build(out_dir: Path) -> list[Path]:
    spec = importlib.util.spec_from_file_location("msd", ROOT / "scripts" / "make_sample_data.py")
    msd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msd)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory() as tmp:
        msd.main(["--data-dir", tmp, "--today", TODAY.isoformat(), "--force"])
        engine = make_engine(load_config(Paths(Path(tmp))).paths.db)
        session = make_sessionmaker(engine)()
        month = live_month(session)
        paychecks.pay_next_daycare(session, month)  # show the row part-way through the month
        paychecks.pay_next_daycare(session, month)
        session.flush()
        sm = summary.month_summary(session, month, today=TODAY)
        page = summary.bills_page(session, month)
        label = month_label(month.year, month.month)
        all_checks = paychecks.checks_in_month(session, month)
        check = all_checks[0]
        from budget.web.common import quick_add_context

        categories = [c.name for c in quick_add_context(session)["categories"]]
        screens = {
            "home": home(sm),
            "quick-add": quick_add(categories, label),
            "bills": bills(page, label),
            "checks": checks(check, paychecks.remainder(check), label),
        }
        for name, svg in screens.items():
            path = out_dir / f"{name}.svg"
            path.write_text(svg, encoding="utf-8")
            written.append(path)
        session.close()
        engine.dispose()
    return written


if __name__ == "__main__":
    for p in build(ROOT / "docs" / "screens"):
        print(p.relative_to(ROOT))
