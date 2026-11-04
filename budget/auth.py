"""Shared-PIN sign-in, signed session cookies, and request guards.

Security model (see README):
- Uvicorn listens on 127.0.0.1 only. Phones arrive through ``tailscale serve``,
  which proxies to localhost from the tailnet.
- The Host header must be localhost or a ``*.ts.net`` name (blocks DNS rebinding).
- The desktop itself (Host = localhost, no proxy headers) may skip the PIN.
- Everyone else needs the shared PIN (Argon2 hash in the database). A signed
  cookie keeps trusted phones signed in for ``session_days``.
- Unsafe methods must come from the same origin (blocks cross-site form posts).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy.orm import Session

from budget.services.common import get_setting, set_setting

PIN_HASH_KEY = "pin_hash"
SESSION_SECRET_KEY = "session_secret"  # noqa: S105 - a settings key name, not a secret
COOKIE_NAME = "hb_session"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
LOCAL_CLIENTS = {"127.0.0.1", "::1"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "tailscale-user-login", "forwarded")

_hasher = PasswordHasher()


# ---------------------------------------------------------------- PIN


def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not pin.isdigit() or not 4 <= len(pin) <= 12:
        raise ValueError("PIN must be 4 to 12 digits.")
    return pin


def hash_pin(pin: str) -> str:
    return _hasher.hash(validate_pin(pin))


def verify_pin(pin_hash: str | None, pin: str) -> bool:
    if not pin_hash:
        return False
    try:
        return _hasher.verify(pin_hash, (pin or "").strip())
    except (VerificationError, InvalidHashError):
        return False


def pin_is_set(s: Session) -> bool:
    return bool(get_setting(s, PIN_HASH_KEY))


def set_pin(s: Session, pin: str) -> None:
    """Store a new PIN hash and rotate the session secret (signs everyone out)."""
    set_setting(s, PIN_HASH_KEY, hash_pin(pin))
    set_setting(s, SESSION_SECRET_KEY, secrets.token_hex(32))


def session_secret(s: Session) -> str:
    secret = get_setting(s, SESSION_SECRET_KEY)
    if not secret:
        secret = secrets.token_hex(32)
        set_setting(s, SESSION_SECRET_KEY, secret)
    return secret


# ---------------------------------------------------------------- cookies


def _sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(secret: str, days: int, now: float | None = None) -> str:
    expires = int((now or time.time()) + days * 86400)
    payload = f"{expires}.{secrets.token_hex(8)}"
    return f"{payload}.{_sig(secret, payload)}"


def check_session_cookie(secret: str, value: str | None, now: float | None = None) -> bool:
    if not value or value.count(".") != 2:
        return False
    expires, nonce, sig = value.split(".")
    payload = f"{expires}.{nonce}"
    if not hmac.compare_digest(sig, _sig(secret, payload)):
        return False
    try:
        return int(expires) > (now or time.time())
    except ValueError:
        return False


# ---------------------------------------------------------------- rate limit


@dataclass
class RateLimiter:
    """Locks sign-in after too many wrong PINs. Global (all phones share one proxy address)."""

    max_failures: int = 5
    window_seconds: int = 15 * 60
    lockout_seconds: int = 5 * 60
    failures: deque = field(default_factory=deque)
    locked_until: float = 0.0

    def _prune(self, now: float) -> None:
        while self.failures and self.failures[0] < now - self.window_seconds:
            self.failures.popleft()

    def seconds_locked(self, now: float | None = None) -> int:
        now = now or time.time()
        return max(0, int(self.locked_until - now + 0.999))

    def record_failure(self, now: float | None = None) -> None:
        now = now or time.time()
        self._prune(now)
        self.failures.append(now)
        if len(self.failures) >= self.max_failures:
            self.locked_until = now + self.lockout_seconds
            self.failures.clear()

    def reset(self) -> None:
        self.failures.clear()
        self.locked_until = 0.0


# ---------------------------------------------------------------- request checks


def host_name(host_header: str | None) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):  # IPv6 literal, maybe with port
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def host_allowed(host_header: str | None, extra: list[str]) -> bool:
    h = host_name(host_header)
    return h in LOCAL_HOSTS or h.endswith(".ts.net") or h in extra


def is_direct_local(host_header: str | None, client_host: str | None, headers) -> bool:
    """True only for the desktop's own browser hitting localhost directly (not via a proxy)."""
    if host_name(host_header) not in LOCAL_HOSTS:
        return False
    if client_host not in LOCAL_CLIENTS:
        return False
    return not any(h in headers for h in PROXY_HEADERS)


def same_origin(method: str, headers) -> bool:
    """Reject cross-site writes. Browsers send Origin (and Sec-Fetch-Site) on form posts."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if headers.get("sec-fetch-site", "") == "cross-site":
        return False
    origin = headers.get("origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()
    referer = headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() == (headers.get("host") or "").lower()
    return True
