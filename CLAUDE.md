# CLAUDE.md: household-budget

A self-hosted household budget app for one family: fast manual entry from phones, monthly paycheck planning, and a "are we spending more than we earn?" dashboard. It runs 24/7 on a Windows 10 desktop and is reached from phones over Tailscale.

## Read first
- The full spec lives **outside this repo** at `%BUDGET_DATA_DIR%\PLAN.md` (default `C:\BudgetData\PLAN.md`). Read it before starting work.
- **That file and everything in `C:\BudgetData\` is private.** Never copy real names, employers, card or bank names, dollar amounts, or spreadsheet contents into this repo, including code, comments, tests, fixtures, docs, commit messages, and screenshots.
- Use only the fake sample household from `scripts/make_sample_data.py` ("Alex" and "Sam," "Card A"/"Card B," round invented amounts).

## Stack
Python 3.12 · FastAPI + Uvicorn (bound to `127.0.0.1:8765` only) · Jinja2 + HTMX · SQLite via SQLAlchemy 2.x + Alembic · Chart.js (vendored in `static/`) · pytest · Ruff · GitHub Actions

## Rules
- Money is **integer cents**. Positive = in, negative = out.
- All paths come from the `BUDGET_DATA_DIR` env var. Never hard-code personal paths or values.
- Household-specific settings (pay anchors, check amounts, targets, PIN hash) live in `config.toml` or the database, never in source.
- Write tests for money math before the UI that uses it. Keep `pytest` and `ruff` green.
- Never use `eval` (including in the spreadsheet importer). Use the safe arithmetic parser.
- Mobile-first: every page must work one-handed on an iPhone. Quick add should take under 10 seconds.
- Don't add bank linking, CSV import, or anything that stores bank credentials.

## Commands
```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install
python scripts\make_sample_data.py      # fake data for dev/tests
uvicorn budget.app:app --reload --host 127.0.0.1 --port 8765
pytest
ruff check . ; ruff format .
```

## Privacy guardrails (don't weaken these)
- `.gitignore` blocks `*.db`, `*.sqlite*`, `*.xlsx`, `*.csv`, `.env`, `config.toml`, `data/`, `logs/`, `backups/`.
- Pre-commit runs `gitleaks` and `scripts/check_private_data.py`. The latter reads a deny list from outside the repo and blocks matching commits plus any spreadsheet or binary files.
- The repo stays **private** until the owner reviews it and makes it public.

## Working style
- Follow the phases in the private plan in order. Use a task list and commit after each meaningful step.
- When a step needs the owner (admin/UAC prompts, GitHub login, Tailscale sign-in, choosing the PIN, plugging in the backup drive, verifying the import), **stop and say exactly what's needed.** Don't work around it.
- Finish with: a summary of what was built, open questions, and the repo URL.
