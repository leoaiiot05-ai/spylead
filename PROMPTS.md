# Build prompts for Claude Code

## How to use

Unattended (the normal way):

1. Run `bootstrap.ps1` once from the kit folder. It:
   - creates `E:\00_CODING\spylead`;
   - initializes git with the GitHub remote, commits these files and the vendored ECharts, and pushes;
   - writes `build.cmd` and registers the logon task;
   - starts `run_prompts.py` with max effort and ultracode.
2. `run_prompts.py` runs the prompts below in order, one headless Claude Code session each.
   - Each plan goes to `plans/prompt-NN.md` instead of waiting for approval.
   - After each prompt it runs the tests, asks the same session to repair a failure, then commits and pushes.
3. **Restarts.** After a restart (Windows Update included), the logon task starts the runner again, and it continues the unfinished prompt.
4. **Usage limits.** At a usage limit, it waits for the reset and continues.
5. **Cloud hand-over.**
   - To hand over: run `build.cmd --hold`, start a Claude Code cloud session on the repository, and tell it: "Read CLOUD.md and continue the build."
   - To come back: run `build.cmd --release`. It pulls the cloud's commits and continues locally.

Interactive alternative: start Claude Code in the folder and paste the prompts in order, one per session. Each asks for a plan first: approve or correct it, let it implement, run the tests, commit, then start a new session for the next prompt.

Whenever Claude Code works on this PC, keep IB Gateway closed, logged in to the paper account, or set to "Read-Only API". The runner stops if a live API port is open.

If a result disagrees with `SPEC.md`, the spec wins. If the spec is wrong, change the spec first, then the code.

| # | Prompt | Acceptance criteria covered |
|---|---|---|
| 1 | Scaffold, lock file, config and catalog | AC-20 |
| 2 | Smoothing core | AC-1, AC-2, AC-3 |
| 3 | Point-in-time core | AC-4 |
| 4 | Analysis core | AC-5, AC-6, AC-7, AC-8, AC-14 (analysis part) |
| 5 | Store | supports AC-9 to AC-16, AC-21 (WAL fallback) |
| 6 | Data sources | AC-12 (requests), AC-17 (redaction) |
| 7 | Jobs and compute | AC-9 to AC-16, AC-22 (determinism) |
| 8 | Web API and wiring | AC-17, AC-18, AC-21 |
| 9 | Web UI and PWA | AC-19, UI checklist |
| 10 | Operations and README | AC-21 (kept), checks of SPEC.md §16.3 |
| 11 | Final audit | all |

---

## Prompt 1: Scaffold, lock file, config and catalog

```text
Read SPEC.md and TODO.md in full. CLAUDE.md is already loaded.

Goal: scaffold the repository. No analysis code and no network code.

Build (SPEC.md §2, §4.3, §13, §14, and the logging line of §15):
- requirements.in with exactly numpy, scipy, aiohttp, tzdata and pytest. Then create the venv, upgrade pip, generate pylock.toml and install from it, as §13 describes. No pyproject.toml: the project runs from the repository root.
- Only the modules this phase needs: spylead/__init__.py, __main__.py, config.py and catalog.py. Do not create placeholder files for later phases.
- `python -m spylead --config <path>`: parse the arguments, load and validate the config and the catalog, set up logging, then exit with a message that the app is not implemented yet.
- The config loader of §14 and the catalog loader of §4.3, with errors that name the offending key or entry and never echo a value.
- catalog.toml with the 18 entries of the §4.3 table, in that order, with English and Traditional Chinese titles.
- config.example.toml, and .gitignore (config.toml, data/, logs/, .venv/, __pycache__/, .pytest_cache/).
- The vendored ECharts files are already in spylead/static/vendor/echarts/. Leave them as they are.

Tests:
- Config loading: a valid file, a missing file, an unknown key, a missing key, a wrong type, the placeholder token, a short token, and empty API keys (accepted with a warning).
- Catalog loading: the committed catalog.toml, a duplicate name, a bad name, a bad form and a missing key.
- No error message contains a value from the file (AC-20).
- A test runs `python -m spylead --config config.example.toml` in a subprocess and checks that it fails validation on the placeholder token, without touching the network.

Done when: pylock.toml holds a hash for every package, AC-20 holds, and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 2: Smoothing core

```text
Read SPEC.md §3, §6 and §7.7. Skim the existing code and follow its patterns.

Goal: the pure smoothing core.

