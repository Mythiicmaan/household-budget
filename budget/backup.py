"""Nightly database backups and restore.

- Uses SQLite's online backup API, so it is safe while the app is running.
- Writes ``budget-YYYY-MM-DD.db`` into the configured backup folder.
- Keeps the newest 30 daily backups plus one per month (the earliest of each
  month, normally the 1st) for the newest 12 months.
- If the backup drive is missing it FAILS LOUDLY (status + log + banner);
  it never quietly writes somewhere else.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from budget.services.common import get_setting, set_setting

log = logging.getLogger("budget.backup")

KEEP_DAILY = 30
KEEP_MONTHLY = 12
STALE_AFTER = timedelta(hours=36)
NIGHTLY_AT_HOUR = 2  # 2:00 AM
NAME_RE = re.compile(r"^budget-(\d{4}-\d{2}-\d{2})\.db$")

OK_KEY = "backup_last_ok_at"
FILE_KEY = "backup_last_file"
ERR_KEY = "backup_last_error"
ERR_AT_KEY = "backup_last_error_at"
ATTEMPT_KEY = "backup_last_attempt_date"


class BackupError(RuntimeError):
    pass


def check_target(backup_dir: Path | None) -> Path:
    """The backup folder, created if needed, but only on a drive that is present."""
    if backup_dir is None:
        raise BackupError("No backup folder is configured ([backup] dir in config.toml).")
    root = Path(backup_dir.anchor) if backup_dir.anchor else None
    if root is None or not root.exists():
        raise BackupError(
            f"Backup drive {backup_dir.anchor or backup_dir} is not connected. "
            "Plug in the backup drive; nothing was written anywhere else."
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def integrity_ok(db_file: Path) -> bool:
    con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def copy_database(source_db: Path, dest_file: Path) -> None:
    """Consistent copy via the online backup API, verified, then moved into place."""
    tmp = dest_file.with_name(dest_file.name + ".partial")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(source_db.as_posix())
    dst = sqlite3.connect(tmp.as_posix())
    try:
        src.backup(dst)
        # A backup should be one self-contained file, not a WAL-mode database with side files.
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    if not integrity_ok(tmp):
        tmp.unlink(missing_ok=True)
        raise BackupError("The backup copy failed its integrity check.")
    for side in ("-wal", "-shm"):  # stale side files would not belong to the new copy
        Path(str(dest_file) + side).unlink(missing_ok=True)
    tmp.replace(dest_file)
    for side in ("-wal", "-shm"):
        Path(str(tmp) + side).unlink(missing_ok=True)


def backups_in(folder: Path) -> list[tuple[date, Path]]:
    out = []
    for p in folder.glob("budget-*.db"):
        m = NAME_RE.match(p.name)
        if m:
            out.append((date.fromisoformat(m.group(1)), p))
    return sorted(out)


def files_to_prune(dated: list[tuple[date, Path]]) -> list[Path]:
    """Everything except the newest 30 and the first backup of each of the newest 12 months."""
    keep: set[Path] = {p for _d, p in dated[-KEEP_DAILY:]}
    first_of_month: dict[tuple[int, int], Path] = {}
    for d, p in dated:  # oldest first, so the first one seen per month wins
        first_of_month.setdefault((d.year, d.month), p)
    for key in sorted(first_of_month)[-KEEP_MONTHLY:]:
        keep.add(first_of_month[key])
    return [p for _d, p in dated if p not in keep]


@dataclass
class BackupResult:
    file: Path
    pruned: list[Path]


def run_backup(source_db: Path, backup_dir: Path | None, today: date | None = None) -> BackupResult:
    today = today or date.today()
    folder = check_target(backup_dir)
    dest = folder / f"budget-{today.isoformat()}.db"
    copy_database(source_db, dest)
    pruned = files_to_prune(backups_in(folder))
    for p in pruned:
        p.unlink(missing_ok=True)
    return BackupResult(file=dest, pruned=pruned)


# ---------------------------------------------------------------- status kept in the database


def record_success(s: Session, result: BackupResult, now: datetime) -> None:
    set_setting(s, OK_KEY, now.isoformat(timespec="seconds"))
    set_setting(s, FILE_KEY, str(result.file))
    set_setting(s, ERR_KEY, "")
    set_setting(s, ATTEMPT_KEY, now.date().isoformat())


def record_failure(s: Session, error: str, now: datetime) -> None:
    set_setting(s, ERR_KEY, error)
    set_setting(s, ERR_AT_KEY, now.isoformat(timespec="seconds"))
    set_setting(s, ATTEMPT_KEY, now.date().isoformat())


@dataclass
class BackupStatus:
    configured: bool
    last_ok_at: datetime | None
    last_file: str
    last_error: str
    last_error_at: datetime | None
    problem: str | None  # banner text, or None when all is well

    @property
    def ok(self) -> bool:
        return self.problem is None


def _dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def status(s: Session, backup_dir: Path | None, now: datetime | None = None) -> BackupStatus:
    now = now or datetime.now()
    ok_at = _dt(get_setting(s, OK_KEY))
    err = get_setting(s, ERR_KEY, "") or ""
    err_at = _dt(get_setting(s, ERR_AT_KEY))
    problem = None
    if backup_dir is None:
        pass
    elif err and (ok_at is None or (err_at and err_at > ok_at)):
        problem = f"Last backup failed: {err}"
    elif ok_at is None:
        problem = "No backup has been made yet."
    elif now - ok_at > STALE_AFTER:
        problem = f"Last good backup is old ({ok_at:%b %d %I:%M %p})."
    return BackupStatus(
        configured=backup_dir is not None,
        last_ok_at=ok_at,
        last_file=get_setting(s, FILE_KEY, "") or "",
        last_error=err,
        last_error_at=err_at,
        problem=problem,
    )


def due(s: Session, now: datetime) -> bool:
    """Run at 2 AM each night, and catch up right away when the last good backup is
    more than a day old (e.g. the PC was off at 2 AM)."""
    ok_at = _dt(get_setting(s, OK_KEY))
    attempted_today = get_setting(s, ATTEMPT_KEY) == now.date().isoformat()
    if ok_at is None or now - ok_at > timedelta(hours=24):
        # Don't hammer a missing drive: after a failed attempt, retry hourly.
        err_at = _dt(get_setting(s, ERR_AT_KEY))
        return err_at is None or now - err_at >= timedelta(hours=1)
    return not attempted_today and now.hour >= NIGHTLY_AT_HOUR and ok_at.date() < now.date()


def backup_and_record(s: Session, source_db: Path, backup_dir: Path | None) -> BackupResult:
    """Run one backup and store the outcome. Raises BackupError on failure."""
    now = datetime.now()
    try:
        result = run_backup(source_db, backup_dir, now.date())
    except (BackupError, OSError, sqlite3.Error) as exc:
        msg = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        record_failure(s, msg, now)
        log.error("Backup FAILED: %s", msg)
        raise BackupError(msg) from exc
    record_success(s, result, now)
    log.info("Backup written to %s (pruned %d old file(s))", result.file, len(result.pruned))
    return result


# ---------------------------------------------------------------- restore


def restore(backup_file: Path, db_path: Path) -> Path:
    """Replace the live database with a backup. The current database (and its WAL files)
    is renamed aside first, never deleted. The app must be stopped. Returns the aside path."""
    if not backup_file.exists():
        raise BackupError(f"Backup file not found: {backup_file}")
    if not integrity_ok(backup_file):
        raise BackupError(f"{backup_file} failed its integrity check; not restoring it.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = db_path.with_name(f"budget-before-restore-{stamp}.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.rename(Path(str(aside) + suffix))
    shutil.copyfile(backup_file, db_path)
    if not integrity_ok(db_path):
        raise BackupError("The restored database failed its integrity check.")
    return aside
