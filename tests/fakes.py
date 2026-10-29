"""Fakes for the jobs tests (SPEC.md §16.1): the process pool, the clock, the sources, and quick numerical stand-ins."""
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime

import numpy as np

from spylead import analysis
from spylead.analysis import Combination, MemberFit, Step3, Step5
from spylead.pit import Observations
from spylead.smoothing import Anchors, OneSided, Reference, refit_days
from spylead.sources import FredSeries, SourceError

NONE = Observations(np.empty(0, np.int64), np.empty(0), np.empty(0, np.int64), np.empty(0, bool))


class InlinePool(concurrent.futures.Executor):
    """Runs each task at once, in this process, and records the function. `hold` holds back one function's tasks."""

    def __init__(self, hold=None):
        self.calls = []
        self.hold = hold

    def submit(self, fn, /, *args, **kwargs):
        self.calls.append(fn)
        future = concurrent.futures.Future()
        if fn is self.hold:
            return future
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future


class Clock:
    def __init__(self, instant):
        self.instant = instant

    def __call__(self):
        return self.instant

    def advance(self, **delta):
        self.instant += datetime.timedelta(**delta)


class Gate:
    """Holds a fake request until opened, to keep a job running."""

    def __init__(self):
        self.event = asyncio.Event()
        self.reached = asyncio.Event()

    async def wait(self):
        self.reached.set()
        await self.event.wait()


class FakeTiingo:
    """SPY's full history. Answers by range, as Tiingo does, and records each request."""

    def __init__(self, closes=NONE, error=None):
        self.closes = closes
        self.error = error
        self.calls = []
        self.gate = None

    async def answer(self, call):
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error

    async def end_day(self):
        await self.answer(("end_day",))
        return int(self.closes.obs_day[-1])

    async def prices(self, first_day, last_day):
        await self.answer(("prices", first_day, last_day))
        days = self.closes.obs_day
        return self.closes.take((days >= first_day) & (days <= last_day))


class FakeFred:
    """Each series' metadata and full first-release history, by FRED ID. Unknown IDs answer HTTP 400, as FRED does."""

    def __init__(self):
        self.metadata = {}
        self.history = {}
        self.errors = {}
        self.calls = []
        self.gate = None

    def add(self, fred_id, history, units="Index", frequency="M", last_updated="2026-10-01 07:45:03-05"):
        self.metadata[fred_id] = FredSeries(f"Title of {fred_id}", units, frequency, last_updated, 0, 0)
        self.history[fred_id] = history

    def update(self, fred_id, history, last_updated):
        self.metadata[fred_id] = dataclasses.replace(self.metadata[fred_id], last_updated=last_updated)
        self.history[fred_id] = history

    async def answer(self, call):
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        if call[1] in self.errors:
            raise self.errors[call[1]]
        if call[1] not in self.metadata:
            raise SourceError("HTTP_400", "FRED answered HTTP 400")

    async def series(self, fred_id):
        await self.answer(("series", fred_id))
        return self.metadata[fred_id]

    async def observations(self, fred_id, first_day):
        await self.answer(("observations", fred_id, first_day))
        history = self.history[fred_id]
        return history.take(history.obs_day >= first_day)


def sources_of(tiingo, fred):
    @contextlib.asynccontextmanager
    async def opened():
        yield tiingo, fred

    return opened


def no_anchors():
    empty = np.empty(0)
    return Anchors(np.empty(0, np.int64), empty, empty, empty, empty, np.empty(0, bool))


def quick_smooth(release_days, y, n_per_year, today):
    """Stands in for jobs.smooth_series: the points joined by straight lines, defined after §6.4's first refit."""
    first_day = int(release_days[0])
    days = np.arange(first_day, today + 1)
    values = np.interp(days, release_days, y)
    month_ends = refit_days(first_day, today)
    values[days <= (month_ends[0] if month_ends.size else today)] = np.nan
    reference_days = np.arange(first_day, int(release_days[-1]) + 1)
    reference = Reference(first_day, np.interp(reference_days, release_days, y), 1.0, False)
    return OneSided(first_day, values, no_anchors()), reference


def quick_step3(spy, indicator):
    """Stands in for analysis.step3 with its real window: a lead of 90 days, or, for a constant series, A* = its value."""
    w3 = analysis.step3_window(spy, indicator)
    if w3 is None:
        return None
    defined = indicator.values[~np.isnan(indicator.values)]
    a_star = int(round(defined[0])) if np.ptp(defined) == 0 else -90
    return Step3(w3, a_star, 1.0, 0.05, 0.07, 0.4)


def quick_step5(spy, members):
    """Stands in for analysis.step5 with its real statuses: each b is the member's A*."""
    if not members:
        return Step5(analysis.NO_MEMBERS)
    w5 = analysis.step5_window(spy, members)
    if w5 is None:
        return Step5(analysis.INSUFFICIENT_OVERLAP)
    fits = tuple(MemberFit(member.name, member.a_star, 0.5) for member in members)
    curve = np.ones(w5.length)
    return Step5(analysis.OK, Combination(fits, 0.1, 0.04, 0.06, 0.1, w5, w5.split()[1].start, curve, curve * 2))
