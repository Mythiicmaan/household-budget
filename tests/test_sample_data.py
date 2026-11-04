import importlib.util
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("msd", ROOT / "scripts" / "make_sample_data.py")
msd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msd)


def test_sample_data_builds(tmp_path):
    assert (
        msd.main(["--data-dir", str(tmp_path), "--today", "2027-03-10", "--history-months", "2"])
        == 0
    )
    assert (tmp_path / "budget.db").exists()
    assert msd.main(["--data-dir", str(tmp_path)]) == 1  # no overwrite without --force


def test_refuses_real_data_folder(tmp_path):
    (tmp_path / "denylist.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        msd.main(["--data-dir", str(tmp_path)])


def test_today_parsing():
    assert date.fromisoformat("2027-03-10").month == 3
