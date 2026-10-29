# Prompt 4: Analysis core

## Steps

1. `spylead/analysis.py`, pure (no I/O, clock, logging or aiohttp). Inputs are one-sided daily series in y (§3) with their first days, plus series metadata. Outputs are frozen dataclasses of Python ints and floats, with NumPy arrays only for F's daily curves. All of it pickles for the pool.
   - `Daily(first_day, values)`: one float64 per day from `first_day`, NaN where undefined (§6.4). `span()` gives the first and last defined days; `at(days)` reads values on any array of days.
   - `Window(start, end)`: every day from `start` to `end`.
     - `length`: the number of days;
     - `days()`: every day, for reported results; `search_days()`: every 7th day counting back from `end`, for searches (§7.1);
     - `split()`: the first 70% and the rest (§7.5).
   - `window(start, end)`: the window, or None when it is shorter than 10 years.
   - LAD (§7.1):
     - `lad(X, y)`: IRLS batched over k designs of shape (k, n, p). Unit weights first; the weighted normal equations by `numpy.linalg.solve` with the explicit trailing axis of §7.7; `δ = max(1e-6 · median|res|, 1e-12)` per design; a stop when the objective changes by less than 1e-8 relative, or after 100 solves. Each design stops on its own, so a result never depends on the rest of its batch.
     - `fit_scores(y, regressors)`: LAD with an intercept for each of k candidates, then each score.
     - `fit(spy, series, shifts, window)`: one LAD fit over every day of a window, for reports.
   - `score(log_f)`: the mean of |ln F − median ln F| along the last axis. `slope_pct_per_year(days, log_f)`: the OLS slope against `d / 365.25`, as `100 · (exp(slope) − 1)`.
   - `best(values, scores)`: the lowest score. Ties, a relative difference of at most 1e-12, go to the smaller |value|. The one tie rule for A*, for step 5's candidates, and for its two starts.
   - Step 3 (§7.2, §7.5):
     - `step3_window(spy, indicator)`: W3;
     - `search_shift(spy, indicator, window)`: every A from −730 to 730 on the search days, in chunks that keep every array under 64 MB, then `best`;
     - `step3(spy, indicator) -> Step3 | None`: None when W3 is shorter than 10 years. Otherwise A*, then β, the score and the slope over every day of W3, then the out-of-sample score: the same search and fit on the first 70%, scored on the rest.
   - Step 4 and statuses (§7.3, §7.6):
     - `status(release_days, *, fetch_failed, invalid_values, step3, today)`: exactly one status;
     - `discontinued(release_days, today)`: the release-day spacing rule;
     - `lead_name(name)`: `LEADL1_<name>`;
     - `is_member(status, name, excluded)`: KEPT and not unchecked (§7.4).
     - The status names, in §7.6's order, and the F statuses as constants, for the jobs module and the locale test of prompt 9.
   - Step 5 (§7.4, §7.5):
     - `step5_window(spy, members)`: W5;
     - `neighbours(b, step, radius)`: `b + k · step` within ± radius, kept inside [−730, 0];
     - `descend(spy, members, window, start)`: the three stages of passes over the members, each member update one batch of candidates;
     - `step5(spy, members) -> Step5`: NO_MEMBERS, INSUFFICIENT_OVERLAP, or OK with a `Combination`:
       - both starts, and the choice between them;
       - each b_i and β_i, c, the score and the slope over every day of W5;
       - F over W5 with median 1, and the composite `exp(c + Σ β_i r_i(d + b_i))`;
       - the out-of-sample score: one descent from the full-window b on the first 70%, a fit over every day of it, scored on the rest; and the split day.
2. `tests/test_analysis.py`, on synthetic one-sided series with fixed seeds. A smooth latent process z stands for SPY's s = ln SPY:
   - AC-5:
     - an indicator equal to z 90 days later gives A* = −90 exactly, built through `pit.y_values` in LOG form (`exp z`) and in LEVEL form (`z` minus a constant, crossing zero);
     - one window for every A: changing s outside W3, or s on days of W3 off the 7-day grid, changes no shift's score; changing s on one grid day changes every score;
     - ties: `best` on hand-made scores (exact ties, 1e-13 and 1e-11 relative), and an indicator that is a straight line, which ties every A and gives A* = 0;
     - AT_BOUNDARY from leads of 800 days in both directions, NOT_LEADING from a 90-day lag and from no lag, INSUFFICIENT_HISTORY from a 3-year series through `smoothing.one_sided`, and the 10-year edge of the window.
   - AC-6:
     - three independent components with periods of 1,500 days or more, and leads of 60, 200 and 450 days: step 5 returns b = −60, −200, −450 exactly, with the β of the construction;
     - two runs are identical, bit for bit;
     - every candidate a descent tries lies in [−730, 0]; `neighbours` at the edges;
     - with the coarse stage alone, every b_i stays on the 28-day grid around its start.
   - AC-7:
     - LAD against `scipy.optimize.linprog` (HiGHS) on a small problem with an intercept and two regressors, within 1e-6;
     - `score`, `slope_pct_per_year` and F's normalization on hand-computed arrays;
     - the reported F, composite, score and slope of a step-5 result against the formulas of §7.1 and §7.4, computed independently in the test.
   - AC-8:
     - adding a constant to s from the split day on leaves the out-of-sample score unchanged, for step 3 and for step 5's check; adding it from the day before changes it;
     - a different split fraction (patched) changes only the out-of-sample score and the split day: A*, b, β, c, the scores, the slopes and the curves stay bit-identical.
   - AC-14, analysis part: every status of §7.6 from a small scenario, in a table, including the 3-year series and a stale series (DISCONTINUED, and `is_member` false); the threshold of DISCONTINUED to the day; NO_MEMBERS, INSUFFICIENT_OVERLAP from two members whose spans overlap 8 years, and OK.
3. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **PENDING and FETCH_FAILED.** Read literally, PENDING ("no data stored yet") comes first and would cover FETCH_FAILED too, so FETCH_FAILED could never apply. Without data, a failed last fetch gives FETCH_FAILED and anything else PENDING, which is the only reading in which both statuses can arise (AC-14). Suggested §7.6 wording for PENDING: "No data stored yet, and no failed fetch".
- **Window length** is the number of days in the window. "Shorter than 10 years" uses x's year (§3): fewer than 10 × 365.25 days, so a window needs at least 3,653 days.
- **The split day** is `start + floor(0.7 · length)`, computed as `7 · length // 10` so that no rounding of 0.7 can move it. The first 70% are the days before the split day; the out-of-sample part starts on it.
- **Equal |A|.** A tie between A and −A goes to the positive A, which claims no lead. Step 5 has no such case: b ≤ 0.
- **Step 5's candidates** `b_i + k · step` within ± radius are restricted to [−730, 0], not moved onto its ends. Every candidate then stays on its stage's grid around the start, which is what "the coarse stage runs on the 28-day grid" asks.
- **IRLS keeps the better fit,** as in `smoothing.py`: a solve that raises the objective is discarded, and that design stops. The capped weights and the moving δ make a slightly worse solve possible.
- **Convergence** is a fall of at most 1e-8 of the objective, as `smoothing.py` words its rule, so that an exact fit, with objective 0, stops at once.
- **Reported results** refit β and c by LAD over every day of the window at the chosen shifts. The searches only choose the shifts.
- **The step-3 report** has no intercept: neither the state document nor a chart uses it. Step 5 keeps c for the composite.
- **Medians** are NumPy's, the mean of the two middle values for an even count. The score does not depend on the choice. F is `exp(ln F − median ln F)`: the median of ln F is then exactly 0, and F's own median is 1 for an odd count and `cosh(Δ/2)`, a hair above 1, for an even one, Δ being the gap between the two middle values of ln F.
- **DISCONTINUED:**
  - the release days are those of the points (§5.3), and the latest release day is the last one;
  - the spacings are the gaps between consecutive release days that end within the 2 years up to the latest release day, the window of `typical_lag` (`pit.years_before`);
  - "the last 2 years" counts back from the latest release, not from today: a series that stopped years ago has no release in the 2 years before today, and so no spacing.
- **The random start** is one draw of m integers, `default_rng(0).integers(−730, 0, size=m, endpoint=True)`, in member order.
- **The two starts.** Equal Σ|b_i| after a tie keeps the first start, b_i = A*_i.
- **Passes end.** Each change lowers the score by more than 1e-12 relative, so no set of shifts repeats, and the grid is finite. No pass limit is needed.
- **The current score** of a member update is the score of its own candidate k = 0 in the same batch. One set of shifts always gives the same score, whichever member's update computes it.
- **Step 5's out-of-sample check** is one descent through the three stages from the full-window b, on the first 70%, then a fit over every day of it. The random start is not repeated.
- **Members arrive in §7.4's order** (catalog order, then USER series by name) from the caller, which lists the candidates in that order for the state document too (§11).
- **Chunks.** 64 MB is 2^26 bytes. The largest array of a chunk is its design with the intercept, k × n × p float64.
- **F's curves.** `Combination` holds F and the composite over every day of W5. SPYL1 over W5 is `exp(s)`, which the caller already has.

## Extension points

- **Scoring** (§18): `score` is the one flatness measure. An L2 measure replaces it alone.
- **Prompt 7** composes:
  - `step3` is the pool worker for step 3: top-level, and its inputs are two `Daily`;
  - `status` runs after it, with the points' release days, `last_fetch_error`, `pit.has_invalid_values` and today;
  - `step5` is one pool task. REFIT_F builds the `Member`s from the stored curves, statuses and A*, through `is_member` and the stored exclusions;
  - results serialize with `dataclasses.asdict`: `Window` nests as `{"start", "end"}`, as in the state document. F's arrays need `.tolist()`.
- **Prompts 7 and 8:** ADD_SERIES should refuse a FRED ID already stored as a `source_id`, such as a catalog entry's. Two members with identical curves make step 5's normal equations singular at equal shifts (see the deferred item).
- **Step 5's search:** more starts or other stages are the start list in `step5` and `STAGES`.

## Refactor seams

- **The solver.** `lad` is the only LAD solver, and `fit_scores` the only place that adds the intercept to a design.
- **Days.** `Window` holds every rule about days: length, the 7-day search grid, and the split.
- **Ties.** `best` is the one tie rule.
- **Speed.** Step 3's search is the largest cost. If the bench (prompt 7) shows it is too slow, the 2 × 2 normal equations have a closed form, and the active designs could be compressed less often.

## Deferred items (appended to TODO.md)

- Guard steps 3 and 5 against singular normal equations: an indicator that is constant over its window, or two members that are affine copies of each other. Today `numpy.linalg.solve` raises LinAlgError, or the fit is meaningless. Seam: `analysis.lad`.
