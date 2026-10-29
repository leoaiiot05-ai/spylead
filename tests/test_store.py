import dataclasses
import datetime
import logging
import sqlite3
import time

import numpy as np
import pytest

from spylead import store as store_module
from spylead.catalog import CatalogEntry
from spylead.pit import Observations
from spylead.store import EVENT_KINDS, Counts, Curve, Event, Results, SeriesRow, Store, StoreError

START = datetime.datetime(2026, 10, 28, 14, 3, 11, tzinfo=datetime.UTC)
M2 = CatalogEntry("M2", "M2SL", "LOG", 55, "M2 money stock", "M2 貨幣供給量")
VIX = CatalogEntry("VIX", "VIXCLS", "LOG", 1, "CBOE volatility index", "芝加哥選擇權交易所波動率指數")
NFCI = CatalogEntry("NFCI", "NFCI", "LEVEL", 5, "Chicago Fed National Financial Conditions Index", "芝加哥聯準銀行全國金融狀況指數")


class Clock:
    def __init__(self):
        self.instant = START

    def __call__(self):
        return self.instant

    def advance(self, seconds):
        self.instant += datetime.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "spylead.db"


@pytest.fixture
def store(path, clock):
    opened = Store.open(path, clock)
    yield opened
    opened.close()


def observations(obs_day, value, release_day, estimated=None):
    obs_day = np.asarray(obs_day, dtype=np.int64)
    flags = np.zeros(obs_day.size, dtype=bool) if estimated is None else np.asarray(estimated, dtype=bool)
    return Observations(obs_day, np.asarray(value, dtype=float), np.asarray(release_day, dtype=np.int64), flags)


def columns(result):
    return result.obs_day.tolist(), result.value.tolist(), result.release_day.tolist(), result.estimated.tolist()


def curve(first_day, size, offset=0.0):
    values = np.arange(size, dtype=float) + offset
    values[0] = np.nan
    return Curve(first_day, values, first_day + 3, values[1:] * 2)


def raw(path):
    return sqlite3.connect(path, isolation_level=None)


# The schema (SPEC.md §10)

# (name, type, not null, default, primary key position) per column, as PRAGMA table_info gives them.
TABLES = {
    "series": [
        ("name", "TEXT", 0, None, 1),
        ("origin", "TEXT", 1, None, 0),
        ("source", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, None, 0),
        ("form", "TEXT", 1, None, 0),
        ("default_lag_days", "INTEGER", 1, None, 0),
        ("title", "TEXT", 0, None, 0),
        ("units", "TEXT", 0, None, 0),
        ("frequency", "TEXT", 0, None, 0),
        ("last_updated", "TEXT", 0, None, 0),
        ("last_fetch_at", "TEXT", 0, None, 0),
        ("last_fetch_error", "TEXT", 0, None, 0),
        ("added_at", "TEXT", 1, None, 0),
    ],
    "observations": [
        ("series", "TEXT", 1, None, 1),
        ("obs_day", "INTEGER", 1, None, 2),
        ("value", "REAL", 1, None, 0),
        ("release_day", "INTEGER", 1, None, 0),
        ("estimated", "INTEGER", 1, "0", 0),
    ],
    "settings": [("key", "TEXT", 0, None, 1), ("value", "TEXT", 1, None, 0)],
    "curves": [
        ("series", "TEXT", 0, None, 1),
        ("rt_first_day", "INTEGER", 1, None, 0),
        ("rt", "BLOB", 1, None, 0),
        ("ref_first_day", "INTEGER", 1, None, 0),
        ("ref", "BLOB", 1, None, 0),
    ],
    "results": [("id", "INTEGER", 0, None, 1), ("computed_at", "TEXT", 1, None, 0), ("payload", "TEXT", 1, None, 0)],
    "events": [
        ("id", "INTEGER", 0, None, 1),
        ("ts", "TEXT", 1, None, 0),
        ("level", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("series", "TEXT", 0, None, 0),
        ("message", "TEXT", 1, None, 0),
    ],
}


def schema(path):
    db = raw(path)
    tables = {
        name: [row[1:] for row in db.execute(f"PRAGMA table_info({name})").fetchall()]
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    }
    sql = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())
    version = db.execute("PRAGMA user_version").fetchone()[0]
    db.close()
    return tables, sql, version


