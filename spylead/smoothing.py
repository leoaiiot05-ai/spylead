"""Robust smoothing spline, smoothness N, one-sided series and reference fit (SPEC.md §6). Pure.

A fit minimizes Σ|y_i − g(x_i)| + λ ∫ g''² over natural cubic splines g, with x = day / 365.25 in years.
"""
import math
from dataclasses import dataclass

import numpy as np
from scipy.linalg import solveh_banded

from spylead.pit import DAYS_PER_YEAR, days_to_x

MIN_POINTS = 8
WARM_UP_MONTHS = 36
LOG_LAMBDA_LOW = -8.0
LOG_LAMBDA_HIGH = 8.0
WARM_HALF_WIDTH = 1.0
MAX_HALVINGS = 30
BRACKET_WIDTH = 0.005
MAX_SOLVES = 50
OBJECTIVE_TOLERANCE = 1e-6
DELTA_FRACTION = 1e-4
DELTA_FLOOR = 1e-12
FLAT_FRACTION = 1e-9


@dataclass(frozen=True)
class Spline:
    """A natural cubic spline: values g and second derivatives gamma at knots x. gamma is 0 at both ends."""

    x: np.ndarray
    g: np.ndarray
    gamma: np.ndarray


@dataclass(frozen=True)
class RobustFit:
    spline: Spline
    weights: np.ndarray  # for the next solve, from this fit's residuals
    objective: float  # Σ|y − g| + λ ∫ g''²


@dataclass(frozen=True)
class Smoothed:
    """The λ that SPEC.md §6.3 chooses, and the fit there."""

    log10_lam: float
    at_limit: bool  # λ = 10^8 still leaves more than T inflection points: LAMBDA_AT_LIMIT
    fit: RobustFit

    @property
    def lam(self) -> float:
        return 10.0**self.log10_lam


@dataclass(frozen=True)
class Anchors:
    """One entry per refit that produced an anchor, in month-end order (SPEC.md §6.4)."""

    month_end: np.ndarray  # m_k, as a day
    x: np.ndarray  # x_k: the x of the last point the refit used
    value: np.ndarray  # v_k: the fitted value there
    slope: np.ndarray  # s_k: the end slope, per year
    lam: np.ndarray
    at_limit: np.ndarray


@dataclass(frozen=True)
class OneSided:
    """One value per day from first_day, NaN where undefined (SPEC.md §6.4), and the anchors behind them."""

    first_day: int
    values: np.ndarray
    anchors: Anchors


@dataclass(frozen=True)
class Reference:
    """The reference fit (SPEC.md §6.5): one value per day from first_day. Display only."""

    first_day: int
    values: np.ndarray
    lam: float
    at_limit: bool


def weighted_spline(x: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float) -> Spline:
    """Minimizes Σ w_i (y_i − g_i)² + λ ∫ g''² over natural cubic splines with knots x (SPEC.md §6.2).

    Reinsch form: (R + λ QᵀW⁻¹Q) γ = Qᵀy, a band of two off-diagonals, then g = y − λ W⁻¹Qγ.
    Column j of Q holds q0, q1 and q2 in rows j, j + 1 and j + 2. Needs at least 3 knots.
    """
    h = np.diff(x)
    q0, q2 = 1.0 / h[:-1], 1.0 / h[1:]
    q1 = -(q0 + q2)
    d = 1.0 / w
    band = np.zeros((3, x.size - 2))
    band[0] = (h[:-1] + h[1:]) / 3 + lam * (q0**2 * d[:-2] + q1**2 * d[1:-1] + q2**2 * d[2:])
    band[1, :-1] = h[1:-1] / 6 + lam * (q1[:-1] * d[1:-2] * q0[1:] + q2[:-1] * d[2:-1] * q1[1:])
    band[2, :-2] = lam * q2[:-2] * d[2:-2] * q0[2:]
    gamma = solveh_banded(band, q0 * y[:-2] + q1 * y[1:-1] + q2 * y[2:], lower=True)
    q_gamma = np.zeros(x.size)
    q_gamma[:-2] += q0 * gamma
    q_gamma[1:-1] += q1 * gamma
    q_gamma[2:] += q2 * gamma
    return Spline(x, y - lam * d * q_gamma, np.concatenate(([0.0], gamma, [0.0])))


def spline_values(spline: Spline, at: np.ndarray) -> np.ndarray:
    """g at any x: the cubic inside [x_1, x_n], the linear extension outside (SPEC.md §6.2)."""
    x, g, gamma = spline.x, spline.g, spline.gamma
    at = np.asarray(at, dtype=float)
    i = np.clip(np.searchsorted(x, at, side="right") - 1, 0, x.size - 2)
    h = x[i + 1] - x[i]
    a, b = at - x[i], x[i + 1] - at
    cubic = (a * g[i + 1] + b * g[i]) / h - a * b / 6 * ((1 + a / h) * gamma[i + 1] + (1 + b / h) * gamma[i])
    first_slope, last_slope = end_slopes(spline)
    before = g[0] + first_slope * (at - x[0])
    after = g[-1] + last_slope * (at - x[-1])
    return np.where(at < x[0], before, np.where(at > x[-1], after, cubic))


