# spylead specification (v1)

This is the agreed behaviour. Behaviour marked **intentional** is the owner's explicit choice and must not be changed without asking, even where it looks like a bug.

## 1. Purpose and scope

spylead runs on the owner's Windows 11 PC. It:

1. downloads SPY daily closing prices and a catalog of US leading indicators, 30 years back from the first fetch plus a 3-year warm-up (§8);
2. turns every series into a point-in-time series: each value sits on the day it was first published (§5);
3. smooths every series with a one-sided robust cubic smoothing spline, the owner's "L1 smoothing spline": L1 fit, L2 roughness (§6);
4. for each indicator, finds the shift A in calendar days that makes `F_L1(X, A) = SPYL1(X) / IND_L1(X + A)^β` closest to a horizontal line, and keeps the indicators whose best A is negative, that is, those that lead SPY (§7.2, §7.3);
5. finds the shifts `b_i ≤ 0` that make `F(X) = SPYL1(X) / Π LEADL1_i(X + b_i)^β_i` closest to a horizontal line (§7.4);
6. serves a web UI, installable as a PWA, with the charts and a [Fetch Data] button. It is used from the PC in Edge and from iPhone and iPad in Safari through Tailscale Serve (§12).

In scope: one user, one process, research display only. spylead never trades and never places orders.

Out of scope: everything in `TODO.md`. Notably projections past today, native apps, scheduled fetches, and data sources other than Tiingo and FRED.

## 2. Architecture

One Python process. aiohttp serves the API and the static UI on one asyncio loop. Heavy numerical work runs in a process pool so the loop stays responsive.

```
Edge on the PC (http://127.0.0.1:8090)      Safari on iPhone/iPad (https, Tailscale Serve :8443)
                    \                          /
          web (aiohttp, 127.0.0.1 only: JSON API + static PWA)
                              |
          jobs (one job at a time: fetch, compute, add/remove series)
           |            |                     \
       sources       store (SQLite)        process pool
   (Tiingo, FRED)                     pit, smoothing, analysis (pure)
```

Starting layout. Deviate only with a reason stated in the plan.

| Path | Responsibility |
|---|---|
| `spylead/__main__.py` | CLI, wiring, lifecycle (§14, §15) |
| `spylead/config.py` | Load and validate `config.toml` (§14) |
| `spylead/catalog.py` | Load and validate `catalog.toml` (§4.3) |
| `spylead/pit.py` | Point-in-time series (§5). Pure. |
| `spylead/smoothing.py` | Robust smoothing spline, N, one-sided series, reference fit (§6). Pure. |
| `spylead/analysis.py` | LAD fits, score, steps 3 to 5, out-of-sample check, statuses (§7). Pure. |
| `spylead/sources.py` | Tiingo and FRED clients, and pure translation helpers (§4, §8.4) |
| `spylead/store.py` | SQLite persistence (§10) |
| `spylead/jobs.py` | Job runner: fetch, compute, add and remove series, progress (§8, §9) |
| `spylead/web.py` | aiohttp app (§11) |
| `spylead/bench.py` | Timing on production-size synthetic data (§9) |
| `spylead/static/` | `index.html`, `app.js`, `style.css`, `manifest.webmanifest`, `icons/`, `locales/zh-TW.json`, `locales/en.json`, `vendor/echarts/` (§12) |
| `catalog.toml` | The candidate list (§4.3). Committed. |
| `scripts/install-autostart.ps1` | Start the app at Windows logon (§15) |
| `run.bat` | Start the app from the venv in a console |
| `tests/` | Tests, plus fakes for the sources, the clock and the process pool |

## 3. Terms

| Term | Meaning |
|---|---|
| day | A calendar date. Internally an integer: days since 1970-01-01. |
| today | The current date in America/New_York. |
| x | A day expressed in years for fitting: `day / 365.25`. |
| observation | One value of a series for one observation date: FRED's `date`, or a SPY trading date. |
| release day | The day an observation's value was first published (§5). |
| point | The release day and value of a known observation (§5.3). |
| y | A point's fitted value: `ln(value)` for LOG form, `value` for LEVEL form (§5.4). |
| one-sided series | The smoothed series in which the value on day d uses only points released by the last month end before d (§6.4). |
| s(d), r_i(d) | One-sided values of SPY and of indicator i, in the fitted scale (y). |
| SPYL1 | `exp(s)`: smoothed SPY in USD. |
| IND_L1, LEADL1_name | An indicator's one-sided series in original units: `exp(r)` for LOG form, `r` for LEVEL form. `LEADL1_` names one that step 4 kept. |
| A, b_i | Shifts in calendar days. Negative means the indicator's value from \|A\| days earlier is paired with SPY: the indicator leads. |
| β | An indicator's fitted exponent in F (§7.1). |
| score | Flatness: mean absolute deviation of `ln F` from its median (§7.1). Lower is flatter. |
| N | Smoothness: the most inflection points a fitted spline may have per year (§6.3). Default 2. |
| fetch_start, analysis_start | Fixed at the first fetch: `analysis_start` is that day minus 30 years; `fetch_start` is `analysis_start` minus 3 years (warm-up). |

## 4. Data sources and catalog

### 4.1 SPY

- Source: Tiingo end-of-day API. The raw `close`, not the adjusted close. **Intentional**: closing prices without dividends, so a stored value never changes.
- Authentication: header `Authorization: Token <key>`. The key never goes in a URL.
- Meta: `GET https://api.tiingo.com/tiingo/daily/SPY`. Its `endDate` is the latest trading day available.
- Prices: `GET https://api.tiingo.com/tiingo/daily/SPY/prices?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&format=json`. Each row has `date` (ISO timestamp; use its date part) and `close`.
- The release day of a SPY close is its trading day.
- FRED is not used for SPY: it has no SPY ETF prices, and its S&P 500 index series carries only 10 years.
- spylead makes at most two Tiingo requests per fetch, far inside the free plan's hourly and daily limits.

### 4.2 FRED and ALFRED

- Series metadata: `GET https://api.stlouisfed.org/fred/series?series_id=<id>&api_key=<key>&file_type=json`. Use `seriess[0]`: `title`, `units`, `frequency_short`, `last_updated`, `observation_start`, `observation_end`.
- First releases: `GET https://api.stlouisfed.org/fred/series/observations?series_id=<id>&api_key=<key>&file_type=json&realtime_start=1776-07-04&realtime_end=9999-12-31&output_type=4&observation_start=<YYYY-MM-DD>&limit=100000`.
  - `output_type=4` returns each observation's initial release only.
  - Each observation carries `date`, `value` (a string; `"."` means missing) and `realtime_start`, the day that value was first published.
