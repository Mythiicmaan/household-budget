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
