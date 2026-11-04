import pytest

from budget import auth


def test_pin_hash_and_verify():
    h = auth.hash_pin("2468")
    assert h.startswith("$argon2")
    assert auth.verify_pin(h, "2468")
    assert not auth.verify_pin(h, "2469")
    assert not auth.verify_pin(None, "2468")
    assert not auth.verify_pin("garbage", "2468")


@pytest.mark.parametrize("bad", ["", "12", "abcd", "12 34", "1234567890123"])
def test_pin_validation(bad):
    with pytest.raises(ValueError):
        auth.hash_pin(bad)


def test_session_cookie_sign_verify_expire():
    c = auth.make_session_cookie("secret", days=1, now=1000)
    assert auth.check_session_cookie("secret", c, now=1001)
    assert not auth.check_session_cookie("other", c, now=1001)
    assert not auth.check_session_cookie("secret", c, now=1000 + 86401)
    tampered = c.replace(c.split(".")[0], "9999999999", 1)
    assert not auth.check_session_cookie("secret", tampered, now=1001)
    assert not auth.check_session_cookie("secret", None)
    assert not auth.check_session_cookie("secret", "a.b")


def test_rate_limiter_locks_and_expires():
    rl = auth.RateLimiter(max_failures=3, window_seconds=60, lockout_seconds=30)
    for t in (0, 1, 2):
        rl.record_failure(now=t)
    assert rl.seconds_locked(now=3) > 0
    assert rl.seconds_locked(now=40) == 0
    rl.record_failure(now=100)
    rl.record_failure(now=200)  # first one fell out of the window
    assert rl.seconds_locked(now=201) == 0


@pytest.mark.parametrize(
    "host,ok",
    [
        ("localhost:8765", True),
        ("127.0.0.1:8765", True),
        ("[::1]:8765", True),
        ("desktop.tail1234.ts.net", True),
        ("evil.com", False),
        ("localhost.evil.com", False),
        ("ts.net.evil.com", False),
        ("", False),
    ],
)
def test_host_allowed(host, ok):
    assert auth.host_allowed(host, []) is ok


def test_is_direct_local():
    assert auth.is_direct_local("localhost:8765", "127.0.0.1", {})
    assert not auth.is_direct_local("localhost:8765", "127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
    assert not auth.is_direct_local("pc.tailnet.ts.net", "127.0.0.1", {})
    assert not auth.is_direct_local("localhost", "100.64.0.2", {})
    assert not auth.is_direct_local("localhost", "127.0.0.1", {"Tailscale-User-Name": "x"})
    assert not auth.is_direct_local("localhost", "127.0.0.1", {"x-forwarded-proto": "https"})


def test_same_origin():
    h = {"host": "localhost:8765", "origin": "http://localhost:8765"}
    assert auth.same_origin("POST", h)
    assert not auth.same_origin("POST", {"host": "localhost:8765", "origin": "http://evil.com"})
    assert not auth.same_origin("POST", {"host": "a", "sec-fetch-site": "cross-site"})
    assert auth.same_origin("GET", {"host": "a", "origin": "http://evil.com"})
    assert not auth.same_origin("POST", {"host": "a", "referer": "http://evil.com/x"})


def test_same_origin_behind_proxy_that_rewrites_host():
    h = {
        "host": "127.0.0.1:8765",
        "x-forwarded-host": "pc.tail1234.ts.net",
        "origin": "https://pc.tail1234.ts.net",
    }
    assert auth.same_origin("POST", h)
    h["origin"] = "https://evil.example.com"
    assert not auth.same_origin("POST", h)