def end_slopes(spline: Spline) -> tuple[float, float]:
    """The slopes at x_1 and x_n (SPEC.md §6.2), which the linear extension continues."""
    x, g, gamma = spline.x, spline.g, spline.gamma
    first, last = x[1] - x[0], x[-1] - x[-2]
    return (
        float((g[1] - g[0]) / first - first * gamma[1] / 6),
        float((g[-1] - g[-2]) / last + last * gamma[-2] / 6),
    )


def roughness(spline: Spline) -> float:
    """∫ g''² over [x_1, x_n], exact because g'' is linear between knots."""
    h = np.diff(spline.x)
    left, right = spline.gamma[:-1], spline.gamma[1:]
    return float(np.sum(h * (left**2 + left * right + right**2)) / 3)


def irls_weights(residuals: np.ndarray) -> np.ndarray:
    """w_i = 1 / max(r_i, δ) for absolute residuals r_i (SPEC.md §6.2)."""
    delta = max(DELTA_FRACTION * float(np.median(residuals)), DELTA_FLOOR)
    return 1.0 / np.maximum(residuals, delta)


def robust_fit(x: np.ndarray, y: np.ndarray, lam: float, weights: np.ndarray | None = None) -> RobustFit:
    """The L1 fit of SPEC.md §6.1 by IRLS (§6.2), from unit weights or from `weights`."""
    fit = irls_step(x, y, lam, np.ones(x.size) if weights is None else weights)
    for _ in range(MAX_SOLVES - 1):
        trial = irls_step(x, y, lam, fit.weights)
        if trial.objective > fit.objective:
            # δ moves between solves and caps some weights, so a solve can come out slightly worse.
            break
        converged = fit.objective - trial.objective <= OBJECTIVE_TOLERANCE * fit.objective
        fit = trial
        if converged:
            break
    return fit


def irls_step(x: np.ndarray, y: np.ndarray, lam: float, w: np.ndarray) -> RobustFit:
    # (w r² + 1/w) / 2 ≥ |r|, with equality at the residuals that set w. With penalty 2λ, the solve minimizes that bound
    # of Σ|r| + λ ∫ g''²; with penalty λ, IRLS would converge to the fit for λ / 2.
    spline = weighted_spline(x, y, w, 2 * lam)
    residuals = np.abs(y - spline.g)
    return RobustFit(spline, irls_weights(residuals), float(residuals.sum()) + lam * roughness(spline))


def inflection_count(gamma: np.ndarray) -> int:
    """Sign changes in γ after dropping the entries with |γ| ≤ 1e-9 · max|γ| (SPEC.md §6.2)."""
    magnitude = np.abs(gamma)
    signs = np.sign(gamma[magnitude > FLAT_FRACTION * magnitude.max()])
    return int(np.count_nonzero(signs[1:] != signs[:-1]))


def inflections(fit: RobustFit) -> int:
    return inflection_count(fit.spline.gamma)


def inflection_target(days: np.ndarray, n_per_year: float) -> int:
    """T = floor(N · (x_n − x_1)) (SPEC.md §6.3), from whole days so that a span of exact years is not rounded down."""
    return math.floor(n_per_year * int(days[-1] - days[0]) / DAYS_PER_YEAR)


def search_lambda(x: np.ndarray, y: np.ndarray, target: int, start: float | None = None) -> Smoothed:
    """The λ of SPEC.md §6.3 for at most `target` inflection points, bracketed around log10 λ = `start` if given."""
    weights = None
    if start is not None:
        lo, hi = max(start - WARM_HALF_WIDTH, LOG_LAMBDA_LOW), min(start + WARM_HALF_WIDTH, LOG_LAMBDA_HIGH)
        hi_fit = robust_fit(x, y, 10.0**hi)
        lo_fit = robust_fit(x, y, 10.0**lo, hi_fit.weights)
        if inflections(lo_fit) > target >= inflections(hi_fit):
            return bisect_lambda(x, y, target, lo, hi, hi_fit, lo_fit.weights)
        weights = lo_fit.weights
    top = robust_fit(x, y, 10.0**LOG_LAMBDA_HIGH, weights)
    if inflections(top) > target:
        return Smoothed(LOG_LAMBDA_HIGH, True, top)
    bottom = robust_fit(x, y, 10.0**LOG_LAMBDA_LOW, top.weights)
    if inflections(bottom) <= target:
        return Smoothed(LOG_LAMBDA_LOW, False, bottom)
    return bisect_lambda(x, y, target, LOG_LAMBDA_LOW, LOG_LAMBDA_HIGH, top, bottom.weights)


