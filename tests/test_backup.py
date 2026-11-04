import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from budget import backup
from budget.app import create_app
from budget.config import Paths


def make_db(path: Path, rows: int = 3) -> Path:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")  # like the real database
    con.execute("CREATE TABLE t (x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()
    return path


def count_rows(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        con.close()


def test_run_backup_writes_verified_copy(tmp_path):
    src = make_db(tmp_path / "budget.db", rows=5)
    result = backup.run_backup(src, tmp_path / "backups" / "nested", date(2027, 1, 2))
    assert result.file.name == "budget-2027-01-02.db"
    assert count_rows(result.file) == 5
    assert backup.integrity_ok(result.file)
    folder = tmp_path / "backups" / "nested"
    assert [f.name for f in folder.iterdir()] == ["budget-2027-01-02.db"]  # one standalone file


def test_same_day_backup_replaces_that_days_file(tmp_path):
    src = make_db(tmp_path / "budget.db", rows=1)
    backup.run_backup(src, tmp_path / "b", date(2027, 1, 2))
    con = sqlite3.connect(src)
    con.execute("INSERT INTO t VALUES (9)")
    con.commit()
    con.close()
    backup.run_backup(src, tmp_path / "b", date(2027, 1, 2))
    files = list((tmp_path / "b").glob("budget-*.db"))
    assert len(files) == 1 and count_rows(files[0]) == 2


def test_retention_keeps_30_daily_and_12_monthly():
    start = date(2025, 1, 1)
    dated = [
        (start + timedelta(days=i), Path(f"budget-{start + timedelta(days=i)}.db"))
        for i in range(500)
    ]
    prune = set(backup.files_to_prune(dated))
    kept = [d for d, p in dated if p not in prune]
    newest = dated[-1][0]
    daily = [d for d in kept if d > newest - timedelta(days=30)]
    assert len(daily) == 30
    monthly = sorted({(d.year, d.month) for d in kept if d.day == 1})
    assert len(monthly) >= 12
    # Nothing older than the 12 monthly backups survives.
    oldest_kept = min(kept)
    assert oldest_kept.day == 1
    assert (newest.year - oldest_kept.year) * 12 + newest.month - oldest_kept.month <= 12
    assert len(kept) <= 30 + 12


def test_missing_config_fails_loudly():
    with pytest.raises(backup.BackupError, match="No backup folder"):
        backup.check_target(None)


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_missing_drive_fails_and_writes_nothing_elsewhere(tmp_path):
    letter = next(c for c in "QRSTUVWXYZ" if not Path(f"{c}:\\").exists())
    src = make_db(tmp_path / "budget.db")
    with pytest.raises(backup.BackupError, match="not connected"):
        backup.run_backup(src, Path(f"{letter}:\\Backups\\HouseholdBudget"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["budget.db"]


def test_restore_keeps_previous_database(tmp_path):
    live = make_db(tmp_path / "budget.db", rows=2)
    good = make_db(tmp_path / "budget-2027-01-01.db", rows=7)
    aside = backup.restore(good, live)
    assert count_rows(live) == 7
    assert count_rows(aside) == 2
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.restore(bad, live)
    assert count_rows(live) == 7  # untouched by the failed restore
    with pytest.raises(backup.BackupError):
        backup.restore(tmp_path / "missing.db", live)


def test_due_and_status(s):
    now = datetime(2027, 1, 10, 1, 0)
    st = backup.status(s, Path("X:/b"), now)
    assert st.problem == "No backup has been made yet."
    assert backup.due(s, now)  # never backed up -> right away

    result = backup.BackupResult(file=Path("X:/b/budget-2027-01-10.db"), pruned=[])
    now = datetime(2027, 1, 10, 2, 0)  # the nightly run
    backup.record_success(s, result, now)
    assert backup.status(s, Path("X:/b"), now).ok
    assert not backup.due(s, now + timedelta(hours=3))  # already done today
    assert backup.due(s, datetime(2027, 1, 11, 2, 0))  # next night at 2 AM
    assert not backup.due(s, datetime(2027, 1, 11, 1, 59))

    backup.record_failure(s, "Backup drive H:\\ is not connected.", datetime(2027, 1, 11, 2, 0))
    st = backup.status(s, Path("X:/b"), datetime(2027, 1, 11, 2, 1))
    assert st.problem.startswith("Last backup failed")
    assert not backup.due(s, datetime(2027, 1, 11, 2, 30))  # tried tonight already
    # More than a day without a good backup: retry hourly.
    assert backup.due(s, datetime(2027, 1, 11, 3, 1))
    assert not backup.status(s, Path("X:/b"), datetime(2027, 1, 12, 3, 0)).ok
    # Not configured: no banner.
    assert backup.status(s, None, now).problem is None


def test_stale_backup_banner(s):
    now = datetime(2027, 1, 10, 9, 0)
    backup.record_success(s, backup.BackupResult(Path("x.db"), []), now - timedelta(hours=37))
    assert "old" in backup.status(s, Path("X:/b"), now).problem


def test_back_up_now_button_banner_and_health(tmp_path):
    folder = tmp_path / "backups"
    (tmp_path / "config.toml").write_text(
        f"[backup]\ndir = '{folder}'\nnightly = false\n", encoding="utf-8"
    )
    app = create_app(Paths(tmp_path))
    with TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 1)) as c:
        assert "No backup has been made yet" in c.get("/settings").text
        assert c.get("/health").json()["backup"]["ok"] is False
        r = c.post("/backup")
        assert r.status_code == 200 and "Backed up to budget-" in r.text
        assert len(list(folder.glob("budget-*.db"))) == 1
        assert c.get("/health").json()["backup"]["ok"] is True
        assert "Backup problem" not in c.get("/").text
