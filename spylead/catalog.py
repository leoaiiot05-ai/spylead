"""Load and validate catalog.toml, the curated candidates (SPEC.md §4.3)."""
import re
from dataclasses import dataclass
from pathlib import Path

from spylead.config import ConfigError, check_table, read_toml
from spylead.pit import FORMS

NAME = re.compile(r"[A-Za-z0-9_]{1,40}")
SPY_NAME = "SPY"
# SPEC.md §11's pattern for FRED IDs: the ID goes into a FRED URL.
FRED_ID = re.compile(r"[A-Z0-9_]{1,40}")
FIELDS = {"name": str, "fred_id": str, "form": str, "default_lag_days": int, "title_en": str, "title_zh": str}


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    fred_id: str
    form: str
    default_lag_days: int
    title_en: str
    title_zh: str


def load_catalog(path: Path) -> tuple[CatalogEntry, ...]:
    """The entries in file order. Errors name an entry by its position: its name is a value from the file."""
    data = read_toml(path)
    check_table(data, {"series": list}, f"{path}: ")
    entries: list[CatalogEntry] = []
    positions: dict[str, int] = {}
    for position, table in enumerate(data["series"], start=1):
        where = f"{path}: entry {position}: "
        if not isinstance(table, dict):
            raise ConfigError(f"{where}must be a table")
        check_table(table, FIELDS, where)
        entry = CatalogEntry(**table)
        check_entry(entry, where)
        if entry.name in positions:
            raise ConfigError(f"{where}name duplicates entry {positions[entry.name]}")
        positions[entry.name] = position
        entries.append(entry)
    return tuple(entries)


def check_entry(entry: CatalogEntry, where: str) -> None:
    if not NAME.fullmatch(entry.name):
        raise ConfigError(f"{where}name must match {NAME.pattern}")
    if entry.name == SPY_NAME:
        # SPY's own row shares the series table, keyed by name (SPEC.md §10).
        raise ConfigError(f"{where}name {SPY_NAME} is reserved")
    if not FRED_ID.fullmatch(entry.fred_id):
        raise ConfigError(f"{where}fred_id must match {FRED_ID.pattern}")
    if entry.form not in FORMS:
        raise ConfigError(f"{where}form must be {' or '.join(FORMS)}")
    if entry.default_lag_days < 0:
        # A negative lag would date an estimated release before its observation (SPEC.md §5.2).
        raise ConfigError(f"{where}default_lag_days must be 0 or more")