Build `spylead/smoothing.py`:
- The weighted Reinsch solve with a banded Cholesky, the spline value formula with its linear extension, and the roughness integral (§6.2).
- The robust IRLS fit, with weights passed in and returned (§6.2).
- The inflection count (§6.2), and the λ search for a smoothness N with the warm-started bracket and its fallback (§6.3).
- The one-sided series of §6.4: refit days, anchors, and the daily values over a day range. Return them as a float64 array with NaN where undefined, together with each refit's λ and a flag for a λ at its limit, which the caller logs.
- The reference fit of §6.5.

Inputs are plain arrays of release days and y values; outputs are plain arrays and dataclasses. No I/O, no clock, no logging.

Tests:
- AC-1:
  - compare with scipy.interpolate.make_smoothing_spline (in tests only) on random data with uneven spacing, random weights, and λ of 1e-4, 1e-2 and 1;
  - continuity of the value formula at the knots;
  - the end slopes of the linear extension.
- AC-2:
  - IRLS never increases the objective;
  - a single spike moves the robust fit less than the L2 fit;
  - the inflection count on hand-made γ sequences, including near-zero entries;
  - the λ search invariant, agreement between the warm-started and full brackets, and the λ limits.
- AC-3:
  - for random days d, altering or removing points released after the last month end before d leaves every value on or before d unchanged;
  - values follow the anchor line;
  - nothing before the first anchor;
  - a month without a new point keeps the previous anchor.

Keep test data small. Production-size timing belongs to the bench (prompt 7).

Done when: AC-1, AC-2 and AC-3 hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 3: Point-in-time core

```text
Read SPEC.md §3, §4.1, §4.2 and §5. Skim the existing code and follow its patterns.

Goal: the pure point-in-time module.

Build `spylead/pit.py`:
- Translation of a FRED initial-release payload (the observations list of §4.2) and of Tiingo price rows into observation records: observation day, value and release day. Skip missing values.
- typical_lag, pre-archive detection and estimated release days (§5.2).
- The known-value rule (§5.3), producing strictly increasing points.
- The forms of §5.4: y values, and the check for values that are not positive in LOG form.
- The day helpers of §3: dates to days, days to x, and today in America/New_York from an injected instant.

Tests (AC-4):
- hand-written payloads with missing values;
- a series with fewer than 12 recent observations (default lag) and one with more (median);
- pre-archive observations with the flag;
- a late release of an older observation, skipped;
- two observations on one release day;
- strictly increasing release days;
- a LOG series with a zero value;
- date and day round trips, including 29 February.

Done when: AC-4 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 4: Analysis core

```text
Read SPEC.md §3, §7, and §17 items 7 and 8. Skim smoothing.py and pit.py.

Goal: the pure lead analysis.

Build `spylead/analysis.py`:
- LAD fits by IRLS, batched over candidate shifts; the score, the slope and the evaluation points (§7.1).
- Step 3, with its window, chunked search, tie rule and report (§7.2); step 4 (§7.3).
- Step 5: members, window, coordinate descent with two starts and three stages, tie rules, the report, F normalized to median 1, and the composite (§7.4).
- The out-of-sample check for both steps (§7.5).
- The statuses of §7.6, including DISCONTINUED from the release-day spacing, and the F statuses.

Inputs are one-sided daily arrays with their first days, plus series metadata. Outputs are dataclasses that the jobs module stores as JSON.

Tests:
- AC-5:
  - on synthetic data where an indicator equals SPY's latent process 90 days later, A* is exactly −90, in LOG and in LEVEL form;
  - the window is the same for every A;
  - a tie goes to the smaller |A|;
  - AT_BOUNDARY, NOT_LEADING and INSUFFICIENT_HISTORY.
- AC-6:
  - independent components with periods of at least 1,500 days and known leads give every b_i exactly;
  - two runs are identical;
  - the bounds hold, and the coarse stage runs on the 28-day grid.
- AC-7:
  - LAD against scipy.optimize.linprog (HiGHS) on a small case;
  - score, slope, F with median 1, and the composite against hand-computed values.
- AC-8: the out-of-sample check uses only the first 70% and changes nothing else.
- AC-14, analysis part: every status and F status from small scenarios, including a 3-year series and a stale series.

Done when: those criteria hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 5: Store

