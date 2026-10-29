"""The jobs with fake sources, a temporary database, an injected clock, and the in-process pool (SPEC.md §16.1).

The numerical steps are quick stand-ins with the real inputs and outputs (fakes.py); test_compute.py runs the real ones.
"""
import asyncio
import dataclasses
import datetime
import logging

import numpy as np
import pytest

from fakes import NONE, Clock, FakeFred, FakeTiingo, Gate, InlinePool, quick_smooth, quick_step3, quick_step5, sources_of
from spylead import analysis
from spylead import jobs as jobs_module
from spylead.analysis import Step5
from spylead.catalog import CatalogEntry
from spylead.config import Config
from spylead.jobs import JobRunning, Jobs
from spylead.pit import Observations, joined
from spylead.smoothing import Anchors
from spylead.sources import Fred, SourceError, Tiingo
from spylead.store import Store

EPOCH = datetime.date(1970, 1, 1)
NOW = datetime.datetime(2026, 10, 28, 15, 0, tzinfo=datetime.UTC)


def day(year, month, date):
    return (datetime.date(year, month, date) - EPOCH).days


TODAY = day(2026, 10, 28)
ANALYSIS_START = day(1996, 10, 28)
FETCH_START = day(1993, 10, 28)
LAST_MONTH = day(2026, 9, 1)  # released a month later, before today


def weekdays(first, last):
    days = np.arange(first, last + 1)
    return days[(days + 3) % 7 < 5]


def month_starts(first, last):
    month = np.timedelta64(1, "M")
    months = np.arange(np.datetime64(first, "D").astype("datetime64[M]"), np.datetime64(last, "D").astype("datetime64[M]") + month)
    days = months.astype("datetime64[D]").astype(np.int64)
    return days[(days >= first) & (days <= last)]


def walk(size, seed, start=100.0):
    return start * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.01, size)))


def history(days, values, lag=0):
    days = np.asarray(days, dtype=np.int64)
    return Observations(days, np.asarray(values, dtype=float), days + lag, np.zeros(days.size, dtype=bool))


def monthly(first=FETCH_START, last=LAST_MONTH, seed=0, values=None):
    """First releases of a monthly series, each 30 days after its date."""
    days = month_starts(first, last)
    return history(days, walk(days.size, seed) if values is None else values, lag=30)


SPY_DAYS = weekdays(FETCH_START, TODAY - 1)
SPY_HISTORY = history(SPY_DAYS, walk(SPY_DAYS.size, 1, 450.0))
LEAD = CatalogEntry("Lead", "LEADID", "LOG", 30, "Lead", "領先")
OTHER = CatalogEntry("Other", "OTHERID", "LOG", 30, "Other", "其他")


@dataclasses.dataclass
class World:
    store: Store
    jobs: Jobs
    tiingo: FakeTiingo
    fred: FakeFred
    pool: InlinePool
    clock: Clock


def make_world(path, catalog, tiingo=None, fred=None):
    clock = Clock(NOW)
    store = Store.open(path, clock)
    store.sync_catalog(catalog)
    tiingo = FakeTiingo(SPY_HISTORY) if tiingo is None else tiingo
    fred = FakeFred() if fred is None else fred
    pool = InlinePool()
    return World(store, Jobs(store, catalog, pool, clock, sources_of(tiingo, fred)), tiingo, fred, pool, clock)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(jobs_module, "smooth_series", quick_smooth)
    monkeypatch.setattr(analysis, "step3", quick_step3)
    monkeypatch.setattr(analysis, "step5", quick_step5)


@pytest.fixture
def world(tmp_path):
    world = make_world(tmp_path / "spylead.db", [LEAD, OTHER])
    world.fred.add("LEADID", monthly(seed=2))
    world.fred.add("OTHERID", monthly(seed=3))
    yield world
    world.store.close()


def run(world, start, *arguments):
    async def job():
        getattr(world.jobs, start)(*arguments)
        await world.jobs.wait()
        return world.jobs.last

    return asyncio.run(job())


def kinds(store):
    return [event.kind for event in reversed(store.events(500))]


