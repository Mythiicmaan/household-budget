"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from budget import auth
from budget.config import Paths, load_config
from budget.db import PACKAGE_DIR, make_engine, make_sessionmaker, migrate, session_scope
from budget.money import MoneyError
from budget.services.common import BudgetError, get_setting
from budget.services.seed import seed_if_empty
from budget.web import cards_settings, pages, plans, system
from budget.web.common import back_url, redirect, render

log = logging.getLogger("budget")

PUBLIC_PREFIXES = ("/static/", "/login", "/health")
PUBLIC_PATHS = {
    "/manifest.webmanifest",
    "/apple-touch-icon.png",
    "/icon-192.png",
    "/icon-512.png",
    "/favicon.ico",
}

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app(paths: Paths | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = load_config(paths)
        cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
        engine = make_engine(cfg.paths.db)
        migrate(engine)
        factory = make_sessionmaker(engine)
        with session_scope(factory) as s:
            seed_if_empty(s, cfg.seed)
            auth.session_secret(s)
        app.state.config = cfg
        app.state.engine = engine
        app.state.sessionmaker = factory
        app.state.limiter = auth.RateLimiter()

        def refresh_auth(s: Session) -> None:
            app.state.auth_secret = auth.session_secret(s)
            app.state.pin_set = bool(get_setting(s, auth.PIN_HASH_KEY))

        app.state.refresh_auth = refresh_auth
        with session_scope(factory) as s:
            refresh_auth(s)
        yield
        engine.dispose()

    app = FastAPI(
        title="Household Budget", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        cfg = request.app.state.config
        headers = request.headers
        if not auth.host_allowed(headers.get("host"), cfg.extra_allowed_hosts):
            return PlainTextResponse("Host not allowed", status_code=400)
        if not auth.same_origin(request.method, headers):
            return PlainTextResponse("Cross-site request blocked", status_code=403)

        client = request.client.host if request.client else None
        local = auth.is_direct_local(headers.get("host"), client, headers)
        request.state.is_local = local
        path = request.url.path
        public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
        allowed = (
            public
            or (local and cfg.localhost_skip_pin)
            or auth.check_session_cookie(
                request.app.state.auth_secret, request.cookies.get(auth.COOKIE_NAME)
            )
        )
        if not allowed:
            if path.startswith("/api/"):
                return JSONResponse({"error": "sign in required"}, status_code=401)
            if not request.app.state.pin_set:
                return PlainTextResponse(
                    "No PIN has been set yet. Set one on the home PC (Settings) first.",
                    status_code=403,
                )
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(BudgetError)
    @app.exception_handler(MoneyError)
    async def friendly_error(request: Request, exc: Exception):
        if request.method == "GET":
            return render(request, "error.html", {"message": str(exc)}, status_code=400)
        return redirect(back_url(request), str(exc), error=True)

    for module in (pages, plans, cards_settings, system):
        app.include_router(module.router)
    return app


app = create_app()
