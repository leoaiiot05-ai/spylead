"""Jobs (SPEC.md §8, §9): fetching, computing, and adding or removing series, one job at a time, with progress.

The jobs compose the sources, the store and the pure modules. The numerical steps run in the injected pool.
"""
import asyncio
import contextlib
import dataclasses
import datetime
import functools
import logging
import multiprocessing
import os
import signal
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import aiohttp
import numpy as np

from spylead import analysis
from spylead.analysis import Daily, Member, Step3, Step5
from spylead.catalog import SPY_NAME, CatalogEntry
from spylead.config import Config
from spylead.pit import (
    Observations,
    estimate_release_days,
    has_invalid_values,
    joined,
    known_points,
    today_at,
    typical_lag,
    user_form,
    utc_text,
    y_values,
    years_before,
)
from spylead.smoothing import OneSided, Reference, one_sided, reference_fit
from spylead.sources import Fred, FredSeries, SourceError, Tiingo, redact
from spylead.store import ORIGIN_USER, Curve, Results, SeriesRow, Store, StoreError

log = logging.getLogger(__name__)

ANALYSIS_YEARS = 30
WARM_UP_YEARS = 3
SPY_OVERLAP_DAYS = 7
FRED_OVERLAP_DAYS = 400
ANNUAL = "A"
UNKNOWN_SERIES_CODES = ("HTTP_400", "HTTP_404")  # FRED answers an unknown series_id with 400
WORKERS = max(1, (os.cpu_count() or 1) - 1)  # SPEC.md §9

FETCH, RECOMPUTE, REFIT_F, ADD_SERIES, REMOVE_SERIES = "FETCH", "RECOMPUTE", "REFIT_F", "ADD_SERIES", "REMOVE_SERIES"
KINDS = (FETCH, RECOMPUTE, REFIT_F, ADD_SERIES, REMOVE_SERIES)
SMOOTH, STEP3, STEP5, SAVE = "SMOOTH", "STEP3", "STEP5", "SAVE"
PHASES = (FETCH, SMOOTH, STEP3, STEP5, SAVE)
UP_TO_DATE, UPDATED, COMPUTED, FAILED, INTERRUPTED = "UP_TO_DATE", "UPDATED", "COMPUTED", "FAILED", "INTERRUPTED"
MISSING_KEY, NO_SPY_DATA = "MISSING_KEY", "NO_SPY_DATA"
UNKNOWN_SERIES, UNSUPPORTED_FREQUENCY = "UNKNOWN_SERIES", "UNSUPPORTED_FREQUENCY"
OUTCOMES = (
    *(UP_TO_DATE, UPDATED, COMPUTED, FAILED, INTERRUPTED),
    *(MISSING_KEY, NO_SPY_DATA, UNKNOWN_SERIES, UNSUPPORTED_FREQUENCY),
)

Sources = Callable[[], contextlib.AbstractAsyncContextManager[tuple[Tiingo, Fred]]]


class JobRunning(Exception):
    """Another job runs (SPEC.md §9): 409 JOB_RUNNING."""


class JobEnd(Exception):
    """Ends a job early with one of the outcomes of SPEC.md §11."""

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


@dataclass
class Progress:
    """The running job, as the state document shows it (SPEC.md §9, §11)."""

    kind: str
    started_at: str
    phase: str | None = None
    done: int = 0
    total: int = 0
    series: str | None = None


@dataclass(frozen=True)
class LastJob:
    kind: str
    outcome: str
    finished_at: str
    message: str


@contextlib.asynccontextmanager
async def open_sources(config: Config, sleep: Callable[[float], Awaitable[None]]) -> AsyncIterator[tuple[Tiingo, Fred]]:
    """The job's one ClientSession (SPEC.md §8.4), with both clients on it."""
    async with aiohttp.ClientSession() as session:
        yield Tiingo(session, config.tiingo_api_key, sleep), Fred(session, config.fred_api_key, sleep)


