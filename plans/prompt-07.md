# Prompt 7: Jobs and compute

## Steps

1. `spylead/pit.py`: two helpers the jobs need, next to the rules they serve.
   - `joined(first, second)`: two sets of observations as one, for `typical_lag` over stored and new observations (§5.2);
   - `user_form(units, values)`: §4.4's form of a USER series, LEVEL when the units contain "Percent" or a value is zero or negative, LOG otherwise.
2. `spylead/jobs.py`:
   - `smooth_series(release_days, y, n_per_year, today)`: one pool task per series, `smoothing.one_sided` from the first release day to today and `smoothing.reference_fit`. Top-level, plain arrays (§7.7).
   - `open_sources(config, sleep)`: one aiohttp ClientSession per job (§8.4), with the Tiingo and FRED clients on it. Injected, so tests pass fake clients and open no session.
   - `Progress` (kind, phase, done, total, series, started_at) and `LastJob` (kind, outcome, finished_at, message): the state document's `job` and `last_job` (§11).
   - `Jobs(store, catalog, pool, clock, sources)`, the single-job runner (§9):
     - `fetch()`, `recompute(inflections_per_year)`, `refit(excluded)`, `add_series(fred_id)`, `remove_series(name)` start a job, or raise `JobRunning` while one runs (409 JOB_RUNNING in prompt 8). `recompute` and `refit` store the new N or selection, with SETTING_CHANGED or SELECTION_CHANGED, only once the job can start;
     - `progress` and `last`; `wait()` for the running job; `shutdown()` cancels it (§9).
     - Each job runs as one asyncio task. Its outcome is one of §11's; an unexpected exception ends it FAILED with JOB_FAILED, a cancellation INTERRUPTED with JOB_INTERRUPTED. Results are saved in one transaction, so an interrupted compute leaves the previous ones in place; observations already inserted stay.
   - FETCH (§8):
     - the first fetch sets fetch_start and analysis_start (`pit.years_before`), once;
     - SPY: without stored rows, prices from fetch_start to today; otherwise Tiingo's endDate, up to date when not later than the latest stored day, else prices from 7 days before it to endDate;
     - FRED: the metadata (stored on the row), up to date when `last_updated` equals the stored value and rows exist, else first releases from fetch_start (no rows) or from the latest observation day minus 400 days. `typical_lag` over the stored and the new observations, `estimate_release_days` on the new ones, then `Store.insert_observations` with `last_updated`. USER series get §4.4's form again;
     - a SourceError fails that series only: `last_fetch_error`, SERIES_FAILED, and the job goes on;
     - SPY without data after the fetch ends NO_SPY_DATA without a compute; all series up to date ends UP_TO_DATE without a compute; otherwise SERIES_UPDATED per fetched series, FETCH_FINISHED, a compute, UPDATED.
   - The compute (§9):
     - points (`pit.known_points`), INVALID_VALUES (`pit.has_invalid_values`), y;
     - SMOOTH: `smooth_series` in the pool for SPY and every candidate with valid points;
     - STEP3: `analysis.step3` in the pool for every candidate with a one-sided series; then `analysis.status` for every candidate (step 4);
     - STEP5: `analysis.step5` in the pool for the members (`analysis.is_member` with the stored exclusions), which runs both out-of-sample checks;
     - SAVE: `Store.save_compute` with every curve and the payload, in one transaction; then STATUS_CHANGED for each status that differs from the previous results, LAMBDA_AT_LIMIT, INVALID_VALUES, and COMPUTE_FINISHED.
     - The time of each phase is logged, and returned for the bench.
   - RECOMPUTE: the compute on the stored data. REFIT_F: step 5 alone, from the stored curves and the stored statuses and A*, then `Store.save_results`. ADD_SERIES: FRED's metadata (UNKNOWN_SERIES for HTTP 400 or 404, UNSUPPORTED_FREQUENCY for annual, MISSING_KEY without the key), the USER row with §4.4's form, SERIES_ADDED, the fetch from fetch_start, the compute. REMOVE_SERIES: the deletion, SERIES_REMOVED, then REFIT_F (§4.4, §9).
