# Prompt 3: Point-in-time core

## Steps

1. `spylead/pit.py`, pure (no I/O, clock, logging or aiohttp). Days are ints or int64 arrays, values float64.
   - Day helpers (§3):
     - `date_to_day(date)`, `day_to_date(day)`, and `parse_day(text)` for an ISO date;
     - `days_to_x(days)`: `day / 365.25`, with `DAYS_PER_YEAR`;
     - `years_before(day, years)`: the same month and day, 29 February becoming 28 February (§8.1). §5.2's 24 months are 2 years;
     - `today_at(instant)`: the instant's date in America/New_York through `zoneinfo` (§15). The caller's clock gives the instant.
   - `Observations(obs_day, value, release_day, estimated)`: a frozen dataclass of parallel arrays. Points (§5.3) have the same shape: the observations that became points, in release-day order. `take(index)` selects entries.
   - Translation (§4.1, §4.2, §5.1):
     - `fred_observations(payload)`: the decoded body of the first-release request. Each entry of its `observations` list gives `date`, `value`, and `realtime_start` as the release day. A `"."` value is skipped.
     - `tiingo_observations(rows)`: the decoded body of the price request. The date part of `date` is the trading day and the release day; `close` is the value. A null close is skipped.
     - `PayloadError(ValueError)` for a body of the wrong shape, a row that is not an object, a missing key, a wrong type (a JSON boolean is not a number), a value that is not a finite number, a date that is not a date, and two rows with the same date. Messages name the row by position and the key, never a value. Prompt 6 reports it as BAD_PAYLOAD.
   - §5.2:
     - `typical_lag(observations, default_lag_days)`;
     - `estimate_release_days(observations, lag)`: a lag above `lag + 60` gives the release day `obs_day + lag` and the flag; the other observations are unchanged.
   - §5.3: `known_points(observations)`, vectorized. Sort by release day, then observation day. Take the newest observation of each release day, and keep it when it is newer than every observation released on an earlier day.
   - §5.4:
     - `TRANSFORMS = {"LOG": np.log, "LEVEL": unchanged}`, and `FORMS`, its keys in order;
     - `y_values(values, form)`;
     - `has_invalid_values(values, form)`: some value has no finite y. In LOG form that is a value ≤ 0 (INVALID_VALUES).
2. One definition of each term:
   - `catalog.py` takes `FORMS` from `pit.py` instead of its own tuple. Its message stays "form must be LOG or LEVEL".
   - `smoothing.py` takes `DAYS_PER_YEAR` and `days_to_x` from `pit.py`. The arithmetic is the same, so its results are bit-identical.
3. `tests/test_pit.py` (AC-4), table-driven, with fixed seeds:
   - translation: hand-written FRED and Tiingo bodies with missing values; a table of bad bodies, each with its exact message;
   - days:
     - every day from 1896 to 2104 round-trips through its date and agrees with `date.toordinal`;
     - fixed days around 29 February in 2000 and 2024, and in 1900 and 2100, which are not leap years;
     - `parse_day` refuses 29 February in a common year;
     - `years_before`, `days_to_x`;
     - `today_at` on both sides of New York midnight in summer and winter time, on both clock-change days, and from a UTC+8 instant; a naive instant is refused;
   - typical_lag:
     - fewer than 12 recent observations (a quarterly series; 11 monthly) give the default, 12 or more the median;
     - the first day of the window, also from 29 February;
     - older observations do not count;
     - a median on a half day rounds up;
     - no observations give the default;
   - pre-archive observations get estimated release days and the flag. A lag of exactly `typical_lag + 60` keeps its release day, and flags already set stay;
   - the known-value rule:
     - a late release of an older observation is skipped;
     - two observations on one release day;
     - a release day whose newest observation is older than a point gives no point;
     - a seeded random property test against a literal walk of §5.3: strictly increasing release days and observation days; points exactly on the release days where the newest known observation changes, each holding that observation;
   - forms: y for LOG and LEVEL; a LOG series with a zero or negative value is invalid, a LEVEL series is not, and no NumPy warning is raised; the catalog accepts exactly `pit.FORMS`;
   - end to end: a FRED body with pre-archive observations, missing values and a shared release day becomes the expected points.
4. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **The 24-month window** of §5.2 holds the observation days after the same date two years before the latest observation, up to the latest: 24 monthly observations, not 25. 29 February counts back to 28 February, as in §8.1.
- **typical_lag is in whole days**: the median, rounded up. A median of an even count can fall on a half day, and an estimated release must fall on a day. Rounding up keeps the estimates on the late side.
- **"Exceeds" is strict**: a lag of exactly `typical_lag + 60` keeps its release day.
- **Estimation is a separate step** from translation: `typical_lag` and `estimate_release_days` take any observations. The store keeps `release_day` and `estimated` per row and never updates a row (§8.2, §10), so prompt 7 applies them to fetched observations before inserting them.
- **Points use the observations' type**, with their observation day and flag, so the estimated-points count (§5.2) comes from the points themselves.
- **The translations take the decoded JSON bodies.** All payload handling then lives in pit.py, and sources.py only maps PayloadError to BAD_PAYLOAD (prompt 6).
- **Strict payloads.** A value must be a finite number: FRED sends decimal strings, Tiingo JSON numbers, and Python's json module also decodes `NaN` and `Infinity`. A date must be a date. An observation date may not repeat, because the store keys observations by day (§10). Other fields, such as `realtime_end` and Tiingo's adjusted prices, are ignored.
- **A null Tiingo close is skipped** as missing: the prompt asks to skip missing values for both sources, and Tiingo has no other missing marker.
- **Tiingo dates** are the part before `T`, with no time-zone conversion: Tiingo stamps each trading day at midnight UTC.
- **A release day before its observation day** is accepted. Nothing in §5 forbids it, and it is no look-ahead.
- **An instant without a time zone** is refused: `astimezone` would read it as the PC's local time.
- **zoneinfo** reads New York's rules from the tzdata package once, at import: static data that §15 requires, not I/O in the sense of CLAUDE.md.
- **INVALID_VALUES** is "a value with no finite y", which in LOG form is exactly a value ≤ 0. A new form then brings its own domain (§18).

## Extension points

- **A new source** (§18): one more translation into `Observations`. Estimation and the known-value rule stay as they are.
- **A new form** (§18): one entry in `pit.TRANSFORMS`; catalog validation and INVALID_VALUES follow from it. The way back to original units (`exp` for LOG, §5.4) and §4.4's form rule for USER series belong next to it when prompt 7 or 8 needs them.
- **Prompt 7** composes: the translation, `typical_lag` and `estimate_release_days`, then the store. At compute time: `known_points`, `has_invalid_values`, `y_values`, then `smoothing.one_sided`. `today_at` and `years_before` give today, `analysis_start` and `fetch_start` (§8.1).
- **Prompt 8**: `day_to_date(day).isoformat()` gives the state document's dates.

## Refactor seams

- **Observations** is the one record type between the sources, the store and the compute.
- **PayloadError** is the one error for a malformed payload.
- **Days and x** have one definition, in pit.py, which smoothing.py uses. Forms have one, which catalog.py uses.

## Deferred items (appended to TODO.md)

- Re-date only the observations released on the archive's first vintage day, the series' earliest release day. See below.

### For the owner: §5.2 and late releases

- §5.2 re-dates every observation whose first release came more than `typical_lag + 60` days after its date. The rule cannot tell an observation older than the archive from one whose first release was simply late inside the archive, for example a release delayed by months.
- The late one then gets `observation day + typical_lag`, which is earlier than the day it was published: a look-ahead for that observation.
- pit.py implements §5.2 as written. A possible refinement, not built: observations older than the archive all carry the archive's first vintage day, the series' earliest release day, so the rule could apply only to observations released on that day. It is in TODO.md.

## Result

- `.venv/Scripts/python -m pytest -q`: 204 passed, 90 of them new in `tests/test_pit.py`, in about 9 s.
- AC-4, clause by clause:
  - first releases parsed and missing values skipped: the hand-written FRED body (`"."` twice) and Tiingo rows (a null close), and the end-to-end FRED body;
  - `typical_lag` from the last 24 months with 12 or more observations, the default otherwise: the median test, the quarterly series, and the window-edge table (11 against 12 observations, also from 29 February);
  - estimated release days and the flag: the pre-archive series, where the observation exactly 60 days past `typical_lag` keeps its day;
  - late older releases skipped and same-day releases resolved: three hand-made cases, and the property test against a literal walk of §5.3 on 6 seeds;
  - strictly increasing release days: the property test and the end-to-end test;
  - INVALID_VALUES: LOG with a zero and with a negative value; LEVEL never; no NumPy warning.
- `smoothing.py` gives bit-identical results: `test_smoothing.py` compares anchors and reference values exactly, and passes unchanged.
- Mutation checks, each applied to a temporary copy of `pit.py` outside the repository, with `tests/test_pit.py` run against it. All 19 were caught:
  - the window: its first day included, 3 years instead of 2, 11 observations enough;
  - the median: the mean instead, rounded down;
  - estimation: "exceeds" not strict, flags reset;
  - the known-value rule: the oldest observation of a release day instead of the newest, late older releases kept;
  - days: today in UTC, a naive instant accepted, no 29 February rule;
  - translation: missing values kept, a null close kept, the whole Tiingo timestamp parsed, booleans taken as numbers, non-finite values accepted, repeated dates accepted;
  - forms: NumPy warnings no longer suppressed.
