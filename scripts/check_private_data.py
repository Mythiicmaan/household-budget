"""Pre-commit guard: block private household data from entering the repo.

Reads a deny list from OUTSIDE the repo (``%BUDGET_DATA_DIR%\\denylist.txt``)
and fails if any staged file, or the commit message, contains a listed
word or phrase (case-insensitive, whole words). It also blocks spreadsheets,
databases, and any binary file.

Usage (wired up in .pre-commit-config.yaml):
    python scripts/check_private_data.py FILE [FILE ...]
    python scripts/check_private_data.py --commit-msg .git/COMMIT_EDITMSG

If the deny list is missing, the check FAILS, except in CI (``CI`` env var
set), where no private data exists to compare against.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BLOCKED_SUFFIXES = {
    ".xlsx",
    ".xls",
    ".xlsm",
    ".ods",
    ".csv",
    ".tsv",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".bak",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".heic",
    ".zip",
}
BLOCKED_NAMES = {"config.toml", ".env"}
DEFAULT_DATA_DIR = r"C:\BudgetData"


def data_dir() -> Path:
    return Path(os.environ.get("BUDGET_DATA_DIR", DEFAULT_DATA_DIR))


def load_denylist(path: Path) -> list[str]:
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Whole-word, case-insensitive patterns. Whitespace inside a phrase matches any whitespace."""
    out = []
    for term in terms:
        parts = [re.escape(p) for p in term.split()]
        body = r"\s+".join(parts)
        # \b only works next to word characters; use look-arounds so terms like
        # "$1,234" or "a@b.com" still match as whole tokens.
        out.append((term, re.compile(rf"(?<![\w]){body}(?![\w])", re.IGNORECASE)))
    return out


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def check_text(text: str, patterns) -> list[str]:
    return [term for term, pat in patterns if pat.search(text)]


def check_file(path: Path, patterns) -> list[str]:
    problems = []
    name = path.name.lower()
    if name in BLOCKED_NAMES or path.suffix.lower() in BLOCKED_SUFFIXES:
        problems.append(f"{path}: file type is not allowed in this repo")
        return problems
    try:
        data = path.read_bytes()
    except OSError as exc:  # deleted/renamed files
        return [f"{path}: could not read ({exc})"] if path.exists() else []
    if is_binary(data):
        problems.append(f"{path}: binary files are not allowed in this repo")
        return problems
    text = data.decode("utf-8", errors="replace")
    for term in check_text(text, patterns):
        # Never echo the full term into logs that might be shared; show a hint only.
        problems.append(f"{path}: contains a deny-listed term ({mask(term)})")
    return problems


def mask(term: str) -> str:
    if len(term) <= 2:
        return "*" * len(term)
    return term[0] + "*" * (len(term) - 2) + term[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--commit-msg", dest="commit_msg")
    parser.add_argument("--denylist", help="override deny list path (tests)")
    args = parser.parse_args(argv)

    deny_path = Path(args.denylist) if args.denylist else data_dir() / "denylist.txt"
    if deny_path.exists():
        patterns = compile_terms(load_denylist(deny_path))
    elif os.environ.get("CI"):
        patterns = []
    else:
        print(f"check_private_data: deny list not found at {deny_path}. Refusing to continue.")
        return 1

    problems: list[str] = []
    if args.commit_msg:
        text = Path(args.commit_msg).read_text(encoding="utf-8", errors="replace")
        # Ignore git's comment lines.
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
        for term in check_text(text, patterns):
            problems.append(f"commit message: contains a deny-listed term ({mask(term)})")
    for f in args.files:
        problems.extend(check_file(Path(f), patterns))

    if problems:
        print("Private-data check FAILED:")
        for p in problems:
            print("  - " + p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
