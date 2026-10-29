import dataclasses
import datetime
import functools
import math

import numpy as np
import pytest
from scipy.optimize import linprog

from spylead import analysis
from spylead.analysis import (
    AT_BOUNDARY,
    DISCONTINUED,
    FETCH_FAILED,
    INSUFFICIENT_HISTORY,
    INSUFFICIENT_OVERLAP,
    INVALID_VALUES,
    KEPT,
    NO_MEMBERS,
    NOT_LEADING,
    OK,
    PENDING,
    SHIFTS,
    Daily,
    Member,
    Step3,
    Step5,
    Window,
    best,
    candidate_scores,
    descend,
    discontinued,
    is_member,
    lad,
    lead_name,
    neighbours,
    score,
    shift_scores,
    slope_pct_per_year,
    status,
    step3,
    step3_out_of_sample,
    step3_window,
    step5,
    step5_out_of_sample,
    step5_window,
    ten_year_window,
)
from spylead.pit import y_values
from spylead.smoothing import one_sided

EPOCH = datetime.date(1970, 1, 1)


def day(year, month, date):
    return (datetime.date(year, month, date) - EPOCH).days


def smooth_walk(size, seed, sigma=20):
    """A random walk smoothed with a Gaussian kernel of `sigma` days: one value per day."""
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.normal(0, 0.01, size + 8 * sigma))
    kernel = np.exp(-0.5 * (np.arange(-4 * sigma, 4 * sigma + 1) / sigma) ** 2)
    return np.convolve(walk, kernel / kernel.sum(), mode="valid")[:size]


def assert_identical(a, b):
    """Equal field by field, arrays bit for bit."""
    if dataclasses.is_dataclass(a):
        assert type(a) is type(b)
        for field in dataclasses.fields(a):
            assert_identical(getattr(a, field.name), getattr(b, field.name))
    elif isinstance(a, tuple):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            assert_identical(x, y)
    elif isinstance(a, np.ndarray):
        assert a.dtype == b.dtype and a.tobytes() == b.tobytes()
    else:
        assert a == b


# Step 3 data: SPY's s = ln SPY is 4 + z, for a smooth latent process z. 14 years give W3 10 years and a day.

FIRST = day(1995, 1, 1)
SPAN = int(14 * 365.25) + 1
DAYS = np.arange(FIRST, FIRST + SPAN)
LAST = int(DAYS[-1])
Z_FIRST = FIRST - 2000
Z = smooth_walk(SPAN + 4000, 1)
SPY = Daily(FIRST, 4.0 + Z[DAYS - Z_FIRST])
RELEASES = DAYS  # daily points, up to date
TODAY = LAST + 1


def z(days):
    return Z[np.asarray(days) - Z_FIRST]


def indicator(lead, form="LEVEL"):
    """An indicator equal to SPY's latent process `lead` days later, in original units, then in y."""
    later = z(DAYS + lead)
    values = np.exp(4.0 + later) if form == "LOG" else later - 0.05
    return Daily(FIRST, y_values(values, form))


@functools.cache
def step3_of(lead, form="LEVEL"):
    return step3(SPY, indicator(lead, form))


def status_of(result, release_days=RELEASES, today=TODAY):
    return status(release_days, fetch_failed=False, invalid_values=False, step3=result, today=today)


# AC-5


@pytest.mark.parametrize("form", ["LOG", "LEVEL"])
def test_an_indicator_equal_to_spy_90_days_later_leads_by_exactly_90_days(form):
    if form == "LEVEL":
        # LEVEL handles an indicator that crosses zero (§7.1).
        assert indicator(90).values.min() < 0 < indicator(90).values.max()
    result = step3_of(90, form)
    assert result.a_star == -90
    assert result.window == Window(FIRST + 730, LAST - 730)
    assert result.beta == pytest.approx(1.0, rel=1e-9)
    assert result.score < 1e-12
    assert result.score_oos < 1e-12
    assert status_of(result) == KEPT