- FRED requires the key in the query string. No URL that contains it is ever logged or stored (§13).

### 4.3 Catalog

`catalog.toml` holds the curated candidates. The owner edits it; spylead loads it at startup. Each entry has `name`, `fred_id`, `form` (`LOG` or `LEVEL`, §5.4), `default_lag_days` (§5.2), `title_en` and `title_zh`. Names are unique and match `[A-Za-z0-9_]{1,40}`.

The initial catalog:

| name | FRED ID | Indicator | form | default lag (days after the FRED date) |
|---|---|---|---|---|
| M2 | M2SL | M2 money stock | LOG | 55 |
| Spread_CCC | BAMLH0A3HYC | ICE BofA CCC & lower high-yield option-adjusted spread | LEVEL | 1 |
| PMI_Philly | GACDFSA066MSFRBPHI | Philadelphia Fed manufacturing survey, current general activity | LEVEL | 18 |
| PMI_Philly_Future | GAFDFSA066MSFRBPHI | Philadelphia Fed manufacturing survey, future general activity (six months ahead) | LEVEL | 18 |
| Spread_10Y2Y | T10Y2Y | 10-year minus 2-year Treasury | LEVEL | 1 |
| Spread_10Y3M | T10Y3M | 10-year minus 3-month Treasury | LEVEL | 1 |
| Spread_BAA10Y | BAA10Y | Moody's Baa corporate minus 10-year Treasury | LEVEL | 1 |
| Spread_HY | BAMLH0A0HYM2 | ICE BofA US high-yield option-adjusted spread | LEVEL | 1 |
| Claims | ICSA | Initial jobless claims | LOG | 5 |
| Permits | PERMIT | New private housing units authorized by building permits | LOG | 48 |
| Hours_Mfg | AWHMAN | Average weekly hours, manufacturing | LOG | 35 |
| Orders_Capex | NEWORDER | New orders, nondefense capital goods excluding aircraft | LOG | 56 |
| Sentiment | UMCSENT | University of Michigan consumer sentiment | LOG | 60 |
| NFCI | NFCI | Chicago Fed National Financial Conditions Index | LEVEL | 5 |
| STLFSI | STLFSI4 | St. Louis Fed Financial Stress Index | LEVEL | 6 |
| VIX | VIXCLS | CBOE volatility index | LOG | 1 |
| FedFunds | DFF | Effective federal funds rate | LEVEL | 1 |
| OECD_CLI | USALOLITOAASTSAM | OECD composite leading indicator, amplitude adjusted | LOG | 44 |

- Rates, spreads, diffusion indices and stress indices are LEVEL: they can be zero or negative. Positive quantities and indices are LOG. **Intentional.**
- FRED keeps only the latest 3 years of the ICE BofA series, so Spread_CCC and Spread_HY are expected to show INSUFFICIENT_HISTORY. They stay in the catalog, and Spread_BAA10Y represents credit spreads. **Intentional.**
- ISM PMI is not available from FRED, so the Philadelphia Fed survey replaces it. The Conference Board LEI has no free source and is not in the catalog (TODO.md).

### 4.4 Series the owner adds

- The owner adds any FRED series ID in the UI (§11). It is stored in the database, not in `catalog.toml`, with origin `USER`.
- Its name is the FRED ID. Its form is LEVEL when the FRED `units` contain "Percent" or any stored value is zero or negative, otherwise LOG. Its default lag is 30 days.
- Annual series are refused (UNSUPPORTED_FREQUENCY): they give too few points for one-sided fits.
- The owner can remove a USER series in the UI. Catalog series are changed only in `catalog.toml`. A catalog series removed from the file is deleted from the database, with its observations and curves, at the next start.

### 4.5 Licences

FRED, ICE and Tiingo data are for the owner's internal use only. spylead shows them only on the owner's own devices and never publishes them.

## 5. Point-in-time series

### 5.1 First releases

- For FRED series, each observation's value is its initial release and its release day is its `realtime_start` (§4.2). Missing values (`"."`) are skipped.
- For SPY, the release day is the trading day.
- **Intentional**: first releases never change, so a stored observation is never updated (§8.2).

### 5.2 Estimated release days

FRED's archive does not reach back to every observation. An observation older than the archive appears with the archive's first vintage date as its release day, which can be years after its date. Such observations get an estimated release day:

- `typical_lag` is the median of `release day − observation day` over the observations dated within the 24 months before the series' latest observation, when there are at least 12 of them. Otherwise it is the series' `default_lag_days`.
- An observation whose `release day − observation day` exceeds `typical_lag + 60` gets the release day `observation day + typical_lag`, and is flagged as estimated.
- The UI shows how many points of each series are estimated.

### 5.3 The known-value rule

Points are built by walking observations in release-day order, and within one release day in observation-day order:

- An observation becomes a point only if its observation day is later than that of every point before it. A late first release of an older observation is skipped (§17).
- When several observations share a release day, the one with the latest observation day becomes the point.
- So release days are strictly increasing, and each point holds the value of the newest observation known on its day.

### 5.4 Forms

- LOG: `y = ln(value)`. A value that is zero or negative makes the series INVALID_VALUES (§7.6).
- LEVEL: `y = value`.
- SPY is LOG.
- Fitting uses y. Charts show original units: `exp(y)` for LOG.

## 6. Smoothing

### 6.1 The fit

For points `(x_1, y_1) … (x_n, y_n)` with strictly increasing x in years, the fit is the natural cubic spline g with knots at the x_i that minimizes

```
Σ |y_i − g(x_i)|  +  λ ∫ g''(x)² dx
```

This is a robust smoothing spline: L1 fit, L2 roughness. **Intentional.**

### 6.2 Algorithm

- **Weighted solve.** The weighted problem `Σ w_i (y_i − g_i)² + λ ∫ g''²` is solved in Reinsch form (Green & Silverman, *Nonparametric Regression and Generalized Linear Models*, 1994, ch. 2):
  - with `h_i = x_{i+1} − x_i`, build the tridiagonal matrices Q (n × (n−2)) and R ((n−2) × (n−2));
  - solve `(R + λ Qᵀ W⁻¹ Q) γ = Qᵀ y` with a banded Cholesky: `scipy.linalg.solveh_banded`, two off-diagonals;
  - then `g = y − λ W⁻¹ Q γ`. γ holds the second derivatives at the interior knots; it is 0 at both ends.