```text
Read SPEC.md §4.4, §8.1, §9 and §10. Skim the existing code and follow its patterns.

Goal: persistence.

Build `spylead/store.py`:
- Open or create `data/spylead.db` with the schema and pragmas of §10, including the WAL fallback.
- Series rows:
  - the SPY row, and the catalog sync at startup: upsert, and delete rows that left the catalog together with their observations and curves;
  - USER rows: add and remove.
- Observations:
  - insert only new observation days, in one transaction;
  - read the points of a series, the latest observation day, and the counts and estimated counts.
- Settings: fetch_start and analysis_start (set once), inflections_per_year (default 2), and f_excluded.
- Curves and results: replace both in one transaction, and read them.
- Events: append, and read the latest N, newest first.
- A 1000 ms busy timeout. Write failures surface as one store error type.

Tests against a temporary database file:
- schema creation is idempotent;
- round trips for each table;
- the catalog sync adds, updates and deletes;
- inserting an existing observation day leaves the stored row unchanged;
- fetch_start is set once;
- f_excluded survives close and reopen;
- latest-N ordering;
- the WAL fallback, with a patched pragma;
- a write against a locked database fails within the busy timeout with the store error type.

Done when: pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 6: Data sources

```text
Read SPEC.md §4.1, §4.2, §4.4, §8.3, §8.4, and the secrets rule of §13. Skim pit.py.

Goal: the Tiingo and FRED clients.

Build `spylead/sources.py`:
- Tiingo: the meta request (endDate) and the price request, with the key in the Authorization header.
- FRED: the series metadata request and the initial-release observations request of §4.2, with the key in the query string.
- The request rules of §8.4:
  - one ClientSession, passed in;
  - 30 s timeouts and the FRED pacing;
  - retries with an injected sleep, Retry-After for 429, and no retry on other 4xx.
- Errors as one source error type, with a code (TIMEOUT, HTTP_<status>, BAD_PAYLOAD or MISSING_KEY) and English text that never contains a URL or a key.
- A redaction helper for any FRED URL that reaches a log.
- Keep translation helpers apart from network calls. The payload-to-observation translation lives in pit.py.

Tests, with aiohttp's test server or an injected fake transport, and hand-written JSON samples:
- each request's path, parameters and headers;
- the up-to-date inputs: endDate and last_updated;
- retries, Retry-After, and no retry on 404, with an injected sleep;
- MISSING_KEY without any request;
- a bad payload;
- every error text and every log line free of keys (AC-17, redaction part).

Done when: pytest passes and no test opens a real connection.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 7: Jobs and compute

```text
Read SPEC.md §5, §6.4, §7.6, §8, §9 and §10. Skim pit.py, smoothing.py, analysis.py, store.py and sources.py. The jobs module composes them and must not duplicate their logic.

Goal: fetching and computing, driven through fakes.

Build:
- `spylead/jobs.py`:
  - the single-job runner, with its kinds, phases and progress, refusing a second job while one runs (§9);
  - FETCH: the first fetch, later fetches, up-to-date detection, per-series failures, NO_SPY_DATA and MISSING_KEY (§8);
  - the compute (§9):
    - points, then one-sided series and reference fits in the pool;
    - step 3 in the pool, then step 4;
    - step 5 with both out-of-sample checks;
    - one save transaction, the events, and STATUS_CHANGED when a status changes;
  - RECOMPUTE, REFIT_F from the stored curves, ADD_SERIES and REMOVE_SERIES (§4.4, §9);
  - interruption on shutdown (§9).

  The pool is injected; tests use an in-process stand-in.
- `spylead/bench.py`: production-size synthetic data (SPY daily for 33 years, plus series of the catalog's frequencies), one full compute with the real pool, and the time of each phase (§9). Do not run it.

Tests (fake sources, a temporary database, an injected clock and sleep, and the in-process pool): AC-9, AC-10, AC-11, AC-12, AC-13, the job parts of AC-14, AC-15, AC-16, and the determinism half of AC-22.

Done when: those criteria hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 8: Web API and wiring

```text
Read SPEC.md §11, §13, §14 and §15. Skim jobs.py and store.py.

Goal: the JSON API and a runnable app.

Build:
- `spylead/web.py`: an aiohttp application with:
  - every route of §11, the session-cookie scheme, the JSON and number rules, the error format, and the chart payloads;
  - the §13 headers on every response, including errors and static files;
  - listening on 127.0.0.1 only;
  - static files from `spylead/static/`, with a minimal placeholder index.html until prompt 9.
- `spylead/__main__.py`: take the database lock first (§15), then wire config, catalog, store, jobs (with the real process pool) and the web server. Shut down in the order of §15.

The web layer validates input and calls the jobs module and the store. It holds no analysis logic.