def statuses(store):
    return {entry["name"]: entry["status"] for entry in store.results().payload["series"]}


def members(store):
    return [member["name"] for member in store.results().payload["f"].get("members", [])]


def extended_spy(days_on):
    """SPY with new trading days up to `days_on` days after today, and other values for the days already stored."""
    new_days = weekdays(TODAY, TODAY + days_on - 1)
    days = np.concatenate([SPY_DAYS, new_days])
    return history(days, np.concatenate([SPY_HISTORY.value + 1.0, walk(new_days.size, 9, 470.0)])), new_days


# AC-9


def test_the_first_fetch_downloads_everything_from_fetch_start_and_computes(world):
    last = run(world, "fetch")
    assert (last.kind, last.outcome, last.finished_at) == ("FETCH", "UPDATED", "2026-10-28T15:00:00Z")
    assert world.store.start_days() == (FETCH_START, ANALYSIS_START)
    assert world.tiingo.calls == [("prices", FETCH_START, TODAY)]
    assert world.fred.calls == [
        ("series", "LEADID"),
        ("observations", "LEADID", FETCH_START),
        ("series", "OTHERID"),
        ("observations", "OTHERID", FETCH_START),
    ]
    assert world.store.latest_obs_day("SPY") == SPY_DAYS[-1]
    assert sorted(world.store.curves()) == ["Lead", "Other", "SPY"]
    assert statuses(world.store) == {"Lead": "KEPT", "Other": "KEPT"}
    assert members(world.store) == ["Lead", "Other"]
    assert world.store.results().payload["today"] == TODAY
    updates = ["SERIES_UPDATED"] * 3
    assert kinds(world.store) == ["FETCH_STARTED", *updates, "FETCH_FINISHED", "COMPUTE_FINISHED"]


def test_the_start_days_are_set_once(world):
    run(world, "fetch")
    world.clock.advance(days=400)
    run(world, "fetch")
    assert world.store.start_days() == (FETCH_START, ANALYSIS_START)


def test_fred_metadata_is_stored_on_the_series_row(world):
    run(world, "fetch")
    row = world.store.series("Lead")
    assert (row.title, row.units, row.frequency) == ("Title of LEADID", "Index", "M")
    assert row.last_updated == "2026-10-01 07:45:03-05"
    assert (row.last_fetch_at, row.last_fetch_error) == ("2026-10-28T15:00:00Z", None)


# AC-10


def test_a_fetch_with_everything_up_to_date_makes_metadata_requests_only_and_no_compute(world):
    run(world, "fetch")
    computed_at = world.store.results().computed_at
    for calls in (world.tiingo.calls, world.fred.calls, world.pool.calls):
        calls.clear()
    world.clock.advance(hours=2)
    last = run(world, "fetch")
    assert (last.outcome, last.message) == ("UP_TO_DATE", "Every series is up to date")
    assert world.tiingo.calls == [("end_day",)]
    assert world.fred.calls == [("series", "LEADID"), ("series", "OTHERID")]
    assert world.pool.calls == []
    assert world.store.results().computed_at == computed_at
    assert kinds(world.store)[-2:] == ["FETCH_STARTED", "UP_TO_DATE"]


# AC-11


def test_an_incremental_fetch_inserts_only_new_days_and_computes(world):
    run(world, "fetch")
    spy_before, lead_before = world.store.observations("SPY"), world.store.observations("Lead")
    computed_at = world.store.results().computed_at
    world.clock.advance(days=7)
    world.tiingo.closes, new_days = extended_spy(7)
    newer = monthly(last=day(2026, 10, 1), seed=2)
    world.fred.update("LEADID", dataclasses.replace(newer, value=newer.value + 1.0), "2026-11-02 07:45:03-06")
    world.tiingo.calls.clear()
    world.fred.calls.clear()
    last = run(world, "fetch")
    assert last.outcome == "UPDATED"
    assert world.tiingo.calls == [("end_day",), ("prices", int(SPY_DAYS[-1]) - 7, int(new_days[-1]))]
    assert world.fred.calls == [
        ("series", "LEADID"),
        ("observations", "LEADID", LAST_MONTH - 400),
        ("series", "OTHERID"),
    ]
    spy, lead = world.store.observations("SPY"), world.store.observations("Lead")
    assert spy.obs_day.tolist() == SPY_DAYS.tolist() + new_days.tolist()
    np.testing.assert_array_equal(spy.value[: SPY_DAYS.size], spy_before.value)
    assert lead.obs_day.tolist() == lead_before.obs_day.tolist() + [day(2026, 10, 1)]
    np.testing.assert_array_equal(lead.value[:-1], lead_before.value)
    assert world.store.series("Lead").last_updated == "2026-11-02 07:45:03-06"
    assert world.store.results().computed_at != computed_at
    updates = [event.message for event in world.store.events(4) if event.kind == "SERIES_UPDATED"]
    assert updates == ["1 new observations", f"{new_days.size} new observations"]


