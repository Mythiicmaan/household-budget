"""Dashboard, quick add, and ledger."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from budget.models import Category, Month, Txn
from budget.money import MoneyError, parse_money
from budget.services import cards, ledger, months, summary
from budget.services.common import BudgetError, active_txns_query, month_for_date
from budget.web.common import (
    back_url,
    checkbox,
    get_db,
    month_qs,
    redirect,
    render,
    resolve_month,
)

router = APIRouter()


def _signed_amount(raw: str, direction: str) -> int:
    cents = abs(parse_money(raw))
    if cents == 0:
        raise BudgetError("Enter an amount.")
    return cents if direction == "in" else -cents


def _opt_int(v: str | None) -> int | None:
    return int(v) if v not in (None, "", "none") else None


@router.get("/")
def dashboard(request: Request, m: str | None = None, s: Session = Depends(get_db)):
    month = resolve_month(s, m)
    if month is None:
        return render(request, "welcome.html", {"s": s, "preview": months.next_month_preview(s)})
    summ = summary.month_summary(s, month)
    preview = months.next_month_preview(s)
    days_left = None
    if not month.is_history:
        from budget.schedule import month_bounds

        days_left = (month_bounds(month.year, month.month)[1] - date.today()).days
    return render(
        request,
        "dashboard.html",
        {
            "s": s,
            "month": month,
            "sm": summ,
            "preview": preview,
            "show_start_next": days_left is not None and days_left <= 3,
            "card_total": cards.total_active_balance(s),
            "qs": month_qs(month, None),
        },
    )


@router.post("/month/{month_id}/bank-balance")
def set_bank_balance(
    month_id: int, request: Request, amount: str = Form(""), s: Session = Depends(get_db)
):
    month = s.get(Month, month_id) or _404()
    months.set_bank_balance(month, parse_money(amount) if amount.strip() else None)
    return redirect(back_url(request), "Starting balance saved")


@router.post("/month/{month_id}/lock")
def lock_month(
    month_id: int, request: Request, locked: str = Form(""), s: Session = Depends(get_db)
):
    month = s.get(Month, month_id) or _404()
    months.set_locked(month, checkbox(locked))
    return redirect(back_url(request), "Month locked" if month.locked else "Month unlocked")


@router.get("/add")
def add_page(request: Request, s: Session = Depends(get_db)):
    return render(request, "add.html", {"s": s, "autofocus": True})


@router.post("/add")
def add_entry(
    request: Request,
    amount: str = Form(...),
    direction: str = Form("out"),
    description: str = Form(""),
    category_id: str | None = Form(None),
    date_: str = Form("", alias="date"),
    pending: str | None = Form(None),
    card_id: str | None = Form(None),
    token: str = Form(""),
    s: Session = Depends(get_db),
):
    d = date.fromisoformat(date_) if date_ else date.today()
    month = month_for_date(s, d)
    if month is None:
        raise BudgetError("Start a month first.")
    cents = _signed_amount(amount, direction)
    card = _opt_int(card_id)
    txn = ledger.add_txn(
        s,
        month=month,
        date_=d,
        amount_cents=cents,
        description=description,
        category_id=_opt_int(category_id),
        pending=checkbox(pending),
        card_id=card if cents < 0 else None,
        client_token=token or None,
    )
    s.flush()
    label = txn.description or "Entry"
    return redirect("/", f"Saved: {label}", undo=f"/txn/{txn.id}/delete?undo=1")


@router.get("/ledger")
def ledger_page(
    request: Request,
    m: str | None = None,
    cat: str | None = None,
    pending: str | None = None,
    s: Session = Depends(get_db),
):
    month = resolve_month(s, m)
    if month is None:
        return redirect("/")
    q = active_txns_query(month.id)
    entries = list(s.scalars(q))
    running = sum(t.amount_cents for t in entries)
    shown = entries
    cat_id = _opt_int(cat) if cat not in ("uncat",) else None
    if cat == "uncat":
        shown = [t for t in shown if t.category_id is None]
    elif cat_id:
        shown = [t for t in shown if t.category_id == cat_id]
    if checkbox(pending):
        shown = [t for t in shown if t.is_outstanding]
    cats = list(s.scalars(select(Category).order_by(Category.sort_order, Category.id)))
    return render(
        request,
        "ledger.html",
        {
            "s": s,
            "month": month,
            "entries": shown,
            "running": running,
            "filtered_total": sum(t.amount_cents for t in shown),
            "filter_cat": cat,
            "filter_pending": checkbox(pending),
            "all_categories": cats,
            "all_months": months.all_months(s),
        },
    )


@router.get("/txn/{txn_id}")
def edit_txn_page(txn_id: int, request: Request, s: Session = Depends(get_db)):
    txn = s.get(Txn, txn_id) or _404()
    links = ledger.links_for(s, txn)
    return render(
        request,
        "txn_edit.html",
        {
            "s": s,
            "txn": txn,
            "month": s.get(Month, txn.month_id),
            "links": links,
            "all_categories": list(
                s.scalars(select(Category).order_by(Category.sort_order, Category.id))
            ),
            "all_cards": cards.active_cards(s),
        },
    )


@router.post("/txn/{txn_id}")
def edit_txn(
    txn_id: int,
    amount: str = Form(...),
    direction: str = Form("out"),
    description: str = Form(""),
    category_id: str | None = Form(None),
    date_: str = Form("", alias="date"),
    pending: str | None = Form(None),
    cleared: str | None = Form(None),
    card_id: str | None = Form(None),
    note: str = Form(""),
    s: Session = Depends(get_db),
):
    txn = s.get(Txn, txn_id) or _404()
    month = s.get(Month, txn.month_id)
    cents = _signed_amount(amount, direction)
    ledger.update_txn(
        s,
        txn,
        date_=date.fromisoformat(date_) if date_ else None,
        amount_cents=cents,
        description=description,
        category_id=_opt_int(category_id),
        pending=checkbox(pending),
        card_id=_opt_int(card_id) if cents < 0 else None,
        note=note,
    )
    if txn.pending:
        ledger.set_cleared(txn, (txn.cleared_date or date.today()) if checkbox(cleared) else None)
    return redirect(f"/ledger{month_qs(month, None)}", "Entry updated")


@router.post("/txn/{txn_id}/delete")
def delete_txn(
    txn_id: int, request: Request, undo: str | None = None, s: Session = Depends(get_db)
):
    """Delete an entry (soft). ``?undo=1`` is used by the quick-add toast's Undo button."""
    txn = s.get(Txn, txn_id) or _404()
    month = s.get(Month, txn.month_id)
    links = ledger.delete_txn(s, txn)
    if undo:
        return redirect(back_url(request), "Entry removed")
    msg = "Entry deleted"
    if links.bill:
        msg += f" ({links.bill.name} marked unpaid)"
    if links.paycheck:
        msg += f" ({links.paycheck.label} marked not received)"
    return redirect(f"/ledger{month_qs(month, None)}", msg, undo=f"/txn/{txn.id}/restore")


@router.post("/txn/{txn_id}/restore")
def restore_txn(txn_id: int, request: Request, s: Session = Depends(get_db)):
    txn = s.get(Txn, txn_id) or _404()
    ledger.restore_txn(s, txn)
    return redirect(back_url(request), "Restored")


@router.post("/txn/{txn_id}/cleared")
def mark_cleared(txn_id: int, request: Request, s: Session = Depends(get_db)):
    txn = s.get(Txn, txn_id) or _404()
    ledger.set_cleared(txn, date.today())
    return redirect(back_url(request), f"Cleared: {txn.description or 'entry'}")


def _404():
    raise HTTPException(status_code=404)


__all__ = ["router", "MoneyError"]