def test_the_schema_is_that_of_section_10(store, path):
    tables, sql, version = schema(path)
    assert tables == TABLES
    assert sql["observations"].endswith("WITHOUT ROWID")
    assert "AUTOINCREMENT" in sql["events"]
    assert "CHECK (id = 1)" in sql["results"]
    assert version == 1


def test_the_database_uses_wal_with_synchronous_normal_and_a_busy_timeout_of_1000_ms(store):
    assert store.rows("PRAGMA journal_mode") == [("wal",)]
    assert store.rows("PRAGMA synchronous") == [(1,)]
    assert store.rows("PRAGMA busy_timeout") == [(1000,)]


def test_opening_again_changes_nothing(store, path, clock):
    store.sync_catalog([M2])
    before = schema(path)
    store.close()
    again = Store.open(path, clock)
    assert schema(path) == before
    assert [row.name for row in again.all_series()] == ["M2", "SPY"]
    again.close()


def test_a_database_of_another_schema_version_is_refused(path, clock):
    db = raw(path)
    db.execute("PRAGMA user_version = 2")
    db.close()
    with pytest.raises(StoreError, match="schema version 2"):
        Store.open(path, clock)


def test_a_file_that_is_not_a_database_is_refused(path, clock):
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(StoreError):
        Store.open(path, clock)


def test_the_wal_fallback_uses_the_delete_journal_with_a_warning(monkeypatch, caplog, path, clock):
    # A drive that refuses WAL answers the pragma with another journal mode.
    monkeypatch.setattr(store_module, "WAL_PRAGMA", "PRAGMA journal_mode = TRUNCATE")
    with caplog.at_level(logging.WARNING, logger="spylead.store"):
        store = Store.open(path, clock)
    assert store.rows("PRAGMA journal_mode") == [("delete",)]
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "DELETE" in caplog.records[0].getMessage()
    store.append_event("INFO", "FETCH_STARTED", "Fetch started")
    assert len(store.events()) == 1
    store.close()


def test_a_write_to_a_locked_database_fails_after_the_busy_timeout(monkeypatch, path, clock):
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_S", 0.2)
    store = Store.open(path, clock)
    other = raw(path)
    other.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(StoreError, match="database is locked"):
        store.append_event("INFO", "FETCH_STARTED", "Fetch started")
    assert 0.15 <= time.monotonic() - started < 2
    other.execute("ROLLBACK")
    other.close()
    assert store.events() == []
    store.close()


# Series rows


def test_the_catalog_sync_adds_spy_and_the_catalog(store):
    store.sync_catalog([M2, VIX])
    rows = {row.name: row for row in store.all_series()}
    assert list(rows) == ["M2", "SPY", "VIX"]
    assert rows["SPY"] == SeriesRow(
        "SPY", "SPY", "TIINGO", "SPY", "LOG", 0, None, "USD", "D", None, None, None, "2026-10-28T14:03:11Z"
    )
    assert rows["M2"] == SeriesRow(
        "M2", "CATALOG", "FRED", "M2SL", "LOG", 55, None, None, None, None, None, None, "2026-10-28T14:03:11Z"
    )


def test_the_catalog_sync_updates_and_keeps_metadata_fetch_fields_and_data(store, clock):
    store.sync_catalog([M2])
    store.set_metadata("M2", "M2", "Billions of Dollars", "M")
    store.insert_observations("M2", observations([100, 131], [1.5, 1.6], [150, 180]), last_updated="2026-10-22 07:54:01-05")
    store.record_fetch("M2", "TIMEOUT: FRED did not answer in time")
    clock.advance(3600)
    store.sync_catalog([dataclasses.replace(M2, form="LEVEL", default_lag_days=50)])
    row = store.series("M2")
    assert (row.form, row.default_lag_days) == ("LEVEL", 50)
    assert (row.title, row.units, row.frequency) == ("M2", "Billions of Dollars", "M")
    assert row.last_updated == "2026-10-22 07:54:01-05"
    assert (row.last_fetch_at, row.last_fetch_error) == ("2026-10-28T14:03:11Z", "TIMEOUT: FRED did not answer in time")
    assert row.added_at == "2026-10-28T14:03:11Z"
    assert store.latest_obs_day("M2") == 131