def test_estimated_release_days_come_from_the_typical_lag(world):
    # Observations from before the archive appear with its first vintage date as their release day (SPEC.md §5.2).
    older = monthly(last=day(2004, 12, 1), seed=2)
    archived = dataclasses.replace(older, release_day=np.full(older.obs_day.size, day(2005, 6, 1)))
    world.fred.add("LEADID", joined(archived, monthly(first=day(2005, 1, 1), seed=2)))
    run(world, "fetch")
    stored = world.store.observations("Lead")
    assert stored.estimated.tolist() == [True] * older.obs_day.size + [False] * (stored.obs_day.size - older.obs_day.size)
    np.testing.assert_array_equal(stored.release_day[stored.estimated], older.obs_day + 30)


# AC-12


def test_a_failing_series_keeps_its_data_and_records_its_error_while_the_others_continue(world):
    run(world, "fetch")
    other_before = world.store.observations("Other")
    computed_at = world.store.results().computed_at
    world.clock.advance(days=7)
    world.fred.errors["OTHERID"] = SourceError("TIMEOUT", "FRED did not answer within 30 s")
    world.fred.update("LEADID", monthly(last=day(2026, 10, 1), seed=2), "2026-11-02 07:45:03-06")
    last = run(world, "fetch")
    assert (last.outcome, last.message) == ("UPDATED", "1 series fetched, 1 up to date, 1 failed")
    assert world.store.series("Other").last_fetch_error == "TIMEOUT: FRED did not answer within 30 s"
    np.testing.assert_array_equal(world.store.observations("Other").obs_day, other_before.obs_day)
    assert world.store.latest_obs_day("Lead") == day(2026, 10, 1)
    assert world.store.results().computed_at != computed_at
    assert statuses(world.store)["Other"] == "KEPT"
    failed = [event for event in world.store.events(20) if event.kind == "SERIES_FAILED"]
    assert [(event.series, event.level, event.message) for event in failed] == [
        ("Other", "WARNING", "TIMEOUT: FRED did not answer within 30 s")
    ]
    world.fred.errors.clear()
    world.clock.advance(days=1)
    run(world, "fetch")
    assert world.store.series("Other").last_fetch_error is None


def test_spy_without_data_ends_no_spy_data_without_a_compute(world):
    world.tiingo.error = SourceError("HTTP_500", "Tiingo answered HTTP 500")
    last = run(world, "fetch")
    assert (last.outcome, last.message) == ("NO_SPY_DATA", "SPY has no data: HTTP_500: Tiingo answered HTTP 500")
    assert world.store.results() is None
    assert world.pool.calls == []
    assert world.store.latest_obs_day("Lead") == LAST_MONTH
    assert kinds(world.store)[-2:] == ["FETCH_FINISHED", "JOB_FAILED"]
    assert world.store.events(1)[0].level == "WARNING"


