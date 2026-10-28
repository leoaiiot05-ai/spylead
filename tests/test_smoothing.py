import bisect
import datetime
import math

import numpy as np
import pytest
from scipy.interpolate import make_smoothing_spline

from spylead import smoothing
from spylead.smoothing import (
    MIN_POINTS,
    end_slopes,
    inflection_count,
    inflection_target,
    inflections,
    irls_weights,
    one_sided,
    reference_fit,
    refit_anchors,
    refit_days,
    robust_fit,
    roughness,
    search_lambda,
    spline_values,
    weighted_spline,
)

EPOCH = datetime.date(1970, 1, 1)


def day(year, month, date):
    return (datetime.date(year, month, date) - EPOCH).days


def last_month_end_before(d):
    return ((EPOCH + datetime.timedelta(days=d)).replace(day=1) - EPOCH).days - 1


def uneven(seed, n=40):
    """Uneven knots in years, values well away from zero, and random weights."""
    rng = np.random.default_rng(seed)
    x = 30 + np.cumsum(rng.uniform(0.02, 0.5, n))
    return x, 2 + np.sin(x) + rng.normal(0, 0.1, n), rng.uniform(0.2, 5.0, n)


def noisy(seed, n=60):
    """A cycle with heavy-tailed noise, so that some points are outliers."""
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.uniform(0.01, 0.2, n))
    return x, np.sin(3 * x) + 0.2 * rng.standard_t(2, n)


def search_series(seed):
    """80 release days over 6 years, with a 1.5-year cycle on a trend."""
    rng = np.random.default_rng(seed)
    days = 10_000 + np.sort(rng.choice(int(6 * 365.25), 80, replace=False))
    x = days / 365.25
    return days, x, np.sin(2 * np.pi * x / 1.5) + 0.3 * x + rng.normal(0, 0.1, x.size)