def test_a_changed_fred_id_drops_the_old_series_data(store, clock):
    store.sync_catalog([M2])
    store.set_metadata("M2", "M2", "Billions of Dollars", "M")
    store.insert_observations("M2", observations([100], [1.5], [150]), last_updated="2026-10-22 07:54:01-05")
    store.record_fetch("M2", None)
    store.save_compute({"M2": curve(100, 5)}, {})
    clock.advance(60)
    store.sync_catalog([dataclasses.replace(M2, fred_id="M2NS")])
    assert store.series("M2") == SeriesRow(
        "M2", "CATALOG", "FRED", "M2NS", "LOG", 55, None, None, None, None, None, None, "2026-10-28T14:03:11Z"
    )
    assert store.latest_obs_day("M2") is None
    assert store.curve("M2") is None


def test_the_catalog_sync_deletes_rows_that_left_the_catalog_with_their_data(store):
    store.sync_catalog([M2, VIX])
    store.add_user_series("T5YIE", "LEVEL", "5-Year Breakeven Inflation Rate", "Percent", "D")
    for name in ("M2", "VIX", "T5YIE"):
        store.insert_observations(name, observations([100], [1.5], [150]))
    store.save_compute({name: curve(100, 5) for name in ("M2", "VIX", "T5YIE")}, {})
    store.set_excluded(["VIX", "M2"])
    store.sync_catalog([M2])
    assert [row.name for row in store.all_series()] == ["M2", "SPY", "T5YIE"]
    assert store.latest_obs_day("VIX") is None
    assert store.curve("VIX") is None
    assert sorted(store.curves()) == ["M2", "T5YIE"]
    assert store.excluded() == ["M2"]


def test_a_user_row_with_the_name_of_a_new_catalog_entry_becomes_that_entry(store):
    store.add_user_series("NFCI", "LEVEL", "National Financial Conditions Index", "Index", "W")
    store.insert_observations("NFCI", observations([100], [-0.5], [105]))
    store.sync_catalog([NFCI])
    row = store.series("NFCI")
    assert (row.origin, row.source_id, row.default_lag_days, row.title) == ("CATALOG", "NFCI", 5, "National Financial Conditions Index")
    assert store.latest_obs_day("NFCI") == 100


def test_a_user_series_is_named_by_its_fred_id_with_a_lag_of_30_days(store, clock):
    store.add_user_series("T5YIE", "LEVEL", "5-Year Breakeven Inflation Rate", "Percent", "D")
    assert store.series("T5YIE") == SeriesRow(
        "T5YIE", "USER", "FRED", "T5YIE", "LEVEL", 30, "5-Year Breakeven Inflation Rate", "Percent", "D",
        None, None, None, "2026-10-28T14:03:11Z",
    )


def test_a_second_user_series_of_one_name_is_refused(store):
    store.add_user_series("T5YIE", "LEVEL", None, None, None)
    with pytest.raises(StoreError):
        store.add_user_series("T5YIE", "LOG", None, None, None)
    assert store.series("T5YIE").form == "LEVEL"


def test_removing_a_user_series_deletes_its_data_and_its_exclusion(store):
    store.sync_catalog([M2])
    for name in ("T5YIE", "DGS10"):
        store.add_user_series(name, "LEVEL", None, None, None)
        store.insert_observations(name, observations([100, 101], [1.5, 1.6], [101, 102]))
    store.save_compute({"T5YIE": curve(100, 5), "DGS10": curve(100, 5), "M2": curve(100, 5)}, {})
    store.set_excluded(["T5YIE", "M2"])
    assert store.remove_user_series("T5YIE")
    assert store.series("T5YIE") is None
    assert store.observations("T5YIE").obs_day.size == 0
    assert sorted(store.curves()) == ["DGS10", "M2"]
    assert store.excluded() == ["M2"]
    assert store.latest_obs_day("DGS10") == 101


@pytest.mark.parametrize("name", ["M2", "SPY", "MISSING"])
def test_only_a_user_series_can_be_removed(store, name):
    store.sync_catalog([M2])
    store.insert_observations("M2", observations([100], [1.5], [150]))
    assert not store.remove_user_series(name)
    assert [row.name for row in store.all_series()] == ["M2", "SPY"]
    assert store.latest_obs_day("M2") == 100


def test_metadata_form_and_fetch_round_trip(store, clock):
    store.add_user_series("T5YIE", "LOG", None, None, None)
    store.set_metadata("T5YIE", "5-Year Breakeven Inflation Rate", "Percent", "D")
    store.set_form("T5YIE", "LEVEL")
    clock.advance(5)
    store.record_fetch("T5YIE", "HTTP_500: FRED answered with an error")
    row = store.series("T5YIE")
    assert (row.title, row.units, row.frequency, row.form) == ("5-Year Breakeven Inflation Rate", "Percent", "D", "LEVEL")
    assert (row.last_fetch_at, row.last_fetch_error) == ("2026-10-28T14:03:16Z", "HTTP_500: FRED answered with an error")
    clock.advance(5)
    store.record_fetch("T5YIE", None)
    row = store.series("T5YIE")
    assert (row.last_fetch_at, row.last_fetch_error) == ("2026-10-28T14:03:21Z", None)


