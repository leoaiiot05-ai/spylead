# Prompt 8: Web API and wiring

## Steps

1. `spylead/pit.py`: `original_values(y, form)`, the way back from y to original units (`exp` for LOG), next to `TRANSFORMS` (§5.4, §18).
2. `spylead/jobs.py`: `candidates(rows, catalog)`, the order of §7.4 and §11, used by `Jobs` and by the web layer.
3. `spylead/web.py`, `create_app(config, store, jobs, catalog, clock, delay)`:
   - every route of §11; static files from `spylead/static/` with `GET /` serving `index.html`, without a session;
   - middlewares: errors as `{"error", "message"}` for `/api/*` (an unexpected exception is logged, redacted, and answered 500 INTERNAL_ERROR without its text); then the session check, 401 UNAUTHORIZED, on every `/api/*` route but login;
   - the §13 headers on every response, errors and static files included, through `on_response_prepare`: CSP, nosniff, no-referrer, and `Cache-Control: no-store` on `/api/*`, `no-cache` elsewhere;
   - the session: the hex HMAC-SHA256 of `spylead-session` keyed with the token, in an HttpOnly, SameSite=Strict cookie with a Max-Age of 30 days, Secure with `X-Forwarded-Proto: https`; tokens and cookies compared in constant time; a failed login waits 1 s (injected) and answers 401 LOGIN_FAILED;
   - bodies: `application/json` only (415), valid JSON (400 INVALID_JSON), exactly the expected fields, finite numbers that are not booleans (400 INVALID_FIELD);
   - input rules: N from 0.25 to 24 in steps of 0.25; `excluded` names KEPT series only; a FRED ID uppercased, then `[A-Z0-9_]{1,40}`, and not already a series' name or FRED ID (409 ALREADY_EXISTS); DELETE only for USER series (404 NOT_FOUND, 400 NOT_USER_SERIES); `JobRunning` becomes 409 JOB_RUNNING;
   - the state document of §11, from the store, the results payload and the jobs' progress;
   - the chart payloads of §11: points at release days and the one-sided and reference series in original units, integer days, values to 6 significant digits, null for NaN; 404 NO_DATA before a compute, NOT_FOUND for an unknown series, NO_F without F.
   - `spylead/static/index.html`: a placeholder until prompt 9.
4. `spylead/__main__.py` (§15):
   - after the config, the catalog and logging: `data/`, then `take_lock(data/spylead.lock)`, `msvcrt.locking` on Windows and `fcntl.flock` elsewhere. Another process's lock ends the process with exit code 3 before the database or the port is opened;
   - `serve(config, catalog, data_dir)`: the store with the catalog sync, the process pool (`max(1, cpu_count − 1)` workers), the jobs, the web app on `127.0.0.1` and the configured port; then it waits for Ctrl+C;
   - shutdown in §15's order through one exit stack: stop accepting requests (the runner's cleanup), interrupt the job, shut the pool down, close the store;
   - aiohttp's access log keeps its default format, which shows the request line and status, never a cookie or a body.
5. Tests:
   - `tests/test_web.py`, with aiohttp's `TestServer` and `TestClient` (a loopback server inside the test process), a temporary store, a stub jobs object, and an injected delay:
     - AC-17: every `/api/*` route without the session (401), with a wrong cookie, and after logout; 415, 400 INVALID_JSON, NaN, Infinity and booleans; the §13 headers on an API response, an error, a static file and a static 404; the cookie's attributes with and without `X-Forwarded-Proto: https`; no token, session value or key in any response, chart payload, log line or event across all routes;
     - AC-18: the SPY, series and F payloads (shapes, units, original units, integer days, nulls, 6 significant digits), 404 NO_DATA before a compute and NO_F without F;
     - login success, failure with the 1 s delay injected, and logout;
     - every route's success and error paths, and 409 JOB_RUNNING;
     - the state document with and without results.
   - `tests/test_main.py`, updated:
     - a valid config wires the lock and `serve` (patched), and logs the empty keys;
     - AC-21: a second `python -m spylead` exits with code 3 while the lock is held, before creating the database and without any socket (the audit hook of prompt 1); a lock left by a killed process does not block the next one; `serve` listens on 127.0.0.1 only and shuts down in §15's order.
   - `tests/test_pit.py`: `original_values`.
6. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **The cookie** is named `spylead_session`, with `Path=/`.
- **Bodies hold exactly their fields.** A missing or unknown field is 400 INVALID_FIELD, like config.toml's unknown keys. `{}` is the body of fetch and logout.
- **Python's json reads `NaN` and `Infinity`;** they reach the field checks, which answer INVALID_FIELD as §11 asks, not INVALID_JSON.
- **More error codes.** Besides §11's, the API answers 404 NOT_FOUND for an unknown `/api/*` path, 405 METHOD_NOT_ALLOWED, 413 PAYLOAD_TOO_LARGE (aiohttp's 1 MB limit), and 500 INTERNAL_ERROR. Prompt 9's locales cover them.
- **ALREADY_EXISTS** also covers a FRED ID that is already a catalog entry's: two series of one FRED ID would give step 5 identical members (prompt 4).
- **`DELETE /api/series/SPY`** is 400 NOT_USER_SERIES; **`GET /api/charts/series/SPY`** is 404 NOT_FOUND: SPY is not a candidate, and has its own chart.
- **The state document** follows §11, with these readings:
  - a candidate missing from the results, before the first compute or added since, is FETCH_FAILED when it has no data and a fetch error, PENDING otherwise; its step-3 fields are null;
  - `points` and `estimated_points` are the latest compute's counts of points (§3); without a compute, the stored observations;
  - `title` is the catalog's English title for a catalog series and FRED's title for a USER series; `title_zh`, an added field, is the catalog's Traditional Chinese title, null for USER series, for the zh-TW UI;
  - `selected` is null for a candidate that is not KEPT;
  - `f` is null before the first compute;
  - `job` and `last_job` are the jobs' `Progress` and `LastJob`.
- **Exit code 3** when another process holds the lock; 2 stays for a config or catalog error.
- **The lock file** is opened for appending and never truncated or deleted: only the OS lock counts (§15).
- **Web tests use a loopback server.** `TestServer` listens on 127.0.0.1 with a free port inside the test process: §16.1's "aiohttp's test utilities". No test reaches another host.

## Extension points

- **Prompt 9:** the static files replace the placeholder; the UI reads the state document and the chart payloads, and localizes `ERROR_CODES`, the statuses, outcomes, kinds, phases and event kinds by code.
- **Prompt 10:** `run.bat` and the autostart launcher start `python -m spylead --config config.toml`; exit code 3 tells a second start that one runs already.
- **A native client** (§18): the JSON API, with the same session cookie.

## Refactor seams

- **Validation** lives in the web layer only: `json_body` and the field checks; the jobs trust their input.
- **Presentation.** `state_document`, `series_chart` and `f_chart` are the only code that turns stored days into dates and y into original units.
- **Lifecycle.** `serve`'s exit stack is the one place that orders startup and shutdown.

## Deferred items (appended to TODO.md)

None.

### Static files

aiohttp answers a missing static file with an empty 404 response rather than an exception, so one static route at `/` would also answer unknown `/api/*` paths, outside the JSON error format. The app therefore registers each top-level file and folder of `spylead/static/` by name at startup, and `GET /` serves `index.html`. aiohttp's static content types come from Python's own table, not the Windows registry, so `.js` is `text/javascript` and `.webmanifest` `application/manifest+json` on the PC too.

## Result

- `.venv/bin/python -m pytest -q` (Linux VM, Python 3.13, the locked package versions): 618 passed, in about 39 s. New: 134 in `tests/test_web.py` (2 s), 6 in `tests/test_main.py` (one replaced), 3 in `tests/test_pit.py`.
- AC-17:
  - each of the 10 session routes answers 401 UNAUTHORIZED without a cookie, with a wrong, short or long one, and after logout, and starts no job;
  - 415 for every POST without JSON, 400 INVALID_JSON for four bad bodies, 400 INVALID_FIELD for NaN, ±Infinity, booleans, null, strings and lists;
  - the §13 headers on an API answer, API errors (401, 404, NO_F), static files (`/`, `index.html`, the ECharts file) and a static 404, with `no-store` and `no-cache` where §13 says;
  - the cookie is HttpOnly, SameSite=Strict, Max-Age 2592000 and Path=/, and Secure only behind `X-Forwarded-Proto: https`; tokens and cookies go through `hmac.compare_digest`;
  - across every route, error and aiohttp access-log line, and every stored event, neither the token, the session value nor either key appears (the login's Set-Cookie header carries the session value by design);
  - `serve` listens on 127.0.0.1 only (`test_main.py`).
- AC-18: the SPY chart (USD), a LOG and a LEVEL series chart, and the F chart, with points at release days, original units, integer days, null for NaN and 6 significant digits; 404 NO_DATA before a compute and for a series without a curve, NOT_FOUND for an unknown series and for SPY, NO_F before a compute and without F.
- AC-21: a second `python -m spylead` exits with code 3 while the lock is held, under the socket audit hook, leaving only `spylead.lock` in `data/`; a lock held by a killed child is free for the next process; `serve` shuts down in §15's order, both when it ends and when cancelled as Ctrl+C does; the WAL fallback is prompt 5's test.
- Login, logout, every route's success and error paths, 409 JOB_RUNNING, and the state document with and without results.
- Mutation checks, each applied to a copy of the repository outside it, with `tests/test_web.py` run against the copy. All 14 were caught (the constant-time one after its test was added):
  - headers only on successes, `no-cache` on the API, a cookie without HttpOnly, Secure always, the state route without a session, a plain string comparison;
  - no login delay, any content type, booleans as numbers, an exception's text in a 500;
  - 5 significant digits, charts in y, raw points at observation days, ALREADY_EXISTS by name only.