def test_one_window_serves_every_shift():
    # SPY's span sets W3's first day and the indicator's span its last day.
    first, last = FIRST - 1000, LAST - 400
    noise = Z[first - Z_FIRST : last - Z_FIRST + 1][::-1]
    series = Daily(first, z(np.arange(first, last + 1) + 50) + noise)
    w3 = step3_window(SPY, series)
    assert w3 == Window(FIRST, last - 730)
    base = shift_scores(SPY, series, w3)
    # SPY outside W3 and off the 7-day grid, and the indicator beyond W3 ± 730 days.
    off_grid = np.setdiff1d(DAYS, w3.search_days())
    spy = Daily(FIRST, np.where(np.isin(DAYS, off_grid), SPY.values + 1.0, SPY.values))
    early = np.arange(first, w3.start - 730)
    other = Daily(first, np.where(np.isin(np.arange(first, last + 1), early), 5.0, series.values))
    np.testing.assert_array_equal(shift_scores(spy, other, w3), base)
    one_day = SPY.values.copy()
    one_day[w3.search_days()[100] - FIRST] += 1.0
    assert (shift_scores(Daily(FIRST, one_day), series, w3) != base).all()


TIES = [
    pytest.param([-3, -1, 2], [0.5, 0.5, 0.5], 1, id="exact"),
    pytest.param([-2, 2], [0.5, 0.5], 1, id="a-and-minus-a"),
    pytest.param([-4, 1], [1.0, 1.0 + 1e-13], 1, id="1e-13-relative"),
    pytest.param([-4, 1], [1.0, 1.0 + 1e-12], 1, id="1e-12-relative"),
    pytest.param([-4, 1], [1.0, 1.0 + 1.1e-12], 0, id="above-1e-12-relative"),
    pytest.param([-4, 1, 3], [2.0, 1.0, 0.5], 2, id="lowest"),
]


@pytest.mark.parametrize(("values", "scores", "expected"), TIES)
def test_ties_go_to_the_smaller_absolute_value(values, scores, expected):
    assert best(np.array(values), np.array(scores)) == expected


def test_a_straight_line_ties_every_shift_and_gives_a_star_0():
    # r(d + A) is r(d) plus a constant, which the intercept absorbs.
    line = Daily(FIRST, 0.3 + 0.02 * DAYS / 365.25)
    w3 = step3_window(SPY, line)
    scores = shift_scores(SPY, line, w3)
    assert scores.max() - scores.min() <= 1e-12 * scores.min()
    assert int(SHIFTS[best(SHIFTS, scores)]) == 0


@pytest.mark.parametrize(
    ("lead", "a_star", "expected"),
    [(800, -730, AT_BOUNDARY), (-800, 730, AT_BOUNDARY), (-90, 90, NOT_LEADING)],
    ids=["lead-beyond-730", "lag-beyond-730", "lag"],
)
def test_step_4(lead, a_star, expected):
    result = step3_of(lead)
    assert result.a_star == a_star
    assert status_of(result) == expected


def test_a_3_year_series_has_insufficient_history():
    weekly = np.arange(day(2005, 12, 1), day(2008, 12, 2), 7)
    y = np.sin(weekly / 200)
    smoothed = one_sided(weekly, y, 2, today=TODAY, first_day=FIRST, last_day=LAST)
    assert step3(SPY, Daily(smoothed.first_day, smoothed.values)) is None
    assert status(weekly, fetch_failed=False, invalid_values=False, step3=None, today=TODAY) == INSUFFICIENT_HISTORY


def test_a_window_needs_10_years_of_days():
    assert ten_year_window(FIRST, FIRST + 3652) == Window(FIRST, FIRST + 3652)
    assert ten_year_window(FIRST, FIRST + 3651) is None
    # An indicator of 5,113 days leaves a W3 of 3,653 days.
    assert step3_window(SPY, Daily(FIRST, SPY.values[:5113])) == Window(FIRST + 730, FIRST + 4382)
    assert step3_window(SPY, Daily(FIRST, SPY.values[:5112])) is None
    assert step3(SPY, Daily(FIRST, SPY.values[:5112])) is None


def test_undefined_days_do_not_count():
    values = np.full(SPAN, np.nan)
    assert Daily(FIRST, values).span() is None
    assert step3(SPY, Daily(FIRST, values)) is None
    values[100:-50] = 1.0
    assert Daily(FIRST, values).span() == (FIRST + 100, LAST - 50)