@pytest.mark.parametrize("empty", ["fred", "tiingo"])
def test_an_empty_key_fails_every_series_of_its_source_with_missing_key(tmp_path, empty):
    # The real clients: with an empty key they fail before any request, so their session is never used.
    session = object()
    tiingo = Tiingo(session, "", asyncio.sleep) if empty == "tiingo" else FakeTiingo(SPY_HISTORY)
    fred = Fred(session, "", asyncio.sleep) if empty == "fred" else FakeFred()
    if empty == "tiingo":
        fred.add("LEADID", monthly(seed=2))
        fred.add("OTHERID", monthly(seed=3))
    world = make_world(tmp_path / "spylead.db", [LEAD, OTHER], tiingo, fred)
    last = run(world, "fetch")
    failing = ["SPY"] if empty == "tiingo" else ["Lead", "Other"]
    source = "Tiingo" if empty == "tiingo" else "FRED"
    for name in failing:
        assert world.store.series(name).last_fetch_error == f"MISSING_KEY: the {source} API key is empty in config.toml"
    if empty == "tiingo":
        assert last.outcome == "NO_SPY_DATA"
    else:
        assert last.outcome == "UPDATED"
        assert statuses(world.store) == {"Lead": "FETCH_FAILED", "Other": "FETCH_FAILED"}
    world.store.close()


# AC-13


def test_a_second_job_is_refused_while_one_runs(world):
    run(world, "fetch")
    world.fred.gate = gate = Gate()

    async def job():
        world.jobs.fetch()
        await gate.reached.wait()
        starts = [("fetch", ()), ("recompute", (3.0,)), ("refit", (["Lead"],)), ("add_series", ("NEWID",))]
        for start, arguments in [*starts, ("remove_series", ("Lead",))]:
            with pytest.raises(JobRunning):
                getattr(world.jobs, start)(*arguments)
        assert (world.jobs.progress.kind, world.jobs.progress.phase) == ("FETCH", "FETCH")
        gate.event.set()
        await world.jobs.wait()

    asyncio.run(job())
    assert world.jobs.last.outcome == "UP_TO_DATE"
    assert world.store.inflections_per_year() == 2
    assert world.store.excluded() == []


def test_progress_runs_through_every_phase(world):
    seen = []
    step = world.jobs.step

    def recording(series):
        step(series)
        progress = world.jobs.progress
        seen.append((progress.kind, progress.phase, progress.done, progress.total, progress.series))

    world.jobs.step = recording
    run(world, "fetch")
    assert seen == [
        ("FETCH", "FETCH", 1, 3, "SPY"),
        ("FETCH", "FETCH", 2, 3, "Lead"),
        ("FETCH", "FETCH", 3, 3, "Other"),
        ("FETCH", "SMOOTH", 1, 3, "SPY"),
        ("FETCH", "SMOOTH", 2, 3, "Lead"),
        ("FETCH", "SMOOTH", 3, 3, "Other"),
        ("FETCH", "STEP3", 1, 2, "Lead"),
        ("FETCH", "STEP3", 2, 2, "Other"),
        ("FETCH", "STEP5", 1, 1, "F"),
        ("FETCH", "SAVE", 1, 1, None),
    ]
    assert list(world.jobs.timings) == ["FETCH", "SMOOTH", "STEP3", "STEP5", "SAVE"]
    assert world.jobs.progress is None


def test_progress_starts_with_the_kind_and_start_time(world):
    world.fred.gate = gate = Gate()

    async def job():
        world.jobs.fetch()
        await gate.reached.wait()
        progress = dataclasses.asdict(world.jobs.progress)
        gate.event.set()
        await world.jobs.wait()
        return progress

    progress = asyncio.run(job())
    assert progress == {"kind": "FETCH", "started_at": "2026-10-28T15:00:00Z", "phase": "FETCH", "done": 1, "total": 3, "series": "SPY"}


def test_a_shutdown_during_a_fetch_keeps_the_inserted_observations_and_the_previous_results(world):
    run(world, "fetch")
    before = world.store.results()
    world.clock.advance(days=7)
    world.tiingo.closes, new_days = extended_spy(7)
    world.fred.update("LEADID", monthly(last=day(2026, 10, 1), seed=2), "2026-11-02 07:45:03-06")
    world.fred.gate = gate = Gate()

    async def job():
        world.jobs.fetch()
        await gate.reached.wait()
        await world.jobs.shutdown()

    asyncio.run(job())
    assert (world.jobs.last.outcome, world.jobs.progress) == ("INTERRUPTED", None)
    assert world.store.results() == before
    assert world.store.latest_obs_day("SPY") == new_days[-1]
    assert kinds(world.store)[-1] == "JOB_INTERRUPTED"
    assert world.store.events(1)[0].message == "FETCH INTERRUPTED: A shutdown interrupted the job"