def released(months, seed=0):
    """One point in each of `months`, counted from January 2001, released on a random day of that month."""
    rng = np.random.default_rng(seed)
    days = np.array([day(2001 + m // 12, m % 12 + 1, int(rng.integers(5, 26))) for m in months], dtype=np.int64)
    x = days / 365.25
    return days, np.sin(2 * np.pi * x / 2.5) + 0.2 * x + rng.normal(0, 0.05, days.size)


def objective(y, lam, fit):
    """Σ|y_i − g(x_i)| + λ ∫ g''², the objective of SPEC.md §6.1."""
    return float(np.abs(y - fit.spline.g).sum()) + lam * roughness(fit.spline)


# AC-1


@pytest.mark.parametrize("lam", [1e-4, 1e-2, 1.0])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_weighted_solve_matches_make_smoothing_spline(seed, lam):
    x, y, w = uneven(seed)
    ours = weighted_spline(x, y, w, lam)
    theirs = make_smoothing_spline(x, y, w=w, lam=lam)
    np.testing.assert_allclose(ours.g, theirs(x), rtol=1e-6)
    grid = np.linspace(x[0], x[-1], 2001)
    np.testing.assert_allclose(spline_values(ours, grid), theirs(grid), rtol=1e-6)
    second = theirs.derivative(2)(x)
    np.testing.assert_allclose(ours.gamma, second, rtol=0, atol=1e-6 * np.abs(second).max())


def test_roughness_is_the_integral_of_the_squared_second_derivative():
    x, y, w = uneven(3)
    second = make_smoothing_spline(x, y, w=w, lam=1e-2).derivative(2)
    # g'' is linear between knots, so Simpson's rule is exact on each interval.
    middle = (x[:-1] + x[1:]) / 2
    simpson = np.sum(np.diff(x) / 6 * (second(x[:-1]) ** 2 + 4 * second(middle) ** 2 + second(x[1:]) ** 2))
    assert roughness(weighted_spline(x, y, w, 1e-2)) == pytest.approx(simpson, rel=1e-6)


def test_value_formula_is_continuous_at_the_knots():
    x, y, w = uneven(4)
    spline = weighted_spline(x, y, w, 1e-2)
    np.testing.assert_allclose(spline_values(spline, x), spline.g, rtol=1e-12)
    # Just below a knot, the piece that ends there gives the same value as the piece that starts there.
    np.testing.assert_allclose(spline_values(spline, np.nextafter(x[1:], -np.inf)), spline.g[1:], rtol=1e-12)


def test_linear_extension_continues_the_end_values_with_the_end_slopes():
    x, y, w = uneven(5)
    spline = weighted_spline(x, y, w, 1e-2)
    first, last = end_slopes(spline)
    derivative = make_smoothing_spline(x, y, w=w, lam=1e-2).derivative()
    np.testing.assert_allclose([first, last], derivative([x[0], x[-1]]), rtol=1e-6)
    before, after = x[0] - np.array([0.25, 3.0]), x[-1] + np.array([0.25, 3.0])
    np.testing.assert_allclose(spline_values(spline, before), spline.g[0] + first * (before - x[0]), rtol=1e-12)
    np.testing.assert_allclose(spline_values(spline, after), spline.g[-1] + last * (after - x[-1]), rtol=1e-12)


# AC-2


IRLS_CASES = [
    pytest.param(0, 1e-3, None, id="unit-weights"),
    pytest.param(1, 1e-1, 10.0, id="weights-from-a-smoother-fit"),
    pytest.param(2, 10.0, 1e-3, id="weights-from-a-rougher-fit"),
    # Solve 9 comes out 3e-8 worse than solve 8, and is discarded.
    pytest.param(27, 1e6, None, id="worse-solve"),
]


@pytest.mark.parametrize(("seed", "lam", "start_lam"), IRLS_CASES)
def test_irls_never_increases_the_objective(monkeypatch, seed, lam, start_lam):
    x, y = noisy(seed)
    weights = None if start_lam is None else robust_fit(x, y, start_lam).weights
    objectives = []
    for solves in range(1, smoothing.MAX_SOLVES + 1):
        monkeypatch.setattr(smoothing, "MAX_SOLVES", solves)
        objectives.append(robust_fit(x, y, lam, weights).objective)
    assert objectives[-1] < objectives[0]
    assert all(later <= earlier for earlier, later in zip(objectives, objectives[1:]))


@pytest.mark.parametrize("lam", [1e-3, 1e-1, 10.0])
def test_robust_fit_minimizes_the_objective_of_section_6_1(lam):
    x, y = noisy(3)
    fit = robust_fit(x, y, lam)
    assert fit.objective == pytest.approx(objective(y, lam, fit), rel=1e-12)
    for other in (lam / 2, lam * 2):
        assert objective(y, lam, robust_fit(x, y, other)) > fit.objective


def test_robust_fit_starts_from_the_weights_passed_in_and_returns_the_next_ones(monkeypatch):
    x, y = noisy(4)
    weights = np.random.default_rng(4).uniform(0.5, 2.0, x.size)
    monkeypatch.setattr(smoothing, "MAX_SOLVES", 1)
    fit = robust_fit(x, y, 0.1, weights)
    # Penalty 2λ: plans/prompt-02.md explains why.
    np.testing.assert_array_equal(fit.spline.g, weighted_spline(x, y, weights, 0.2).g)
    np.testing.assert_array_equal(fit.weights, irls_weights(np.abs(y - fit.spline.g)))


def test_irls_weights_cap_small_residuals():
    residuals = np.array([0.0, 1e-9, 0.5, 1.0, 2.0])
    delta = 1e-4 * 0.5
    np.testing.assert_allclose(irls_weights(residuals), 1 / np.array([delta, delta, 0.5, 1.0, 2.0]), rtol=1e-15)
    np.testing.assert_array_equal(irls_weights(np.zeros(3)), np.full(3, 1e12))


def test_a_spike_moves_the_robust_fit_less_than_the_l2_fit():
    rng = np.random.default_rng(5)
    x = np.cumsum(rng.uniform(0.02, 0.1, 60))
    y = np.sin(2 * x) + rng.normal(0, 0.05, x.size)
    spiked = y.copy()
    spiked[30] += 3.0

    def l2(values):
        return weighted_spline(x, values, np.ones(x.size), 1e-3).g

    def l1(values):
        return robust_fit(x, values, 1e-3).spline.g

    assert np.abs(l1(spiked) - l1(y)).max() < np.abs(l2(spiked) - l2(y)).max() / 10


INFLECTIONS = [
    pytest.param([0, 0, 0, 0], 0, id="straight-line"),
    pytest.param([0, 1, 2, 1, 0], 0, id="one-sign"),
    pytest.param([0, 1, -1, 0], 1, id="one-change"),
    pytest.param([0, -2, 3, -1, 2, 0], 3, id="alternating"),
    pytest.param([0, 1, 1e-10, -1, 0], 1, id="near-zero-between-the-signs"),
    pytest.param([0, 1, -1e-10, 1, 0], 0, id="near-zero-of-the-other-sign"),
    pytest.param([0, 1, -1e-9, 1, 0], 0, id="at-the-threshold"),
    pytest.param([0, 1, -1.1e-9, 1, 0], 2, id="above-the-threshold"),
    pytest.param([0, 1e6, -1e-4, 1e6, 0], 0, id="relative-to-the-largest"),
    pytest.param([0, 5e-12, -5e-12, 0], 1, id="tiny-but-alone"),
]


@pytest.mark.parametrize(("gamma", "expected"), INFLECTIONS)
def test_inflection_count(gamma, expected):
    assert inflection_count(np.array(gamma, dtype=float)) == expected


@pytest.mark.parametrize(
    ("span", "n_per_year", "expected"),
    [(1461, 0.25, 1), (1460, 0.25, 0), (1096, 2, 6), (1095, 2, 5), (3653, 24, 240)],
)
def test_inflection_target_is_floor_of_n_times_the_span_in_years(span, n_per_year, expected):
    assert inflection_target(np.array([9000, 9000 + span]), n_per_year) == expected


@pytest.mark.parametrize(("seed", "n_per_year"), [(0, 2), (1, 6), (3, 0.5)])
def test_search_returns_at_most_t_inflections_and_more_just_below(seed, n_per_year):
    days, x, y = search_series(seed)
    target = inflection_target(days, n_per_year)
    smoothed = search_lambda(x, y, target)
    assert not smoothed.at_limit
    assert smoothing.LOG_LAMBDA_LOW < smoothed.log10_lam < smoothing.LOG_LAMBDA_HIGH
    assert inflections(smoothed.fit) <= target
    # Weights carried from the search's fit, as from one bisection step to the next.
    below = robust_fit(x, y, smoothed.lam * 10**-0.005, smoothed.fit.weights)
    assert inflections(below) > target


def test_a_warm_start_gives_the_lambda_of_the_full_bracket():
    days, x, y = search_series(1)
    target = inflection_target(days, 0.5)
    full = search_lambda(x, y, target)
    # The previous refit's λ, and starts on the 1/256 grid that every bisection result lies on.
    previous = search_lambda(x[:-3], y[:-3], inflection_target(days[:-3], 0.5))
    starts = [previous.log10_lam] + [full.log10_lam + k / 256 for k in (-192, -64, 0, 64, 224)]
    for start in starts:
        assert abs(start - full.log10_lam) < smoothing.WARM_HALF_WIDTH
        assert search_lambda(x, y, target, start).log10_lam == full.log10_lam


def test_a_warm_bracket_without_the_crossing_falls_back_to_the_full_bracket():
    days, x, y = search_series(1)
    target = inflection_target(days, 0.5)
    full = search_lambda(x, y, target)
    for start in (full.log10_lam - 3, full.log10_lam + 3):
        smoothed = search_lambda(x, y, target, start)
        assert abs(smoothed.log10_lam - start) > smoothing.WARM_HALF_WIDTH
        assert smoothed.log10_lam == full.log10_lam


@pytest.mark.parametrize("start", [7.6, -7.6])
def test_a_warm_bracket_stays_within_the_lambda_limits(monkeypatch, start):
    days, x, y = search_series(1)
    tried = []
    fit = smoothing.robust_fit

    def recording(x, y, lam, weights=None):
        tried.append(math.log10(lam))
        return fit(x, y, lam, weights)

    monkeypatch.setattr(smoothing, "robust_fit", recording)
    search_lambda(x, y, inflection_target(days, 0.5), start)
    assert -8 - 1e-9 <= min(tried) and max(tried) <= 8 + 1e-9


@pytest.mark.parametrize("start", [None, 7.5])
def test_too_many_inflections_at_the_largest_lambda_give_the_upper_limit(start):
    rng = np.random.default_rng(0)
    x = np.sort(rng.uniform(0, 3, 40))
    y = (x - 1.5) ** 3 + rng.normal(0, 0.01, x.size)
    smoothed = search_lambda(x, y, 0, start)
    assert (smoothed.log10_lam, smoothed.lam, smoothed.at_limit) == (8.0, 1e8, True)
    assert inflections(smoothed.fit) > 0


def test_few_enough_inflections_at_the_smallest_lambda_give_the_lower_limit():
    x = np.sort(np.random.default_rng(0).uniform(0, 3, 40))
    smoothed = search_lambda(x, x**2, 0)
    assert (smoothed.log10_lam, smoothed.at_limit) == (-8.0, False)
    assert smoothed.lam == pytest.approx(1e-8)


# AC-3

REFIT_DAYS = [
    pytest.param(
        day(2001, 2, 14), day(2004, 6, 1), [day(2004, 2, 29), day(2004, 3, 31), day(2004, 4, 30), day(2004, 5, 31)],
        id="mid-month",
    ),
    pytest.param(day(2001, 1, 31), day(2004, 1, 31), [], id="first-refit-day-is-today"),
    pytest.param(day(2001, 1, 31), day(2004, 2, 1), [day(2004, 1, 31)], id="exactly-3-years"),
    pytest.param(day(2000, 2, 29), day(2003, 3, 1), [day(2003, 2, 28)], id="29-february"),
    pytest.param(day(2003, 12, 1), day(2005, 1, 15), [], id="under-3-years"),
]


@pytest.mark.parametrize(("first_day", "today", "expected"), REFIT_DAYS)
def test_refit_days_run_from_3_years_after_the_first_point_to_before_today(first_day, today, expected):
    assert refit_days(first_day, today).tolist() == expected


TODAY = day(2004, 7, 20)


@pytest.fixture(scope="module")
def monthly():
    """42 monthly points from January 2001, and their one-sided series: refits from January to June 2004."""
    days, y = released(range(42))
    return days, y, one_sided(days, y, 2, today=TODAY, first_day=day(2000, 12, 1), last_day=TODAY)


def test_no_value_depends_on_points_released_after_the_last_month_end_before_it(monthly):
    days, y, base = monthly
    rng = np.random.default_rng(11)
    first_anchor = int(base.anchors.month_end[0])
    # A refit's own month end is where an off-by-one would look ahead.
    for d in [*rng.integers(first_anchor - 15, TODAY + 1, 2).tolist(), int(base.anchors.month_end[2])]:
        cut = last_month_end_before(d)
        later = days > cut
        changed = np.where(later, y + rng.normal(0, 3, y.size), y)
        known = d - base.first_day + 1
        for other_days, other_y in ((days, changed), (days[~later], y[~later])):
            other = one_sided(other_days, other_y, 2, today=TODAY, first_day=base.first_day, last_day=TODAY)
            np.testing.assert_array_equal(other.values[:known], base.values[:known])
            if (base.anchors.month_end > cut).any():
                assert not np.array_equal(other.values, base.values, equal_nan=True)


def test_values_follow_the_anchor_line(monthly):
    _, _, result = monthly
    anchors = result.anchors
    month_ends = anchors.month_end.tolist()
    expected = []
    for d in range(result.first_day, result.first_day + result.values.size):
        k = bisect.bisect_left(month_ends, d) - 1
        expected.append(math.nan if k < 0 else anchors.value[k] + anchors.slope[k] * (d / 365.25 - anchors.x[k]))
    np.testing.assert_allclose(result.values, expected, rtol=1e-12)


def test_each_anchor_is_the_end_value_and_end_slope_of_its_refit(monthly):
    days, y, result = monthly
    anchors = result.anchors
    assert anchors.month_end.tolist() == refit_days(int(days[0]), TODAY).tolist()
    start = None
    for k, month_end in enumerate(anchors.month_end.tolist()):
        known = days <= month_end
        smoothed = search_lambda(days[known] / 365.25, y[known], inflection_target(days[known], 2), start)
        spline = smoothed.fit.spline
        assert anchors.x[k] == days[known][-1] / 365.25
        assert (anchors.value[k], anchors.slope[k]) == (spline.g[-1], end_slopes(spline)[1])
        assert (anchors.lam[k], anchors.at_limit[k]) == (smoothed.lam, smoothed.at_limit)
        start = smoothed.log10_lam


def test_no_value_before_the_first_anchor(monthly):
    _, _, result = monthly
    first = int(result.anchors.month_end[0]) - result.first_day
    defined = ~np.isnan(result.values)
    assert not defined[: first + 1].any()
    assert defined[first + 1 :].all()


def test_a_refit_with_fewer_than_8_points_gives_no_anchor():
    # A point every 6 months: 7 by January 2004, the 8th in July 2004.
    days, y = released(range(0, 43, 6))
    result = one_sided(days, y, 2, today=day(2004, 9, 20), first_day=day(2004, 1, 1), last_day=day(2004, 9, 20))
    assert refit_days(int(days[0]), day(2004, 9, 20))[0] == day(2004, 1, 31)
    # August has no new point, so it keeps July's anchor.
    assert result.anchors.month_end.tolist() == [day(2004, 7, 31)]
    assert np.isnan(result.values[: day(2004, 7, 31) - day(2004, 1, 1) + 1]).all()


def test_a_month_without_a_new_point_keeps_the_previous_anchor():
    days, y = released([m for m in range(42) if m != 39])  # nothing released in April 2004
    result = one_sided(days, y, 2, today=TODAY, first_day=day(2004, 1, 1), last_day=TODAY)
    anchors = result.anchors
    month_ends = anchors.month_end.tolist()
    assert day(2004, 4, 30) not in month_ends
    k = month_ends.index(day(2004, 3, 31))
    assert month_ends[k + 1] == day(2004, 5, 31)
    april_and_may = np.arange(day(2004, 4, 1), day(2004, 5, 31) + 1)
    expected = anchors.value[k] + anchors.slope[k] * (april_and_may / 365.25 - anchors.x[k])
    np.testing.assert_allclose(result.values[april_and_may - result.first_day], expected, rtol=1e-12)


def test_each_refit_reports_its_lambda_and_whether_it_is_at_the_upper_limit():
    # N = 0.25 over 3 to 3.5 years allows no inflection point, which even λ = 10^8 cannot reach here.
    days, y = released(range(42))
    anchors = refit_anchors(days, y, 0.25, TODAY)
    assert anchors.month_end.size == 6
    assert anchors.at_limit.all()
    np.testing.assert_array_equal(anchors.lam, np.full(6, 1e8))


@pytest.mark.parametrize("count", [0, MIN_POINTS - 1])
def test_fewer_than_8_points_give_no_one_sided_value_and_no_reference_fit(count):
    days, y = released(range(0, 6 * count, 6))
    today = day(2006, 1, 1)
    result = one_sided(days, y, 2, today=today, first_day=day(2001, 1, 1), last_day=today)
    assert result.anchors.month_end.size == 0
    assert result.values.size == today - day(2001, 1, 1) + 1
    assert np.isnan(result.values).all()
    assert reference_fit(days, y, 2).values.size == 0


def test_reference_fit_is_one_fit_of_every_point_evaluated_daily_inside_them():
    days, y = released(range(42))
    reference = reference_fit(days, y, 2)
    smoothed = search_lambda(days / 365.25, y, inflection_target(days, 2))
    assert reference.first_day == days[0]
    assert reference.values.size == days[-1] - days[0] + 1
    assert np.isfinite(reference.values).all()
    np.testing.assert_allclose(reference.values[days - days[0]], smoothed.fit.spline.g, rtol=1e-12)
    assert (reference.lam, reference.at_limit) == (smoothed.lam, smoothed.at_limit)