def test_search_days_count_back_from_the_last_day():
    assert Window(0, 20).search_days().tolist() == [6, 13, 20]
    assert Window(0, 21).search_days().tolist() == [0, 7, 14, 21]


def test_chunks_keep_every_design_within_the_limit_and_change_no_score(monkeypatch):
    rng = np.random.default_rng(3)
    y = rng.normal(size=100)
    rows = rng.normal(size=(50, 100))

    def regressors(chunk):
        return rows[chunk][:, None]

    whole = candidate_scores(y, regressors, np.arange(50), 1)
    sizes = []
    fit = analysis.lad

    def recording(X, y):
        sizes.append(X.nbytes)
        return fit(X, y)

    monkeypatch.setattr(analysis, "lad", recording)
    monkeypatch.setattr(analysis, "CHUNK_BYTES", 7 * 2 * 100 * 8)
    np.testing.assert_array_equal(candidate_scores(y, regressors, np.arange(50), 1), whole)
    assert len(sizes) == 8
    assert max(sizes) <= 7 * 2 * 100 * 8


# AC-6: three independent components with periods of at least 1,500 days, which SPY follows with known leads.

PERIODS = ((1500, 2300), (1700, 3100), (1900, 2700))
LEADS = (60, 200, 450)
BETAS = (0.8, -0.5, 1.3)
F_DAYS = np.arange(FIRST, FIRST + int(12.5 * 365.25))


def component(days, j):
    p, q = PERIODS[j]
    return np.sin(2 * np.pi * days / p + j) + 0.6 * np.sin(2 * np.pi * days / q + 2 * j + 1)


F_SPY = Daily(FIRST, 4.0 + sum(beta * component(F_DAYS, j) for j, beta in enumerate(BETAS)))
# Step 3 on one component at a time is no guide when SPY follows all three, so every first start is −365.
F_MEMBERS = tuple(Member(f"M{j}", Daily(FIRST, component(F_DAYS + lead, j)), -365) for j, lead in enumerate(LEADS))


@functools.cache
def recorded_step5():
    """Step 5 on the components, and every set of candidates its descents tried."""
    tried = []
    original = analysis.neighbours

    def recording(b, step, radius):
        tried.append(original(b, step, radius))
        return tried[-1]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(analysis, "neighbours", recording)
        return step5(F_SPY, F_MEMBERS), tried


def test_step5_recovers_every_lead_exactly():
    result, _ = recorded_step5()
    assert result.status == OK
    fit = result.combination
    assert [member.name for member in fit.members] == ["M0", "M1", "M2"]
    assert [member.b for member in fit.members] == [-60, -200, -450]
    np.testing.assert_allclose([member.beta for member in fit.members], BETAS, rtol=1e-9)
    assert fit.c == pytest.approx(4.0, rel=1e-12)
    assert fit.score < 1e-12
    assert fit.score_oos < 1e-12
    assert fit.window == Window(FIRST + 730, int(F_DAYS[-1]))


def test_two_runs_are_identical():
    assert_identical(step5(F_SPY, F_MEMBERS), recorded_step5()[0])


def test_every_candidate_lies_within_minus_730_and_0():
    _, tried = recorded_step5()
    candidates = np.concatenate(tried)
    assert candidates.min() >= -730
    assert candidates.max() <= 0


def test_the_coarse_stage_runs_on_the_28_day_grid(monkeypatch):
    tried = []
    original = analysis.neighbours

    def recording(b, step, radius):
        tried.append(original(b, step, radius))
        return tried[-1]

    monkeypatch.setattr(analysis, "neighbours", recording)
    monkeypatch.setattr(analysis, "STAGES", ((28, 730),))
    start = np.array([-365, -300, -700])
    b, _ = descend(F_SPY, F_MEMBERS, step5_window(F_SPY, F_MEMBERS), start)
    assert ((b - start) % 28 == 0).all()
    assert all(((candidates - start[k % 3]) % 28 == 0).all() for k, candidates in enumerate(tried))


MOVES = [
    pytest.param({-100: 1.0, -44: 1.0 - 2e-12}, -44, id="gain-above-1e-12"),
    # −72 ties with the start, but has the smaller |b|.
    pytest.param({-100: 1.0, -72: 1.0 - 5e-13}, -100, id="gain-within-1e-12"),
    pytest.param({-100: 1.0, -72: 0.5, -44: 0.5}, -44, id="tie-to-the-smaller-abs"),
]


