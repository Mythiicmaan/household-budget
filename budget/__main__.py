"""Command line entry point.

python -m budget            run the server on 127.0.0.1:8765 (what the scheduled task runs)
python -m budget set-pin    set or change the shared PIN
python -m budget migrate    apply database migrations
"""

from __future__ import annotations

import argparse
import getpass
import sys

HOST = "127.0.0.1"  # never listen on the LAN; phones come in through Tailscale
PORT = 8765


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m budget")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "set-pin", "migrate"]
    )
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)

    from budget.config import load_config
    from budget.db import make_engine, make_sessionmaker, migrate, session_scope

    if args.command == "serve":
        import uvicorn

        uvicorn.run("budget.app:app", host=HOST, port=args.port, proxy_headers=False)
        return 0

    cfg = load_config()
    engine = make_engine(cfg.paths.db)
    migrate(engine)
    if args.command == "migrate":
        print(f"Database is up to date: {cfg.paths.db}")
        return 0

    from budget import auth

    pin = getpass.getpass("New PIN (4-12 digits): ")
    if pin != getpass.getpass("Repeat PIN: "):
        print("PINs don't match.")
        return 1
    try:
        with session_scope(make_sessionmaker(engine)) as s:
            auth.set_pin(s, pin)
    except ValueError as exc:
        print(exc)
        return 1
    print("PIN saved. Restart the app so phones are asked to sign in again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
