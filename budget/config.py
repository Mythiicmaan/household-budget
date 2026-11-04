"""Locations and local configuration.

Everything comes from the ``BUDGET_DATA_DIR`` environment variable
(default ``C:\\BudgetData``). Household-specific values live in
``config.toml`` inside that folder, never in source.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = r"C:\BudgetData"


@dataclass
class Paths:
    data_dir: Path

    @property
    def db(self) -> Path:
        return self.data_dir / "budget.db"

    @property
    def config(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "Import"


@dataclass
class Config:
    paths: Paths
    raw: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}

    @property
    def account_name(self) -> str:
        return str(self.section("app").get("account_name", "Joint"))

    @property
    def extra_allowed_hosts(self) -> list[str]:
        return [str(h).lower() for h in self.section("app").get("extra_allowed_hosts", [])]

    @property
    def localhost_skip_pin(self) -> bool:
        return bool(self.section("auth").get("localhost_skip_pin", True))

    @property
    def session_days(self) -> int:
        return int(self.section("auth").get("session_days", 90))

    @property
    def backup_dir(self) -> Path | None:
        d = self.section("backup").get("dir")
        return Path(d) if d else None

    @property
    def seed(self) -> dict[str, Any]:
        return self.section("seed")

    @property
    def import_options(self) -> dict[str, Any]:
        return self.section("import")


def get_paths() -> Paths:
    return Paths(Path(os.environ.get("BUDGET_DATA_DIR", DEFAULT_DATA_DIR)))


def load_config(paths: Paths | None = None) -> Config:
    paths = paths or get_paths()
    raw: dict[str, Any] = {}
    if paths.config.exists():
        with paths.config.open("rb") as fh:
            raw = tomllib.load(fh)
    return Config(paths=paths, raw=raw)