- **Equivalence check.** Tests compare this solve with `scipy.interpolate.make_smoothing_spline`, which minimizes the same weighted objective with unsquared weights. Production code does not use `make_smoothing_spline`: the banded solve is about 70 times faster, and the one-sided refits need that.
- **IRLS for the L1 fit.**
  - Start from `w_i = 1`, or from the weights passed in.
  - Repeat: solve; `r_i = |y_i − g_i|`; `δ = max(1e-4 · median(r), 1e-12)`; `w_i = 1 / max(r_i, δ)`.
  - Stop when the objective `Σ r_i + λ ∫ g''²` falls by less than `1e-6` of its value, or after 50 solves.
- **Roughness.** `∫ g''² = Σ h_i (γ_i² + γ_i γ_{i+1} + γ_{i+1}²) / 3`.
- **Value at any x.**
  - Inside `[x_1, x_n]`: the natural cubic spline formula from g and γ.
  - Outside: the linear extension with the end value and end slope.
  - Slope at `x_n`: `(g_n − g_{n−1}) / h_{n−1} + h_{n−1} γ_{n−1} / 6`. Slope at `x_1`: `(g_2 − g_1) / h_1 − h_1 γ_2 / 6`.
- **Inflection points.** Count the sign changes in γ_1 … γ_n after dropping the entries with `|γ| ≤ 1e-9 · max|γ|`.

### 6.3 The smoothness parameter N

- N is the most inflection points a fitted spline may have per year. **Intentional.**
- The target for a fit is `T = floor(N · (x_n − x_1))`.
- λ is found by bisection on `log10 λ` over [−8, 8]:
  - keep `lo` and `hi` with `count(10^lo) > T ≥ count(10^hi)`;
  - stop after 30 halvings, or when `hi − lo < 0.005`;
  - the result is `λ = 10^hi`.
- Limits: if `count(10^8) > T`, λ is `10^8`, with a LAMBDA_AT_LIMIT event. If `count(10^-8) ≤ T`, λ is `10^-8`.
- IRLS weights carry over from one bisection step to the next.
- N ranges from 0.25 to 24 in steps of 0.25, with a default of 2. It is stored in the database and shared by every series. The owner changes it in the UI, which recomputes everything.

### 6.4 One-sided series

**Intentional**: the analysis uses no data published after the day it describes.

- **Refit days.** `m_k` is the last calendar day of each month. Refits start at the first month end at least 3 years after the series' first point, and end at the last month end before today.
- **Refit k.**
  - It fits the points with release day `≤ m_k`, with λ from §6.3.
  - Its bisection starts from the previous refit's `log10 λ ± 1`. It falls back to [−8, 8] when that bracket does not satisfy the invariant.
  - A refit with fewer than 8 points produces no anchor.
- **Anchor k** is `(x_k, v_k, s_k)`: the last point's x, the fitted value there, and the end slope.
- **One-sided value.**
  - For a day d with `m_k < d ≤ m_{k+1}`, or `d > m_K` for the last refit K, the value is `v_k + s_k · (d / 365.25 − x_k)`.
  - The value is undefined before the first anchor.
  - A month whose refit has no anchor keeps the previous anchor.
- The value on day d therefore depends only on points released on or before the last month end before d. Within a month, the value follows the previous month end's fit along its end slope (§17).

### 6.5 Reference fit

- One fit with all points and the same N, evaluated daily inside `[x_1, x_n]`.
- It is display only, drawn as a dashed line. The analysis never uses it.

## 7. Lead analysis

### 7.1 Formulas

- `ln F(d) = s(d) − Σ β_i r_i(d + b_i)`.
  - For a LOG indicator this divides by `IND_L1^β`; for a LEVEL indicator, by `exp(β · IND_L1)`.
  - **Intentional**: fitted exponents rather than a plain ratio. They handle indicators that move against SPY, indicators that cross zero, and indicators in arbitrary units.
- **β by least absolute deviations.**
  - β_i and an intercept c minimize `Σ |s(d) − c − Σ β_i r_i(d + b_i)|` over the evaluation points.
  - The fit is IRLS, batched over candidate shifts: start with `w = 1`; solve the weighted normal equations; `δ = max(1e-6 · median|res|, 1e-12)`; stop when the objective changes by less than `1e-8` relative, or after 100 iterations.
- **score** = the mean over the evaluation points of `|ln F(d) − median(ln F)|`. **Intentional**: an L1 distance to the flattest horizontal line. The intercept does not affect it.
- **slope** = the ordinary least-squares slope of `ln F` against `d / 365.25`, reported as `100 · (exp(slope) − 1)` percent per year. Information only.
- **Evaluation points.**
  - Searches use every 7th day of the window, counting back from its last day.
  - Reported results use every day of the window.
- Every computation is in float64 and deterministic.

### 7.2 Step 3: one indicator at a time

- **Shifts.** `A = −730 … +730` (`F_L1(X, A) = SPYL1(X) / IND_L1(X + A)^β`). **Intentional.**
- **Window.** `W3 = [max(s_first, r_first + 730), min(s_last, r_last − 730)]`, where `_first` and `_last` are the first and last days with a defined one-sided value.
  - **Intentional**: the same window serves every A, so scores compare.
  - A window shorter than 10 years gives INSUFFICIENT_HISTORY.
- **Search.**
  - For every A, fit β and c by LAD and compute the score.
  - A* is the A with the lowest score. Ties, a relative difference of at most 1e-12, go to the smaller |A|.
  - Process the shifts in chunks, so that no array exceeds 64 MB.
- **Report.** A*, β, the score over every day of the window, and the slope.

### 7.3 Step 4: what is kept

- `−730 < A* < 0`: KEPT. The series is named `LEADL1_<name>`.
- `A* ≥ 0`: NOT_LEADING.
- `A* = −730` or `A* = +730`: AT_BOUNDARY, meaning no lead was found in range.
- A KEPT series whose data has stopped is DISCONTINUED (§7.6). It is shown, but it never enters F.

### 7.4 Step 5: the combination F

