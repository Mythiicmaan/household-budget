"""Shared web helpers: templates, DB session dependency, toasts, month lookup."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import date

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from budget.db import PACKAGE_DIR
from budget.models import Month
from budget.money import cents_to_input, format_cents
from budget.schedule import month_label
from budget.services.common import get_month, live_month

templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
TOAST_COOKIE = "hb_toast"


def _money(cents: int | None, sign: bool = False) -> str:
    return "—" if cents is None else format_cents(cents, sign=sign)


def _short_date(d: date | None) -> str:
    return "" if d is None else f"{d.strftime('%a')} {d.month}/{d.day}"


templates.env.filters["money"] = _money
templates.env.filters["input_cents"] = cents_to_input
templates.env.filters["short_date"] = _short_date
templates.env.globals["month_label"] = month_label


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.sessionmaker
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _encode_toast(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _decode_toast(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def redirect(
    url: str, toast: str | None = None, *, undo: str | None = None, error: bool = False
) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if toast:
        resp.set_cookie(
            TOAST_COOKIE,
            _encode_toast({"msg": toast, "undo": undo, "error": error}),
            max_age=60,
            httponly=True,
            samesite="lax",
        )
    return resp


def back_url(request: Request, default: str = "/") -> str:
    """Same-site path to return to after a form post."""
    ref = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    if ref and host and f"//{host}" in ref:
        path = ref.split(f"//{host}", 1)[1]
        if path.startswith("/") and not path.startswith("//"):
            return path
    return default


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    s: Session | None = (ctx or {}).get("s")
    context = {
        "request": request,
        "toast": _decode_toast(request.cookies.get(TOAST_COOKIE)),
        "account_name": request.app.state.config.account_name,
        "today": date.today(),
        "nav": name.split(".")[0],
        "add_token": uuid.uuid4().hex,
    }
    if s is not None:
        context["live"] = live_month(s)
        context["qa"] = quick_add_context(s)
    context.update(ctx or {})
    context.pop("s", None)
    resp = templates.TemplateResponse(request, name, context, status_code=status_code)
    if context["toast"]:
        resp.delete_cookie(TOAST_COOKIE)
    return resp


def quick_add_context(s: Session) -> dict:
    """Data the quick-add sheet (on every page) needs."""
    from sqlalchemy import select

    from budget.models import Category
    from budget.services import cards, ledger

    cats = list(
        s.scalars(
            select(Category)
            .where(Category.hidden.is_(False))
            .order_by(Category.sort_order, Category.id)
        )
    )
    descs = ledger.recent_descriptions(s)
    return {
        "categories": cats,
        "cards": cards.active_cards(s),
        "descriptions": descs,
        "desc_map": json.dumps({d.lower(): c for d, c in descs if c}),
    }


def parse_month_key(key: str | None) -> tuple[int, int] | None:
    if not key:
        return None
    try:
        y, m = key.split("-")
        y_i, m_i = int(y), int(m)
    except ValueError:
        return None
    if not 1 <= m_i <= 12:
        return None
    return y_i, m_i


def resolve_month(s: Session, key: str | None) -> Month | None:
    ym = parse_month_key(key)
    if ym:
        found = get_month(s, *ym)
        if found:
            return found
    return live_month(s)


def month_qs(month: Month | None, live: Month | None) -> str:
    """Query string to keep a non-live month selected across links."""
    if month is None or (live is not None and month.id == live.id):
        return ""
    return f"?m={month.key}"


def checkbox(value: str | None) -> bool:
    return value in ("on", "true", "1", "yes")
