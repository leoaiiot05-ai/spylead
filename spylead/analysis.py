"""Lead analysis (SPEC.md §7): LAD fits, the score, steps 3 to 5, the out-of-sample check and the statuses. Pure.

ln F(d) = s(d) − Σ β_i r_i(d + b_i), where s is SPY's one-sided series and r_i an indicator's, both in y (§3).
A shift is in days; a negative one pairs SPY with the indicator's value from that many days earlier: a lead.
"""
import math
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

import numpy as np

from spylead.pit import DAYS_PER_YEAR, days_to_x, years_before

MAX_SHIFT = 730
SHIFTS = np.arange(-MAX_SHIFT, MAX_SHIFT + 1)  # step 3's A
MIN_WINDOW_YEARS = 10
SEARCH_STRIDE = 7
SPLIT_TENTHS = 7
TIE = 1e-12
CHUNK_BYTES = 64 * 2**20
MAX_SOLVES = 100
OBJECTIVE_TOLERANCE = 1e-8
DELTA_FRACTION = 1e-6
DELTA_FLOOR = 1e-12
STAGES = ((28, 730), (7, 28), (1, 7))  # (step, radius) in days
RANDOM_SEED = 0
LEAD_PREFIX = "LEADL1_"
SPACING_YEARS = 2
STALE_SPACINGS = 3
STALE_MARGIN_DAYS = 14

# Candidate statuses in the order of SPEC.md §7.6: the first that applies.
PENDING = "PENDING"
FETCH_FAILED = "FETCH_FAILED"
INVALID_VALUES = "INVALID_VALUES"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
AT_BOUNDARY = "AT_BOUNDARY"
NOT_LEADING = "NOT_LEADING"
DISCONTINUED = "DISCONTINUED"
KEPT = "KEPT"
STATUSES = (PENDING, FETCH_FAILED, INVALID_VALUES, INSUFFICIENT_HISTORY, AT_BOUNDARY, NOT_LEADING, DISCONTINUED, KEPT)
OK = "OK"
NO_MEMBERS = "NO_MEMBERS"
INSUFFICIENT_OVERLAP = "INSUFFICIENT_OVERLAP"
F_STATUSES = (OK, NO_MEMBERS, INSUFFICIENT_OVERLAP)


@dataclass(frozen=True)
class Daily:
    """A one-sided series (SPEC.md §6.4) in y: one float64 per day from first_day, NaN where undefined."""

    first_day: int
    values: np.ndarray

    def span(self) -> tuple[int, int] | None:
        """The first and last days with a defined value, or None when there is none."""
        defined = np.flatnonzero(~np.isnan(self.values))
        if defined.size == 0:
            return None
        return self.first_day + int(defined[0]), self.first_day + int(defined[-1])

    def at(self, days: np.ndarray) -> np.ndarray:
        return self.values[days - self.first_day]


@dataclass(frozen=True)
class Window:
    """Every day from start to end."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def days(self) -> np.ndarray:
        """Every day: the evaluation points of reported results (SPEC.md §7.1)."""
        return np.arange(self.start, self.end + 1)

    def search_days(self) -> np.ndarray:
        """Every 7th day, counting back from the last: the evaluation points of searches (SPEC.md §7.1)."""
        return np.arange(self.end, self.start - 1, -SEARCH_STRIDE)[::-1]

    def split(self) -> tuple["Window", "Window"]:
        """The first 70% and the rest, which starts on the split day (SPEC.md §7.5)."""
        # In integers, so that no rounding of 0.7 can move the split day.
        split_day = self.start + SPLIT_TENTHS * self.length // 10
        return Window(self.start, split_day - 1), Window(split_day, self.end)


@dataclass(frozen=True)
class Step3:
    """A candidate's step-3 report (SPEC.md §7.2) and out-of-sample score (§7.5)."""

    window: Window  # W3
    a_star: int
    beta: float
    score: float
    score_oos: float
    slope_pct_per_year: float


