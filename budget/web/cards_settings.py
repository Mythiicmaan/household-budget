"""Cards, history, and settings pages."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget import auth
from budget.models import BillTemplate, Card, Category, Person
from budget.money import parse_money
from budget.schedule import PerCheckLineTemplate
from budget.services import cards, months, summary
from budget.services.common import (
    BudgetError,
    per_check_line_templates,
    save_per_check_line_templates,
)
from budget.web.common import back_url, checkbox, get_db, redirect, render

router = APIRouter()


def _404():
    raise HTTPException(status_code=404)


# ---------------------------------------------------------------- cards


@router.get("/cards")
def cards_page(request: Request, s: Session = Depends(get_db)):
    return render(
        request,
        "cards.html",
        {
            "s": s,
            "active": cards.active_cards(s),
            "archived": cards.archived_cards(s),
            "total": cards.total_active_balance(s),
        },
    )


@router.post("/cards/add")
def add_card(
    request: Request, name: str = Form(...), balance: str = Form("0"), s: Session = Depends(get_db)
):
    cards.add_card(s, name, parse_money(balance or "0"))
    return redirect("/cards", f"Added {name.strip()}")


@router.post("/card/{card_id}/balance")
def update_balance(
    card_id: int,
    balance: str = Form(...),
    note: str = Form(""),
    s: Session = Depends(get_db),
):
    card = s.get(Card, card_id) or _404()
    cards.set_balance(s, card, parse_money(balance), note.strip())
    return redirect("/cards", f"{card.name} updated")


@router.post("/card/{card_id}/archive")
def archive_card(card_id: int, archived: str = Form("on"), s: Session = Depends(get_db)):
    card = s.get(Card, card_id) or _404()
    cards.archive(card, checkbox(archived))
    return redirect("/cards", f"{card.name} {'archived' if card.archived else 'restored'}")


@router.post("/card/{card_id}/delete")
def delete_card(card_id: int, s: Session = Depends(get_db)):
    card = s.get(Card, card_id) or _404()
    name = card.name
    cards.delete_card(s, card)
    return redirect("/cards", f"Deleted {name}")


# ---------------------------------------------------------------- history


@router.get("/history")
def history_page(request: Request, s: Session = Depends(get_db)):
    rows = summary.history(s)
    chart = {
        "labels": [r.key for r in rows],
        "in": [r.money_in_cents / 100 for r in rows],
        "out": [-r.money_out_cents / 100 for r in rows],
        "net": [r.net_cents / 100 for r in rows],
    }
    return render(
        request,
        "history.html",
        {"s": s, "rows": list(reversed(rows)), "chart_json": json.dumps(chart)},
    )


# ---------------------------------------------------------------- settings


@router.get("/settings")
def settings_page(request: Request, s: Session = Depends(get_db)):
    people = list(s.scalars(select(Person).order_by(Person.id)))
    return render(
        request,
        "settings.html",
        {
            "s": s,
            "categories": list(
                s.scalars(select(Category).order_by(Category.sort_order, Category.id))
            ),
            "people": people,
            "line_templates": per_check_line_templates(s),
            "bill_templates": list(
                s.scalars(
                    select(BillTemplate).order_by(
                        BillTemplate.active.desc(), BillTemplate.sort_order, BillTemplate.id
                    )
                )
            ),
            "months": list(reversed(months.all_months(s))),
            "pin_set": auth.pin_is_set(s),
            "is_local": getattr(request.state, "is_local", False),
            "backup_dir": request.app.state.config.backup_dir,
        },
    )


@router.post("/settings/category/add")
def add_category(name: str = Form(...), target: str = Form(""), s: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        raise BudgetError("Category name is required.")
    order = (s.scalar(select(func.max(Category.sort_order))) or 0) + 1
    s.add(
        Category(
            name=name,
            sort_order=order,
            monthly_target_cents=parse_money(target) if target.strip() else None,
        )
    )
    return redirect("/settings#categories", f"Added {name}")


@router.post("/settings/category/{cat_id}")
def edit_category(
    cat_id: int,
    name: str = Form(...),
    target: str = Form(""),
    hidden: str | None = Form(None),
    move: str = Form(""),
    s: Session = Depends(get_db),
):
    cat = s.get(Category, cat_id) or _404()
    if move in ("up", "down"):
        cats = list(s.scalars(select(Category).order_by(Category.sort_order, Category.id)))
        i = cats.index(cat)
        j = i - 1 if move == "up" else i + 1
        if 0 <= j < len(cats):
            cats[i], cats[j] = cats[j], cats[i]
        for n, c in enumerate(cats):
            c.sort_order = n
        return redirect("/settings#categories")
    if name.strip():
        cat.name = name.strip()
    cat.monthly_target_cents = abs(parse_money(target)) if target.strip() else None
    cat.hidden = checkbox(hidden)
    return redirect("/settings#categories", f"{cat.name} saved")


@router.post("/settings/person/add")
def add_person(
    name: str = Form(...),
    pay_anchor: str = Form(""),
    interval: str = Form("14"),
    default_check: str = Form("0"),
    s: Session = Depends(get_db),
):
    s.add(
        Person(
            name=name.strip(),
            pay_anchor_date=date.fromisoformat(pay_anchor) if pay_anchor else None,
            pay_interval_days=int(interval or 14),
            default_check_cents=parse_money(default_check or "0"),
        )
    )
    return redirect("/settings#people", f"Added {name.strip()}")


@router.post("/settings/person/{person_id}")
def edit_person(
    person_id: int,
    name: str = Form(...),
    pay_anchor: str = Form(""),
    interval: str = Form("14"),
    default_check: str = Form("0"),
    active: str | None = Form(None),
    s: Session = Depends(get_db),
):
    p = s.get(Person, person_id) or _404()
    p.name = name.strip() or p.name
    p.pay_anchor_date = date.fromisoformat(pay_anchor) if pay_anchor else None
    p.pay_interval_days = max(1, int(interval or 14))
    p.default_check_cents = parse_money(default_check or "0")
    p.active = checkbox(active)
    return redirect("/settings#people", f"{p.name} saved")


@router.post("/settings/per-check-lines")
async def save_lines(request: Request, s: Session = Depends(get_db)):
    form = await request.form()
    kinds = form.getlist("kind")
    labels = form.getlist("label")
    amounts = form.getlist("amount")
    offsets = form.getlist("day_offset")
    removes = set(form.getlist("remove"))
    out = []
    for i, (k, lbl, amt, off) in enumerate(zip(kinds, labels, amounts, offsets, strict=False)):
        if str(i) in removes or not str(amt).strip():
            continue
        out.append(
            PerCheckLineTemplate(
                kind=str(k),
                label=str(lbl).strip() or str(k).title(),
                amount_cents=abs(parse_money(str(amt))),
                day_offset=int(off or 0),
            )
        )
    save_per_check_line_templates(s, out)
    return redirect("/settings#lines", "Per-check lines saved (used for new months)")


@router.post("/settings/bill-template/{tpl_id:int}")
def edit_bill_template(
    tpl_id: int,
    name: str = Form(...),
    amount: str = Form("0"),
    person_id: str = Form(""),
    ordinal: str = Form(""),
    note: str = Form(""),
    active: str | None = Form(None),
    s: Session = Depends(get_db),
):
    tpl = s.get(BillTemplate, tpl_id) or _404()
    tpl.name = name.strip() or tpl.name
    tpl.default_amount_cents = abs(parse_money(amount or "0"))
    tpl.assigned_person_id = int(person_id) if person_id else None
    tpl.assigned_check_ordinal = int(ordinal) if ordinal else None
    tpl.note = note
    tpl.active = checkbox(active)
    return redirect("/settings#templates", f"{tpl.name} template saved")


@router.post("/settings/bill-template/add")
def add_bill_template(
    name: str = Form(...),
    amount: str = Form("0"),
    person_id: str = Form(""),
    ordinal: str = Form(""),
    note: str = Form(""),
    s: Session = Depends(get_db),
):
    order = (s.scalar(select(func.max(BillTemplate.sort_order))) or 0) + 1
    s.add(
        BillTemplate(
            name=name.strip(),
            default_amount_cents=abs(parse_money(amount or "0")),
            assigned_person_id=int(person_id) if person_id else None,
            assigned_check_ordinal=int(ordinal) if ordinal else None,
            note=note,
            sort_order=order,
        )
    )
    return redirect("/settings#templates", "Template added (used for new months)")


@router.post("/settings/pin")
def change_pin(
    request: Request,
    current: str = Form(""),
    new: str = Form(...),
    confirm: str = Form(...),
    s: Session = Depends(get_db),
):
    from budget.services.common import get_setting

    existing = get_setting(s, auth.PIN_HASH_KEY)
    if existing and not auth.verify_pin(existing, current):
        raise BudgetError("Current PIN is wrong.")
    if new.strip() != confirm.strip():
        raise BudgetError("The new PINs don't match.")
    try:
        auth.set_pin(s, new)
    except ValueError as exc:
        raise BudgetError(str(exc)) from exc
    s.flush()
    request.app.state.refresh_auth(s)
    return redirect(back_url(request, "/settings"), "PIN saved. Other devices must sign in again.")