- **Members.** KEPT series that the owner has not unchecked in the UI. New KEPT series are included by default. With no members there is no F (NO_MEMBERS).
- **Shifts.** SPY's shift `a = 0`, and `b_i ∈ [−730, 0]`. **Intentional.**
- **Window.** `W5 = [max(s_first, max_i r_i_first + 730), min(s_last, min_i r_i_last)]`. A window shorter than 10 years gives no F (INSUFFICIENT_OVERLAP).
- **Coordinate descent.**
  - **Starts.** Two starts. The first is `b_i = A*_i` from step 3. The second draws each b_i uniformly from the integers in [−730, 0] with `numpy.random.default_rng(0)`.
  - **Stages.** Three stages of (step, radius): (28, 730), (7, 28), (1, 7).
  - **Passes.** Within a stage, repeat passes over the members until a pass changes nothing. Members go in catalog order, then USER series by name.
  - **Member update.** For member i, the candidates are `b_i + k · step` within ± radius, clipped to [−730, 0]. Take the candidate with the lowest score if it beats the current score by more than 1e-12 relative. Ties go to the smaller |b_i|.
  - **Choice.** The better of the two starts wins. Ties go to the smaller `Σ|b_i|`.
- **Report.**
  - Each b_i and β_i; c; the score over every day of W5; the slope.
  - `F(d) = exp(ln F(d) − median ln F)`, so that its median is 1.
  - `composite(d) = exp(c + Σ β_i r_i(d + b_i))` in USD, to draw against SPYL1.
- With several members, different sets of shifts can fit almost equally well. The result is the optimizer's best, not a unique "true" lead (§17).

### 7.5 Out-of-sample check

- The search and fit are repeated on the first 70% of the window. The split day is the window's first day plus `floor(0.7 · length)`.
- The resulting shifts and β are scored on the remaining 30%, over every day, with the median taken over those days.
- This runs for step 3, per candidate, and for step 5, starting from the full-window b.
- The out-of-sample score is shown next to the full-window score. **Intentional**: it never changes A*, b, β, or which series are kept.

### 7.6 Statuses

Each candidate has exactly one status, the first that applies:

| Status | When |
|---|---|
| PENDING | No data stored yet |
| FETCH_FAILED | The last fetch failed and no data is stored |
| INVALID_VALUES | LOG form with a value that is zero or negative |
| INSUFFICIENT_HISTORY | W3 shorter than 10 years |
| AT_BOUNDARY | A* = ±730 |
| NOT_LEADING | A* ≥ 0 |
| DISCONTINUED | Would be KEPT, but the latest release day is earlier than `today − (3 × the median spacing of release days over the last 2 years + 14)` |
| KEPT | Otherwise |

- A failed fetch of a series that already has data does not change its status. The UI shows the error next to it (`last_fetch_error`).
- F has its own status: OK, NO_MEMBERS or INSUFFICIENT_OVERLAP.

### 7.7 Numerical notes

- NumPy 2 changed batched `numpy.linalg.solve`: pass stacked right-hand sides with an explicit trailing axis (`rhs[..., None]`) and drop it afterwards.
- Worker functions are top-level and take plain arrays, because Windows starts pool workers by spawning.

## 8. Fetch Data

### 8.1 First fetch

When the database has no `fetch_start`:

- `analysis_start` = today minus 30 years, on the same month and day (29 February becomes 28 February). `fetch_start` = `analysis_start` minus 3 years. Both are stored and never change. **Intentional.**
- Download SPY from `fetch_start` to today, and every candidate's first releases with `observation_start = fetch_start`.
- Then compute (§9). The charts refresh when the compute finishes.

### 8.2 Later fetches and "up to date"

For each series:

- **SPY.** Read `endDate` from Tiingo's meta. If it is not later than the latest stored day, SPY is up to date. Otherwise, request prices from 7 days before the latest stored day to `endDate`, and insert only the days not yet stored.
- **FRED.** Read `last_updated` from the series metadata. If it equals the stored value, the series is up to date. Otherwise, request first releases with `observation_start` = the latest stored observation day minus 400 days. Insert only the observation days not yet stored, then store `last_updated`.
- A candidate with no stored rows, because it is new to the catalog or failed before, is fetched from `fetch_start`.
- Stored rows are never updated (§5.1).

Outcomes:

- When every series is up to date: outcome UP_TO_DATE. Nothing else happens; no compute runs.
- Otherwise: outcome UPDATED, with a count per series, then a compute.

### 8.3 Failures

- A timeout, an HTTP error or a bad payload fails that series only. It keeps its data, its `last_fetch_error` is set, an event records it, and the job continues.
- If SPY has no data at all after the fetch, the job ends FAILED (NO_SPY_DATA) without a compute.
- A missing API key fails every series of that source with MISSING_KEY, and the UI says which key is missing.

### 8.4 Requests

- One aiohttp ClientSession per job, and a 30 s timeout per request.
- At most one FRED request per 0.5 s.
- Retries:
  - timeouts, connection errors and HTTP 5xx: 3 retries, after 2, 4 and 8 s;
  - HTTP 429: wait `Retry-After`, at most 60 s, then retry once;
  - other 4xx: no retry.
- Sleeping goes through an injected function, so tests do not wait.

## 9. Jobs and compute

- **One job at a time.** Kinds: FETCH, RECOMPUTE (after an N change), REFIT_F (after a selection change), ADD_SERIES and REMOVE_SERIES. A request for another job while one runs gets 409 JOB_RUNNING.
- **Progress.** The job reports its phase (FETCH, SMOOTH, STEP3, STEP5, SAVE), done and total counts, and the current series. It appears in the state document.
- **Compute.**
  1. Load the points of every series.
  2. Build the one-sided series and the reference fits in parallel.
  3. Run step 3 per candidate in parallel.
  4. Apply step 4.
  5. Run step 5 and both out-of-sample checks.
  6. Save the curves and the results in one transaction.
  7. Record COMPUTE_FINISHED.
- **Process pool.** `concurrent.futures.ProcessPoolExecutor` with `max(1, os.cpu_count() − 1)` workers.
- **REFIT_F** reruns step 5 from the stored one-sided curves.
- **ADD_SERIES.**
  - Read the FRED metadata. An unknown ID ends with UNKNOWN_SERIES; an annual series with UNSUPPORTED_FREQUENCY.
  - Otherwise, store the series, fetch it from `fetch_start`, and compute.
- **REMOVE_SERIES** deletes a USER series with its observations and curves, then runs REFIT_F.
- **Shutdown.** A job that a shutdown interrupts leaves the previous results in place. Observations already inserted stay. The job is recorded as INTERRUPTED.
- **Budget.** A full compute on the catalog finishes within 10 minutes on the owner's PC. `python -m spylead.bench` times one on production-size synthetic data: SPY daily for 33 years, plus series of the catalog's frequencies (§16.3).