@dataclass(frozen=True)
class Member:
    """A series in F (SPEC.md §7.4), with its step-3 A* as the first start."""

    name: str
    series: Daily
    a_star: int


@dataclass(frozen=True)
class MemberFit:
    name: str
    b: int
    beta: float


@dataclass(frozen=True)
class Combination:
    """F of SPEC.md §7.4 and its out-of-sample score (§7.5)."""

    members: tuple[MemberFit, ...]
    c: float
    score: float
    score_oos: float
    slope_pct_per_year: float
    window: Window  # W5
    split_day: int
    f: np.ndarray  # F on every day of the window, normalized to median 1
    composite: np.ndarray  # exp(c + Σ β_i r_i(d + b_i)) on every day of the window, in USD


@dataclass(frozen=True)
class Step5:
    status: str  # OK, NO_MEMBERS or INSUFFICIENT_OVERLAP
    combination: Combination | None = None  # when OK


@dataclass(frozen=True)
class Fit:
    """A LAD fit over every day of a window, at given shifts (SPEC.md §7.1)."""

    c: float
    beta: np.ndarray
    combined: np.ndarray  # Σ β_i r_i(d + b_i)
    log_f: np.ndarray  # s(d) − combined


def ten_year_window(start: int, end: int) -> Window | None:
    """The window from start to end, or None when it is shorter than 10 years (SPEC.md §7.2, §7.4)."""
    if end - start + 1 < MIN_WINDOW_YEARS * DAYS_PER_YEAR:
        return None
    return Window(start, end)


