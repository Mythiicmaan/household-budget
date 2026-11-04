"""Import the old budget spreadsheet into a fresh database.

    python scripts/import_xlsx.py              # uses %BUDGET_DATA_DIR%\\Import\\<the one .xlsx>
    python scripts/import_xlsx.py --replace    # set aside an existing database first

The workbook and the database stay in BUDGET_DATA_DIR (outside the repo).
A report is written to %BUDGET_DATA_DIR%\\logs\\import-report.txt.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from budget import auth  # noqa: E402
from budget.config import load_config  # noqa: E402
from budget.db import make_engine, make_sessionmaker, migrate, session_scope  # noqa: E402
from budget.importer import ImportProblem, find_workbook, import_workbook  # noqa: E402
from budget.services.seed import seed_if_empty  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", help="path to the .xlsx (default: the one file in Import)")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="rename an existing budget.db aside (never deleted) and import fresh",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    paths = cfg.paths
    try:
        workbook = Path(args.workbook) if args.workbook else find_workbook(paths.import_dir)
    except ImportProblem as exc:
        print(exc)
        return 2

    if paths.db.exists():
        if not args.replace:
            print(
                f"{paths.db} already exists. Re-run with --replace to set it aside and re-import."
            )
            return 2
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(paths.db) + suffix)
            if p.exists():
                p.rename(paths.data_dir / f"budget-before-import-{stamp}.db{suffix}")

    engine = make_engine(paths.db)
    migrate(engine)
    options = cfg.import_options
    try:
        with session_scope(make_sessionmaker(engine)) as s:
            seed_if_empty(s, cfg.seed)
            auth.session_secret(s)
            s.flush()
            result = import_workbook(
                s,
                workbook,
                live_key=options.get("live_month"),
                auto_tag={str(k): str(v) for k, v in options.get("auto_tag", {}).items()},
            )
    except ImportProblem as exc:
        engine.dispose()
        paths.db.unlink(missing_ok=True)
        print(f"Import stopped: {exc}")
        return 2
    engine.dispose()

    paths.logs.mkdir(parents=True, exist_ok=True)
    report_path = paths.logs / "import-report.txt"
    report_path.write_text("\n".join(result.report) + "\n", encoding="utf-8")
    print(result.report[-1])
    print(f"Report: {report_path}")
    return 0 if result.problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
