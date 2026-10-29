"""SQLite persistence (SPEC.md §10): series, observations, settings, curves, results and events.

One Store per process, used from one thread. Every change runs in one transaction that takes the write lock first.
"""
import datetime
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np

from spylead.catalog import SPY_NAME, CatalogEntry
from spylead.pit import Observations, day_to_date, parse_day, utc_text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 1.0
WAL_PRAGMA = "PRAGMA journal_mode = WAL"
DEFAULT_INFLECTIONS_PER_YEAR = 2.0
USER_LAG_DAYS = 30  # SPEC.md §4.4
ORIGIN_SPY, ORIGIN_CATALOG, ORIGIN_USER = "SPY", "CATALOG", "USER"
SOURCE_TIINGO, SOURCE_FRED = "TIINGO", "FRED"
LEVELS = ("INFO", "WARNING", "ERROR")
# SPEC.md §10: fetching, computing, owner actions, job ends.
EVENT_KINDS = (
    *("FETCH_STARTED", "FETCH_FINISHED", "UP_TO_DATE", "SERIES_UPDATED", "SERIES_FAILED"),
    *("COMPUTE_FINISHED", "STATUS_CHANGED", "LAMBDA_AT_LIMIT", "INVALID_VALUES"),
    *("SETTING_CHANGED", "SELECTION_CHANGED", "SERIES_ADDED", "SERIES_REMOVED"),
    *("JOB_FAILED", "JOB_INTERRUPTED"),
)

SCHEMA = (
    """CREATE TABLE series (
  name             TEXT PRIMARY KEY,
  origin           TEXT NOT NULL,      -- SPY | CATALOG | USER
  source           TEXT NOT NULL,      -- TIINGO | FRED
  source_id        TEXT NOT NULL,      -- SPY, or the FRED ID
  form             TEXT NOT NULL,      -- LOG | LEVEL
  default_lag_days INTEGER NOT NULL,
  title            TEXT,
  units            TEXT,
  frequency        TEXT,
  last_updated     TEXT,               -- FRED's last_updated, as given
  last_fetch_at    TEXT,               -- UTC ISO 8601
  last_fetch_error TEXT,               -- error code and English text; never a URL or a key
  added_at         TEXT NOT NULL
)""",
    """CREATE TABLE observations (
  series      TEXT    NOT NULL,
  obs_day     INTEGER NOT NULL,
  value       REAL    NOT NULL,
  release_day INTEGER NOT NULL,
  estimated   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (series, obs_day)
) WITHOUT ROWID""",
    """CREATE TABLE settings (
  key   TEXT PRIMARY KEY,              -- fetch_start | analysis_start | inflections_per_year | f_excluded (JSON list)
  value TEXT NOT NULL
)""",
    """CREATE TABLE curves (                  -- latest compute only
  series        TEXT PRIMARY KEY,
  rt_first_day  INTEGER NOT NULL,
  rt            BLOB    NOT NULL,      -- float64 little-endian, one per day, NaN = undefined
  ref_first_day INTEGER NOT NULL,
  ref           BLOB    NOT NULL
)""",
    """CREATE TABLE results (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  computed_at TEXT NOT NULL,
  payload     TEXT NOT NULL            -- JSON: per-candidate step 3 and 4 results, F (§11)
)""",
    """CREATE TABLE events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,               -- UTC ISO 8601
  level   TEXT NOT NULL,               -- INFO | WARNING | ERROR
  kind    TEXT NOT NULL,
  series  TEXT,
  message TEXT NOT NULL
)""",
)
# An update keeps added_at, FRED's metadata and the fetch fields.
UPSERT_SERIES = """INSERT INTO series (name, origin, source, source_id, form, default_lag_days, units, frequency, added_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE SET origin = excluded.origin, source = excluded.source, source_id = excluded.source_id,
  form = excluded.form, default_lag_days = excluded.default_lag_days"""


class StoreError(Exception):
    """A failed database operation. The message is SQLite's, which holds no stored value."""


@dataclass(frozen=True)
class SeriesRow:
    """A row of the series table (SPEC.md §10)."""

    name: str
    origin: str
    source: str
    source_id: str
    form: str
    default_lag_days: int
    title: str | None
    units: str | None
    frequency: str | None
    last_updated: str | None
    last_fetch_at: str | None
    last_fetch_error: str | None
    added_at: str


SERIES_COLUMNS = ", ".join(field.name for field in fields(SeriesRow))


