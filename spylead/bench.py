"""python -m spylead.bench: one full compute on production-size synthetic data, timed by phase (SPEC.md §9, §16.3).

The data lives in a temporary database: the bench never reads data/ and never fetches.
"""
import asyncio
import contextlib
import datetime
import os
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path

import numpy as np

from spylead.catalog import SPY_NAME, CatalogEntry, load_catalog
from spylead.jobs import COMPUTED, PHASES, Jobs
from spylead.pit import Observations, today_at, years_before
from spylead.store import Store

ROOT = Path(__file__).resolve().parents[1]
YEARS = 33
BUDGET_S = 600
# Each catalog entry's FRED frequency: every day (D), weekdays (B), weekly (W) or monthly (M).
FREQUENCIES = {
    "M2": "M",
    "Spread_CCC": "B",
    "PMI_Philly": "M",
    "PMI_Philly_Future": "M",
    "Spread_10Y2Y": "B",
    "Spread_10Y3M": "B",
    "Spread_BAA10Y": "B",
    "Spread_HY": "B",
    "Claims": "W",
    "Permits": "M",
    "Hours_Mfg": "M",
    "Orders_Capex": "M",
    "Sentiment": "M",
    "NFCI": "W",
    "STLFSI": "W",
    "VIX": "B",
    "FedFunds": "D",
    "OECD_CLI": "M",
}
# FRED keeps only 3 years of the ICE BofA series (SPEC.md §4.3).
HISTORY_YEARS = {"Spread_CCC": 3, "Spread_HY": 3}


def observation_days(frequency: str, first_day: int, last_day: int) -> np.ndarray:
    """The days with an observation, from first_day to last_day. Weekly observations fall on Fridays."""
    days = np.arange(first_day, last_day + 1)
    weekday = (days + 3) % 7  # 1970-01-01 was a Thursday; Monday is 0
    if frequency == "D":
        return days
    if frequency == "B":
        return days[weekday < 5]
    if frequency == "W":
        return days[weekday == 4]
    months = days.astype("datetime64[D]").astype("datetime64[M]")
    return days[months.astype("datetime64[D]").astype(np.int64) == days]


def synthetic(frequency: str, first_day: int, today: int, lag: int, form: str, seed: int) -> Observations:
    """A random walk at the given frequency, each value released `lag` days after its date and by today."""
    days = observation_days(frequency, first_day, today - max(lag, 1))
    walk = np.cumsum(np.random.default_rng(seed).normal(0, 0.01, days.size))
    values = 100 * np.exp(walk) if form == "LOG" else walk
    return Observations(days, values, days + lag, np.zeros(days.size, dtype=bool))


def fill(store: Store, catalog: Sequence[CatalogEntry], today: int, years: int = YEARS) -> None:
    """SPY on weekdays and every catalog entry at its frequency, for `years` years up to today."""
    store.insert_observations(SPY_NAME, synthetic("B", years_before(today, years), today, 0, "LOG", 0))
    for seed, entry in enumerate(catalog, start=1):
        first_day = years_before(today, min(years, HISTORY_YEARS.get(entry.name, years)))
        observations = synthetic(FREQUENCIES[entry.name], first_day, today, entry.default_lag_days, entry.form, seed)
        store.insert_observations(entry.name, observations)


@contextlib.asynccontextmanager
async def no_sources() -> AsyncIterator[None]:
    raise RuntimeError("the bench never fetches")
    yield


def timed_compute(store: Store, catalog: Sequence[CatalogEntry], pool: Executor, clock: Callable) -> dict[str, float]:
    """One RECOMPUTE of the stored data: the seconds of each phase."""

    async def compute() -> dict[str, float]:
        jobs = Jobs(store, catalog, pool, clock, no_sources)
        jobs.recompute(store.inflections_per_year())
        await jobs.wait()
        if jobs.last.outcome != COMPUTED:
            raise RuntimeError(f"the compute ended {jobs.last.outcome}: {jobs.last.message}")
        return jobs.timings

    return asyncio.run(compute())


def report(timings: dict[str, float], total: float, workers: int, candidates: int) -> str:
    lines = [f"spylead bench: SPY on weekdays for {YEARS} years, {candidates} candidates, {workers} workers"]
    lines += [f"  {phase:<7}{timings[phase]:8.1f} s" for phase in PHASES if phase in timings]
    verdict = "within" if total <= BUDGET_S else "over"
    lines.append(f"  {'total':<7}{total:8.1f} s, {verdict} the budget of {BUDGET_S} s")
    return "\n".join(lines)


def main() -> int:
    catalog = load_catalog(ROOT / "catalog.toml")

    def clock() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    workers = max(1, (os.cpu_count() or 1) - 1)
    with tempfile.TemporaryDirectory() as folder, ProcessPoolExecutor(workers) as pool:
        store = Store.open(Path(folder) / "bench.db", clock)
        try:
            store.sync_catalog(catalog)
            fill(store, catalog, today_at(clock()))
            started = time.perf_counter()
            timings = timed_compute(store, catalog, pool, clock)
            total = time.perf_counter() - started
        finally:
            store.close()
    print(report(timings, total, workers, len(catalog)))
    return 0 if total <= BUDGET_S else 1


if __name__ == "__main__":
    sys.exit(main())
