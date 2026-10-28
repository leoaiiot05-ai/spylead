# Prompt 2: Smoothing core

## Steps

1. `spylead/smoothing.py`, pure (no I/O, clock, logging or aiohttp). Inputs are release days (int64, strictly increasing) and y (float64). Outputs are NumPy arrays and frozen dataclasses, picklable for the process pool.
   - `Spline(x, g, gamma)`: a natural cubic spline, its values and second derivatives at the knots (γ = 0 at both ends).
   - `weighted_spline(x, y, w, lam)` (§6.2): Q and R from `h`, the lower band of `R + λ QᵀW⁻¹Q` (two off-diagonals), `scipy.linalg.solveh_banded`, then `g = y − λ W⁻¹ Q γ`.
   - `spline_values(spline, at)`: the cubic formula of Green & Silverman inside `[x_1, x_n]`, the linear extension outside. `end_slopes(spline)`: §6.2's slopes at `x_1` and `x_n`. `roughness(spline)`: `Σ h_i (γ_i² + γ_i γ_{i+1} + γ_{i+1}²) / 3`.
   - `irls_weights(residuals)`: `δ = max(1e-4 · median(r), 1e-12)`, `w = 1 / max(r, δ)`.
   - `robust_fit(x, y, lam, weights=None) -> RobustFit(spline, weights, objective)`: IRLS from unit weights or from the weights passed in. It stops when the objective falls by less than 1e-6 of its value, or after 50 solves. The weights it returns are those for the next solve, so a caller can carry them over.
   - `inflection_count(gamma)`: sign changes after dropping `|γ| ≤ 1e-9 · max|γ|`.
   - `inflection_target(days, n_per_year)`: `T = floor(N · (x_n − x_1))`.
   - `search_lambda(x, y, target, start=None) -> Smoothed(log10_lam, at_limit, fit)` (§6.3):
     - the warm bracket `start ± 1`;
     - the fallback to `[−8, 8]`, with its limits;
     - bisection until the width is below 0.005 or after 30 halvings;
     - IRLS weights carried from each fit to the next.
   - `refit_days(first_day, today)`: the month ends `m_k` (§6.4).
   - `refit_anchors(days, y, n_per_year, today) -> Anchors`: one row per refit that produced an anchor, holding `month_end`, `x`, `value`, `slope`, `lam` and `at_limit`.
   - `anchor_values(anchors, first_day, last_day)`: one float64 per day, `v_k + s_k · (d / 365.25 − x_k)` for the last `m_k < d`, NaN before the first anchor.
   - `one_sided(days, y, n_per_year, *, today, first_day, last_day) -> OneSided(first_day, values, anchors)`: the anchors carry each refit's λ and the flag the caller logs as LAMBDA_AT_LIMIT.
   - `reference_fit(days, y, n_per_year) -> Reference(first_day, values, lam, at_limit)` (§6.5): one search on all points, evaluated on every day from the first point to the last.
2. `tests/test_smoothing.py`, on small synthetic data with fixed seeds:
   - AC-1:
     - the solve against `make_smoothing_spline` (tests only) at λ = 1e-4, 1e-2 and 1, on uneven spacing with random weights: g, γ and a dense grid within 1e-6 relative;
     - the roughness against Simpson's rule on SciPy's second derivative;
     - values and first derivatives continuous at the knots;
     - §6.2's end slopes equal SciPy's derivative at both ends, and the extension is the line through the end value with that slope.
   - AC-2:
     - the objective never rises as the solve limit goes from 1 to 50: from unit weights, from passed-in weights, and on a case where a solve comes out worse;
     - the fit at λ has a lower §6.1 objective than the fits at λ/2 and 2λ;
     - a spike moves the robust fit less than the L2 fit;
     - a table of hand-made γ sequences, including entries at, just under and just over the 1e-9 threshold;
     - the search returns `count ≤ T`, and `count > T` at `λ · 10^-0.005`;
     - a warm start (a previous result, and starts on the bisection grid) gives the full bracket's λ, and a start too far away falls back to it;
     - a warm bracket near ±8 never fits outside [10^-8, 10^8];
     - both limits, the upper one flagged.
   - AC-3:
     - for seeded random days d, altering or removing the points released after the last month end before d leaves every value on or before d unchanged;
     - values follow the anchor line, checked with an independent lookup;
     - anchors are the end value and end slope of each refit;
     - nothing before the first anchor, and no anchor from fewer than 8 points;
     - a month without a new point keeps the previous anchor;
     - the refit days, including 29 February and month-end edges;
     - fewer than 8 points or none.
   - The reference fit: every point, daily inside `[x_1, x_n]`, equal to the search's fit at the points.
3. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **IRLS solves with penalty 2λ. The owner should confirm the §6.2 wording.**
  - With `w_i = 1/|r_i|`, `(w_i r_i² + 1/w_i)/2 ≥ |r_i|`, with equality at the current residual. Each solve therefore minimizes an upper bound of `Σ|r| + λ∫g''²` that touches it at the current fit, provided the weighted problem's penalty is 2λ.
  - Read literally, the same λ in each solve makes IRLS converge to the minimizer of `Σ|r| + (λ/2)∫g''²`.
  - Probe, 18 cases (6 seeds × λ = 1e-3, 1e-1, 10, n = 60, heavy-tailed noise, 50 solves):
    - literal λ: the §6.1 objective rose in 1 to 48 of the 49 steps, by up to 1.7%, which breaks AC-2;
    - 2λ: it never rose, and every final objective was lower.
  - The weights, δ, the stopping rule, the 50 solves and the objective stay as §6.2 states them. λ keeps the meaning of §6.1, and the §6.3 search over it is unchanged.
  - Suggested §6.2 wording: "solve the weighted problem with these weights and penalty 2λ".
- **IRLS keeps the better fit.** A solve that raises the objective is discarded, and IRLS stops. δ changes from step to step and rounding is not exact, so a step can be slightly worse: the probe saw one at λ = 1e8 on 8,300 daily points. The returned objective therefore never exceeds an earlier one (AC-2). The returned weights come from the returned fit's residuals.
- **A month without a new point keeps the previous anchor**, as the prompt words it:
  - no refit runs for that month, so it has no λ of its own;
  - a refit on the same points would bisect from a different bracket and could land a hair away from the previous λ.
- **The first refit** is the end of the month 36 months after the first point's month. A date 3 calendar years later keeps its month, so that end is the first month end at least 3 years later. 29 February needs no special case: 2000-02-29 gives 2003-02-28.
- **T** is computed from whole days, `floor(N · (d_n − d_1) / 365.25)`, so a span of exact years is not rounded down.
- **The warm bracket** is clipped to [−8, 8].
- **Order of fits**, which matters for the carried weights:
  - the warm bracket fits its upper end, then its lower end;
  - the full bracket fits 10^8, then 10^-8;
  - then the bisection runs.
- **Only the upper limit is flagged** (`at_limit`, the LAMBDA_AT_LIMIT case). λ = 10^-8 is a normal result.
- **The search returns log10 λ exactly**, and the next refit starts from it. Every result then lies on the grid `−8 + i/256`: 16 halved 12 times and 2 halved 9 times are both 1/256. A warm start from a previous result therefore lands on the full bracket's grid point, which is why the two agree (AC-2). The exception is a bracket clipped at ±8.
- **The reference fit needs 8 points**, like a refit. With fewer, its values are empty.
- **Weights carry over within one search only**, as §6.3 says. Each refit's search starts from unit weights.

## Extension points

- **Refits.** `refit_days` is the schedule. Weekly refits (TODO.md) change only this function: `refit_anchors` and `anchor_values` take whatever schedule it returns.
- **Caching.** `Anchors` is the unit to cache for "Cache the one-sided anchors" (TODO.md). `anchor_values` rebuilds any day range from it without refitting.
- **Another smoother** (§18) replaces `search_lambda` and `robust_fit` behind `refit_anchors` and `reference_fit`.
- **Prompt 7** calls `one_sided` and `reference_fit` in the pool: they are top-level and take plain arrays. It logs LAMBDA_AT_LIMIT from `anchors.at_limit` and `Reference.at_limit`, and the refit details at DEBUG (§15).

## Refactor seams

