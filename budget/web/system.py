"""Sign-in, health, JSON API, export, and the home-screen manifest/icon."""

from __future__ import annotations

import csv
import io
import struct
import zlib
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from budget import auth
from budget.models import Month, Txn
from budget.services import cards, summary
from budget.services.common import get_setting, live_month
from budget.web.common import get_db, redirect, render, resolve_month

router = APIRouter()


# ---------------------------------------------------------------- sign-in


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


@router.get("/login")
def login_page(request: Request, next: str = "/", s: Session = Depends(get_db)):
    limiter: auth.RateLimiter = request.app.state.limiter
    return render(
        request,
        "login.html",
        {
            "next": _safe_next(next),
            "locked_for": limiter.seconds_locked(),
            "pin_set": auth.pin_is_set(s),
        },
    )


@router.post("/login")
def login(
    request: Request, pin: str = Form(""), next: str = Form("/"), s: Session = Depends(get_db)
):
    limiter: auth.RateLimiter = request.app.state.limiter
    if limiter.seconds_locked():
        return redirect(
            f"/login?next={_safe_next(next)}", "Too many tries. Wait a few minutes.", error=True
        )
    if not auth.verify_pin(get_setting(s, auth.PIN_HASH_KEY), pin):
        limiter.record_failure()
        return redirect(f"/login?next={_safe_next(next)}", "Wrong PIN", error=True)
    limiter.reset()
    cfg = request.app.state.config
    resp = redirect(_safe_next(next))
    https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session_cookie(request.app.state.auth_secret, cfg.session_days),
        max_age=cfg.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=https,
    )
    return resp


@router.post("/logout")
def logout():
    resp = redirect("/login", "Signed out")
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------------------------------------------------------------- health + API


@router.get("/health")
def health(request: Request, s: Session = Depends(get_db)):
    s.execute(select(1))
    return {"status": "ok", "time": date.today().isoformat()}


@router.get("/api/summary")
def api_summary(m: str | None = None, s: Session = Depends(get_db)):
    """Read-only summary for other household apps (e.g. a wall display)."""
    month = resolve_month(s, m)
    if month is None:
        return JSONResponse({"month": None})
    sm = summary.month_summary(s, month)
    return {
        "month": month.key,
        "label": sm.label,
        "running_total_cents": sm.running_total_cents,
        "true_start_cents": sm.true_start_cents,
        "projected_end_cents": sm.projected_end_cents,
        "targets": [
            {
                "category": t.category.name,
                "target_cents": t.target_cents,
                "remaining_cents": t.remaining_cents,
            }
            for t in sm.targets
        ],
        "next_check": (
            {
                "label": sm.next_check.label,
                "date": sm.next_check.pay_date.isoformat(),
                "amount_cents": sm.next_check.amount_cents,
            }
            if sm.next_check
            else None
        ),
        "unpaid_bills": len(sm.unpaid_bills),
        "pending_items": len(sm.pending),
        "card_total_cents": cards.total_active_balance(s),
    }


# ---------------------------------------------------------------- export

EXPORT_COLUMNS = [
    "month",
    "date",
    "description",
    "category",
    "amount",
    "pending",
    "cleared_date",
    "card",
    "source",
    "note",
]


def _export_rows(s: Session, scope: str):
    q = (
        select(Txn, Month)
        .join(Month, Month.id == Txn.month_id)
        .where(Txn.deleted_at.is_(None))
        .order_by(Month.year, Month.month, Txn.date, Txn.id)
    )
    if scope != "all":
        lm = live_month(s)
        q = q.where(Txn.month_id == (lm.id if lm else -1))
    for txn, month in s.execute(q):
        yield [
            month.key,
            txn.date.isoformat(),
            txn.description,
            txn.category.name if txn.category else "",
            f"{txn.amount_cents / 100:.2f}",
            "yes" if txn.pending else "",
            txn.cleared_date.isoformat() if txn.cleared_date else "",
            txn.card.name if txn.card else "",
            txn.source,
            txn.note,
        ]


@router.get("/export")
def export(scope: str = "month", fmt: str = "csv", s: Session = Depends(get_db)):
    rows = list(_export_rows(s, scope))
    stamp = date.today().isoformat()
    name = f"budget-{'all' if scope == 'all' else 'month'}-{stamp}"
    if fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Ledger"
        ws.append(EXPORT_COLUMNS)
        for r in rows:
            r = list(r)
            r[4] = float(r[4])
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return Response(
            buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    w.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


# ---------------------------------------------------------------- home screen


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(
        {
            "name": "Household Budget",
            "short_name": "Budget",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        },
        media_type="application/manifest+json",
    )


def make_icon_png(size: int) -> bytes:
    """Draw the app icon (three rising bars) as a PNG, so the repo holds no binary files."""
    bg = (15, 23, 42)
    fg = (34, 197, 94)
    rows = []
    bar_w = size // 7
    gap = size // 14
    left = (size - (3 * bar_w + 2 * gap)) // 2
    bottom = int(size * 0.78)
    heights = [int(size * 0.28), int(size * 0.42), int(size * 0.56)]
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            color = bg
            for i, h in enumerate(heights):
                x0 = left + i * (bar_w + gap)
                if x0 <= x < x0 + bar_w and bottom - h <= y < bottom:
                    color = fg
            row.extend(color)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


_ICONS: dict[int, bytes] = {}


def _icon(size: int) -> Response:
    if size not in _ICONS:
        _ICONS[size] = make_icon_png(size)
    return Response(
        _ICONS[size], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/apple-touch-icon.png")
def apple_icon():
    return _icon(180)


@router.get("/icon-192.png")
def icon_192():
    return _icon(192)


@router.get("/icon-512.png")
def icon_512():
    return _icon(512)


@router.get("/favicon.ico")
def favicon():
    return _icon(32)
