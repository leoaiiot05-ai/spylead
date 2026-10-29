# Prompt 6: Data sources

## Steps

1. `spylead/sources.py`: the Tiingo and FRED clients (SPEC.md §4.1, §4.2, §8.3, §8.4), and their pure translation helpers.
   - `SourceError(code, text)`: the one error type. `code` is TIMEOUT, CONNECTION_FAILED, HTTP_<status>, BAD_PAYLOAD or MISSING_KEY; `text` is English and holds no URL and no key. `str(error)` is `"<code>: <text>"`, the form of `last_fetch_error` (§10).
   - `redact(text)`: every `api_key=<value>` in a text becomes `api_key=***` (§13).
   - `Source`, the request rules of §8.4, shared by both clients:
     - the ClientSession is passed in, with the key, an injected `sleep` and an injected monotonic clock;
     - MISSING_KEY before any request when the key is empty;
     - a 30 s total timeout per request;
     - at most one request per 0.5 s for FRED (`interval`), none for Tiingo;
     - timeouts, connection errors and HTTP 5xx: 3 retries, after 2, 4 and 8 s;
     - HTTP 429: a wait of `Retry-After`, at most 60 s, then one retry;
     - other 4xx: no retry;
     - each request logged at DEBUG with its URL redacted, each retry at WARNING. Headers are never logged.
   - `Tiingo(session, key, sleep)`: the key goes in `Authorization: Token <key>`, never in a URL.
     - `end_day()`: the meta request; its `endDate` as a day;
     - `prices(first_day, last_day)`: the price request, translated by `pit.tiingo_observations`.
   - `Fred(session, key, sleep, monotonic)`: the key goes in the query string.
     - `series(series_id) -> FredSeries`: the metadata request; title, units, frequency_short, last_updated (as given), observation_start and observation_end of `seriess[0]`;
     - `observations(series_id, first_day)`: the initial-release request of §4.2, translated by `pit.fred_observations`.
   - Pure helpers, apart from the network code: `tiingo_end_day(body)`, `fred_series(body)`, and `decode`. A `pit.PayloadError` from any translation becomes BAD_PAYLOAD.
2. `tests/test_sources.py`, with an injected fake session that records each request and plays scripted answers, hand-written JSON samples, an injected sleep and clock, and a guard that fails any test that opens a socket:
   - each request's URL, parameters in §4's order, headers and timeout;
   - the up-to-date inputs: `endDate` and `last_updated`;
   - retries on 5xx, timeouts and connection errors, with the delays 2, 4 and 8 s, then the error;
   - `Retry-After`: its seconds, the 60 s cap, a missing or unreadable value, one retry only;
   - no retry on 400, 401, 403 and 404;
   - FRED's pacing, and none for Tiingo;
   - MISSING_KEY without any request, for both sources;
   - bad payloads: not JSON, the wrong shape, a bad `endDate`, no `seriess`;
   - AC-17, redaction part: every error text and every log line of every scenario free of both keys and of URLs in error texts; the DEBUG line carries the redacted FRED URL; `redact` on its own.
3. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **CONNECTION_FAILED.** §8.4 retries connection errors, but the codes of the prompt name none for one that outlasts the retries. TIMEOUT would tell the owner that a source was slow when it could not be reached at all, for example with the PC offline. Every other `aiohttp.ClientError`, such as a body cut off while reading, counts as a connection error. Prompt 9's locales cover the code.
- **One retry budget per kind.** A request has 3 retries for timeouts, connection errors and 5xx, and separately one retry after a 429. A 429 after the first is HTTP_429.
- **Retry-After** is read as delta-seconds, the integer form. A missing value, an HTTP date or anything else waits the cap, 60 s, which respects the limit whatever the source meant. A negative value waits 0 s.
- **Success is any 2xx.** Its body must decode as JSON; anything else is BAD_PAYLOAD, without retry: a source that sends a malformed body sends it again.
- **The pacing** counts every attempt, retries included. The retry delays already exceed 0.5 s.
- **Errors are raised without their cause** (`from None`). aiohttp's exceptions can carry the request URL, and with it FRED's key, into a logged traceback.
- **BAD_PAYLOAD keeps pit's detail**, such as `observation 3: value must be a finite number`: pit names rows and keys, never values (prompt 3).
- **Unknown FRED series.** FRED answers an unknown `series_id` with HTTP 400. The client reports HTTP_400; prompt 7 turns it into UNKNOWN_SERIES for ADD_SERIES.
- **Logged URLs** are built with `urllib.parse.urlencode`, then redacted. `yarl`, which aiohttp uses, is not an approved dependency to import directly.
- **No socket in tests.** The fake session stands in for `aiohttp.ClientSession.get`: aiohttp's test server would open a loopback connection, which CLAUDE.md rules out.

## Extension points

- **A new source** (§18): one more `Source` subclass with its `name`, `interval` and headers, and a translation in `pit.py`.
- **Prompt 7** creates one ClientSession per job, passes it to `Tiingo` and `Fred` with the config's keys and `asyncio.sleep`, and stores `str(error)` as `last_fetch_error`. It turns HTTP_400 from `Fred.series` into UNKNOWN_SERIES, and `frequency == "A"` into UNSUPPORTED_FREQUENCY (§4.4).
- **Prompt 8:** `redact` serves any other text that could hold a FRED URL, such as aiohttp's access log lines.

## Refactor seams

- **Request rules.** `Source.get_json` is the only network code: timeouts, pacing, retries and logging live there once.
- **Translation.** The clients only decode JSON; every payload rule is in `pit.py` or in this module's pure helpers.

## Deferred items (appended to TODO.md)

None.

## Result

- `.venv/bin/python -m pytest -q` (Linux VM, Python 3.13, the locked package versions): 420 passed, 91 of them new in `tests/test_sources.py`, in about 0.3 s. No test opens a socket: a guard on `socket.socket.connect`, `connect_ex` and `socket.create_connection` fails any test that tries.
- AC-12 (requests): the delays 2, 4 and 8 s, the fourth failure, Retry-After and its cap, one retry for 429, none for 400 to 404, FRED's pacing.
- AC-17 (redaction): in 32 scenarios (each request's success, and seven kinds of failure for each request), neither key appears in an error text, its traceback or a log line, and no error text holds a URL. One scenario feeds aiohttp an error message that contains both keys.
- Mutation checks, each applied to a copy of the repository outside it, with `tests/test_sources.py` run against the copy. All 12 were caught:
  - Tiingo's key in the URL, the request logged unredacted, a redaction that stops early, aiohttp's error chained into the SourceError, the payload error kept as the cause;
  - no FRED pacing, 4xx retried, 429 retried more than once, Retry-After uncapped, other retry delays, a 60 s timeout;
  - the key checked only after a request.
