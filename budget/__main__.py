"""Command line entry point.

python -m budget                  run the server on 127.0.0.1:8765 (what the scheduled task runs)
python -m budget set-pin          set or change the shared PIN
python -m budget migrate          apply database migrations
python -m budget backup           back up the database now
python -m budget restore FILE     replace the database with a backup (stop the app first)
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

HOST = "127.0.0.1"  # never listen on the LAN; phones come in through Tailscale
PORT = 8765


def log_config(logs_dir: Path) -> dict:
    """Uvicorn + app logging to the console and to logs/app.log (rotated, 5 x 2 MB)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(logs_dir / "app.log"),
        "maxBytes": 2_000_000,
        "backupCount": 5,
        "encoding": "utf-8",
        "formatter": "plain",
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {
            "file": handler,
            "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["file", "console"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["file"], "level": "WARNING", "propagate": False},
            "budget": {"handlers": ["file", "console"], "level": "INFO", "propagate": False},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m budget")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "set-pin", "migrate", "backup", "restore"],
    )
    parser.add_argument("file", nargs="?", help="backup file (restore only)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--yes", action="store_true", help="restore without asking")
    args = parser.parse_args(argv)

    from budget.config import load_config
    from budget.db import make_engine, make_sessionmaker, migrate, session_scope

    cfg = load_config()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "budget.app:app",
            host=HOST,
            port=args.port,
            proxy_headers=False,
            log_config=log_config(cfg.paths.logs),
        )
        return 0

    if args.command == "restore":
        from budget import backup

        if not args.file:
            print("Usage: python -m budget restore <backup file>")
            return 2
        src = Path(args.file)
        if not args.yes:
            answer = input(f"Replace {cfg.paths.db} with {src}? Type RESTORE to continue: ")
            if answer.strip() != "RESTORE":
                print("Cancelled. Nothing changed.")
                return 1
        try:
            aside = backup.restore(src, cfg.paths.db)
        except backup.BackupError as exc:
            print(f"Restore stopped: {exc}")
            return 1
        print(f"Restored from {src}. The previous database was kept as {aside}.")
        return 0

    engine = make_engine(cfg.paths.db)
    migrate(engine)
    if args.command == "migrate":
        print(f"Database is up to date: {cfg.paths.db}")
        return 0

    if args.command == "backup":
        import logging.config

        from budget import backup

        logging.config.dictConfig(log_config(cfg.paths.logs))
        result, failure = None, None
        with session_scope(make_sessionmaker(engine)) as s:
            try:  # caught inside the session so the failure is saved, not rolled back
                result = backup.backup_and_record(s, cfg.paths.db, cfg.backup_dir)
            except backup.BackupError as exc:
                failure = exc
        engine.dispose()
        if failure:
            print(f"Backup FAILED: {failure}")
            return 1
        print(f"Backup written: {result.file}")
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