@pytest.mark.parametrize(("scores", "expected"), MOVES)
def test_a_member_moves_only_for_a_gain_of_more_than_1e_12(monkeypatch, scores, expected):
    def fake(y, regressors, candidates, m):
        return np.array([scores.get(int(b), 2.0) for b in candidates])

    monkeypatch.setattr(analysis, "candidate_scores", fake)
    members = F_MEMBERS[:1]
    b, _ = descend(F_SPY, members, step5_window(F_SPY, members), np.array([-100]))
    assert b.tolist() == [expected]


RANDOM_STARTS = np.random.default_rng(0).integers(-730, 0, size=2, endpoint=True).tolist()
CHOICES = [
    pytest.param(([-150, -10], 0.5), ([-90, -20], 0.4), [-90, -20], id="lower-score"),
    pytest.param(([-150, -10], 0.4), ([-90, -20], 0.5), [-150, -10], id="lower-score-first"),
    pytest.param(([-150, -10], 0.5), ([-90, -20], 0.5), [-90, -20], id="tie-to-the-smaller-total"),
    pytest.param(([-150, -10], 0.5), ([-90, -20], 0.5 * (1 + 1e-13)), [-90, -20], id="near-tie"),
    pytest.param(([-100, -50], 0.5), ([-50, -100], 0.5), [-100, -50], id="same-total-keeps-the-first"),
]


@pytest.mark.parametrize(("first", "second", "expected"), CHOICES)
def test_the_better_start_wins_and_a_tie_goes_to_the_smaller_total_shift(monkeypatch, first, second, expected):
    starts = []

    def fake(spy, members, window, start):
        starts.append(start.tolist())
        ends = {1: first, 2: second}
        # The third descent is the out-of-sample check's, from the chosen shifts.
        shifts, end = ends.get(len(starts), (start.tolist(), 0.0))
        return np.array(shifts), end

    monkeypatch.setattr(analysis, "descend", fake)
    fit = step5(F_SPY, F_MEMBERS[:2]).combination
    assert starts == [[-365, -365], RANDOM_STARTS, expected]
    assert [member.b for member in fit.members] == expected


NEIGHBOURS = [
    pytest.param(-10, 28, 730, list(range(-710, -9, 28)), id="coarse-near-0"),
    pytest.param(-720, 7, 28, [-727, -720, -713, -706, -699, -692], id="medium-near-minus-730"),
    pytest.param(0, 1, 7, list(range(-7, 1)), id="fine-at-0"),
    pytest.param(-730, 1, 7, list(range(-730, -722)), id="fine-at-minus-730"),
]


@pytest.mark.parametrize(("b", "step", "radius", "expected"), NEIGHBOURS)
def test_neighbours_stay_on_the_grid_within_the_bounds(b, step, radius, expected):
    assert neighbours(b, step, radius).tolist() == expected


# AC-7


def linprog_lad(design, y):
    """The LAD fit as a linear program: minimize Σ(u + v) subject to Xθ + u − v = y, u, v ≥ 0."""
    p, n = design.shape
    cost = np.concatenate([np.zeros(p), np.ones(2 * n)])
    equality = np.hstack([design.T, np.eye(n), -np.eye(n)])
    bounds = [(None, None)] * p + [(0, None)] * (2 * n)
    return linprog(cost, A_eq=equality, b_eq=y, bounds=bounds, method="highs").x[:p]


def small_case(seed, noise="normal"):
    """30 points, an intercept and two regressors."""
    rng = np.random.default_rng(seed)
    r = rng.normal(size=(2, 30))
    errors = rng.normal(size=30) if noise == "normal" else rng.standard_t(2, 30)
    return np.vstack([np.ones(30), r]), 0.5 + 1.5 * r[0] - 0.7 * r[1] + 0.3 * errors


def test_lad_matches_linprog_on_a_small_case():
    design, y = small_case(0)
    np.testing.assert_allclose(lad(design[None], y)[0], linprog_lad(design, y), rtol=0, atol=1e-6)