@dataclass(frozen=True)
class Counts:
    """A series' stored observations. Points (SPEC.md §5.3) can be fewer: a late older release is no point."""

    observations: int
    estimated: int
    last_obs_day: int
    last_release_day: int


@dataclass(frozen=True)
class Curve:
    """A series' one-sided series and reference fit in y (SPEC.md §6.4, §6.5), one value per day from each first day."""

    rt_first_day: int
    rt: np.ndarray
    ref_first_day: int
    ref: np.ndarray


@dataclass(frozen=True)
class Results:
    computed_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    level: str
    kind: str
    series: str | None
    message: str


class Store:
    """data/spylead.db (SPEC.md §10)."""

    def __init__(self, db: sqlite3.Connection, clock: Callable[[], datetime.datetime]) -> None:
        self.db = db
        self.clock = clock

    @classmethod
    def open(cls, path: Path, clock: Callable[[], datetime.datetime]) -> "Store":
        """Opens or creates the database, with the pragmas and schema of SPEC.md §10."""
        try:
            db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        except sqlite3.Error as error:
            raise StoreError(f"database cannot be opened: {error}") from error
        store = cls(db, clock)
        try:
            store.set_journal()
            store.create_schema()
        except BaseException:
            db.close()
            raise
        return store

    def close(self) -> None:
        self.db.close()

    def set_journal(self) -> None:
        if self.rows(WAL_PRAGMA)[0][0] == "wal":
            self.rows("PRAGMA synchronous = NORMAL")
            return
        # Some external drives cannot hold WAL's shared memory (SPEC.md §10).
        log.warning("The database cannot use the WAL journal on this drive; it uses the DELETE journal instead")
        self.rows("PRAGMA journal_mode = DELETE")

    def create_schema(self) -> None:
        version = self.rows("PRAGMA user_version")[0][0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise StoreError(f"database has schema version {version}, and this spylead knows version {SCHEMA_VERSION}")
        with self.write() as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One transaction. It takes the write lock first, so a locked database fails before anything changes."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise StoreError(f"database write failed: {error}") from error
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException as error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise StoreError(f"database write failed: {error}") from error
            raise

    def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[tuple]:
        try:
            return self.db.execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise StoreError(f"database read failed: {error}") from error

    def now(self) -> str:
        return utc_text(self.clock())

    # Series

    def sync_catalog(self, entries: Sequence[CatalogEntry]) -> None:
        """Upserts SPY's row and the catalog's (SPEC.md §10). Catalog rows no longer in it go, with their data."""
        now = self.now()
        with self.write() as db:
            db.execute(UPSERT_SERIES, (SPY_NAME, ORIGIN_SPY, SOURCE_TIINGO, SPY_NAME, "LOG", 0, "USD", "D", now))
            stored = dict(db.execute("SELECT name, source_id FROM series").fetchall())
            for entry in entries:
                if stored.get(entry.name, entry.fred_id) != entry.fred_id:
                    # Another FRED series under the same name: the old first releases must not mix with its own.
                    forget(db, entry.name)
                row = (entry.name, ORIGIN_CATALOG, SOURCE_FRED, entry.fred_id, entry.form, entry.default_lag_days)
                db.execute(UPSERT_SERIES, (*row, None, None, now))
            names = {entry.name for entry in entries}
            for (name,) in db.execute("SELECT name FROM series WHERE origin = ?", (ORIGIN_CATALOG,)).fetchall():
                if name not in names:
                    delete(db, name)

    def add_user_series(self, fred_id: str, form: str, title: str | None, units: str | None, frequency: str | None) -> None:
        """A series the owner adds (SPEC.md §4.4): named by its FRED ID, with a default lag of 30 days."""
        row = (fred_id, ORIGIN_USER, SOURCE_FRED, fred_id, form, USER_LAG_DAYS, title, units, frequency, self.now())
        with self.write() as db:
            db.execute(
                "INSERT INTO series (name, origin, source, source_id, form, default_lag_days, title, units, frequency, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    def remove_user_series(self, name: str) -> bool:
        """Deletes a USER series with its observations and curves. False when there is no USER series of that name."""
        with self.write() as db:
            if db.execute("SELECT 1 FROM series WHERE name = ? AND origin = ?", (name, ORIGIN_USER)).fetchone() is None:
                return False
            delete(db, name)
        return True

    def set_metadata(self, name: str, title: str | None, units: str | None, frequency: str | None) -> None:
        with self.write() as db:
            db.execute("UPDATE series SET title = ?, units = ?, frequency = ? WHERE name = ?", (title, units, frequency, name))

    def set_form(self, name: str, form: str) -> None:
        with self.write() as db:
            db.execute("UPDATE series SET form = ? WHERE name = ?", (form, name))

    def record_fetch(self, name: str, error: str | None) -> None:
        """Stamps a fetch of the series, with its error, or None to clear the last one."""
        with self.write() as db:
            db.execute("UPDATE series SET last_fetch_at = ?, last_fetch_error = ? WHERE name = ?", (self.now(), error, name))

    def series(self, name: str) -> SeriesRow | None:
        rows = self.rows(f"SELECT {SERIES_COLUMNS} FROM series WHERE name = ?", (name,))
        return SeriesRow(*rows[0]) if rows else None

    def all_series(self) -> list[SeriesRow]:
        """Every row, by name."""
        return [SeriesRow(*row) for row in self.rows(f"SELECT {SERIES_COLUMNS} FROM series ORDER BY name")]

    # Observations

    def insert_observations(self, name: str, observations: Observations, last_updated: str | None = None) -> int:
        """Stores the observation days not yet stored, and FRED's last_updated when given (SPEC.md §8.2).

        Stored rows never change (§5.1). One transaction. Returns how many rows were inserted.
        """
        rows = zip(
            observations.obs_day.tolist(),
            observations.value.tolist(),
            observations.release_day.tolist(),
            observations.estimated.astype(int).tolist(),
        )
        with self.write() as db:
            inserted = db.executemany(
                "INSERT INTO observations (series, obs_day, value, release_day, estimated) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (series, obs_day) DO NOTHING",
                ((name, *row) for row in rows),
            ).rowcount
            if last_updated is not None:
                db.execute("UPDATE series SET last_updated = ? WHERE name = ?", (last_updated, name))
        return inserted

    def observations(self, name: str) -> Observations:
        """Every stored observation of the series, by observation day."""
        rows = self.rows(
            "SELECT obs_day, value, release_day, estimated FROM observations WHERE series = ? ORDER BY obs_day", (name,)
        )
        obs_day, value, release_day, estimated = zip(*rows) if rows else ((), (), (), ())
        return Observations(
            obs_day=np.array(obs_day, dtype=np.int64),
            value=np.array(value, dtype=float),
            release_day=np.array(release_day, dtype=np.int64),
            estimated=np.array(estimated, dtype=bool),
        )

    def latest_obs_day(self, name: str) -> int | None:
        return self.rows("SELECT MAX(obs_day) FROM observations WHERE series = ?", (name,))[0][0]

    def counts(self) -> dict[str, Counts]:
        """Per series with observations: how many, how many estimated, and the latest observation and release days."""
        rows = self.rows(
            "SELECT series, COUNT(*), SUM(estimated), MAX(obs_day), MAX(release_day) FROM observations GROUP BY series"
        )
        return {row[0]: Counts(*row[1:]) for row in rows}

    # Settings

    def setting(self, key: str) -> str | None:
        rows = self.rows("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def put_setting(self, key: str, value: str) -> None:
        with self.write() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def start_days(self) -> tuple[int, int] | None:
        """fetch_start and analysis_start (SPEC.md §8.1), or None before the first fetch."""
        fetch_start, analysis_start = self.setting("fetch_start"), self.setting("analysis_start")
        if fetch_start is None or analysis_start is None:
            return None
        return parse_day(fetch_start), parse_day(analysis_start)

    def set_start_days(self, fetch_start: int, analysis_start: int) -> tuple[int, int]:
        """Stores both days unless they are stored already: they never change (SPEC.md §8.1). Returns the stored days."""
        with self.write() as db:
            for key, day in (("fetch_start", fetch_start), ("analysis_start", analysis_start)):
                db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                    (key, day_to_date(day).isoformat()),
                )
        return self.start_days()

    def inflections_per_year(self) -> float:
        """N (SPEC.md §6.3), 2 until the owner changes it."""
        value = self.setting("inflections_per_year")
        return DEFAULT_INFLECTIONS_PER_YEAR if value is None else float(json.loads(value))

    def set_inflections_per_year(self, value: float) -> None:
        self.put_setting("inflections_per_year", json.dumps(value))

    def excluded(self) -> list[str]:
        """The names the owner unchecked from F (SPEC.md §7.4, §10)."""
        value = self.setting("f_excluded")
        return [] if value is None else json.loads(value)

    def set_excluded(self, names: Iterable[str]) -> None:
        self.put_setting("f_excluded", json.dumps(sorted(set(names))))

    # Curves and results

    def save_compute(self, curves: Mapping[str, Curve], payload: dict[str, Any]) -> str:
        """Replaces every curve and the results, in one transaction (SPEC.md §9). Returns computed_at."""
        computed_at = self.now()
        with self.write() as db:
            db.execute("DELETE FROM curves")
            db.executemany(
                "INSERT INTO curves (series, rt_first_day, rt, ref_first_day, ref) VALUES (?, ?, ?, ?, ?)",
                [
                    (name, curve.rt_first_day, to_blob(curve.rt), curve.ref_first_day, to_blob(curve.ref))
                    for name, curve in curves.items()
                ],
            )
            put_results(db, computed_at, payload)
        return computed_at

    def save_results(self, payload: dict[str, Any]) -> str:
        """Replaces the results alone, for REFIT_F (SPEC.md §9). Returns computed_at."""
        computed_at = self.now()
        with self.write() as db:
            put_results(db, computed_at, payload)
        return computed_at

    def curve(self, name: str) -> Curve | None:
        rows = self.rows("SELECT rt_first_day, rt, ref_first_day, ref FROM curves WHERE series = ?", (name,))
        return to_curve(rows[0]) if rows else None

    def curves(self) -> dict[str, Curve]:
        rows = self.rows("SELECT series, rt_first_day, rt, ref_first_day, ref FROM curves")
        return {row[0]: to_curve(row[1:]) for row in rows}

    def results(self) -> Results | None:
        rows = self.rows("SELECT computed_at, payload FROM results WHERE id = 1")
        return Results(rows[0][0], json.loads(rows[0][1])) if rows else None

    # Events

    def append_event(self, level: str, kind: str, message: str, series: str | None = None) -> int:
        """Records an event of SPEC.md §10, stamped now. Returns its id."""
        if level not in LEVELS or kind not in EVENT_KINDS:
            raise ValueError(f"unknown event level {level} or kind {kind}")
        with self.write() as db:
            return db.execute(
                "INSERT INTO events (ts, level, kind, series, message) VALUES (?, ?, ?, ?, ?)",
                (self.now(), level, kind, series, message),
            ).lastrowid

    def events(self, limit: int = 50) -> list[Event]:
        """The latest events, newest first."""
        rows = self.rows("SELECT id, ts, level, kind, series, message FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [Event(*row) for row in rows]


def forget(db: sqlite3.Connection, name: str) -> None:
    """Drops a series' observations, curves, FRED metadata and fetch fields, so its next fetch starts afresh."""
    db.execute("DELETE FROM observations WHERE series = ?", (name,))
    db.execute("DELETE FROM curves WHERE series = ?", (name,))
    db.execute(
        "UPDATE series SET title = NULL, units = NULL, frequency = NULL, last_updated = NULL, last_fetch_at = NULL, "
        "last_fetch_error = NULL WHERE name = ?",
        (name,),
    )


def delete(db: sqlite3.Connection, name: str) -> None:
    """Deletes a series with its observations and curves."""
    db.execute("DELETE FROM observations WHERE series = ?", (name,))
    db.execute("DELETE FROM curves WHERE series = ?", (name,))
    db.execute("DELETE FROM series WHERE name = ?", (name,))
    row = db.execute("SELECT value FROM settings WHERE key = 'f_excluded'").fetchone()
    if row is not None:
        # A series added again later is in F by default (SPEC.md §7.4).
        kept = [excluded for excluded in json.loads(row[0]) if excluded != name]
        db.execute("UPDATE settings SET value = ? WHERE key = 'f_excluded'", (json.dumps(kept),))


def put_results(db: sqlite3.Connection, computed_at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO results (id, computed_at, payload) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET computed_at = excluded.computed_at, payload = excluded.payload",
        (computed_at, json.dumps(payload, allow_nan=False)),
    )


def to_blob(values: np.ndarray) -> bytes:
    return np.ascontiguousarray(values, dtype="<f8").tobytes()


def to_curve(row: tuple) -> Curve:
    rt_first_day, rt, ref_first_day, ref = row
    return Curve(rt_first_day, np.frombuffer(rt, dtype="<f8"), ref_first_day, np.frombuffer(ref, dtype="<f8"))