def test_an_unknown_series_reads_as_none(store):
    assert store.series("MISSING") is None


# Observations


def test_observations_round_trip_in_observation_day_order(store):
    store.sync_catalog([M2])
    given = observations([131, 100, 159], [1.6, 1.5, 0.1 + 0.2], [180, 150, 4000], [False, False, True])
    assert store.insert_observations("M2", given) == 3
    assert columns(store.observations("M2")) == ([100, 131, 159], [1.5, 1.6, 0.1 + 0.2], [150, 180, 4000], [False, False, True])
    assert store.observations("M2").obs_day.dtype == np.int64


def test_inserting_an_existing_observation_day_leaves_the_stored_row_unchanged(store):
    store.sync_catalog([M2])
    store.insert_observations("M2", observations([100], [1.5], [150]))
    assert store.insert_observations("M2", observations([100, 131], [99.0, 1.6], [999, 180], [True, False])) == 1
    assert columns(store.observations("M2")) == ([100, 131], [1.5, 1.6], [150, 180], [False, False])


def test_a_failed_insert_stores_nothing(store):
    store.sync_catalog([M2])
    store.insert_observations("M2", observations([100], [1.5], [150]), last_updated="first")
    # SQLite stores NaN as NULL, which the NOT NULL column refuses: the last row fails the whole insert.
    with pytest.raises(StoreError):
        store.insert_observations("M2", observations([131, 159], [1.6, np.nan], [180, 210]), last_updated="second")
    assert columns(store.observations("M2")) == ([100], [1.5], [150], [False])
    assert store.series("M2").last_updated == "first"


def test_the_insert_stores_last_updated(store):
    store.sync_catalog([M2])
    store.insert_observations("M2", observations([100], [1.5], [150]), last_updated="2026-10-22 07:54:01-05")
    assert store.series("M2").last_updated == "2026-10-22 07:54:01-05"
    store.insert_observations("M2", observations([131], [1.6], [180]))
    assert store.series("M2").last_updated == "2026-10-22 07:54:01-05"


def test_the_latest_observation_day(store):
    store.sync_catalog([M2])
    assert store.latest_obs_day("M2") is None
    store.insert_observations("M2", observations([131, 100], [1.6, 1.5], [180, 150]))
    assert store.latest_obs_day("M2") == 131


def test_counts_per_series(store):
    store.sync_catalog([M2, VIX])
    store.insert_observations("M2", observations([100, 131, 159], [1.5, 1.6, 1.7], [4000, 180, 200], [True, False, False]))
    store.insert_observations("VIX", observations([10], [20.0], [10]))
    assert store.counts() == {"M2": Counts(3, 1, 159, 4000), "VIX": Counts(1, 0, 10, 10)}


def test_no_observations_read_as_empty_arrays(store):
    result = store.observations("M2")
    assert columns(result) == ([], [], [], [])
    assert (result.obs_day.dtype, result.estimated.dtype) == (np.int64, bool)


# Settings


def test_the_start_days_are_set_once(store, path, clock):
    assert store.start_days() is None
    assert store.set_start_days(8701, 9797) == (8701, 9797)
    assert store.set_start_days(9000, 10000) == (8701, 9797)
    assert store.setting("fetch_start") == "1993-10-28"
    store.close()
    again = Store.open(path, clock)
    assert again.set_start_days(9000, 10000) == (8701, 9797)
    again.close()


def test_n_is_2_until_set(store, path, clock):
    assert store.inflections_per_year() == 2
    store.set_inflections_per_year(2.75)
    assert store.inflections_per_year() == 2.75
    store.close()
    again = Store.open(path, clock)
    assert again.inflections_per_year() == 2.75
    again.close()


def test_the_exclusions_survive_close_and_reopen(store, path, clock):
    assert store.excluded() == []
    store.set_excluded(["VIX", "M2", "VIX"])
    store.close()
    again = Store.open(path, clock)
    assert again.excluded() == ["M2", "VIX"]
    again.set_excluded([])
    assert again.excluded() == []
    again.close()