@pytest.mark.parametrize(("seed", "noise"), [(3, "normal"), (7, "normal"), (1, "t")])
def test_lad_reaches_the_linprog_optimum_given_enough_solves(monkeypatch, seed, noise):
    # §7.1's rules stop these cases early (plans/prompt-04.md); the iteration itself converges to the optimum.
    design, y = small_case(seed, noise)
    assert np.abs(lad(design[None], y)[0] - linprog_lad(design, y)).max() > 1e-6
    monkeypatch.setattr(analysis, "MAX_SOLVES", 3000)
    monkeypatch.setattr(analysis, "OBJECTIVE_TOLERANCE", 0.0)
    np.testing.assert_allclose(lad(design[None], y)[0], linprog_lad(design, y), rtol=0, atol=1e-6)


def test_irls_never_increases_the_objective(monkeypatch):
    design, y = small_case(7)
    objectives = []
    for solves in range(1, 101):
        monkeypatch.setattr(analysis, "MAX_SOLVES", solves)
        objectives.append(np.abs(y - lad(design[None], y)[0] @ design).sum())
    assert objectives[-1] < objectives[0]
    assert all(later <= earlier for earlier, later in zip(objectives, objectives[1:]))


def test_a_solve_that_raises_the_objective_is_discarded(monkeypatch):
    design, y = small_case(0)
    monkeypatch.setattr(analysis, "MAX_SOLVES", 2)
    after_two = lad(design[None], y)
    monkeypatch.setattr(analysis, "MAX_SOLVES", 100)
    solves = []
    solve = analysis.weighted_solve

    def third_is_worse(X, y, w):
        solves.append(X.shape[0])
        theta = solve(X, y, w)
        return theta + 1.0 if len(solves) == 3 else theta

    monkeypatch.setattr(analysis, "weighted_solve", third_is_worse)
    np.testing.assert_array_equal(lad(design[None], y), after_two)
    assert len(solves) == 3


@pytest.mark.parametrize(
    ("log_f", "expected"),
    [([0.0, 1.0, 3.0], 1.0), ([0.0, 1.0, 3.0, 10.0], 3.0), ([5.0, 5.0, 5.0], 0.0), ([3.0, 0.0, 1.0], 1.0)],
    ids=["odd", "even", "flat", "unordered"],
)
def test_score_is_the_mean_absolute_deviation_from_the_median(log_f, expected):
    assert score(np.array(log_f)) == expected


def test_score_runs_along_the_last_axis():
    np.testing.assert_array_equal(score(np.array([[0.0, 1.0, 3.0], [2.0, 2.0, 2.0]])), [1.0, 0.0])


@pytest.mark.parametrize("rate", [0.1, -0.05, 0.0])
def test_slope_is_in_percent_per_year(rate):
    days = np.arange(9000, 9000 + 4 * 365)
    expected = 100 * (math.exp(rate) - 1)
    assert slope_pct_per_year(days, 3.0 + rate * days / 365.25) == pytest.approx(expected, rel=1e-9, abs=1e-12)


ONE_DAYS = F_DAYS
ONE_SPY = Daily(FIRST, 4.0 + 0.7 * component(ONE_DAYS, 0) + np.random.default_rng(5).normal(0, 0.02, ONE_DAYS.size))
ONE_MEMBER = Member("M0", Daily(FIRST, component(ONE_DAYS + 120, 0)), -100)


@functools.cache
def one_member():
    return step5(ONE_SPY, (ONE_MEMBER,)).combination


def test_reported_f_and_composite_follow_the_formulas():
    fit = one_member()
    (member,) = fit.members
    days = fit.window.days()
    combined = member.beta * ONE_MEMBER.series.at(days + member.b)
    log_f = ONE_SPY.at(days) - combined
    np.testing.assert_allclose(fit.f, np.exp(log_f - np.median(log_f)), rtol=1e-12)
    assert np.median(np.log(fit.f)) == pytest.approx(0.0, abs=1e-15)
    assert np.ptp(fit.f) > 0.05
    np.testing.assert_allclose(fit.composite, np.exp(fit.c + combined), rtol=1e-12)
    # The composite is in USD: it tracks SPYL1 where F is 1.
    np.testing.assert_allclose(fit.composite * fit.f, np.exp(ONE_SPY.at(days)), rtol=1e-6)
    assert fit.score == pytest.approx(np.mean(np.abs(log_f - np.median(log_f))), rel=1e-12)
    slope = np.polyfit(days / 365.25, log_f, 1)[0]
    assert fit.slope_pct_per_year == pytest.approx(100 * (math.exp(slope) - 1), rel=1e-6, abs=1e-9)
    assert fit.split_day == fit.window.start + int(0.7 * fit.window.length)


