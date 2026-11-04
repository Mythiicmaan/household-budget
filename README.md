# household-budget

A self-hosted household budget app for one family: fast manual entry from phones,
monthly paycheck planning, and an "are we spending more than we earn?" dashboard.

> Work in progress. The full README (screenshots, architecture, security design) comes
> in the publishing phase. Everything in this repo uses a **fake sample household**.

## Quick start (development)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install
python scripts\make_sample_data.py          # creates .\data\sample\budget.db (fake data)
$env:BUDGET_DATA_DIR = "$PWD\data\sample"
uvicorn budget.app:app --reload --host 127.0.0.1 --port 8765
```

Then open <http://localhost:8765>.

## Checks

```powershell
pytest
ruff check . ; ruff format --check .
```

## Running 24/7 on Windows

`scripts/install.ps1` (run once from an **admin** PowerShell) sets up:

- a standard, non-admin local account (`budgetsvc`) that runs the app, with Modify rights
  only on the data and backup folders and Read on the code;
- a scheduled task that starts `python -m budget` at boot, before anyone signs in, and
  restarts it on failure;
- power settings so the PC never sleeps on AC power;
- nightly backups (SQLite online backup API) at 2:00 AM, keeping 30 daily + 12 monthly.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1        # set up
powershell -ExecutionPolicy Bypass -File .\scripts\check_service.ps1  # running? backups OK?
powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 <backup .db file>
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1      # undo (data untouched)
```

The app binds to `127.0.0.1` only, so there are no firewall rules or open ports; phones reach
it through Tailscale. Logs go to `%BUDGET_DATA_DIR%\logs\app.log`.