- **The solve.** `weighted_spline` is the only code that builds and solves the band. A faster solver replaces it alone.
- **The target.** `search_lambda` takes the target T rather than N. The smoothness rule (`inflection_target`) and the counting rule (`inflection_count`) are separate functions.
- **Evaluation.** Every evaluation goes through `spline_values` and `end_slopes`.
- **Speed.** One IRLS fit of 8,300 daily points took 20–85 ms in the probe, about 1.7 ms per solve. The bench (prompt 7) decides whether carrying weights across refits is needed (TODO.md).

## Deferred items (appended to TODO.md)

- Carry IRLS weights across refits (the previous refit's weights, 1 for new points) if the bench shows the one-sided refits dominate the compute. Today each refit's search starts from unit weights. Seam: `smoothing.refit_anchors`.

## Result

- `.venv/Scripts/python -m pytest -q`: 114 passed, 62 of them new in `tests/test_smoothing.py`, in about 7.5 s.
- AC-1:
  - the solve matches `make_smoothing_spline` within 6e-14 relative in g and 9e-13 in γ, far inside 1e-6;
  - the roughness matches Simpson's rule;
  - the values are continuous at the knots, and the end slopes equal SciPy's derivative at both ends.
- AC-2 and AC-3 hold on the test data. See below for what AC-2's λ-search criteria need.
- Mutation checks, each made by hand in `smoothing.py` and reverted:
  - penalty λ instead of 2λ in IRLS: 7 tests fail;
  - no guard against a worse solve: the worse-solve case fails;
  - no clipping of the warm bracket: both clipping tests fail;
  - an anchor applying from its own month end, a one-day look-ahead: 5 tests fail, the AC-3 property test among them. The property test caught it only after an anchor's month end was added to its random days, so that day is now always checked.

### For the owner: the λ search on realistic data

These follow from §6.3 as written. No rule was changed.

- **The count is not monotone in λ.** A bracket can hold more than one crossing of T, and the bisection returns one of them.
- **Near T, the count depends on the IRLS path.**
  - On slow cases the 50-solve cap binds before the 1e-6 rule: one fit stopped at solve 50 where the 1e-6 rule would have stopped at 117. The fit then stays about 3.5e-4 relative above the optimum, enough to flip an inflection near T.
  - Weights carried along the bisection converge further. In the 3 cases checked against fully converged fits, the result sat exactly on the count's switching point.
  - Fresh fits from unit weights at λ·10^-0.005 gave count ≤ T in 2 of the 11 explored searches that were not at a limit.
  - So the AC-2 test carries the search's weights to λ·10^-0.005, as one more bisection step would. The count was then above T in all 11 cases not at a limit.
- **Warm and full brackets.**
  - On the test series (seed 1, N = 0.5), every warm start gives exactly the full bracket's λ, as AC-2 asks.
  - On the other explored series, a warm start mostly ended within 0.03 of it in log10 λ, but up to 0.14 away on two series and 0.31 on one.
  - So a refit's λ, though deterministic, can differ slightly from a fresh full search on the same points.
- **LAMBDA_AT_LIMIT with small N.** On the cyclic test series, N = 0.25 puts every refit at the limit: T is 0 or 1 for spans of 3 to 5 years. With N = 0.25 the caller would log one LAMBDA_AT_LIMIT per refit for such series.
- **The guard** against a worse solve fired in 18 of 810 small cases explored, with λ from 1e-8 to 1e8. The rises were 1.6e-7 relative or less.

### Speed, for prompt 7

- One solve takes about 0.3 ms at n = 50, where the overhead dominates, and about 1.7 ms at n = 8,300.
- A refit takes about 320 solves, in 11 to 14 fits.
- Rough estimate for the catalog: about 1,100 CPU seconds, so 3 to 4 minutes on 7 workers. DFF, about 12,000 daily points, is the longest single series.
- If the bench shows it is too slow, three options:
  - compute the terms of the band that depend only on x once per search, since they do not change between solves;
  - call LAPACK `dpbsv` directly: 6 µs instead of 40 µs per call at n = 50. SPEC.md names `solveh_banded`, so ask the owner first;
  - carry weights across refits (TODO.md).