def bisect_lambda(
    x: np.ndarray, y: np.ndarray, target: int, lo: float, hi: float, fit: RobustFit, weights: np.ndarray
) -> Smoothed:
    """Halves [lo, hi], where count(10^lo) > target ≥ count(10^hi), `fit` is the fit at hi and `weights` the latest."""
    for _ in range(MAX_HALVINGS):
        if hi - lo < BRACKET_WIDTH:
            break
        mid = (lo + hi) / 2
        trial = robust_fit(x, y, 10.0**mid, weights)
        weights = trial.weights
        if inflections(trial) > target:
            lo = mid
        else:
            hi, fit = mid, trial
    return Smoothed(hi, False, fit)


def refit_days(first_day: int, today: int) -> np.ndarray:
    """The month ends m_k of SPEC.md §6.4, as days.

    From the first month end at least 3 years after first_day to the last one before today.
    """
    month = np.timedelta64(1, "M")
    # A date 3 years on keeps its month, so that month's end is the first month end at least 3 years on.
    first = np.datetime64(first_day, "D").astype("datetime64[M]") + WARM_UP_MONTHS * month
    last = np.datetime64(today, "D").astype("datetime64[M]") - month
    months = np.arange(first, last + month, month)
    return ((months + month).astype("datetime64[D]") - np.timedelta64(1, "D")).astype(np.int64)


def refit_anchors(days: np.ndarray, y: np.ndarray, n_per_year: float, today: int) -> Anchors:
    """Refit k fits the points released by m_k, starting its bisection from refit k − 1's λ (SPEC.md §6.4)."""
    month_ends = refit_days(int(days[0]), today) if days.size else np.empty(0, dtype=np.int64)
    counts = np.searchsorted(days, month_ends, side="right")
    # Fewer than MIN_POINTS points give no anchor, and a month without a new point keeps the previous anchor.
    fitted = (counts >= MIN_POINTS) & (np.diff(counts, prepend=0) > 0)
    x = days_to_x(days)
    results = []
    start = None
    for count in counts[fitted].tolist():
        smoothed = search_lambda(x[:count], y[:count], inflection_target(days[:count], n_per_year), start)
        results.append(smoothed)
        start = smoothed.log10_lam
    splines = [smoothed.fit.spline for smoothed in results]
    return Anchors(
        month_end=month_ends[fitted],
        x=x[counts[fitted] - 1],
        value=np.array([spline.g[-1] for spline in splines]),
        slope=np.array([end_slopes(spline)[1] for spline in splines]),
        lam=np.array([smoothed.lam for smoothed in results]),
        at_limit=np.array([smoothed.at_limit for smoothed in results], dtype=bool),
    )


def anchor_values(anchors: Anchors, first_day: int, last_day: int) -> np.ndarray:
    """v_k + s_k · (d / 365.25 − x_k) for each day d from first_day to last_day, with m_k the last refit before d.

    NaN where no refit before d produced an anchor.
    """
    day = np.arange(first_day, last_day + 1)
    k = np.searchsorted(anchors.month_end, day, side="left") - 1
    values = np.full(day.size, np.nan)
    defined = k >= 0
    k = k[defined]
    values[defined] = anchors.value[k] + anchors.slope[k] * (days_to_x(day[defined]) - anchors.x[k])
    return values


def one_sided(
    days: np.ndarray, y: np.ndarray, n_per_year: float, *, today: int, first_day: int, last_day: int
) -> OneSided:
    """The one-sided series of SPEC.md §6.4 for the points (days, y), from first_day to last_day."""
    days, y = np.asarray(days, dtype=np.int64), np.asarray(y, dtype=float)
    anchors = refit_anchors(days, y, n_per_year, today)
    return OneSided(first_day, anchor_values(anchors, first_day, last_day), anchors)


def reference_fit(days: np.ndarray, y: np.ndarray, n_per_year: float) -> Reference:
    """One fit of every point, evaluated on each day from the first point to the last (SPEC.md §6.5)."""
    days, y = np.asarray(days, dtype=np.int64), np.asarray(y, dtype=float)
    if days.size < MIN_POINTS:
        return Reference(int(days[0]) if days.size else 0, np.empty(0), math.nan, False)
    smoothed = search_lambda(days_to_x(days), y, inflection_target(days, n_per_year))
    grid = days_to_x(np.arange(days[0], days[-1] + 1))
    return Reference(int(days[0]), spline_values(smoothed.fit.spline, grid), smoothed.lam, smoothed.at_limit)