def lad(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """θ (k, p) minimizing Σ |y − Xθ| for each of the k designs X (k, p, n), by IRLS (SPEC.md §7.1).

    Each design stops on its own, so its θ does not depend on the rest of the batch.
    """
    theta = weighted_solve(X, y, np.ones((X.shape[0], y.size)))
    residuals = y - np.einsum("kpn,kp->kn", X, theta)
    objective = np.abs(residuals).sum(axis=1)
    active = np.arange(X.shape[0])
    for _ in range(MAX_SOLVES - 1):
        if active.size == 0:
            break
        design = X[active]
        trial = weighted_solve(design, y, lad_weights(np.abs(residuals[active])))
        trial_residuals = y - np.einsum("kpn,kp->kn", design, trial)
        trial_objective = np.abs(trial_residuals).sum(axis=1)
        previous = objective[active]
        # The weights are capped at 1/δ, and δ moves between solves, so a solve can come out slightly worse.
        better = trial_objective <= previous
        converged = previous - trial_objective <= OBJECTIVE_TOLERANCE * previous
        rows = active[better]
        theta[rows] = trial[better]
        residuals[rows] = trial_residuals[better]
        objective[rows] = trial_objective[better]
        active = active[better & ~converged]
    return theta


def weighted_solve(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """The weighted normal equations XᵀWX θ = XᵀWy of each design."""
    # einsum rather than matmul: BLAS may order its sums by its thread count, and a result must not depend on the machine.
    # Days run along the last, contiguous axis, which makes these sums several times faster.
    lhs = np.einsum("kn,kpn,kqn->kpq", w, X, X)
    rhs = np.einsum("kn,kpn,n->kp", w, X, y)
    # NumPy 2 reads a stacked right-hand side as matrices, so it needs the trailing axis (SPEC.md §7.7).
    return np.linalg.solve(lhs, rhs[..., None])[..., 0]


def lad_weights(residuals: np.ndarray) -> np.ndarray:
    """w = 1 / max(|res|, δ) with δ = max(1e-6 · median|res|, 1e-12), per design (SPEC.md §7.1)."""
    delta = np.maximum(DELTA_FRACTION * np.median(residuals, axis=1), DELTA_FLOOR)
    return 1.0 / np.maximum(residuals, delta[:, None])


def lad_fit(y: np.ndarray, regressors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """c (k,) and β (k, m) of the LAD fit of y (n,) on each of k regressor sets (k, m, n) and an intercept."""
    design = np.concatenate((np.ones((regressors.shape[0], 1, y.size)), regressors), axis=1)
    theta = lad(design, y)
    return theta[:, 0], theta[:, 1:]


def combined(regressors: np.ndarray, beta: np.ndarray) -> np.ndarray:
    """Σ β_i r_i(d + b_i), for regressors (..., m, n) and β (..., m)."""
    return np.einsum("...mn,...m->...n", regressors, beta)


def score(log_f: np.ndarray) -> np.ndarray:
    """The mean of |ln F − median ln F| along the last axis (SPEC.md §7.1). Lower is flatter."""
    return np.abs(log_f - np.median(log_f, axis=-1, keepdims=True)).mean(axis=-1)


def slope_pct_per_year(days: np.ndarray, log_f: np.ndarray) -> float:
    """The OLS slope of ln F against d / 365.25, as 100 · (exp(slope) − 1) percent per year (SPEC.md §7.1)."""
    x = days_to_x(days)
    x = x - x.mean()
    return 100 * math.expm1(float((x * (log_f - log_f.mean())).sum() / (x * x).sum()))


def best(values: np.ndarray, scores: np.ndarray) -> int:
    """The index of the lowest score (SPEC.md §7.2, §7.4).

    Ties, a relative difference of at most 1e-12, go to the smaller |value|, and between v and −v to the positive one.
    """
    tied = np.flatnonzero(scores <= scores.min() * (1 + TIE))
    order = np.lexsort((-values[tied], np.abs(values[tied])))
    return int(tied[order[0]])


def candidate_scores(
    y: np.ndarray, regressors: Callable[[np.ndarray], np.ndarray], candidates: np.ndarray, m: int
) -> np.ndarray:
    """The score of each candidate, fitted in chunks so that no array exceeds 64 MB (SPEC.md §7.2).

    regressors(chunk) gives the m regressors of a chunk of candidates, shape (k, m, n).
    """
    # The design, with its intercept, is the largest array of a chunk.
    size = max(1, CHUNK_BYTES // (8 * y.size * (m + 1)))
    scores = np.empty(candidates.size)
    for start in range(0, candidates.size, size):
        chunk = regressors(candidates[start : start + size])
        _, beta = lad_fit(y, chunk)
        scores[start : start + size] = score(y - combined(chunk, beta))
    return scores


def columns(series: Sequence[Daily], shifts: Sequence[int], days: np.ndarray) -> np.ndarray:
    """r_i(d + b_i): one row per series, one column per day d."""
    return np.stack([r.at(days + int(b)) for r, b in zip(series, shifts)])


def fit(spy: Daily, series: Sequence[Daily], shifts: Sequence[int], window: Window) -> Fit:
    """The LAD fit over every day of the window, for reports (SPEC.md §7.1)."""
    days = window.days()
    y, regressors = spy.at(days), columns(series, shifts, days)
    c, beta = lad_fit(y, regressors[None])
    total = combined(regressors, beta[0])
    return Fit(float(c[0]), beta[0], total, y - total)


def out_of_sample_score(spy: Daily, series: Sequence[Daily], shifts: Sequence[int], beta: np.ndarray, rest: Window) -> float:
    """The score of given shifts and β on every day of the rest of the window, with its own median (SPEC.md §7.5)."""
    days = rest.days()
    return float(score(spy.at(days) - combined(columns(series, shifts, days), beta)))


def step3_window(spy: Daily, indicator: Daily) -> Window | None:
    """W3 of SPEC.md §7.2, the one window of every A, or None when it is shorter than 10 years."""
    spy_span, span = spy.span(), indicator.span()
    if spy_span is None or span is None:
        return None
    return ten_year_window(max(spy_span[0], span[0] + MAX_SHIFT), min(spy_span[1], span[1] - MAX_SHIFT))


def shift_scores(spy: Daily, indicator: Daily, window: Window) -> np.ndarray:
    """The score of every A in SHIFTS on the window's search days, the same days for every A (SPEC.md §7.2)."""
    days = window.search_days()
    return candidate_scores(spy.at(days), lambda chunk: indicator.at(days + chunk[:, None])[:, None], SHIFTS, 1)


def search_shift(spy: Daily, indicator: Daily, window: Window) -> int:
    """A*: the A with the lowest score (SPEC.md §7.2)."""
    return int(SHIFTS[best(SHIFTS, shift_scores(spy, indicator, window))])


def step3(spy: Daily, indicator: Daily) -> Step3 | None:
    """Step 3 of SPEC.md §7.2 for one candidate, with its out-of-sample check (§7.5).

    None when W3 is shorter than 10 years: INSUFFICIENT_HISTORY.
    """
    w3 = step3_window(spy, indicator)
    if w3 is None:
        return None
    a_star = search_shift(spy, indicator, w3)
    full = fit(spy, [indicator], [a_star], w3)
    return Step3(
        window=w3,
        a_star=a_star,
        beta=float(full.beta[0]),
        score=float(score(full.log_f)),
        score_oos=step3_out_of_sample(spy, indicator, w3),
        slope_pct_per_year=slope_pct_per_year(w3.days(), full.log_f),
    )


def step3_out_of_sample(spy: Daily, indicator: Daily, w3: Window) -> float:
    """A and β from the first 70% of W3, scored on the rest (SPEC.md §7.5)."""
    first, rest = w3.split()
    shift = search_shift(spy, indicator, first)
    return out_of_sample_score(spy, [indicator], [shift], fit(spy, [indicator], [shift], first).beta, rest)


def status(
    release_days: np.ndarray, *, fetch_failed: bool, invalid_values: bool, step3: Step3 | None, today: int
) -> str:
    """The candidate's status (SPEC.md §7.6): the first that applies. Steps 3 and 4 decide the last four.

    release_days are those of the points (§5.3). Without data, a failed last fetch gives FETCH_FAILED, anything else
    PENDING.
    """
    if release_days.size == 0:
        return FETCH_FAILED if fetch_failed else PENDING
    if invalid_values:
        return INVALID_VALUES
    if step3 is None:
        return INSUFFICIENT_HISTORY
    if abs(step3.a_star) == MAX_SHIFT:
        return AT_BOUNDARY
    if step3.a_star >= 0:
        return NOT_LEADING
    if discontinued(release_days, today):
        return DISCONTINUED
    return KEPT


def discontinued(release_days: np.ndarray, today: int) -> bool:
    """The latest release day is earlier than today − (3 × the median spacing of release days over 2 years + 14).

    The spacings are the gaps between consecutive release days that end within the 2 years up to the latest one:
    a series that stopped long ago has no release in the 2 years before today.
    """
    latest = int(release_days[-1])
    gaps = np.diff(release_days)[release_days[1:] > years_before(latest, SPACING_YEARS)]
    return latest < today - (STALE_SPACINGS * float(np.median(gaps)) + STALE_MARGIN_DAYS)


def lead_name(name: str) -> str:
    """The name of a KEPT series (SPEC.md §7.3)."""
    return LEAD_PREFIX + name


def is_member(status: str, name: str, excluded: Collection[str]) -> bool:
    """Whether a candidate enters F (SPEC.md §7.4): KEPT and not unchecked, so a new KEPT series is in by default."""
    return status == KEPT and name not in excluded


def step5_window(spy: Daily, members: Sequence[Member]) -> Window | None:
    """W5 of SPEC.md §7.4, or None when it is shorter than 10 years."""
    spy_first, spy_last = spy.span()
    spans = [member.series.span() for member in members]
    first = max(spy_first, max(span[0] for span in spans) + MAX_SHIFT)
    return ten_year_window(first, min(spy_last, min(span[1] for span in spans)))


def neighbours(b: int, step: int, radius: int) -> np.ndarray:
    """b + k · step within ± radius and inside [−730, 0] (SPEC.md §7.4), in increasing order."""
    candidates = b + step * np.arange(-(radius // step), radius // step + 1)
    return candidates[(candidates >= -MAX_SHIFT) & (candidates <= 0)]


def descend(spy: Daily, members: Sequence[Member], window: Window, start: np.ndarray) -> tuple[np.ndarray, float]:
    """The coordinate descent of SPEC.md §7.4 from `start`, on the window's search days: the shifts and their score."""
    days = window.search_days()
    y = spy.at(days)
    b = start.copy()
    regressors = columns([member.series for member in members], b, days)
    current = math.nan
    for step, radius in STAGES:
        changed = True
        while changed:
            changed = False
            for i, member in enumerate(members):
                candidates = neighbours(int(b[i]), step, radius)
                scores = candidate_scores(
                    y, lambda chunk: with_column(regressors, i, member.series.at(days + chunk[:, None])), candidates, b.size
                )
                # The current shift is a candidate, so one set of shifts always gets its score from the same fit.
                current = scores[np.searchsorted(candidates, b[i])]
                j = best(candidates, scores)
                if current - scores[j] > TIE * current:
                    b[i], current, changed = candidates[j], scores[j], True
                    regressors[i] = member.series.at(days + b[i])
    return b, float(current)


def with_column(regressors: np.ndarray, i: int, values: np.ndarray) -> np.ndarray:
    """One copy of regressors (m, n) per row of values (k, n), with that row as regressor i."""
    copies = np.repeat(regressors[None], values.shape[0], axis=0)
    copies[:, i] = values
    return copies


def step5(spy: Daily, members: Sequence[Member]) -> Step5:
    """Step 5 of SPEC.md §7.4, with its out-of-sample check (§7.5).

    members: the KEPT series in F, in catalog order, then USER series by name.
    """
    if not members:
        return Step5(NO_MEMBERS)
    w5 = step5_window(spy, members)
    if w5 is None:
        return Step5(INSUFFICIENT_OVERLAP)
    starts = (
        np.array([member.a_star for member in members]),
        np.random.default_rng(RANDOM_SEED).integers(-MAX_SHIFT, 0, size=len(members), endpoint=True),
    )
    ends = [descend(spy, members, w5, start) for start in starts]
    # The better start wins, and a tie goes to the smaller Σ|b_i|.
    totals = np.array([np.abs(shifts).sum() for shifts, _ in ends])
    b = ends[best(totals, np.array([end for _, end in ends]))][0]
    series = [member.series for member in members]
    full = fit(spy, series, b, w5)
    return Step5(
        OK,
        Combination(
            members=tuple(MemberFit(m.name, int(shift), float(beta)) for m, shift, beta in zip(members, b, full.beta)),
            c=full.c,
            score=float(score(full.log_f)),
            score_oos=step5_out_of_sample(spy, members, w5, b),
            slope_pct_per_year=slope_pct_per_year(w5.days(), full.log_f),
            window=w5,
            split_day=w5.split()[1].start,
            f=np.exp(full.log_f - np.median(full.log_f)),
            composite=np.exp(full.c + full.combined),
        ),
    )


def step5_out_of_sample(spy: Daily, members: Sequence[Member], w5: Window, b: np.ndarray) -> float:
    """Shifts from a descent on the first 70% of W5, started at the full-window b, and β there, scored on the rest."""
    first, rest = w5.split()
    shifts, _ = descend(spy, members, first, b)
    series = [member.series for member in members]
    return out_of_sample_score(spy, series, shifts, fit(spy, series, shifts, first).beta, rest)