def worker_pool() -> ProcessPoolExecutor:
    """The process pool of SPEC.md §9, whose workers leave Ctrl+C to the main process.

    A console sends Ctrl+C to every process attached to it. Workers that took it would die at once, before the
    shutdown of §15 interrupts the job and then shuts the pool down. Workers start by spawning on every system, as
    Windows must: forking a process that runs threads can deadlock.
    """
    return ProcessPoolExecutor(WORKERS, mp_context=multiprocessing.get_context("spawn"), initializer=ignore_interrupts)


def ignore_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def smooth_series(release_days: np.ndarray, y: np.ndarray, n_per_year: float, today: int) -> tuple[OneSided, Reference]:
    """A series' one-sided series from its first release day to today, and its reference fit: one pool task."""
    first_day = int(release_days[0])
    return (
        one_sided(release_days, y, n_per_year, today=today, first_day=first_day, last_day=today),
        reference_fit(release_days, y, n_per_year),
    )


class Jobs:
    """Runs one job at a time as an asyncio task (SPEC.md §9)."""

    def __init__(
        self,
        store: Store,
        catalog: Sequence[CatalogEntry],
        pool: Executor,
        clock: Callable[[], datetime.datetime],
        sources: Sources,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.pool = pool
        self.clock = clock
        self.sources = sources
        self.progress: Progress | None = None
        self.last: LastJob | None = None
        self.timings: dict[str, float] = {}  # seconds per phase of the latest job
        self.task: asyncio.Task | None = None
        self.phase_started = 0.0

    # Starting and stopping

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def fetch(self) -> None:
        self.start(FETCH, self.fetch_job)

    def recompute(self, inflections_per_year: float) -> None:
        """Stores N and recomputes everything (SPEC.md §6.3)."""
        self.refuse_while_running()
        before = self.store.inflections_per_year()
        self.store.set_inflections_per_year(inflections_per_year)
        self.store.append_event("INFO", "SETTING_CHANGED", f"inflections_per_year: {before:g} → {inflections_per_year:g}")
        self.start(RECOMPUTE, self.recompute_job)

    def refit(self, excluded: Sequence[str]) -> None:
        """Stores the names unchecked from F and refits it (SPEC.md §7.4)."""
        self.refuse_while_running()
        self.store.set_excluded(excluded)
        names = ", ".join(sorted(set(excluded))) or "none"
        self.store.append_event("INFO", "SELECTION_CHANGED", f"Unchecked from F: {names}")
        self.start(REFIT_F, self.refit_job)

    def add_series(self, fred_id: str) -> None:
        self.start(ADD_SERIES, functools.partial(self.add_job, fred_id))

    def remove_series(self, name: str) -> None:
        self.start(REMOVE_SERIES, functools.partial(self.remove_job, name))

    def refuse_while_running(self) -> None:
        if self.running:
            raise JobRunning

    def start(self, kind: str, work: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        self.refuse_while_running()
        self.progress = Progress(kind, self.now())
        self.timings = {}
        self.task = asyncio.get_running_loop().create_task(self.run(kind, work))

    async def wait(self) -> None:
        """Returns once no job runs."""
        if self.task is not None:
            await asyncio.wait([self.task])

    async def shutdown(self) -> None:
        """Interrupts the running job (SPEC.md §9). Its observations stay; results it has not saved never are."""
        if self.running:
            self.task.cancel()
        await self.wait()

    async def run(self, kind: str, work: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        log.info("%s started", kind)
        try:
            outcome, message = await work()
        except asyncio.CancelledError:
            self.finish(kind, INTERRUPTED, "A shutdown interrupted the job", "JOB_INTERRUPTED")
            raise
        except JobEnd as end:
            self.finish(kind, end.outcome, end.message, "JOB_FAILED")
        except Exception as error:
            # The traceback goes to the log file; any FRED URL in it must lose its key first.
            log.error("%s failed:\n%s", kind, redact("".join(traceback.format_exception(error))))
            self.finish(kind, FAILED, redact(f"{type(error).__name__}: {error}"), "JOB_FAILED")
        else:
            self.finish(kind, outcome, message)

    def finish(self, kind: str, outcome: str, message: str, event: str | None = None) -> None:
        self.end_phase()
        self.progress = None
        self.last = LastJob(kind, outcome, self.now(), message)
        log.info("%s ended %s: %s", kind, outcome, message)
        if event is None:
            return
        level = "ERROR" if outcome == FAILED else "WARNING"
        try:
            self.store.append_event(level, event, f"{kind} {outcome}: {message}")
        except StoreError as error:
            log.error("The end of %s could not be recorded: %s", kind, error)

    # Progress

    def enter(self, phase: str, total: int) -> None:
        self.end_phase()
        self.progress.phase, self.progress.done, self.progress.total, self.progress.series = phase, 0, total, None
        self.phase_started = time.perf_counter()

    def step(self, series: str | None) -> None:
        self.progress.done += 1
        self.progress.series = series

    def end_phase(self) -> None:
        if self.progress is not None and self.progress.phase is not None:
            seconds = time.perf_counter() - self.phase_started
            self.timings[self.progress.phase] = seconds
            log.info("%s took %.1f s", self.progress.phase, seconds)

    async def map(self, phase: str, function: Callable[..., Any], tasks: dict[str, tuple]) -> dict[str, Any]:
        """function(*arguments) in the pool for each named task, counting progress. The results come back by name."""
        self.enter(phase, len(tasks))
        loop = asyncio.get_running_loop()

        async def one(name: str, arguments: tuple) -> tuple[str, Any]:
            result = await loop.run_in_executor(self.pool, function, *arguments)
            self.step(name)
            return name, result

        return dict(await asyncio.gather(*(one(name, arguments) for name, arguments in tasks.items())))

    # Jobs

    async def fetch_job(self) -> tuple[str, str]:
        """Fetch Data (SPEC.md §8)."""
        today = self.today()
        fetch_start = self.fetch_start(today)
        self.store.append_event("INFO", "FETCH_STARTED", "Fetch started")
        _, candidates = self.rows()
        self.enter(FETCH, 1 + len(candidates))
        results: dict[str, int | SourceError | None] = {}
        async with self.sources() as (tiingo, fred):
            results[SPY_NAME] = await self.fetch_one(SPY_NAME, self.fetch_spy(tiingo, fetch_start, today))
            for row in candidates:
                results[row.name] = await self.fetch_one(row.name, self.fetch_fred(fred, row, fetch_start))
        self.record_counts(results)
        if all(result is None for result in results.values()):
            self.store.append_event("INFO", "UP_TO_DATE", "Every series is up to date")
            return UP_TO_DATE, "Every series is up to date"
        summary = fetch_summary(results)
        self.store.append_event("INFO", "FETCH_FINISHED", summary)
        if self.store.latest_obs_day(SPY_NAME) is None:
            reason = f": {results[SPY_NAME]}" if isinstance(results[SPY_NAME], SourceError) else ""
            raise JobEnd(NO_SPY_DATA, f"SPY has no data{reason}")
        await self.compute(today)
        return UPDATED, summary

    async def recompute_job(self) -> tuple[str, str]:
        await self.compute(self.today())
        return COMPUTED, "Everything was recomputed"

    async def refit_job(self) -> tuple[str, str]:
        """Step 5 alone, from the stored curves and the stored statuses and A* (SPEC.md §9)."""
        results, curves = self.store.results(), self.store.curves()
        if results is None or SPY_NAME not in curves:
            raise JobEnd(NO_SPY_DATA, "Nothing has been computed yet")
        _, candidates = self.rows()
        current = {row.name for row in candidates}
        entries = [entry for entry in results.payload["series"] if entry["name"] in current]
        excluded = self.store.excluded()
        members = [
            Member(entry["name"], daily_of(curves[entry["name"]]), entry["step3"]["a_star"])
            for entry in entries
            if analysis.is_member(entry["status"], entry["name"], excluded) and entry["name"] in curves
        ]
        f = await self.fit_f(daily_of(curves[SPY_NAME]), members)
        self.enter(SAVE, 1)
        self.store.save_results({**results.payload, "series": entries, "f": f_json(f)})
        self.step(None)
        self.store.append_event("INFO", "COMPUTE_FINISHED", f"F was refitted: {f.status}")
        return COMPUTED, f"F was refitted: {f.status}"

    async def add_job(self, fred_id: str) -> tuple[str, str]:
        """ADD_SERIES (SPEC.md §4.4, §9): FRED's metadata decides, then the series is stored, fetched and computed."""
        today = self.today()
        async with self.sources() as (_, fred):
            metadata = await self.fred_metadata(fred, fred_id)
            if metadata.frequency == ANNUAL:
                raise JobEnd(UNSUPPORTED_FREQUENCY, f"{fred_id} is annual, too few points for one-sided fits")
            form = user_form(metadata.units, np.empty(0))
            self.store.add_user_series(fred_id, form, metadata.title, metadata.units, metadata.frequency)
            self.store.append_event("INFO", "SERIES_ADDED", f"{fred_id} was added", series=fred_id)
            start = self.store.start_days()
            if start is None:
                raise JobEnd(NO_SPY_DATA, f"{fred_id} was added; the first Fetch Data downloads it")
            self.enter(FETCH, 1)
            row = self.store.series(fred_id)
            result = await self.fetch_one(fred_id, self.download(fred, row, metadata, start[0]))
        self.record_counts({fred_id: result})
        await self.compute(today)
        return COMPUTED, f"{fred_id} was added"

    async def remove_job(self, name: str) -> tuple[str, str]:
        """REMOVE_SERIES (SPEC.md §9): the USER series goes with its data, then F is refitted."""
        if self.store.remove_user_series(name):
            self.store.append_event("INFO", "SERIES_REMOVED", f"{name} was removed", series=name)
        _, message = await self.refit_job()
        return COMPUTED, f"{name} was removed; {message}"

    # Fetching

    def fetch_start(self, today: int) -> int:
        """fetch_start, set at the first fetch with analysis_start, then kept (SPEC.md §8.1)."""
        analysis_start = years_before(today, ANALYSIS_YEARS)
        return self.store.set_start_days(years_before(analysis_start, WARM_UP_YEARS), analysis_start)[0]

    async def fetch_one(self, name: str, fetch: Awaitable[int | None]) -> int | SourceError | None:
        """One series' fetch: the count of new observations, None when up to date, or its error (SPEC.md §8.3)."""
        try:
            result = await fetch
        except SourceError as error:
            self.store.record_fetch(name, str(error))
            self.store.append_event("WARNING", "SERIES_FAILED", str(error), series=name)
            result = error
        else:
            self.store.record_fetch(name, None)
        self.step(name)
        return result

    async def fetch_spy(self, tiingo: Tiingo, fetch_start: int, today: int) -> int | None:
        """SPY's new closes (SPEC.md §8.1, §8.2): their count, or None when SPY is up to date."""
        latest = self.store.latest_obs_day(SPY_NAME)
        if latest is None:
            return self.store.insert_observations(SPY_NAME, await tiingo.prices(fetch_start, today))
        end_day = await tiingo.end_day()
        if end_day <= latest:
            return None
        return self.store.insert_observations(SPY_NAME, await tiingo.prices(latest - SPY_OVERLAP_DAYS, end_day))

    async def fetch_fred(self, fred: Fred, row: SeriesRow, fetch_start: int) -> int | None:
        return await self.download(fred, row, await fred.series(row.source_id), fetch_start)

    async def download(self, fred: Fred, row: SeriesRow, metadata: FredSeries, fetch_start: int) -> int | None:
        """A FRED series' new first releases (SPEC.md §5.2, §8.2): their count, or None when it is up to date."""
        self.store.set_metadata(row.name, metadata.title, metadata.units, metadata.frequency)
        latest = self.store.latest_obs_day(row.name)
        if latest is not None and metadata.last_updated == row.last_updated:
            return None
        fetched = await fred.observations(row.source_id, fetch_start if latest is None else latest - FRED_OVERLAP_DAYS)
        stored = self.store.observations(row.name)
        new = fetched.take(~np.isin(fetched.obs_day, stored.obs_day))
        every = joined(stored, new)
        # §5.2's 24 months reach back beyond the overlap of a later fetch.
        lag = typical_lag(every, row.default_lag_days)
        inserted = self.store.insert_observations(
            row.name, estimate_release_days(new, lag), last_updated=metadata.last_updated
        )
        if row.origin == ORIGIN_USER:
            form = user_form(metadata.units, every.value)
            if form != row.form:
                self.store.set_form(row.name, form)
        return inserted

    async def fred_metadata(self, fred: Fred, fred_id: str) -> FredSeries:
        try:
            return await fred.series(fred_id)
        except SourceError as error:
            if error.code == MISSING_KEY:
                raise JobEnd(MISSING_KEY, error.text) from None
            if error.code in UNKNOWN_SERIES_CODES:
                raise JobEnd(UNKNOWN_SERIES, f"FRED has no series {fred_id}") from None
            raise JobEnd(FAILED, str(error)) from None

    def record_counts(self, results: dict[str, int | SourceError | None]) -> None:
        for name, result in results.items():
            if isinstance(result, int):
                self.store.append_event("INFO", "SERIES_UPDATED", f"{result} new observations", series=name)

    # Computing

    async def compute(self, today: int) -> None:
        """The compute of SPEC.md §9, saved in one transaction."""
        n_per_year = self.store.inflections_per_year()
        spy_row, candidates = self.rows()
        rows = [spy_row, *candidates]
        points = {row.name: known_points(self.store.observations(row.name)) for row in rows}
        invalid = {row.name: has_invalid_values(points[row.name].value, row.form) for row in rows}
        if points[SPY_NAME].obs_day.size == 0 or invalid[SPY_NAME]:
            raise JobEnd(NO_SPY_DATA, "SPY has no data")
        tasks = {
            row.name: (points[row.name].release_day, y_values(points[row.name].value, row.form), n_per_year, today)
            for row in rows
            if points[row.name].obs_day.size and not invalid[row.name]
        }
        smoothed = await self.map(SMOOTH, smooth_series, tasks)
        daily = {name: Daily(one.first_day, one.values) for name, (one, _) in smoothed.items()}
        spy = daily[SPY_NAME]
        step3s = await self.map(
            STEP3, analysis.step3, {row.name: (spy, daily[row.name]) for row in candidates if row.name in daily}
        )
        statuses = {
            row.name: analysis.status(
                points[row.name].release_day,
                fetch_failed=row.last_fetch_error is not None,
                invalid_values=invalid[row.name],
                step3=step3s.get(row.name),
                today=today,
            )
            for row in candidates
        }
        excluded = self.store.excluded()
        members = [
            Member(row.name, daily[row.name], step3s[row.name].a_star)
            for row in candidates
            if analysis.is_member(statuses[row.name], row.name, excluded)
        ]
        f = await self.fit_f(spy, members)
        self.enter(SAVE, 1)
        payload = payload_of(today, n_per_year, points, candidates, statuses, step3s, f)
        previous = self.store.results()
        curves = {name: Curve(one.first_day, one.values, ref.first_day, ref.values) for name, (one, ref) in smoothed.items()}
        self.store.save_compute(curves, payload)
        self.step(None)
        self.record_compute(previous, payload, smoothed, invalid)

    async def fit_f(self, spy: Daily, members: list[Member]) -> Step5:
        return (await self.map(STEP5, analysis.step5, {"F": (spy, members)}))["F"]

    def record_compute(
        self,
        previous: Results | None,
        payload: dict[str, Any],
        smoothed: dict[str, tuple[OneSided, Reference]],
        invalid: dict[str, bool],
    ) -> None:
        """The events of a saved compute (SPEC.md §10)."""
        before = {entry["name"]: entry["status"] for entry in previous.payload["series"]} if previous else {}
        for entry in payload["series"]:
            old = before.get(entry["name"])
            if old is not None and old != entry["status"]:
                self.store.append_event("INFO", "STATUS_CHANGED", f"{old} → {entry['status']}", series=entry["name"])
        for name, (one, reference) in smoothed.items():
            limited = int(one.anchors.at_limit.sum())
            lam = one.anchors.lam
            log.debug(
                "%s: %d refits, λ from %.3g to %.3g, %d at the limit; reference λ %.3g",
                *(name, lam.size, lam.min(initial=np.inf), lam.max(initial=0), limited, reference.lam),
            )
            if limited or reference.at_limit:
                reference_note = ", and the reference fit" if reference.at_limit else ""
                message = f"λ reached its limit, 10^8, in {limited} of {one.anchors.month_end.size} refits{reference_note}"
                self.store.append_event("WARNING", "LAMBDA_AT_LIMIT", message, series=name)
        for name, flag in invalid.items():
            if flag:
                message = "A value is zero or negative, which LOG form cannot take"
                self.store.append_event("WARNING", "INVALID_VALUES", message, series=name)
        self.store.append_event("INFO", "COMPUTE_FINISHED", f"{len(payload['series'])} candidates were computed")

    # Helpers

    def rows(self) -> tuple[SeriesRow, list[SeriesRow]]:
        """SPY's row, and the candidates."""
        rows = self.store.all_series()
        return next(row for row in rows if row.name == SPY_NAME), candidates(rows, self.catalog)

    def today(self) -> int:
        return today_at(self.clock())

    def now(self) -> str:
        return utc_text(self.clock())


def candidates(rows: Sequence[SeriesRow], catalog: Sequence[CatalogEntry]) -> list[SeriesRow]:
    """The candidates in catalog order, then USER series by name (SPEC.md §7.4, §11). rows come by name."""
    named = {row.name: row for row in rows}
    return [named[entry.name] for entry in catalog if entry.name in named] + [
        row for row in rows if row.origin == ORIGIN_USER
    ]


def daily_of(curve: Curve) -> Daily:
    return Daily(curve.rt_first_day, curve.rt)


def fetch_summary(results: dict[str, int | SourceError | None]) -> str:
    updated = sum(isinstance(result, int) for result in results.values())
    failed = sum(isinstance(result, SourceError) for result in results.values())
    current = sum(result is None for result in results.values())
    return f"{updated} series fetched, {current} up to date, {failed} failed"


def payload_of(
    today: int,
    n_per_year: float,
    points: dict[str, Observations],
    candidates: list[SeriesRow],
    statuses: dict[str, str],
    step3s: dict[str, Step3 | None],
    f: Step5,
) -> dict[str, Any]:
    """The stored results (SPEC.md §10): what the state document and the F chart need (§11). Days are integers."""
    return {
        "today": today,
        "inflections_per_year": n_per_year,
        "spy": point_counts(points[SPY_NAME]),
        "series": [
            {
                "name": row.name,
                "status": statuses[row.name],
                **point_counts(points[row.name]),
                "step3": None if step3s.get(row.name) is None else dataclasses.asdict(step3s[row.name]),
            }
            for row in candidates
        ],
        "f": f_json(f),
    }


def point_counts(points: Observations) -> dict[str, int]:
    return {"points": int(points.obs_day.size), "estimated_points": int(points.estimated.sum())}


def f_json(f: Step5) -> dict[str, Any]:
    """F's status, and its report and daily curves when it is OK (SPEC.md §7.4, §11)."""
    fit = f.combination
    if fit is None:
        return {"status": f.status}
    return {
        "status": f.status,
        "members": [dataclasses.asdict(member) for member in fit.members],
        "c": fit.c,
        "score": fit.score,
        "score_oos": fit.score_oos,
        "slope_pct_per_year": fit.slope_pct_per_year,
        "window": dataclasses.asdict(fit.window),
        "split_day": fit.split_day,
        "f": fit.f.tolist(),
        "composite": fit.composite.tolist(),
    }