def test_a_shutdown_during_a_compute_keeps_the_previous_results(world):
    run(world, "fetch")
    before = world.store.results()
    world.clock.advance(minutes=5)
    world.pool.hold = quick_step3

    async def job():
        world.jobs.recompute(2.5)
        while world.jobs.progress.phase != "STEP3":
            await asyncio.sleep(0)
        await world.jobs.shutdown()

    asyncio.run(job())
    assert world.jobs.last.outcome == "INTERRUPTED"
    assert world.store.results() == before
    assert kinds(world.store)[-2:] == ["SETTING_CHANGED", "JOB_INTERRUPTED"]


def test_refit_f_reruns_step_5_alone_from_the_stored_curves(world):
    run(world, "fetch")
    curves, before = world.store.curves(), world.store.results()
    world.pool.calls.clear()
    world.clock.advance(minutes=5)
    last = run(world, "refit", ["Other"])
    assert (last.kind, last.outcome) == ("REFIT_F", "COMPUTED")
    assert world.pool.calls == [quick_step5]
    after = world.store.results()
    assert after.computed_at == "2026-10-28T15:05:00Z"
    assert after.payload["series"] == before.payload["series"]
    assert members(world.store) == ["Lead"]
    assert world.store.excluded() == ["Other"]
    for name, curve in world.store.curves().items():
        np.testing.assert_array_equal(curve.rt, curves[name].rt)
    assert kinds(world.store)[-2:] == ["SELECTION_CHANGED", "COMPUTE_FINISHED"]


def test_recompute_stores_the_new_n_and_computes_everything(world):
    run(world, "fetch")
    world.pool.calls.clear()
    last = run(world, "recompute", 2.5)
    assert (last.kind, last.outcome) == ("RECOMPUTE", "COMPUTED")
    assert world.store.inflections_per_year() == 2.5
    assert world.store.results().payload["inflections_per_year"] == 2.5
    assert world.pool.calls == [quick_smooth] * 3 + [quick_step3] * 2 + [quick_step5]
    setting = [event for event in world.store.events(10) if event.kind == "SETTING_CHANGED"]
    assert [event.message for event in setting] == ["inflections_per_year: 2 → 2.5"]


def test_an_unexpected_error_ends_the_job_failed_with_a_redacted_message(monkeypatch, caplog, world):
    def broken(spy, indicator):
        raise RuntimeError("GET https://api.stlouisfed.org/fred/series?series_id=X&api_key=0123456789abcdef")

    monkeypatch.setattr(analysis, "step3", broken)
    with caplog.at_level(logging.INFO, logger="spylead"):
        last = run(world, "fetch")
    assert last.outcome == "FAILED"
    assert last.message == "RuntimeError: GET https://api.stlouisfed.org/fred/series?series_id=X&api_key=***"
    event = world.store.events(1)[0]
    assert (event.kind, event.level) == ("JOB_FAILED", "ERROR")
    assert all("0123456789abcdef" not in record.getMessage() for record in caplog.records)
    assert any("Traceback" in record.getMessage() for record in caplog.records)
    assert world.store.results() is None


# AC-14, job part


def constant(value):
    return monthly(values=np.full(month_starts(FETCH_START, LAST_MONTH).size, float(value)))


def with_zero(observations):
    return dataclasses.replace(observations, value=np.r_[0.0, observations.value[1:]])


