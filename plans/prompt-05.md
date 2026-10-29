# Prompt 5: Store

## Steps

1. `spylead/store.py`: SQLite persistence (SPEC.md §10) behind one class, `Store`. Synchronous, for one thread.
   - `StoreError`: the one error type for a failed database operation, read or write. Its message carries SQLite's own text ("database is locked"), which holds no value from the data.
   - `Store.open(path, clock)`:
     - connects with a busy timeout of 1000 ms, in autocommit mode;
     - sets `journal_mode = WAL` with `synchronous = NORMAL`; when SQLite does not answer `wal`, logs a warning and sets `journal_mode = DELETE`;
     - creates the schema of §10 in one transaction when `user_version` is 0, then sets it to 1; refuses any other version.
     - `close()`.
   - Writes go through one transaction helper, `BEGIN IMMEDIATE` … `COMMIT`: the write lock comes first, so a locked database fails after the busy timeout, before anything changes. Any error rolls the transaction back.
   - Series (`SeriesRow`, the columns of §10):
     - `sync_catalog(entries)`: upserts the SPY row and the catalog rows; deletes the CATALOG rows no longer in the catalog, with their observations and curves. One transaction.
     - `add_user_series(fred_id, form, title, units, frequency)` and `remove_user_series(name)` (USER rows only, with their observations and curves);
     - `set_metadata(name, title, units, frequency)`, `set_form(name, form)`, `record_fetch(name, error)`;
     - `series(name)`, `all_series()`.
   - Observations:
     - `insert_observations(name, observations, last_updated=None) -> int`: only the observation days not yet stored, and `last_updated` when given, in one transaction. Returns the number inserted;
     - `observations(name) -> pit.Observations`, by observation day;
     - `latest_obs_day(name)`;
     - `counts() -> dict[str, Counts]`: per series, the stored observations, the estimated ones, and the latest observation and release days.
   - Settings:
     - `start_days()`, `set_start_days(fetch_start, analysis_start)`: set once, then kept;
     - `inflections_per_year()` (2 until set), `set_inflections_per_year(value)`;
     - `excluded()`, `set_excluded(names)`: f_excluded, a JSON list.
   - Curves and results (`Curve`, `Results`):
     - `save_compute(curves, payload)`: replaces every curve and the results, in one transaction;
     - `save_results(payload)`: the results alone, for REFIT_F;
     - `curve(name)`, `curves()`, `results()`.
   - Events (`Event`): `append_event(level, kind, message, series=None) -> int`; `events(limit=50)`, newest first. `EVENT_KINDS` and `LEVELS` list the values of §10.
2. `pit.utc_text(instant)`: an instant as UTC ISO 8601 to the second, `2026-10-28T14:03:11Z`, the format of the state document. The store, the jobs and the web layer share it.
3. `catalog.py`: an entry may not be named `SPY`.
4. Tests:
   - `tests/test_store.py`, against a temporary database file and an injected clock:
     - the schema: the six tables with the columns, types, keys and `WITHOUT ROWID` of §10; `user_version` 1; WAL, synchronous NORMAL and a busy timeout of 1000 ms; opening again changes nothing; another version and a file that is not a database are refused;
     - round trips for each table;
     - the catalog sync adds, updates and deletes; a changed FRED ID drops the old data; USER rows stay;
     - USER rows: add, a duplicate refused, remove with their data, a catalog row not removed;
     - inserting an existing observation day leaves the stored row unchanged, and a failed insert stores nothing;
     - fetch_start is set once, also across a reopen; N defaults to 2; f_excluded survives close and reopen;
     - a compute replaces every curve, and a failed save keeps the previous curves and results;
     - the latest N events, newest first;
     - the WAL fallback, with the pragma patched;
     - a write against a locked database fails after the busy timeout (patched short) with StoreError, and changes nothing.
   - `tests/test_pit.py`: `utc_text`. `tests/test_catalog.py`: the reserved name.
5. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **The clock is injected into the store** (§15). It stamps `added_at`, `last_fetch_at`, `computed_at` and each event's `ts` with `pit.utc_text`, so every stored instant has one format.
- **Settings in the database:** fetch_start and analysis_start as ISO dates, readable in the file and the state document's format; N as a JSON number; f_excluded as a JSON list (§10). The store's methods take and return days and numbers.
- **SPY's row:** origin SPY, source TIINGO, source_id SPY, form LOG (§5.4), default lag 0 (a close is released on its trading day, §4.1), units USD (§11), frequency D, no title.
- **The catalog sync:**
  - an update keeps `added_at`, the FRED metadata, `last_updated` and the fetch fields;
  - a changed `fred_id` is another series: its observations and curves are deleted and its metadata, `last_updated` and fetch fields cleared, so the next fetch starts from fetch_start (§8.2). Otherwise the stored first releases of the old series would mix with the new one's;
  - a changed form or default lag keeps the data: y is computed at compute time, and stored release days never change (§5.1);
  - a USER row with the name of a new catalog entry becomes that catalog row, under the same FRED ID rule.
- **Removing a series** (a USER removal, or a catalog row leaving the file) also drops its name from f_excluded, so a series added again later is in F by default (§7.4).
- **Curves are stored in y,** the analysis's scale (§3), so REFIT_F reads exactly what step 5 used. The chart routes convert to original units (prompt 8).
- **Counts are of stored observations.** `counts()` is one GROUP BY over the observations: their number, the estimated ones, the latest observation day and the latest release day. Points (§5.3) can be fewer when a late release of an older observation was skipped; prompt 7 or 8 decides which number the state document shows.
- **Payloads are strict JSON** (`allow_nan=False`): prompt 7 turns NaN into null, as §11 does for charts.
- **The DELETE fallback** keeps SQLite's default `synchronous = FULL`. NORMAL is safe with WAL, not with a rollback journal on power loss.
- **Event kinds and levels are checked.** An unknown one is a programming error (ValueError), not a StoreError. The two lists also serve prompt 9's locale test.
- **One thread.** The connection keeps SQLite's same-thread check; the event loop owns the store (prompt 7, 8).
- **The store does not create `data/`:** `__main__` does, before taking the lock in it (§15, prompt 8).
- **`SPY` is a reserved catalog name:** SPY's row shares the series table, and a catalog entry of that name would overwrite it. `load_catalog` refuses it, naming the entry.

## Extension points

- **Prompt 7:**
  - FETCH: `start_days`/`set_start_days`, `latest_obs_day`, `series(name).last_updated`, `insert_observations` with `last_updated`, `record_fetch`, `set_metadata`; ADD_SERIES: `add_user_series`, then `set_form` once the stored values are known (§4.4); REMOVE_SERIES: `remove_user_series`.
  - The compute: `observations` per series, then `save_compute` with every curve and the payload; REFIT_F: `curves` and `results`, then `save_results`.
  - Events through `append_event`.
- **Prompt 8:** the state document reads `all_series`, `counts`, the settings, `results` and `events(50)`; the chart routes read `curve(name)` and `observations(name)` (raw points at release days, §11).
- **A schema change:** `SCHEMA_VERSION`, and a migration step in `Store.open` for the version found.

## Refactor seams

- **The store** is the only SQLite code (§18). Callers see dataclasses, days and NumPy arrays, never rows.
- **Transactions.** `write` is the one path for changes, which keeps the busy timeout and the rollback in one place.
- **Instants.** `pit.utc_text` is the one format for stored and served instants.

## Deferred items (appended to TODO.md)

None.
