"""Point-in-time series (SPEC.md §5), the day helpers of §3, and the translation of Tiingo and FRED payloads. Pure.

A day is a calendar date as an integer: days since 1970-01-01. x = day / 365.25 is a day in years, for fitting.
"""
import calendar
import datetime
import math
from dataclasses import dataclass
from types import NoneType
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

EPOCH = datetime.date(1970, 1, 1)
DAYS_PER_YEAR = 365.25
NEW_YORK = ZoneInfo("America/New_York")
MISSING = "."
LAG_WINDOW_YEARS = 2  # §5.2's 24 months
MIN_LAG_OBSERVATIONS = 12
PRE_ARCHIVE_MARGIN = 60


class PayloadError(ValueError):
    """A Tiingo or FRED payload without the shape of SPEC.md §4.1 or §4.2. Messages name the row and key, never a value."""


@dataclass(frozen=True)
class Observations:
    """One entry per observation (SPEC.md §3), in parallel arrays.

    Points (§5.3) have the same shape: the observations that became points, in release-day order.
    """

    obs_day: np.ndarray  # int64
    value: np.ndarray  # float64, in original units
    release_day: np.ndarray  # int64: the first release (§5.1), or its estimate (§5.2)
    estimated: np.ndarray  # bool: release_day is an estimate

    def take(self, index: np.ndarray) -> "Observations":
        return Observations(self.obs_day[index], self.value[index], self.release_day[index], self.estimated[index])


def joined(first: Observations, second: Observations) -> Observations:
    """Both sets of observations in one: first's, then second's."""
    return Observations(
        np.concatenate((first.obs_day, second.obs_day)),
        np.concatenate((first.value, second.value)),
        np.concatenate((first.release_day, second.release_day)),
        np.concatenate((first.estimated, second.estimated)),
    )


def date_to_day(date: datetime.date) -> int:
    return (date - EPOCH).days


def day_to_date(day: int) -> datetime.date:
    return EPOCH + datetime.timedelta(days=int(day))


def parse_day(text: str) -> int:
    """An ISO date such as 2026-10-28, as a day. Raises ValueError for anything else."""
    return date_to_day(datetime.date.fromisoformat(text))


def days_to_x(days: np.ndarray) -> np.ndarray:
    """x of SPEC.md §3: days in years of 365.25 days."""
    return np.asarray(days) / DAYS_PER_YEAR


def years_before(day: int, years: int) -> int:
    """The same month and day `years` earlier. 29 February becomes 28 February (SPEC.md §8.1)."""
    date = day_to_date(day)
    year = date.year - years
    if (date.month, date.day) == (2, 29) and not calendar.isleap(year):
        return date_to_day(date.replace(year=year, day=28))
    return date_to_day(date.replace(year=year))


def today_at(instant: datetime.datetime) -> int:
    """The date in America/New_York at `instant`, as a day: "today" of SPEC.md §3."""
    return date_to_day(aware(instant).astimezone(NEW_YORK).date())


def utc_text(instant: datetime.datetime) -> str:
    """`instant` in UTC ISO 8601 to the second, such as 2026-10-28T14:03:11Z: how the database and the API write it."""
    return aware(instant).astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def aware(instant: datetime.datetime) -> datetime.datetime:
    if instant.utcoffset() is None:
        # astimezone would read a naive instant as the PC's local time.
        raise ValueError("instant must carry a time zone")
    return instant


def fred_observations(payload: Any) -> Observations:
    """The first releases in the body of a FRED observations request (SPEC.md §4.2, §5.1). Missing values are skipped."""
    rows = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PayloadError("observations must be a list")
    records = []
    for number, row in enumerate(rows, start=1):
        where = f"observation {number}: "
        value = field(row, "value", (str,), where)
        if value != MISSING:
            obs_day = day_field(row, "date", where)
            records.append((obs_day, finite(value, "value", where), day_field(row, "realtime_start", where)))
    return to_observations(records, "observations")


def tiingo_observations(rows: Any) -> Observations:
    """SPY's closes in the body of a Tiingo price request (SPEC.md §4.1). A close is released on its trading day."""
    if not isinstance(rows, list):
        raise PayloadError("prices must be a list")
    records = []
    for number, row in enumerate(rows, start=1):
        where = f"row {number}: "
        close = field(row, "close", (int, float, NoneType), where)
        if close is not None:
            trading_day = day_field(row, "date", where)
            records.append((trading_day, finite(close, "close", where), trading_day))
    return to_observations(records, "rows")


def field(row: Any, key: str, kinds: tuple[type, ...], where: str) -> Any:
    """row[key], whose type must be one of `kinds`."""
    if not isinstance(row, dict):
        raise PayloadError(f"{where}must be an object")
    if key not in row:
        raise PayloadError(f"{where}missing key {key}")
    # Exact types: JSON true and false arrive as bool, which Python counts as an int.
    if type(row[key]) not in kinds:
        raise PayloadError(f"{where}{key} has the wrong type")
    return row[key]