SCENARIO = [
    (CatalogEntry("Kept", "KEPTID", "LOG", 30, "", ""), monthly(seed=4), "KEPT"),
    # The stand-in for step 3 gives a constant series A* = its value.
    (CatalogEntry("Lagging", "LAGID", "LEVEL", 30, "", ""), constant(30), "NOT_LEADING"),
    (CatalogEntry("Edge", "EDGEID", "LEVEL", 30, "", ""), constant(730), "AT_BOUNDARY"),
    (CatalogEntry("Short", "SHORTID", "LOG", 30, "", ""), monthly(first=day(2023, 10, 1), seed=5), "INSUFFICIENT_HISTORY"),
    (CatalogEntry("Stale", "STALEID", "LOG", 30, "", ""), monthly(last=day(2023, 12, 1), seed=6), "DISCONTINUED"),
    (CatalogEntry("Zero", "ZEROID", "LOG", 30, "", ""), with_zero(monthly(seed=7)), "INVALID_VALUES"),
    (CatalogEntry("Empty", "EMPTYID", "LOG", 30, "", ""), NONE, "PENDING"),
    (CatalogEntry("Broken", "BROKENID", "LOG", 30, "", ""), monthly(seed=8), "FETCH_FAILED"),
]


def test_every_status_arises_from_the_data_and_the_fetch(tmp_path):
    world = make_world(tmp_path / "spylead.db", [entry for entry, _, _ in SCENARIO])
    for entry, observations, _ in SCENARIO:
        world.fred.add(entry.fred_id, observations)
    world.fred.errors["BROKENID"] = SourceError("HTTP_500", "FRED answered HTTP 500")
    run(world, "fetch")
    assert statuses(world.store) == {entry.name: status for entry, _, status in SCENARIO}
    # The stale series would be kept, but it is never in F.
    assert members(world.store) == ["Kept"]
    assert [event.series for event in world.store.events(50) if event.kind == "INVALID_VALUES"] == ["Zero"]
    assert "Zero" not in world.store.curves()
    world.store.close()


def test_f_has_no_members_when_nothing_is_kept_or_every_member_is_unchecked(world):
    run(world, "fetch")
    run(world, "refit", ["Lead", "Other"])
    assert world.store.results().payload["f"] == {"status": "NO_MEMBERS"}


def test_the_payload_keeps_f_s_status_as_step_5_gives_it(monkeypatch, world):
    # Every one-sided series runs to today, so W5 holds every member's W3: plans/prompt-07.md.
    monkeypatch.setattr(analysis, "step5", lambda spy, members: Step5(analysis.INSUFFICIENT_OVERLAP))
    run(world, "fetch")
    assert world.store.results().payload["f"] == {"status": "INSUFFICIENT_OVERLAP"}


def test_f_holds_its_report_and_its_curves(world):
    run(world, "fetch")
    f = world.store.results().payload["f"]
    assert f["status"] == "OK"
    assert f["members"] == [{"name": "Lead", "b": -90, "beta": 0.5}, {"name": "Other", "b": -90, "beta": 0.5}]
    assert (f["c"], f["score"], f["score_oos"], f["slope_pct_per_year"]) == (0.1, 0.04, 0.06, 0.1)
    length = f["window"]["end"] - f["window"]["start"] + 1
    assert f["split_day"] == f["window"]["start"] + 7 * length // 10
    assert (len(f["f"]), len(f["composite"])) == (length, length)


def test_a_status_change_is_recorded(world):
    run(world, "fetch")
    world.clock.advance(days=400)
    run(world, "recompute", 2.0)
    changes = [(event.series, event.message) for event in world.store.events(20) if event.kind == "STATUS_CHANGED"]
    assert sorted(changes) == [("Lead", "KEPT → DISCONTINUED"), ("Other", "KEPT → DISCONTINUED")]


def test_a_refit_at_the_lambda_limit_is_recorded_once_per_series(monkeypatch, world):
    def limited(release_days, y, n_per_year, today):
        one, reference = quick_smooth(release_days, y, n_per_year, today)
        anchors = Anchors(*(np.zeros(3) for _ in range(5)), np.array([True, False, True]))
        return dataclasses.replace(one, anchors=anchors), dataclasses.replace(reference, at_limit=True)

    monkeypatch.setattr(jobs_module, "smooth_series", limited)
    run(world, "fetch")
    limits = [(event.series, event.message) for event in world.store.events(50) if event.kind == "LAMBDA_AT_LIMIT"]
    message = "λ reached its limit, 10^8, in 2 of 3 refits, and the reference fit"
    assert sorted(limits) == [("Lead", message), ("Other", message), ("SPY", message)]