## 10. Persistence

SQLite `data/spylead.db`, next to the config file.

- `PRAGMA journal_mode = WAL` with `PRAGMA synchronous = NORMAL`. If WAL cannot be enabled, which can happen on some external drives, fall back to `DELETE` with a warning.
- Busy timeout 1000 ms. `PRAGMA user_version = 1`.

```sql
CREATE TABLE series (
  name             TEXT PRIMARY KEY,
  origin           TEXT NOT NULL,      -- SPY | CATALOG | USER
  source           TEXT NOT NULL,      -- TIINGO | FRED
  source_id        TEXT NOT NULL,      -- SPY, or the FRED ID
  form             TEXT NOT NULL,      -- LOG | LEVEL
  default_lag_days INTEGER NOT NULL,
  title            TEXT,
  units            TEXT,
  frequency        TEXT,
  last_updated     TEXT,               -- FRED's last_updated, as given
  last_fetch_at    TEXT,               -- UTC ISO 8601
  last_fetch_error TEXT,               -- error code and English text; never a URL or a key
  added_at         TEXT NOT NULL
);

CREATE TABLE observations (
  series      TEXT    NOT NULL,
  obs_day     INTEGER NOT NULL,
  value       REAL    NOT NULL,
  release_day INTEGER NOT NULL,
  estimated   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (series, obs_day)
) WITHOUT ROWID;

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,              -- fetch_start | analysis_start | inflections_per_year | f_excluded (JSON list)
  value TEXT NOT NULL
);

CREATE TABLE curves (                  -- latest compute only
  series        TEXT PRIMARY KEY,
  rt_first_day  INTEGER NOT NULL,
  rt            BLOB    NOT NULL,      -- float64 little-endian, one per day, NaN = undefined
  ref_first_day INTEGER NOT NULL,
  ref           BLOB    NOT NULL
);

CREATE TABLE results (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  computed_at TEXT NOT NULL,
  payload     TEXT NOT NULL            -- JSON: per-candidate step 3 and 4 results, F (§11)
);

CREATE TABLE events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,               -- UTC ISO 8601
  level   TEXT NOT NULL,               -- INFO | WARNING | ERROR
  kind    TEXT NOT NULL,
  series  TEXT,
  message TEXT NOT NULL
);
```

- At startup, the SPY row and the catalog rows are upserted from `catalog.toml`, and catalog rows no longer in it are deleted (§4.4).
- The selection is stored as the excluded names, so new KEPT series are included by default.
- Event kinds:
  - fetching: FETCH_STARTED, FETCH_FINISHED, UP_TO_DATE, SERIES_UPDATED, SERIES_FAILED;
  - computing: COMPUTE_FINISHED, STATUS_CHANGED, LAMBDA_AT_LIMIT, INVALID_VALUES;
  - owner actions: SETTING_CHANGED, SELECTION_CHANGED, SERIES_ADDED, SERIES_REMOVED;
  - job ends: JOB_FAILED, JOB_INTERRUPTED.

## 11. Web API

| Method and path | JSON body | Result |
|---|---|---|
| `POST /api/login` | `{"token": "..."}` | 204 and the session cookie. On mismatch: 401 LOGIN_FAILED after a 1 s delay. |
| `POST /api/logout` | `{}` | 204, cookie cleared |
| `GET /api/state` | none | 200, the state document |
| `POST /api/fetch` | `{}` | 202; 409 JOB_RUNNING |
| `POST /api/settings` | `{"inflections_per_year": number}` | 202 (RECOMPUTE); 400 INVALID_FIELD; 409 JOB_RUNNING |
| `POST /api/selection` | `{"excluded": ["name", ...]}` | 202 (REFIT_F); 400 INVALID_FIELD for a name that is not KEPT; 409 JOB_RUNNING |
| `POST /api/series` | `{"fred_id": "..."}` | 202 (ADD_SERIES); 400 INVALID_FIELD; 409 ALREADY_EXISTS or JOB_RUNNING |
| `DELETE /api/series/{name}` | none | 202 (REMOVE_SERIES); 404 NOT_FOUND; 400 NOT_USER_SERIES; 409 JOB_RUNNING |
| `GET /api/charts/spy` | none | 200 chart payload; 404 NO_DATA before the first compute |
| `GET /api/charts/series/{name}` | none | 200 chart payload; 404 NOT_FOUND or NO_DATA |
| `GET /api/charts/f` | none | 200 F payload; 404 NO_F |
| `GET /` and static assets | none | The UI. Static files need no session. |

Rules:

- **Session cookie.** Its value is the hex HMAC-SHA256 of `spylead-session`, keyed with the configured token. It is `HttpOnly` and `SameSite=Strict`, with a `Max-Age` of 30 days. `Secure` is added when the request carries `X-Forwarded-Proto: https`. Compare tokens and cookies in constant time.
- **Authentication.** Every `/api/*` route except login requires the cookie; otherwise 401 UNAUTHORIZED.
- **Request bodies.** POST bodies must be `application/json`; otherwise 415 UNSUPPORTED_MEDIA_TYPE. A body that is not valid JSON gets 400 INVALID_JSON. Numbers must be finite JSON numbers: `NaN`, `Infinity` and booleans get 400 INVALID_FIELD.
- **FRED IDs.** A FRED ID matches `[A-Z0-9_]{1,40}`, uppercased before the check.
- **Errors.** `{"error": "<code>", "message": "<English text>"}`. The UI localizes by code.
- **Listening.** The server listens on `127.0.0.1` only. There is no host setting; the port comes from config.
- **Headers.** Every response carries the headers of §13.

State document:

```json
{
  "app": {
    "server_time": "2026-10-28T14:03:11Z",
    "today": "2026-10-28",
    "fetch_start": "1993-10-28",
    "analysis_start": "1996-10-28",
    "inflections_per_year": 2,
    "keys": {"fred": true, "tiingo": true},
    "job": {"kind": "FETCH", "phase": "STEP3", "done": 7, "total": 18, "series": "M2", "started_at": "2026-10-28T14:02:40Z"},
    "last_job": {"kind": "FETCH", "outcome": "UPDATED", "finished_at": "2026-10-27T22:10:05Z", "message": "..."},
    "computed_at": "2026-10-27T22:10:05Z"
  },
  "spy": {"last_obs_day": "2026-10-27", "points": 8350, "last_fetch_error": null},
  "series": [
    {
      "name": "M2", "origin": "CATALOG", "source_id": "M2SL",
      "title": "...", "units": "...", "frequency": "M", "form": "LOG",
      "last_obs_day": "2026-09-01", "last_release_day": "2026-10-22",
      "points": 400, "estimated_points": 52, "last_fetch_error": null,
      "status": "KEPT", "lead_name": "LEADL1_M2",
      "a_star": -120, "beta": 0.83, "score": 0.051, "score_oos": 0.074, "slope_pct_per_year": 0.4,
      "window": {"start": "1998-10-28", "end": "2024-10-28"},
      "selected": true
    }
  ],
  "f": {
    "status": "OK",
    "members": [{"name": "M2", "lead_name": "LEADL1_M2", "b": -95, "beta": 0.7}],
    "score": 0.043, "score_oos": 0.061, "slope_pct_per_year": 0.1,
    "window": {"start": "1998-10-28", "end": "2026-10-27"}, "split_day": "2018-03-20"
  },
  "events": [{"id": 12, "ts": "...", "level": "INFO", "kind": "FETCH_FINISHED", "series": null, "message": "..."}]
}
```

- `series` lists the candidates, not SPY: catalog entries in catalog order, then USER series by name.
- `job` and `last_job` are null when there is none. `keys` says only whether each key is set, never the key.
- Step-3 fields are null for statuses without a step-3 result. `f` holds only `status` unless it is OK.
- `events` holds the latest 50, newest first.
- `last_job.outcome` is one of: UP_TO_DATE, UPDATED, COMPUTED, FAILED, INTERRUPTED, MISSING_KEY, NO_SPY_DATA, UNKNOWN_SERIES, UNSUPPORTED_FREQUENCY.

Chart payloads (days are integers, days since 1970-01-01; NaN becomes null; values rounded to 6 significant digits):

```json
{"name": "M2", "lead_name": "LEADL1_M2", "units": "...",
 "raw": {"days": [9800, 9831], "values": [3700.1, 3712.5]},
 "rt":  {"first_day": 9900, "values": [3701.0, null]},
 "ref": {"first_day": 9800, "values": [3700.5]}}
```

- `raw` holds the points: first-release values at release days, in original units.
- `rt` holds the one-sided series and `ref` the reference fit, both in original units.
- The SPY payload is the same, with units USD.
- The F payload: `{"f": {...}, "spyl1": {...}, "composite": {...}, "split_day": int, "members": [...]}`, each series as `{"first_day", "values"}` over W5.

## 12. Web UI and PWA

**Technology.**
- Static files, no framework, no build step, no requests to other hosts.
- Charts use Apache ECharts 6.1.0, vendored at `spylead/static/vendor/echarts/` with its LICENSE and NOTICE. The file's SHA-256 is `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0`. Do not replace or upgrade it.
- Never insert server data as HTML. Use `textContent` and DOM APIs: no `innerHTML`, no inline script, no inline style, no inline event handlers. The CSP of §13 forbids them.
- ECharts tooltips use `renderMode: 'richText'`, which draws them on the canvas without HTML.

**One page:**
1. Status bar:
   - the last fetch time and outcome, with a plain "already up to date" message after UP_TO_DATE;
   - job progress (phase, counts, series);
   - the [Fetch Data] button, disabled while a job runs;
   - the language toggle and logout.
2. A banner when a key is missing, when there is no data yet ("press Fetch Data"), and when the last successful poll is older than 10 s.
3. The smoothness setting: N with − and + buttons, and an Apply button. It confirms first, saying that everything will be recomputed.
4. The candidate table:
   - columns: name (and `LEADL1_` name), title, FRED ID, frequency, form, last observation, last release, estimated points, A*, β, score, out-of-sample score, status with reason, an "in F" checkbox (KEPT only), and remove (USER only);
   - below it, an input to add a FRED ID;
   - a row opens that series' chart.
5. The charts:
   - SPY: raw closes, SPYL1 (one-sided), and the reference fit (dashed), with a log-axis toggle;
   - F: normalized to median 1, with a horizontal line at 1 and a marker at the out-of-sample split; the subtitle shows the score, the out-of-sample score and the slope;
   - SPYL1 against the composite;
   - one chart per LEADL1 series, each with its raw points, the one-sided series and the reference fit. They are collapsible and load when opened.

**Chart behaviour.**
- X is the date (a time axis). Y is the value.
- Every chart has pinch and drag zoom (`dataZoom` inside), a slider, and range buttons for 1Y, 5Y, 10Y and All.
- Line series use `sampling: 'lttb'` and the canvas renderer.

**Polling.**
- Poll `GET /api/state` every 2 s while a job runs, otherwise every 30 s.
- Reload the charts when `computed_at` changes.
- Show the login form on 401.

**Confirmations.**
- Before an N change: it recomputes everything and takes some minutes.
- Before removing a series: it is deleted with its data.

**Languages.**
- `zh-TW` and `en`. Every string lives in `locales/zh-TW.json` and `locales/en.json`: flat key to string, fetched by `app.js`.
- The default comes from `navigator.language`: `zh*` selects zh-TW, anything else en. The choice is stored in `localStorage`.
- Every status, outcome, job kind, phase, event kind and error code is localized by code.

**Layout.**
- Usable from 375 px wide (iPhone) through iPad in both orientations to desktop.
- Touch targets are at least 44 px. Tables scroll horizontally inside their own container. Nothing depends on hover. Safe-area insets are respected in standalone mode.
- Status never relies on colour alone. Numbers use `Intl.NumberFormat`. Light and dark follow `prefers-color-scheme`, and the charts switch theme to match.

**PWA.**
- `manifest.webmanifest`: name and short name "spylead", `start_url` "/", `display` "standalone", theme and background colours, and PNG icons of 192 and 512 px (one `maskable`).
- `apple-touch-icon` at 180 px, with the `apple-mobile-web-app-capable` and `mobile-web-app-capable` meta tags.
- The icons are generated by a standard-library script (zlib and struct), committed as PNG.
- No service worker and no offline mode (TODO.md).
- On the PC, Edge installs the site as an app. On iPhone and iPad, Safari adds it to the home screen.

## 13. Security