def test_reported_step3_values_use_every_day_of_the_window():
    result = step3_of(800)
    days = result.window.days()
    regressors = indicator(800).at(days + result.a_star)
    design = np.vstack([np.ones(days.size), regressors])
    assert result.beta == lad(design[None], SPY.at(days))[0, 1]
    log_f = SPY.at(days) - result.beta * regressors
    assert result.score == pytest.approx(np.mean(np.abs(log_f - np.median(log_f))), rel=1e-12)
    slope = np.polyfit(days / 365.25, log_f, 1)[0]
    assert result.slope_pct_per_year == pytest.approx(100 * (math.exp(slope) - 1), rel=1e-6)


# AC-8


def test_the_split_day_starts_the_last_30_percent():
    assert Window(0, 9).split() == (Window(0, 6), Window(7, 9))
    # 0.7 · 90 is 62.99999999999999 in floating point.
    assert Window(0, 89).split()[1].start == 63
    assert Window(100, 3752).split()[1].start == 100 + 2557


def step_after(series, first_day, amount=0.3):
    return Daily(series.first_day, series.values + amount * (np.arange(series.values.size) + series.first_day >= first_day))


def ramp_before(series, last_day, days=400, amount=0.3):
    offset = np.arange(series.values.size) + series.first_day - (last_day - days)
    return Daily(series.first_day, series.values + amount * np.clip(offset / days, 0, 1) * (offset <= days))


def test_step3_out_of_sample_fits_on_the_first_70_percent_only(monkeypatch):
    series = indicator(800)
    result = step3_of(800)
    first, rest = result.window.split()
    windows = []

    def recording(original):
        def wrapper(*args):
            windows.append(args[-1])
            return original(*args)

        return wrapper

    for name in ("search_shift", "fit"):
        monkeypatch.setattr(analysis, name, recording(getattr(analysis, name)))
    # A constant on the last 30% changes no in-sample fit, and the out-of-sample median absorbs it.
    shifted = step3_out_of_sample(step_after(SPY, rest.start), series, result.window)
    assert windows == [first, first]
    assert shifted == pytest.approx(result.score_oos, rel=1e-12)
    assert step3_out_of_sample(ramp_before(SPY, rest.start - 1), series, result.window) != pytest.approx(
        result.score_oos, rel=1e-6
    )


def test_step5_out_of_sample_fits_on_the_first_70_percent_only():
    fit = one_member()
    b = np.array([fit.members[0].b])
    members = (ONE_MEMBER,)
    shifted = step5_out_of_sample(step_after(ONE_SPY, fit.split_day), members, fit.window, b)
    assert shifted == pytest.approx(fit.score_oos, rel=1e-12)
    changed = step5_out_of_sample(ramp_before(ONE_SPY, fit.split_day - 1), members, fit.window, b)
    assert changed != pytest.approx(fit.score_oos, rel=1e-6)


def test_the_out_of_sample_check_changes_nothing_else(monkeypatch):
    monkeypatch.setattr(analysis, "SPLIT_TENTHS", 5)
    patched = step3(SPY, indicator(800))
    base = step3_of(800)
    assert patched.score_oos != base.score_oos
    assert_identical(dataclasses.replace(patched, score_oos=base.score_oos), base)
    patched_f = step5(ONE_SPY, (ONE_MEMBER,)).combination
    base_f = one_member()
    assert (patched_f.score_oos, patched_f.split_day) != (base_f.score_oos, base_f.split_day)
    assert_identical(dataclasses.replace(patched_f, score_oos=base_f.score_oos, split_day=base_f.split_day), base_f)


# AC-14, analysis part


def made(a_star):
    return Step3(Window(0, 3652), a_star, 1.0, 0.1, 0.2, 0.0)


WEEKLY = np.arange(TODAY - 7 * 200, TODAY, 7)
STALE = WEEKLY - 730
NONE = np.empty(0, dtype=np.int64)