def test_points_are_counted_after_the_known_value_rule(world):
    late = monthly(seed=2)
    # The second newest observation is first released after the newest: it is no point (SPEC.md §5.3).
    release_day = late.release_day.copy()
    release_day[-2] = release_day[-1] + 5
    world.fred.add("LEADID", dataclasses.replace(late, release_day=release_day))
    run(world, "fetch")
    entry = next(entry for entry in world.store.results().payload["series"] if entry["name"] == "Lead")
    assert (entry["points"], entry["estimated_points"]) == (late.obs_day.size - 1, 0)
    assert world.store.counts()["Lead"].observations == late.obs_day.size


# AC-15


@pytest.mark.parametrize(
    ("units", "values", "form"),
    [("Percent", None, "LEVEL"), ("Index", None, "LOG"), ("Index", "negative", "LEVEL")],
    ids=["percent", "positive", "a-negative-value"],
)
def test_an_added_series_is_stored_fetched_and_computed_with_the_form_of_section_4_4(world, units, values, form):
    run(world, "fetch")
    observations = monthly(seed=9)
    if values == "negative":
        observations = dataclasses.replace(observations, value=np.r_[-1.0, observations.value[1:]])
    world.fred.add("NEWID", observations, units=units)
    world.fred.calls.clear()
    last = run(world, "add_series", "NEWID")
    assert (last.kind, last.outcome, last.message) == ("ADD_SERIES", "COMPUTED", "NEWID was added")
    row = world.store.series("NEWID")
    assert (row.origin, row.source_id, row.default_lag_days, row.form, row.units) == ("USER", "NEWID", 30, form, units)
    assert world.fred.calls == [("series", "NEWID"), ("observations", "NEWID", FETCH_START)]
    assert list(statuses(world.store)) == ["Lead", "Other", "NEWID"]
    assert kinds(world.store)[-3:] == ["SERIES_ADDED", "SERIES_UPDATED", "COMPUTE_FINISHED"]


@pytest.mark.parametrize(
    ("fred_id", "outcome", "message"),
    [
        ("ANNUALID", "UNSUPPORTED_FREQUENCY", "ANNUALID is annual, too few points for one-sided fits"),
        ("UNKNOWNID", "UNKNOWN_SERIES", "FRED has no series UNKNOWNID"),
    ],
)
def test_an_annual_or_unknown_series_is_refused(world, fred_id, outcome, message):
    run(world, "fetch")
    world.fred.add("ANNUALID", monthly(seed=9), frequency="A")
    computed_at = world.store.results().computed_at
    last = run(world, "add_series", fred_id)
    assert (last.outcome, last.message) == (outcome, message)
    assert world.store.series(fred_id) is None
    assert world.store.results().computed_at == computed_at
    assert kinds(world.store)[-1] == "JOB_FAILED"


def test_adding_without_the_fred_key_ends_missing_key(tmp_path):
    world = make_world(tmp_path / "spylead.db", [LEAD], fred=Fred(object(), "", asyncio.sleep))
    last = run(world, "add_series", "NEWID")
    assert (last.outcome, last.message) == ("MISSING_KEY", "the FRED API key is empty in config.toml")
    assert world.store.series("NEWID") is None
    world.store.close()


def test_a_series_added_before_the_first_fetch_waits_for_it(world):
    world.fred.add("NEWID", monthly(seed=9))
    last = run(world, "add_series", "NEWID")
    assert (last.outcome, last.message) == ("NO_SPY_DATA", "NEWID was added; the first Fetch Data downloads it")
    assert world.store.series("NEWID").origin == "USER"
    assert world.store.latest_obs_day("NEWID") is None
    run(world, "fetch")
    assert ("observations", "NEWID", FETCH_START) in world.fred.calls
    assert statuses(world.store)["NEWID"] == "KEPT"