def day_field(row: Any, key: str, where: str) -> int:
    """The day in row[key]: an ISO date, or the date part of the ISO timestamps that Tiingo sends (SPEC.md §4.1)."""
    text = field(row, key, (str,), where)
    try:
        return parse_day(text.partition("T")[0])
    except ValueError:
        raise PayloadError(f"{where}{key} must be a date") from None


def finite(value: str | int | float, key: str, where: str) -> float:
    """FRED sends values as decimal strings and Tiingo as JSON numbers; Python's json also decodes NaN and Infinity."""
    try:
        number = float(value)
    except (ValueError, OverflowError):
        number = math.nan
    if not math.isfinite(number):
        raise PayloadError(f"{where}{key} must be a finite number")
    return number


def to_observations(records: list[tuple[int, float, int]], what: str) -> Observations:
    obs_day = np.array([record[0] for record in records], dtype=np.int64)
    if np.unique(obs_day).size < obs_day.size:
        # The store keys observations by day (SPEC.md §10).
        raise PayloadError(f"two {what} have the same date")
    return Observations(
        obs_day=obs_day,
        value=np.array([record[1] for record in records], dtype=float),
        release_day=np.array([record[2] for record in records], dtype=np.int64),
        estimated=np.zeros(obs_day.size, dtype=bool),
    )


def typical_lag(observations: Observations, default_lag_days: int) -> int:
    """typical_lag of SPEC.md §5.2, in whole days.

    The median of release day − observation day over the observations dated within the 24 months up to the latest
    one, when there are at least 12 of them; default_lag_days otherwise.
    """
    if observations.obs_day.size == 0:
        return default_lag_days
    recent = observations.obs_day > years_before(int(observations.obs_day.max()), LAG_WINDOW_YEARS)
    if np.count_nonzero(recent) < MIN_LAG_OBSERVATIONS:
        return default_lag_days
    # An estimated release must fall on a day, and the median of an even count can fall on a half day. Rounding up
    # keeps the estimates on the late side.
    return math.ceil(np.median(observations.release_day[recent] - observations.obs_day[recent]))


def estimate_release_days(observations: Observations, lag: int) -> Observations:
    """SPEC.md §5.2 with typical_lag `lag`.

    An observation released more than lag + 60 days after its date is taken to predate the archive: it gets the release
    day observation day + lag and the estimated flag. The others are unchanged.
    """
    late = observations.release_day - observations.obs_day > lag + PRE_ARCHIVE_MARGIN
    return Observations(
        obs_day=observations.obs_day,
        value=observations.value,
        release_day=np.where(late, observations.obs_day + lag, observations.release_day),
        estimated=observations.estimated | late,
    )


def known_points(observations: Observations) -> Observations:
    """The points of SPEC.md §5.3, in release-day order. Release days and observation days both strictly increase.

    On each release day, the newest observation released that day becomes a point if it is newer than every
    observation released on an earlier day.
    """
    if observations.obs_day.size == 0:
        return observations
    order = np.lexsort((observations.obs_day, observations.release_day))
    release_day = observations.release_day[order]
    newest = order[np.append(release_day[1:] != release_day[:-1], True)]
    obs_day = observations.obs_day[newest]
    # An earlier observation that is not a point is no newer than some point, so comparing with every earlier
    # observation is comparing with the points before, as §5.3 words it.
    newer = np.append(True, obs_day[1:] > np.maximum.accumulate(obs_day)[:-1])
    return observations.take(newest[newer])


def unchanged(values: np.ndarray) -> np.ndarray:
    return values


# y for each form (SPEC.md §5.4). A new form is one more entry here (§18).
TRANSFORMS = {"LOG": np.log, "LEVEL": unchanged}
FORMS = tuple(TRANSFORMS)


def y_values(values: np.ndarray, form: str) -> np.ndarray:
    """y of SPEC.md §5.4: ln(value) for LOG, the value itself for LEVEL. Check has_invalid_values first."""
    return TRANSFORMS[form](np.asarray(values, dtype=float))


def user_form(units: str, values: np.ndarray) -> str:
    """The form of a series the owner adds (SPEC.md §4.4): LEVEL for units in "Percent" or a value ≤ 0, else LOG."""
    return "LEVEL" if "Percent" in units or (np.asarray(values) <= 0).any() else "LOG"


def has_invalid_values(values: np.ndarray, form: str) -> bool:
    """Whether some value has no finite y, as one that is zero or negative in LOG form: INVALID_VALUES (§5.4, §7.6)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return not np.isfinite(y_values(values, form)).all()