# Curves and results


def test_curves_and_results_round_trip(store):
    curves = {"SPY": curve(9000, 6), "M2": curve(9100, 4, offset=0.5)}
    computed_at = store.save_compute(curves, {"series": [{"name": "M2", "a_star": -120, "score": None}]})
    assert computed_at == "2026-10-28T14:03:11Z"
    assert store.results() == Results(computed_at, {"series": [{"name": "M2", "a_star": -120, "score": None}]})
    for name, saved in curves.items():
        loaded = store.curve(name)
        assert (loaded.rt_first_day, loaded.ref_first_day) == (saved.rt_first_day, saved.ref_first_day)
        np.testing.assert_array_equal(loaded.rt, saved.rt)
        np.testing.assert_array_equal(loaded.ref, saved.ref)
        assert loaded.rt.dtype == np.float64
    assert sorted(store.curves()) == ["M2", "SPY"]


def test_a_compute_replaces_every_curve(store, clock):
    store.save_compute({"SPY": curve(9000, 6), "M2": curve(9100, 4)}, {"run": 1})
    clock.advance(60)
    store.save_compute({"SPY": curve(9001, 7)}, {"run": 2})
    assert sorted(store.curves()) == ["SPY"]
    assert store.curve("SPY").rt_first_day == 9001
    assert store.results() == Results("2026-10-28T14:04:11Z", {"run": 2})


def test_results_alone_keep_the_curves(store, clock):
    store.save_compute({"SPY": curve(9000, 6)}, {"run": 1})
    clock.advance(60)
    assert store.save_results({"run": 2}) == "2026-10-28T14:04:11Z"
    assert sorted(store.curves()) == ["SPY"]
    assert store.results().payload == {"run": 2}


@pytest.mark.parametrize("payload", [{"bad": {1, 2}}, {"score": float("nan")}], ids=["not-json", "nan"])
def test_a_failed_save_keeps_the_previous_curves_and_results(store, clock, payload):
    store.save_compute({"SPY": curve(9000, 6), "M2": curve(9100, 4)}, {"run": 1})
    clock.advance(60)
    with pytest.raises((TypeError, ValueError)):
        store.save_compute({"SPY": curve(9001, 7)}, payload)
    assert sorted(store.curves()) == ["M2", "SPY"]
    assert store.curve("SPY").rt_first_day == 9000
    assert store.results() == Results("2026-10-28T14:03:11Z", {"run": 1})


def test_nothing_computed_reads_as_none(store):
    assert store.results() is None
    assert store.curve("SPY") is None
    assert store.curves() == {}


# Events


def test_the_latest_events_come_newest_first(store, clock):
    for number in range(55):
        store.append_event("INFO", "SERIES_UPDATED", f"{number} new observations", series="M2")
        clock.advance(1)
    latest = store.events()
    assert len(latest) == 50
    assert [event.message for event in latest[:2]] == ["54 new observations", "53 new observations"]
    assert latest[0] == Event(55, "2026-10-28T14:04:05Z", "INFO", "SERIES_UPDATED", "M2", "54 new observations")
    assert [event.id for event in store.events(limit=3)] == [55, 54, 53]


def test_an_event_without_a_series(store):
    identifier = store.append_event("ERROR", "JOB_FAILED", "Tiingo has no data for SPY")
    assert store.events() == [Event(identifier, "2026-10-28T14:03:11Z", "ERROR", "JOB_FAILED", None, "Tiingo has no data for SPY")]


@pytest.mark.parametrize(("level", "kind"), [("DEBUG", "FETCH_STARTED"), ("INFO", "FETCH_BEGAN")])
def test_an_unknown_event_level_or_kind_is_refused(store, level, kind):
    with pytest.raises(ValueError):
        store.append_event(level, kind, "Fetch started")
    assert store.events() == []


def test_the_event_kinds_are_those_of_section_10():
    assert EVENT_KINDS == (
        "FETCH_STARTED", "FETCH_FINISHED", "UP_TO_DATE", "SERIES_UPDATED", "SERIES_FAILED",
        "COMPUTE_FINISHED", "STATUS_CHANGED", "LAMBDA_AT_LIMIT", "INVALID_VALUES",
        "SETTING_CHANGED", "SELECTION_CHANGED", "SERIES_ADDED", "SERIES_REMOVED",
        "JOB_FAILED", "JOB_INTERRUPTED",
    )