Tests, with aiohttp's test utilities and a stub jobs object:
- AC-17 and AC-18 in full;
- login success and failure, with the 1 s delay injected, and logout;
- every route's success and error paths, and 409 while a job runs;
- the state document's shape, with and without results;
- AC-21: a second process exits while the lock is held, a lock left by a killed process does not block a restart, and the shutdown order.

Do not start the app yourself.

Done when: those criteria hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 9: Web UI and PWA

```text
Read SPEC.md §11 (state document, chart payloads, error format), §12 and §13. Skim web.py.

Goal: the web UI for Edge on the PC and Safari on iPhone and iPad, installable as a PWA.

Build in `spylead/static/`, as §12 describes:
- index.html, app.js and style.css;
- manifest.webmanifest, and icons/ (192, 512 maskable, and the 180 px apple-touch icon), generated by a standard-library script kept in `scripts/`;
- locales/zh-TW.json and locales/en.json.

Use the vendored ECharts only. No framework, no build step, no requests to other hosts. Server data enters the page through textContent and DOM APIs only.

Design brief:
- a research panel, not a marketing page;
- calm, dense, and legible on a phone held at arm's length;
- charts first on small screens; the table scrolls inside its own container;
- status and reason readable without colour;
- the "no data yet", "missing key" and "stale" banners impossible to miss.

Tests (AC-19):
- the two locale files define the same keys and cover every status, outcome, job kind, phase, event kind and error code the backend can emit;
- the static files outside vendor/ contain no innerHTML, insertAdjacentHTML, document.write, script element without src, style attribute or inline event handler;
- the manifest is valid JSON with start_url and display, and its icon files exist as PNG of the stated sizes;
- the ECharts file matches its SHA-256.

Then give me a manual checklist covering:
- devices: Edge on the PC (installed as an app, light and dark, both languages), an iPhone at 375 px in portrait, and an iPad in both orientations through Tailscale Serve, added to the home screen;
- login, and Fetch Data from empty to charts;
- "already up to date", and the progress display;
- the N change with its confirmation, and the "in F" checkboxes;
- adding and removing a FRED ID;
- chart zoom by pinch and by the range buttons, the log axis, and tooltips;
- the stale banner and a missing key.

Done when: AC-19 holds, pytest passes, and the checklist is delivered.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 10: Operations and README

```text
Read SPEC.md §15, §16.3, §17 and §19. Skim __main__.py, jobs.py and bench.py.

Goal: make it operable.

Build:
- `scripts/install-autostart.ps1`, with `-Remove`, as §15 describes: the per-user logon task, the launcher in %LOCALAPPDATA%\spylead that waits for the project folder, and the Startup-folder fallback. It must work in Windows PowerShell 5.1.
- `run.bat`, which starts the app from the venv with config.toml.
- Check the startup order, shutdown, logging and lock against §15, and fix any gap.
- README.md for the owner:
  - what spylead does and does not do, in plain words;
  - prerequisites, and getting the FRED and Tiingo keys (§19);
  - install from pylock.toml, configure, and run for the first time;
  - the first Fetch Data, what "already up to date" means, and how long a compute takes;
  - reading the results: A, β, score, out-of-sample score, statuses, estimated points, and the one-sided and reference curves;
  - the PC: Edge, "Install this site as an app";
  - iPhone and iPad: Tailscale Serve on port 8443, add to home screen, and why Funnel or a public port must never be used;
  - autostart, the external drive's fixed letter, and backups;
  - the bench and the checks of §16.3;
  - the limitations of §17 in plain words.

Tests:
- what can be checked from Python without running install-autostart.ps1: it exists, names the task `spylead`, writes the launcher under %LOCALAPPDATA%, and has a -Remove path and a Startup-folder fallback;
- AC-21 still holds.

Done when: pytest passes, and the README takes me from a clean PC to the first charts without asking you anything.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 11: Final audit

```text
Read SPEC.md in full, then the whole codebase and test suite.

Goal: an audit. No new features.

Deliver:
1. A table of every acceptance criterion in SPEC.md §16.2: the tests that prove each one, or the manual step, or "gap".
2. Every place the code differs from SPEC.md, each marked as a code defect or a spec ambiguity.
3. Every path by which the analysis could use data released after the day it describes, or the evidence that there is none.
4. Every place where a key, the web token or the session value could reach a log, an event, a response or an error, or the evidence that there is none.
5. Dead code, unused dependencies, and anything that breaks a rule in CLAUDE.md.
6. Proposed changes to TODO.md: finished items to remove, and new deferred items to add.

Do not change code yet. After I review the report, fix the gaps I approve, one small diff at a time.
```
