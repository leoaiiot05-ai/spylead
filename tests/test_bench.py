import datetime
from pathlib import Path

import numpy as np
import pytest

from fakes import InlinePool, quick_smooth, quick_step3, quick_step5
from spylead import analysis, bench
from spylead import jobs as jobs_module
from spylead.catalog import load_catalog
from spylead.store import Store

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime.date(1970, 1, 1)
NOW = datetime.datetime(2026, 10, 28, 15, 0, tzinfo=datetime.UTC)
TODAY = (NOW.date() - EPOCH).days


def weekday(days):
    return (EPOCH + datetime.timedelta(days=int(days))).weekday()


def test_the_frequencies_cover_the_catalog():
    assert list(bench.FREQUENCIES) == [entry.name for entry in load_catalog(ROOT / "catalog.toml")]


@pytest.mark.parametrize(
    ("frequency", "check"),
    [
        ("D", lambda days: np.diff(days).tolist() == [1] * (days.size - 1)),
        ("B", lambda days: {weekday(d) for d in days} == {0, 1, 2, 3, 4}),
        ("W", lambda days: {weekday(d) for d in days} == {4} and set(np.diff(days).tolist()) == {7}),
        ("M", lambda days: {(EPOCH + datetime.timedelta(days=int(d))).day for d in days} == {1} and days.size == 12),
    ],
)
def test_observation_days_follow_the_frequency(frequency, check):
    days = bench.observation_days(frequency, TODAY - 380, TODAY)
    assert check(days)
    assert days.min() >= TODAY - 380 and days.max() <= TODAY


@pytest.fixture
def store(tmp_path):
    opened = Store.open(tmp_path / "bench.db", lambda: NOW)
    yield opened
    opened.close()


def test_the_synthetic_data_has_the_catalog_s_frequencies_lags_and_spans(store):
    catalog = load_catalog(ROOT / "catalog.toml")
    store.sync_catalog(catalog)
    bench.fill(store, catalog, TODAY, years=5)
    counts = store.counts()
    spy = store.observations("SPY")
    assert {weekday(d) for d in spy.obs_day} == {0, 1, 2, 3, 4}
    assert spy.obs_day.min() >= TODAY - 5 * 366 and spy.release_day.tolist() == spy.obs_day.tolist()
    for entry in catalog:
        observations = store.observations(entry.name)
        np.testing.assert_array_equal(observations.release_day, observations.obs_day + entry.default_lag_days)
        assert observations.release_day.max() <= TODAY
        if entry.form == "LOG":
            assert (observations.value > 0).all()
    assert counts["Spread_HY"].observations < counts["Spread_BAA10Y"].observations / 1.5
    assert counts["FedFunds"].observations > counts["VIX"].observations > counts["Claims"].observations > counts["M2"].observations


def test_a_small_bench_compute_times_every_phase(monkeypatch, store):
    monkeypatch.setattr(jobs_module, "smooth_series", quick_smooth)
    monkeypatch.setattr(analysis, "step3", quick_step3)
    monkeypatch.setattr(analysis, "step5", quick_step5)
    catalog = load_catalog(ROOT / "catalog.toml")
    store.sync_catalog(catalog)
    bench.fill(store, catalog, TODAY, years=15)
    timings = bench.timed_compute(store, catalog, InlinePool(), lambda: NOW)
    assert list(timings) == ["SMOOTH", "STEP3", "STEP5", "SAVE"]
    assert all(seconds >= 0 for seconds in timings.values())


def test_the_report_names_every_phase_and_the_budget():
    timings = {"SMOOTH": 201.25, "STEP3": 18.2, "STEP5": 12.9, "SAVE": 0.4}
    assert bench.report(timings, 233.0, 7, 18).splitlines() == [
        "spylead bench: SPY on weekdays for 33 years, 18 candidates, 7 workers",
        "  SMOOTH    201.2 s",
        "  STEP3      18.2 s",
        "  STEP5      12.9 s",
        "  SAVE        0.4 s",
        "  total     233.0 s, within the budget of 600 s",
    ]
    assert bench.report(timings, 601.0, 7, 18).endswith("over the budget of 600 s")