- **Listening.** The web server listens on `127.0.0.1` only (§11). The iPhone and iPad reach it through Tailscale Serve, which terminates HTTPS inside the tailnet. Never Funnel, never a public port.
- **Headers.** Every response, including errors and static files, carries:
  - `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `Cache-Control: no-store` on `/api/*`, and `no-cache` on everything else, so that a browser revalidates the UI's files after an upgrade.
- **CSP exception.** If ECharts cannot render under this CSP even with richText tooltips and the canvas renderer, add `style-src 'self' 'unsafe-inline'` only, and record it in the plan. Never relax `script-src`.
- **Secrets.** The web token, the session value, the FRED key and the Tiingo key never appear in logs, events, API responses, chart payloads, `last_fetch_error` or error messages.
  - FRED URLs are redacted before any logging, as `api_key=***`.
  - aiohttp's own request logging must not see them.
- **Dependencies.** They are locked with hashes:
  - `requirements.in` lists exactly `numpy`, `scipy`, `aiohttp`, `tzdata` and `pytest`;
  - `pylock.toml` is generated on the PC with `python -m pip lock -r requirements.in -o pylock.toml`, after upgrading pip in the venv;
  - installs use `python -m pip install -r pylock.toml`. The lock changes only when the owner asks.
- **Git.** `config.toml`, `data/` and `logs/` are git-ignored.
- **Coexistence.** This PC may run IB Gateway for another project. spylead never connects to ports 4001, 4002, 7496 or 7497.

## 14. Configuration and CLI

`python -m spylead --config <path>`

`config.toml` is git-ignored; `config.example.toml` is committed.

```toml
[web]
port = 8090            # always bound to 127.0.0.1; publish it with Tailscale Serve
token = "change-me"    # random, at least 16 characters

[fred]
api_key = ""           # free key from the FRED account page

[tiingo]
api_key = ""           # free API token from the Tiingo account page
```

- Unknown keys, missing keys and wrong types are startup errors that name the key and never echo a value.
- `web.token` must not be the placeholder and must have at least 16 characters.
- Empty API keys are allowed at startup, with a warning. Fetches then fail with MISSING_KEY (§8.3).
- `catalog.toml` sits next to `config.toml` in the repository root. Its errors name the entry and the key.
- `data/` and `logs/` are created next to the config file.
- N, the selection, and the start days live in the database, not in the config file.

## 15. Runtime

- **Startup order.** Config, catalog, the database lock, the store (with catalog sync), the web server, then ready. No fetch runs at startup: fetching is manual (TODO.md).
- **One process per database.**
  - At startup the app takes an operating-system lock on `data/spylead.lock`: `msvcrt.locking` on Windows, `fcntl.flock` elsewhere.
  - It holds the lock for the life of the process, and exits before opening the database or the port if another process holds it.
  - The lock dies with the process; the file's presence means nothing.
- **Logging.** Console at INFO, plus a rotating file `logs/spylead.log` at DEBUG (5 MB, 5 backups). Refit details go to DEBUG.
- **Shutdown on Ctrl+C.** Stop accepting requests, interrupt the running job (§9), shut the pool down, close the store.
- **Autostart.** `scripts/install-autostart.ps1` registers a per-user logon task named `spylead`, and `-Remove` deletes it.
  - **Launcher.** The task runs a launcher written to `%LOCALAPPDATA%\spylead\launch.ps1`, on the system drive. It waits up to 10 minutes for the project folder, because the project lives on an external drive. Then it starts `.venv\Scripts\pythonw.exe -m spylead --config config.toml` in that folder.
  - **Fallback.** If registering the task is refused, the script puts a launcher shortcut in the user's Startup folder instead, and says so.
- **Time.** The app takes a clock callable, so tests control time. "today" uses America/New_York through `zoneinfo`, which needs `tzdata` on Windows.

## 16. Testing and acceptance

### 16.1 Rules

- pytest only. No network. No waiting on real time: inject the clock and the sleep.
- **Pure modules** (`pit`, `smoothing`, `analysis`): table-driven tests on small synthetic data, plus the property tests named below.
- **Source adapters** are tested with injected HTTP fakes or aiohttp's test server, using hand-written JSON samples. Never real downloads.
- **Jobs** are tested with fake sources, a temporary database, and a synchronous stand-in for the process pool.
- **Web** tests use aiohttp's test utilities.
- **Speed.** Keep test data small. Production-size timing belongs to `spylead.bench`, not to the suite.

### 16.2 Acceptance criteria

| Id | Criterion |
|---|---|
| AC-1 | The weighted Reinsch solve matches `make_smoothing_spline` within 1e-6 relative on random data with uneven spacing, random weights and several λ. The value formula is continuous at the knots. The linear extension uses the end value and end slope of §6.2. |
| AC-2 | IRLS never increases the objective. A single spike moves the robust fit less than the L2 fit. The inflection count matches hand-made γ sequences. The λ search returns `count ≤ T`, and `count > T` at `λ · 10^-0.005` unless λ is at a limit. A warm-started bracket gives the same λ as the full bracket on a test series. |
| AC-3 | Changing or removing points released after the last month end before day d changes no one-sided value on or before d. Values follow `v_k + s_k · (d / 365.25 − x_k)`. No value precedes the first anchor. |
| AC-4 | First releases are parsed, and missing values skipped. `typical_lag` comes from the last 24 months when there are 12 or more observations, and from the default otherwise. Pre-archive observations get estimated release days and the flag. The known-value rule skips late older releases and resolves same-day releases. Release days are strictly increasing. A LOG series with a value ≤ 0 is INVALID_VALUES. |
| AC-5 | Step 3: on synthetic data where an indicator equals SPY's latent process 90 days later, A* is exactly −90 for LOG and for LEVEL form. One window serves all A. Ties go to the smaller \|A\|. ±730 gives AT_BOUNDARY, A* ≥ 0 NOT_LEADING, and a window under 10 years INSUFFICIENT_HISTORY. |
| AC-6 | Step 5: on synthetic data with independent long-period components (periods of at least 1,500 days) and known leads, every b_i is recovered exactly. Two runs give identical results. Every b_i lies in [−730, 0], with a = 0. |
| AC-7 | The LAD β of a small case matches `scipy.optimize.linprog` (HiGHS) on the same problem within 1e-6. Score, slope (percent per year), F normalized to median 1, and the composite `exp(c + Σ β r)` match hand-computed values. |
| AC-8 | The out-of-sample check fits on the first 70% only and scores the last 30%. It never changes A*, b, β or the kept set. |
| AC-9 | The first fetch sets `fetch_start` and `analysis_start` once, downloads SPY and every candidate from `fetch_start`, computes, and the chart routes then answer. |
| AC-10 | A fetch with everything up to date makes metadata requests only, downloads no observations, runs no compute, ends UP_TO_DATE, and leaves `computed_at` unchanged. |
| AC-11 | An incremental fetch inserts only new observation days and leaves stored rows unchanged, then computes. |
| AC-12 | One failing series keeps its data and records `last_fetch_error`; the others continue and the compute runs. SPY without data ends NO_SPY_DATA without a compute. A missing key gives MISSING_KEY for that source. Retries follow §8.4 with an injected sleep. |
| AC-13 | One job at a time (409 JOB_RUNNING). Progress is reported. A shutdown during a job keeps the previous results and records INTERRUPTED. REFIT_F reruns only step 5, from the stored curves. |
| AC-14 | Every status of §7.6 arises in a scenario test, including a 3-year series (INSUFFICIENT_HISTORY) and a stale series (DISCONTINUED, never in F). F gives NO_MEMBERS and INSUFFICIENT_OVERLAP when it should. |
| AC-15 | Adding a series validates the ID, applies the form rule of §4.4, and refuses annual (UNSUPPORTED_FREQUENCY) and unknown (UNKNOWN_SERIES) series. Only USER series can be removed. Catalog rows leave the database when they leave `catalog.toml`. |
| AC-16 | Unchecking a member recomputes F without it. The choice survives a restart. A newly KEPT series is included. |
| AC-17 | Every `/api/*` route except login requires the session. A non-JSON POST gets 415. NaN, infinite and boolean numbers are rejected. The §13 headers are on an API response, an error response and a static file. The server listens on 127.0.0.1 only. The cookie is HttpOnly and SameSite=Strict, and Secure with `X-Forwarded-Proto: https`. The web token, the session value, the FRED key and the Tiingo key appear in no log line, event, API response, chart payload or error message, and FRED URLs are logged redacted. |
| AC-18 | The chart payloads have the shapes and units of §11: raw points at release days, one-sided and reference series in original units, integer days, null for NaN. Before a compute they answer 404 NO_DATA; without F, 404 NO_F. |
| AC-19 | Both locale files define the same keys and cover every status, outcome, job kind, phase, event kind and error code. The static files outside `vendor/` contain no `innerHTML`, `insertAdjacentHTML`, `document.write`, inline script, `style` attribute or inline event handler. The manifest is valid, with 192 and 512 px icons and a 180 px apple-touch-icon. The ECharts file matches the pinned SHA-256. |
| AC-20 | Config and catalog errors name the key or entry without echoing values. A placeholder or short token is rejected. Catalog names are unique and forms valid. |
| AC-21 | A second process exits before opening the database or the port. A lock left by a killed process does not block a restart. The WAL fallback works. Ctrl+C shuts down in the order of §15. |
| AC-22 | Two computes on the same data give identical results. `python -m spylead.bench` prints the time of each phase. |

### 16.3 Checks on the PC

Manual, by the owner:

1. Install from `pylock.toml` and run the full suite on Windows.
2. Put both keys in `config.toml` and start the app.
   - Press [Fetch Data] and wait for the charts.
   - Press it again: it says the data is already up to date.
   - Compare SPY's latest close with Tiingo's site, and M2's latest value with FRED's.
3. Run `python -m spylead.bench`. A full compute must take under 10 minutes.
4. Walk through the UI checklist from prompt 9's plan:
   - Edge on the PC, installed as an app;
   - an iPhone at 375 px wide;
   - an iPad in both orientations, through Tailscale Serve, added to the home screen.
5. Install the autostart, sign out and in, and check that the app comes up after the external drive mounts.

## 17. Accepted limitations

Owner decisions. All intentional.

1. One-sided series react late. Within a month, each value follows the previous month end's fit along its end slope, and a turn shows only after the next refits. Leads are measured between these real-time series, not between the hindsight curves.
2. The reference fit uses every point and therefore hindsight. It is for display only.
3. ALFRED's archive does not cover every old observation. Their release days are estimated, and so are the leads that depend on them.
4. A late first release of an older observation is skipped (§5.3).
5. SPY is the raw close without dividends.
6. FRED keeps only 3 years of the ICE BofA series, so the CCC and high-yield spreads cannot be analysed (INSUFFICIENT_HISTORY). ISM PMI and the Conference Board LEI are not available.
7. A good fit is not a forecast. With many shifts and exponents fitted to smooth trending series, a flat F can arise by chance. The out-of-sample score is the guard, and it is only shown.
8. With several members, the shifts of F are not unique (§7.4).
9. No projections past today (TODO.md).
10. The PC must be on, with the app running, for the iPhone and iPad to reach it. Without the external drive, the app cannot start.
11. Data is for personal internal use (§4.5).

## 18. Extension points and refactor seams

Extension points:

- **Sources.** A new data source is a new client in `sources.py` returning observations with release days. The point-in-time rules stay unchanged.
- **Smoothing.** The smoother is one function of points and N. Another smoother, or more frequent refits, changes only `smoothing.py`.
- **Scoring.** The score is one function, so an L2 or another flatness measure is a local change.
- **Forms.** Forms are one mapping, so a new transform (for example a ratio of gross rates) is a local change.
- **API.** The JSON API can serve a native client.

Refactor seams:

- **Layers.** Pure core (`pit`, `smoothing`, `analysis`); orchestration (`jobs`); adapters (`sources`, `store`, `web`).
- **Store.** The store is synchronous SQLite behind one class.
- **Process pool.** The pool is injected, so tests run it in-process.

Deferred items live in `TODO.md`.

## 19. Owner setup

The README carries these.

- **Keys.** A free FRED API key from the FRED account page, and a free Tiingo API token from the Tiingo account page. Put both in `config.toml`.
- **Python.** Python 3.12 or later on Windows, with the `py` launcher.
- **Drive.**
  - The project lives on `E:\00_CODING\spylead`, an external drive.
  - Give the drive a fixed letter in Disk Management, so it is always E:.
  - On exFAT, Git needs `safe.directory` entries for the folder.
- **Tailscale.** `tailscale serve --bg --https=8443 http://127.0.0.1:8090` publishes the app inside the tailnet. Then open `https://<pc-name>.<tailnet>.ts.net:8443` on the iPhone or iPad and add it to the home screen. Never use Funnel.
- **Edge.** Open `http://127.0.0.1:8090`, then use Apps, "Install this site as an app".
- **Backup.** Copy `data/spylead.db` while the app is stopped.
