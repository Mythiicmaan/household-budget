"""Bills, paycheck plans, and the start-next-month flow."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from budget.models import LINE_KINDS, Month, MonthBill, Paycheck, PaycheckLine, Person
from budget.money import parse_money
from budget.services import bills, months, paychecks, summary
from budget.services.common import BudgetError
from budget.web.common import back_url, checkbox, get_db, redirect, render, resolve_month

router = APIRouter()


def _404():
    raise HTTPException(status_code=404)


def _check_or_none(s: Session, raw: str | None) -> Paycheck | None:
    if raw in (None, "", "none"):
        return None
    return s.get(Paycheck, int(raw))


# ---------------------------------------------------------------- bills


@router.get("/bills")
def bills_page(request: Request, m: str | None = None, s: Session = Depends(get_db)):
    month = resolve_month(s, m)
    if month is None:
        return redirect("/")
    page = summary.bills_page(s, month)
    return render(
        request,
        "bills.html",
        {
            "s": s,
            "month": month,
            "bills": page.bills,
            "weekly": page,
            "checks": paychecks.checks_in_month(s, month),
            "total": page.total_cents,
            "unpaid_total": page.unpaid_cents,
        },
    )


@router.post("/month/{month_id}/daycare/pay")
def pay_daycare(month_id: int, request: Request, s: Session = Depends(get_db)):
    month = s.get(Month, month_id) or _404()
    txn = paychecks.pay_next_daycare(s, month)
    return redirect(
        back_url(request, "/bills"),
        f"{txn.description}: paid, added to ledger as pending",
        undo=f"/month/{month_id}/daycare/undo",
    )


@router.post("/month/{month_id}/daycare/undo")
def undo_daycare(month_id: int, request: Request, s: Session = Depends(get_db)):
    month = s.get(Month, month_id) or _404()
    line = paychecks.undo_last_daycare(s, month)
    return redirect(back_url(request, "/bills"), f"{line.label}: not paid")


@router.post("/bills/{month_id}/add")
def add_bill(
    month_id: int,
    request: Request,
    name: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    check_id: str | None = Form(None),
    also_template: str | None = Form(None),
    s: Session = Depends(get_db),
):
    month = s.get(Month, month_id) or _404()
    bills.add_bill(
        s,
        month,
        name=name,
        amount_cents=parse_money(amount),
        note=note,
        check=_check_or_none(s, check_id),
        also_template=checkbox(also_template),
    )
    return redirect(back_url(request, "/bills"), f"Added {name.strip()}")


@router.post("/bill/{bill_id}/toggle")
def toggle_bill(bill_id: int, request: Request, s: Session = Depends(get_db)):
    bill = s.get(MonthBill, bill_id) or _404()
    if bill.paid:
        bills.mark_unpaid(s, bill)
        return redirect(back_url(request, "/bills"), f"{bill.name}: not paid")
    txn = bills.mark_paid(s, bill, date.today())
    return redirect(
        back_url(request, "/bills"),
        f"{bill.name}: paid, added to ledger",
        undo=f"/bill/{bill.id}/toggle" if txn else None,
    )


@router.post("/bill/{bill_id}")
def edit_bill(
    bill_id: int,
    request: Request,
    name: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    check_id: str | None = Form(None),
    also_template: str | None = Form(None),
    s: Session = Depends(get_db),
):
    bill = s.get(MonthBill, bill_id) or _404()
    bills.update_bill(
        s,
        bill,
        name=name,
        amount_cents=parse_money(amount),
        note=note,
        check=_check_or_none(s, check_id),
        also_template=checkbox(also_template),
    )
    msg = "Bill saved" + (" (and template)" if checkbox(also_template) else " for this month")
    return redirect(back_url(request, "/bills"), msg)


@router.post("/bill/{bill_id}/delete")
def delete_bill(
    bill_id: int,
    request: Request,
    also_template: str | None = Form(None),
    s: Session = Depends(get_db),
):
    bill = s.get(MonthBill, bill_id) or _404()
    name = bill.name
    bills.remove_bill(s, bill, also_template=checkbox(also_template))
    return redirect(back_url(request, "/bills"), f"Removed {name}")


# ---------------------------------------------------------------- checks


@router.get("/checks")
def checks_page(request: Request, m: str | None = None, s: Session = Depends(get_db)):
    month = resolve_month(s, m)
    if month is None:
        return redirect("/")
    checks = paychecks.checks_in_month(s, month)
    from sqlalchemy import select

    return render(
        request,
        "checks.html",
        {
            "s": s,
            "month": month,
            "checks": checks,
            "remainders": {c.id: paychecks.remainder(c) for c in checks},
            "total_in": sum(c.amount_cents for c in checks),
            "total_remainder": sum(paychecks.remainder(c) for c in checks),
            "line_kinds": [k for k in LINE_KINDS if k != "bill"],
            "people": list(s.scalars(select(Person).where(Person.active.is_(True)))),
        },
    )


@router.post("/check/{check_id}/toggle")
def toggle_check(check_id: int, request: Request, s: Session = Depends(get_db)):
    check = s.get(Paycheck, check_id) or _404()
    if check.received:
        paychecks.mark_not_received(s, check)
        return redirect(back_url(request, "/checks"), f"{check.label}: not received")
    paychecks.mark_received(s, check)
    return redirect(back_url(request, "/checks"), f"{check.label}: received, added to ledger")


@router.post("/line/{line_id}/toggle")
def toggle_line(line_id: int, request: Request, s: Session = Depends(get_db)):
    line = s.get(PaycheckLine, line_id) or _404()
    if line.paid:
        paychecks.mark_line_unpaid(s, line)
        return redirect(back_url(request, "/checks"), f"{line.label}: not paid")
    paychecks.mark_line_paid(s, line)
    msg = f"{line.label}: paid, added to ledger"
    if line.kind == "daycare":
        msg += " as pending"
    return redirect(back_url(request, "/checks"), msg, undo=f"/line/{line.id}/toggle")


@router.post("/check/{check_id}")
async def edit_check(check_id: int, request: Request, s: Session = Depends(get_db)):
    """Save a whole check card at once: amount, label, date, and every line."""
    check = s.get(Paycheck, check_id) or _404()
    form = await request.form()
    paychecks.update_check(
        s,
        check,
        label=str(form.get("label", "")),
        amount_cents=parse_money(str(form.get("amount", "0"))),
        pay_date=date.fromisoformat(str(form["pay_date"])) if form.get("pay_date") else None,
    )
    for line in list(check.lines):
        prefix = f"line-{line.id}-"
        if checkbox(form.get(prefix + "delete")):
            paychecks.delete_line(s, line)
            continue
        # Only send what the user actually changed. (Saving a check must not rewrite the
        # bills behind its bill lines with the check's own wording or amounts.)
        raw = str(form.get(prefix + "amount", "")).strip()
        new_label = str(form.get(prefix + "label", line.label)).strip()
        new_amount = -abs(parse_money(raw)) if raw else line.amount_cents
        if new_label != line.label.strip() or new_amount != line.amount_cents:
            paychecks.update_line(
                s,
                line,
                label=new_label if new_label != line.label.strip() else None,
                amount_cents=new_amount if new_amount != line.amount_cents else None,
            )
    new_amount = str(form.get("new-amount", "")).strip()
    if new_amount:
        paychecks.add_line(
            s,
            check,
            kind=str(form.get("new-kind", "other")),
            label=str(form.get("new-label", "")),
            amount_cents=-abs(parse_money(new_amount)),
        )
    return redirect(back_url(request, "/checks"), f"{check.label} saved")


@router.post("/checks/{month_id}/one-off")
def add_one_off(
    month_id: int,
    request: Request,
    label: str = Form(...),
    amount: str = Form(...),
    pay_date: str = Form(...),
    person_id: str | None = Form(None),
    s: Session = Depends(get_db),
):
    month = s.get(Month, month_id) or _404()
    paychecks.add_one_off(
        s,
        month,
        label=label,
        amount_cents=parse_money(amount),
        pay_date=date.fromisoformat(pay_date),
        person_id=int(person_id) if person_id else None,
    )
    return redirect(back_url(request, "/checks"), f"Added {label.strip()}")


@router.post("/check/{check_id}/delete")
def delete_check(check_id: int, request: Request, s: Session = Depends(get_db)):
    check = s.get(Paycheck, check_id) or _404()
    label = check.label
    paychecks.delete_check(s, check)
    return redirect(back_url(request, "/checks"), f"Removed {label}")


# ---------------------------------------------------------------- new month


@router.get("/month/new")
def new_month_page(request: Request, s: Session = Depends(get_db)):
    preview = months.next_month_preview(s)
    return render(
        request,
        "new_month.html",
        {
            "s": s,
            "preview": preview,
            "outstanding_total": sum(t.amount_cents for t in preview.outstanding),
        },
    )


@router.post("/month/new")
async def start_new_month(request: Request, s: Session = Depends(get_db)):
    form = await request.form()
    preview = months.next_month_preview(s)
    if f"{preview.year}-{preview.month}" != str(form.get("ym", "")):
        raise BudgetError("The month to start changed. Please try again.")
    raw_balance = str(form.get("bank_balance", "")).strip()
    cleared = {int(v) for v in form.getlist("cleared")}
    months.start_month(
        s,
        preview.year,
        preview.month,
        bank_balance_cents=parse_money(raw_balance) if raw_balance else None,
        cleared_txn_ids=cleared,
    )
    return redirect("/", f"{preview.label} started")