def test_removing_a_user_series_deletes_it_and_refits_f(world):
    run(world, "fetch")
    world.fred.add("NEWID", monthly(seed=9))
    run(world, "add_series", "NEWID")
    assert members(world.store) == ["Lead", "Other", "NEWID"]
    world.pool.calls.clear()
    last = run(world, "remove_series", "NEWID")
    assert (last.kind, last.outcome) == ("REMOVE_SERIES", "COMPUTED")
    assert last.message == "NEWID was removed; F was refitted: OK"
    assert world.store.series("NEWID") is None
    assert "NEWID" not in world.store.curves()
    assert list(statuses(world.store)) == ["Lead", "Other"]
    assert members(world.store) == ["Lead", "Other"]
    assert world.pool.calls == [quick_step5]
    assert kinds(world.store)[-2:] == ["SERIES_REMOVED", "COMPUTE_FINISHED"]


def test_a_catalog_series_is_not_removed(world):
    run(world, "fetch")
    run(world, "remove_series", "Lead")
    assert world.store.series("Lead").origin == "CATALOG"
    assert members(world.store) == ["Lead", "Other"]


def test_a_series_that_left_the_catalog_leaves_the_next_compute(world, tmp_path):
    run(world, "fetch")
    world.store.sync_catalog([LEAD])
    jobs = Jobs(world.store, [LEAD], world.pool, world.clock, sources_of(world.tiingo, world.fred))
    world.jobs = jobs
    run(world, "recompute", 2.0)
    assert list(statuses(world.store)) == ["Lead"]
    assert sorted(world.store.curves()) == ["Lead", "SPY"]


# AC-16


def test_an_unchecked_member_leaves_f_and_stays_out_after_a_restart(world, tmp_path):
    run(world, "fetch")
    run(world, "refit", ["Other"])
    assert members(world.store) == ["Lead"]
    world.store.close()
    world.store = Store.open(tmp_path / "spylead.db", world.clock)
    world.jobs = Jobs(world.store, [LEAD, OTHER], InlinePool(), world.clock, sources_of(world.tiingo, world.fred))
    assert world.store.excluded() == ["Other"]
    run(world, "recompute", 2.0)
    assert members(world.store) == ["Lead"]
    assert statuses(world.store)["Other"] == "KEPT"


def test_a_newly_kept_series_joins_f(world):
    # The stand-in for step 3 lets no constant series lead; once its values move, it leads.
    world.fred.add("OTHERID", constant(30))
    run(world, "fetch")
    assert statuses(world.store)["Other"] == "NOT_LEADING"
    assert members(world.store) == ["Lead"]
    world.clock.advance(days=7)
    world.fred.update("OTHERID", monthly(last=day(2026, 10, 1), seed=3), "2026-11-02 07:45:03-06")
    run(world, "fetch")
    assert statuses(world.store)["Other"] == "KEPT"
    assert members(world.store) == ["Lead", "Other"]


def test_the_typical_lag_of_a_later_fetch_counts_the_stored_observations(tmp_path):
    # The default lag is 5 days and the observed one 30: §5.2's threshold is then 90 days, not 65.
    claims = CatalogEntry("Claims", "CLAIMSID", "LOG", 5, "", "")
    world = make_world(tmp_path / "spylead.db", [claims])
    world.fred.add("CLAIMSID", monthly(seed=2))
    run(world, "fetch")
    world.clock.advance(days=60)
    newer = monthly(last=day(2026, 10, 1), seed=2)
    release_day = newer.release_day.copy()
    release_day[-1] = day(2026, 10, 1) + 80
    world.fred.update("CLAIMSID", dataclasses.replace(newer, release_day=release_day), "2026-12-21 07:45:03-06")
    run(world, "fetch")
    stored = world.store.observations("Claims")
    assert (stored.release_day[-1], stored.estimated[-1]) == (day(2026, 10, 1) + 80, False)
    world.store.close()


def test_a_job_s_sources_share_one_client_session():
    config = Config(port=8090, token="t" * 16, fred_api_key="fred-key", tiingo_api_key="tiingo-key")

    async def opened():
        async with jobs_module.open_sources(config, asyncio.sleep) as (tiingo, fred):
            assert tiingo.session is fred.session
            assert (tiingo.key, fred.key) == ("tiingo-key", "fred-key")
            assert not tiingo.session.closed
            session = tiingo.session
        return session

    assert asyncio.run(opened()).closed
