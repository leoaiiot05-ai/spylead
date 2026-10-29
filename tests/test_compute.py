"""The real smoothing and analysis inside the jobs: determinism (AC-22), and the pool workers in a spawned process.

The windows of §6.4 and §7 need decades of data, which the real steps take minutes to fit. Here the warm-up, the
shifts and the window are patched down to months, which leaves the code paths as they are.
"""
import asyncio
import datetime
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pytest

from fakes import FakeFred, FakeTiingo, InlinePool, sources_of
from spylead import analysis, jobs, smoothing
from spylead.analysis import Daily, Step5
from spylead.catalog import CatalogEntry
from spylead.jobs import Jobs
from spylead.pit import Observations
from spylead.store import Store

EPOCH = datetime.date(1970, 1, 1)
NOW = datetime.datetime(2026, 10, 28, 15, 0, tzinfo=datetime.UTC)
TODAY = (NOW.date() - EPOCH).days
FIRST = TODAY - 900
LEAD = CatalogEntry("Lead", "LEADID", "LOG", 7, "", "")
OTHER = CatalogEntry("Other", "OTHERID", "LEVEL", 7, "", "")


def latent(days):
    return 0.2 * np.sin(2 * np.pi * days / 400) + 0.1 * np.sin(2 * np.pi * days / 170 + 1)


def weekly(lag, values):
    days = np.arange(FIRST, TODAY - lag, 7)
    return Observations(days, values(days), days + lag, np.zeros(days.size, dtype=bool))


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(smoothing, "WARM_UP_MONTHS", 6)
    monkeypatch.setattr(analysis, "MIN_WINDOW_YEARS", 0.5)
    monkeypatch.setattr(analysis, "MAX_SHIFT", 30)
    monkeypatch.setattr(analysis, "SHIFTS", np.arange(-30, 31))


def test_two_computes_on_the_same_data_give_identical_results(small, tmp_path):
    clock = lambda: NOW  # noqa: E731
    store = Store.open(tmp_path / "spylead.db", clock)
    store.sync_catalog([LEAD, OTHER])
    tiingo = FakeTiingo(weekly(0, lambda days: 450 * np.exp(latent(days))))
    fred = FakeFred()
    fred.add("LEADID", weekly(7, lambda days: 100 * np.exp(latent(days + 21))))
    fred.add("OTHERID", weekly(7, lambda days: np.cos(days / 90.0)))
    runner = Jobs(store, [LEAD, OTHER], InlinePool(), clock, sources_of(tiingo, fred))

    async def computes():
        runner.fetch()
        await runner.wait()
        first = (store.results(), store.curves())
        runner.recompute(2.0)
        await runner.wait()
        return first, (store.results(), store.curves())

    (results, curves), (again, curves_again) = asyncio.run(computes())
    assert runner.last.outcome == "COMPUTED"
    payload = results.payload
    assert payload["series"][0]["status"] == "KEPT"
    assert payload["f"]["status"] == "OK"
    assert again.payload == payload
    assert sorted(curves_again) == sorted(curves) == ["Lead", "Other", "SPY"]
    for name, curve in curves.items():
        assert curve.rt.tobytes() == curves_again[name].rt.tobytes()
        assert curve.ref.tobytes() == curves_again[name].ref.tobytes()
    store.close()


def test_the_pool_workers_run_in_a_spawned_process():
    # Windows starts pool workers by spawning: each task and result must pickle, and each worker be top-level.
    days = np.arange(9000, 9000 + 8 * 30, 30)
    y = np.sin(days / 50.0)
    spy = Daily(9000, np.full(10, np.nan))
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        one_sided, reference = pool.submit(jobs.smooth_series, days, y, 2.0, int(days[-1]) + 1).result()
        assert pool.submit(analysis.step3, spy, spy).result() is None
        assert pool.submit(analysis.step5, spy, []).result() == Step5(analysis.NO_MEMBERS)
    expected, expected_reference = jobs.smooth_series(days, y, 2.0, int(days[-1]) + 1)
    np.testing.assert_array_equal(one_sided.values, expected.values)
    np.testing.assert_array_equal(reference.values, expected_reference.values)
    assert reference.values.size == days[-1] - days[0] + 1