3. `spylead/bench.py`, `python -m spylead.bench`: a temporary database filled with production-size synthetic data (SPY on weekdays for 33 years; every catalog entry at its FRED frequency, daily, weekly or monthly, with its default lag, the two ICE BofA series over 3 years only), one full compute through `Jobs` with the real process pool, and the time of each phase. Not run here.
4. Tests:
   - `tests/fakes.py`: an in-process stand-in for the process pool, a settable clock, and fake Tiingo and FRED clients that keep full histories, answer requests by range and record them.
   - `tests/test_jobs.py`, with a temporary database, the fakes, and the numerical steps replaced by fast stand-ins with the same inputs and outputs (the real ones take minutes on the 10-year windows of §7):
     - AC-9: the first fetch sets both start days once, downloads SPY and every candidate from fetch_start, computes, and stores a curve for SPY and each candidate;
     - AC-10: everything up to date: metadata requests only, no observations, no compute, UP_TO_DATE, `computed_at` unchanged;
     - AC-11: an incremental fetch requests the overlaps of §8.2, inserts only new days, leaves stored rows unchanged, and computes;
     - AC-12: one failing series keeps its data, records `last_fetch_error`, the others continue and the compute runs; SPY without data ends NO_SPY_DATA without a compute; an empty key gives MISSING_KEY for each series of that source, through the real clients;
     - AC-13: a second job is refused while one runs; progress through FETCH, SMOOTH, STEP3, STEP5 and SAVE; a shutdown during a fetch and during a compute keeps the previous results and records INTERRUPTED; REFIT_F runs step 5 alone, from the stored curves;
     - AC-14, job part: PENDING, FETCH_FAILED, INVALID_VALUES, INSUFFICIENT_HISTORY from a 3-year series, AT_BOUNDARY, NOT_LEADING, DISCONTINUED from a stale series (never in F), KEPT, and F's NO_MEMBERS, INSUFFICIENT_OVERLAP and OK; STATUS_CHANGED;
     - AC-15: the form rule, UNSUPPORTED_FREQUENCY, UNKNOWN_SERIES, MISSING_KEY, removal of a USER series only;
     - AC-16: unchecking a member refits F without it, the choice survives a restart, a newly KEPT series joins F;
     - AC-22, determinism: two computes on the same data, with the real smoothing and analysis at a reduced scale, give identical curves and payloads;
     - the worker functions run in a spawned process pool, as on Windows.
   - `tests/test_bench.py`: the synthetic data has the catalog's frequencies and spans, and a small bench run prints every phase.
   - `tests/test_pit.py`: `joined` and `user_form`.
5. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **Every compute needs SPY's data.** A job that would compute without SPY data ends NO_SPY_DATA, as §8.3 says for FETCH: also RECOMPUTE and REFIT_F before the first fetch, and ADD_SERIES and REMOVE_SERIES then. ADD_SERIES before the first fetch still stores the series, which the first fetch then downloads from fetch_start.
- **MISSING_KEY as an outcome** is ADD_SERIES's, when the FRED key is empty: nothing can be added. In FETCH, an empty key fails each series of its source with MISSING_KEY (§8.3), and an empty Tiingo key on the first fetch therefore ends NO_SPY_DATA.
- **"Up to date" is literal (§8.2).** A series whose FRED `last_updated` changed is fetched even if no new observation day arrives, and a failed series is not up to date: either way the outcome is UPDATED and a compute runs. Its counts can be 0.
- **typical_lag** runs over the stored observations and the new ones together: §5.2's 24 months reach back further than the 400-day overlap of a later fetch. Stored rows never change, so only new observations get estimated release days.
- **INVALID_VALUES** looks at the points' values, the ones that enter the fit.
- **The one-sided series** runs from the series' first release day to today; the curve stores the leading NaN days. The reference fit runs from the first to the last point.
- **LAMBDA_AT_LIMIT** is one event per series, with the number of refits at the limit and whether the reference fit is: with small N, a series can have hundreds (prompt 2).
- **STATUS_CHANGED** compares with the previous results. A candidate without a previous status, as on the first compute, has no event.
- **The payload** (`results.payload`) holds `today`, N, SPY's point counts, one entry per candidate in §11's order (status, point counts, step 3's report or null) and F (its report and its two daily curves, or its status alone). Days are integers; prompt 8 turns them into dates. Point counts are of points (§3), not of stored observations.
- **ADD_SERIES** reads FRED's metadata once and passes it to the download. HTTP 400 and 404 are UNKNOWN_SERIES; FRED answers an unknown ID with 400. Other source errors end it FAILED, without the series.
- **Job events:** a job that ends NO_SPY_DATA, MISSING_KEY, UNKNOWN_SERIES or UNSUPPORTED_FREQUENCY records JOB_FAILED at WARNING; an unexpected error records JOB_FAILED at ERROR. Its message and the logged traceback go through `sources.redact`.
- **`last_job` lives in memory.** After a restart it is null until the next job; the events keep the history.
- **Progress during a parallel phase** counts finished tasks, and `series` names the latest one.
- **The tests replace the numerical steps** (`jobs.smooth_series`, `analysis.step3`, `analysis.step5`) with stand-ins that take and return the same types: the jobs module only composes them, and the real ones are tested in prompts 2 and 4. One test runs the real ones, with smaller windows and shifts patched in, for determinism (AC-22). A spawned pool checks that the workers pickle as Windows needs.

## Extension points

- **Prompt 8** constructs `Jobs` with the real `ProcessPoolExecutor` and `functools.partial(open_sources, config, asyncio.sleep)`, maps `JobRunning` to 409, validates input before calling the start methods, reads `progress`, `last` and `results.payload` for the state document, and calls `shutdown()` before shutting the pool down (§15).
- **Scheduled fetches** (TODO.md): a timer calling `Jobs.fetch()`.
- **Weekly refits** (TODO.md): `smoothing.refit_days` only; the jobs module is unchanged.

## Refactor seams

- **Pool tasks.** `Jobs.map` is the one place that sends tasks to the pool and counts progress.
- **Sources.** `open_sources` is the only code that creates a ClientSession.
- **Payload.** `payload_of` and the JSON helpers next to it are the one definition of the stored results; prompt 8 reads them.

## Deferred items (appended to TODO.md)

None.