STATUS_CASES = [
    pytest.param(NONE, False, False, None, PENDING, id="no-data"),
    pytest.param(NONE, True, False, None, FETCH_FAILED, id="no-data-after-a-failed-fetch"),
    pytest.param(WEEKLY, True, False, made(-90), KEPT, id="a-failed-fetch-with-data-changes-nothing"),
    pytest.param(WEEKLY, False, True, None, INVALID_VALUES, id="invalid-values"),
    pytest.param(WEEKLY, False, False, None, INSUFFICIENT_HISTORY, id="no-step-3-result"),
    pytest.param(WEEKLY, False, False, made(-730), AT_BOUNDARY, id="a-star-minus-730"),
    pytest.param(WEEKLY, False, False, made(730), AT_BOUNDARY, id="a-star-730"),
    pytest.param(WEEKLY, False, False, made(0), NOT_LEADING, id="a-star-0"),
    pytest.param(WEEKLY, False, False, made(1), NOT_LEADING, id="a-star-1"),
    pytest.param(STALE, False, False, made(30), NOT_LEADING, id="stale-but-not-leading"),
    pytest.param(STALE, False, False, made(-90), DISCONTINUED, id="stale"),
    pytest.param(WEEKLY, False, False, made(-1), KEPT, id="a-star-minus-1"),
    pytest.param(WEEKLY, False, False, made(-729), KEPT, id="a-star-minus-729"),
]


@pytest.mark.parametrize(("release_days", "fetch_failed", "invalid_values", "result", "expected"), STATUS_CASES)
def test_status_is_the_first_that_applies(release_days, fetch_failed, invalid_values, result, expected):
    assert (
        status(release_days, fetch_failed=fetch_failed, invalid_values=invalid_values, step3=result, today=TODAY)
        == expected
    )


def test_a_stale_series_is_discontinued_and_never_in_f():
    stale = status_of(step3_of(90), release_days=RELEASES - 400)
    assert stale == DISCONTINUED
    assert not is_member(stale, "Stale", ())


@pytest.mark.parametrize(("days_after", "expected"), [(35, False), (36, True)])
def test_discontinued_after_3_spacings_and_14_days(days_after, expected):
    # Weekly releases: 3 × 7 + 14 = 35 days.
    assert discontinued(WEEKLY, int(WEEKLY[-1]) + days_after) is expected


def test_the_spacing_comes_from_the_2_years_up_to_the_latest_release():
    # 20 years of monthly releases, then weekly ones: over the last 2 years the median spacing is 7, overall 30.
    monthly = np.arange(day(2000, 1, 15), day(2020, 1, 1), 30)
    days = np.concatenate([monthly, np.arange(monthly[-1] + 7, day(2022, 7, 1), 7)])
    assert discontinued(days, int(days[-1]) + 50)
    # A series that stopped 5 years ago keeps the spacing it had then.
    assert discontinued(days, int(days[-1]) + 5 * 365)
    assert not discontinued(np.array([100, 110, 130, 150, 170]), 170 + 3 * 20 + 14)


@pytest.mark.parametrize(
    ("candidate", "excluded", "expected"),
    [(KEPT, (), True), (KEPT, ("M2",), False), (KEPT, ("Other",), True), (DISCONTINUED, (), False)],
    ids=["kept", "unchecked", "another-unchecked", "discontinued"],
)
def test_members_are_kept_and_not_unchecked(candidate, excluded, expected):
    assert is_member(candidate, "M2", excluded) is expected


def test_a_kept_series_is_named_leadl1():
    assert lead_name("M2") == "LEADL1_M2"


def test_no_members_give_no_f():
    assert step5(SPY, []) == Step5(NO_MEMBERS)


def test_members_that_overlap_less_than_10_years_give_no_f():
    span = int(15 * 365.25)
    early = Member("A", Daily(FIRST, np.zeros(span)), -100)
    late = Member("B", Daily(FIRST + int(5 * 365.25), np.zeros(span)), -100)
    spy = Daily(FIRST, np.zeros(int(20 * 365.25)))
    assert step5_window(spy, [early, late]) is None
    assert step5(spy, [early, late]) == Step5(INSUFFICIENT_OVERLAP)
