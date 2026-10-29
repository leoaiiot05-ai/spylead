import datetime
import math

import numpy as np
import pytest

from spylead import catalog
from spylead.pit import (
    FORMS,
    Observations,
    PayloadError,
    date_to_day,
    day_to_date,
    days_to_x,
    estimate_release_days,
    fred_observations,
    has_invalid_values,
    joined,
    known_points,
    original_values,
    parse_day,
    tiingo_observations,
    today_at,
    typical_lag,
    user_form,
    utc_text,
    y_values,
    years_before,
)

EPOCH = datetime.date(1970, 1, 1)
REMOVED = object()


def day(year, month, date):
    return (datetime.date(year, month, date) - EPOCH).days


def iso_day(text):
    return (datetime.date.fromisoformat(text) - EPOCH).days


def iso(d):
    return (EPOCH + datetime.timedelta(days=int(d))).isoformat()


def month_starts(year, month, count):
    """The 1st of `count` consecutive months from `year` and `month`, as days."""
    return np.array([day(year + (month - 1 + k) // 12, (month - 1 + k) % 12 + 1, 1) for k in range(count)])


def observations(obs_day, release_day, estimated=None):
    """Observations with the values 1, 2, 3, ... in the order given."""
    obs_day = np.asarray(obs_day, dtype=np.int64)
    flags = np.zeros(obs_day.size, dtype=bool) if estimated is None else np.asarray(estimated, dtype=bool)
    return Observations(obs_day, np.arange(1.0, obs_day.size + 1), np.asarray(release_day, dtype=np.int64), flags)


def columns(result):
    return result.obs_day.tolist(), result.value.tolist(), result.release_day.tolist(), result.estimated.tolist()


def changed(row, **changes):
    """A copy of `row` with keys changed, or removed when set to REMOVED."""
    merged = {**row, **changes}
    return {key: value for key, value in merged.items() if value is not REMOVED}


# Translation (AC-4: first releases are parsed, and missing values skipped)

FRED_ROW = {"realtime_start": "2026-02-24", "realtime_end": "9999-12-31", "date": "2026-01-01", "value": "1.5"}
FRED_BODY = {
    "realtime_start": "1776-07-04",
    "realtime_end": "9999-12-31",
    "observation_start": "2026-01-01",
    "observation_end": "9999-12-31",
    "units": "lin",
    "output_type": 4,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 5,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {"realtime_start": "2026-02-24", "realtime_end": "2026-03-23", "date": "2026-01-01", "value": "22105.4"},
        {"realtime_start": "2026-03-24", "realtime_end": "2026-04-27", "date": "2026-02-01", "value": "."},
        {"realtime_start": "2026-04-28", "realtime_end": "2026-05-26", "date": "2026-03-01", "value": "22230"},
        {"realtime_start": "2026-05-27", "realtime_end": "2026-06-22", "date": "2026-04-01", "value": "-0.25"},
        {"realtime_start": "2026-06-23", "realtime_end": "2026-07-27", "date": "2026-05-01", "value": "."},
    ],
}
TIINGO_ROW = {"date": "2026-10-22T00:00:00.000Z", "close": 668.51}
TIINGO_ROWS = [
    {
        "date": "2026-10-22T00:00:00.000Z", "close": 668.51, "high": 671.2, "low": 664.03, "open": 667.4,
        "volume": 71234567, "adjClose": 668.51, "adjHigh": 671.2, "adjLow": 664.03, "adjOpen": 667.4,
        "adjVolume": 71234567, "divCash": 0.0, "splitFactor": 1.0,
    },
    {"date": "2026-10-23T00:00:00.000Z", "close": None, "adjClose": None},
    {"date": "2026-10-26T00:00:00+00:00", "close": 672},
    {"date": "2026-10-27", "close": 669.9},
]


def fred(*rows):
    return {"observations": list(rows)}


def test_fred_first_releases_are_parsed_and_missing_values_skipped():
    result = fred_observations(FRED_BODY)
    assert columns(result) == (
        [day(2026, 1, 1), day(2026, 3, 1), day(2026, 4, 1)],
        [22105.4, 22230.0, -0.25],
        [day(2026, 2, 24), day(2026, 4, 28), day(2026, 5, 27)],
        [False, False, False],
    )
    assert (result.obs_day.dtype, result.value.dtype, result.release_day.dtype) == (np.int64, np.float64, np.int64)


@pytest.mark.parametrize("body", [fred(), fred(changed(FRED_ROW, value="."))], ids=["no-rows", "only-missing-values"])
def test_a_fred_body_without_values_gives_no_observations(body):
    result = fred_observations(body)
    assert columns(result) == ([], [], [], [])
    assert (result.obs_day.dtype, result.release_day.dtype) == (np.int64, np.int64)


def test_tiingo_closes_are_released_on_their_trading_day_and_missing_ones_skipped():
    trading_days = [day(2026, 10, 22), day(2026, 10, 26), day(2026, 10, 27)]
    assert columns(tiingo_observations(TIINGO_ROWS)) == (trading_days, [668.51, 672.0, 669.9], trading_days, [False] * 3)


BAD_FRED = [
    pytest.param(["Teal"], "observations must be a list", id="body-not-an-object"),
    pytest.param({"error_message": "Teal"}, "observations must be a list", id="no-observations"),
    pytest.param({"observations": "Teal"}, "observations must be a list", id="observations-not-a-list"),
    pytest.param(fred(FRED_ROW, "Teal"), "observation 2: must be an object", id="row-not-an-object"),
    pytest.param(fred(changed(FRED_ROW, value=REMOVED)), "observation 1: missing key value", id="missing-value"),
    pytest.param(
        fred(changed(FRED_ROW, realtime_start=REMOVED)), "observation 1: missing key realtime_start", id="missing-release"
    ),
    pytest.param(fred(changed(FRED_ROW, value=1.5)), "observation 1: value has the wrong type", id="number-value"),
    pytest.param(fred(changed(FRED_ROW, value="Teal")), "observation 1: value must be a finite number", id="text-value"),
    pytest.param(fred(changed(FRED_ROW, value="NaN")), "observation 1: value must be a finite number", id="nan-value"),
    pytest.param(
        fred(changed(FRED_ROW, value="-Infinity")), "observation 1: value must be a finite number", id="infinite-value"
    ),
    pytest.param(fred(changed(FRED_ROW, date="2026-02-30")), "observation 1: date must be a date", id="impossible-date"),
    pytest.param(
        fred(changed(FRED_ROW, realtime_start=None)), "observation 1: realtime_start has the wrong type", id="null-release"
    ),
    pytest.param(
        fred(FRED_ROW, changed(FRED_ROW, realtime_start="2026-03-24")), "two observations have the same date",
        id="repeated-date",
    ),
]


@pytest.mark.parametrize(("body", "problem"), BAD_FRED)
def test_a_bad_fred_body_is_refused_naming_the_row_and_key(body, problem):
    with pytest.raises(PayloadError) as caught:
        fred_observations(body)
    assert str(caught.value) == problem


BAD_TIINGO = [
    pytest.param({"detail": "Teal"}, "prices must be a list", id="body-not-a-list"),
    pytest.param([TIINGO_ROW, 7], "row 2: must be an object", id="row-not-an-object"),
    pytest.param([changed(TIINGO_ROW, close=REMOVED)], "row 1: missing key close", id="missing-close"),
    pytest.param([changed(TIINGO_ROW, date=REMOVED)], "row 1: missing key date", id="missing-date"),
    pytest.param([changed(TIINGO_ROW, close="668.51")], "row 1: close has the wrong type", id="text-close"),
    pytest.param([changed(TIINGO_ROW, close=True)], "row 1: close has the wrong type", id="boolean-close"),
    pytest.param([changed(TIINGO_ROW, close=math.nan)], "row 1: close must be a finite number", id="nan-close"),
    pytest.param([changed(TIINGO_ROW, close=10**400)], "row 1: close must be a finite number", id="huge-close"),
    pytest.param([changed(TIINGO_ROW, date=20261022)], "row 1: date has the wrong type", id="number-date"),
    pytest.param([changed(TIINGO_ROW, date="Teal")], "row 1: date must be a date", id="text-date"),
    pytest.param(
        [TIINGO_ROW, changed(TIINGO_ROW, date="2026-10-22T20:00:00.000Z")], "two rows have the same date",
        id="repeated-date",
    ),
]


@pytest.mark.parametrize(("body", "problem"), BAD_TIINGO)
def test_a_bad_tiingo_body_is_refused_naming_the_row_and_key(body, problem):
    with pytest.raises(PayloadError) as caught:
        tiingo_observations(body)
    assert str(caught.value) == problem


# Days (SPEC.md §3)

DATES = [
    pytest.param("1970-01-01", 0, id="epoch"),
    pytest.param("1969-12-31", -1, id="before-the-epoch"),
    pytest.param("2000-02-29", 11016, id="29-february-2000"),
    pytest.param("2000-03-01", 11017, id="1-march-2000"),
    pytest.param("2024-02-29", 19782, id="29-february-2024"),
    pytest.param("1900-03-01", -25508, id="1900-has-no-29-february"),
    pytest.param("2100-03-01", 47541, id="2100-has-no-29-february"),
]


@pytest.mark.parametrize(("text", "expected"), DATES)
def test_dates_and_days(text, expected):
    date = datetime.date.fromisoformat(text)
    assert date_to_day(date) == parse_day(text) == expected
    assert day_to_date(expected) == day_to_date(np.int64(expected)) == date


def test_every_day_from_1896_to_2104_round_trips_through_its_date():
    for d in range(day(1896, 1, 1), day(2104, 12, 31) + 1):
        date = day_to_date(d)
        assert date.toordinal() - EPOCH.toordinal() == d
        assert date_to_day(date) == d


@pytest.mark.parametrize("text", ["2001-02-29", "1900-02-29", "2100-02-29", "2026-04-31", "2026-10-27T00:00:00", "Teal"])
def test_parse_day_refuses_what_is_not_a_date(text):
    with pytest.raises(ValueError):
        parse_day(text)


YEARS_BEFORE = [
    pytest.param("2026-10-29", 30, "1996-10-29", id="30-years"),
    pytest.param("2026-09-01", 2, "2024-09-01", id="24-months"),
    pytest.param("2028-02-29", 2, "2026-02-28", id="29-february-into-a-common-year"),
    pytest.param("2024-02-29", 4, "2020-02-29", id="29-february-into-a-leap-year"),
    pytest.param("2024-03-01", 1, "2023-03-01", id="1-march"),
]


@pytest.mark.parametrize(("start", "years", "expected"), YEARS_BEFORE)
def test_years_before_keeps_the_month_and_day(start, years, expected):
    assert years_before(iso_day(start), years) == iso_day(expected)


def test_x_is_the_day_in_years_of_365_25_days():
    np.testing.assert_array_equal(days_to_x(np.array([0, 1461, 36525, -36525])), [0.0, 4.0, 100.0, -100.0])
    assert days_to_x(np.array([11016]))[0] == 11016 / 365.25


TODAY = [
    pytest.param("2026-10-29T03:59:59+00:00", "2026-10-28", id="before-midnight-in-summer-time"),
    pytest.param("2026-10-29T04:00:00+00:00", "2026-10-29", id="midnight-in-summer-time"),
    pytest.param("2026-12-15T04:59:59+00:00", "2026-12-14", id="before-midnight-in-winter-time"),
    pytest.param("2026-12-15T05:00:00+00:00", "2026-12-15", id="midnight-in-winter-time"),
    pytest.param("2026-03-08T04:59:59+00:00", "2026-03-07", id="before-the-spring-change-day"),
    pytest.param("2026-03-08T07:00:00+00:00", "2026-03-08", id="spring-change"),
    pytest.param("2026-11-01T03:59:59+00:00", "2026-10-31", id="before-the-autumn-change-day"),
    pytest.param("2026-11-01T06:00:00+00:00", "2026-11-01", id="autumn-change"),
    pytest.param("2026-11-02T04:59:59+00:00", "2026-11-01", id="after-the-autumn-change"),
    pytest.param("2026-10-29T11:59:59+08:00", "2026-10-28", id="taipei-before-new-york-midnight"),
    pytest.param("2026-10-29T12:00:00+08:00", "2026-10-29", id="taipei-at-new-york-midnight"),
]


@pytest.mark.parametrize(("instant", "expected"), TODAY)
def test_today_is_the_date_in_new_york(instant, expected):
    assert today_at(datetime.datetime.fromisoformat(instant)) == iso_day(expected)


def test_today_refuses_an_instant_without_a_time_zone():
    with pytest.raises(ValueError):
        today_at(datetime.datetime(2026, 10, 29, 12, 0))


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-28T14:03:11+00:00", "2026-10-28T14:03:11Z"),
        ("2026-10-28T22:03:11.999+08:00", "2026-10-28T14:03:11Z"),
        ("2026-10-28T10:03:11-04:00", "2026-10-28T14:03:11Z"),
        ("2026-12-31T23:30:00-05:00", "2027-01-01T04:30:00Z"),
    ],
    ids=["utc", "taipei-with-milliseconds", "new-york-summer", "new-year-in-utc"],
)
def test_instants_are_written_in_utc_to_the_second(instant, expected):
    assert utc_text(datetime.datetime.fromisoformat(instant)) == expected


def test_an_instant_without_a_time_zone_is_not_written():
    with pytest.raises(ValueError):
        utc_text(datetime.datetime(2026, 10, 29, 12, 0))


# typical_lag and estimated release days (SPEC.md §5.2)


def test_the_typical_lag_is_the_median_over_the_24_months_up_to_the_latest_observation():
    # 24 monthly observations from October 2024 to September 2026, after 60 from before the archive.
    older, recent = month_starts(2014, 1, 60), month_starts(2024, 10, 24)
    lags = np.array([45] * 13 + [30] * 11)
    obs = observations(np.concatenate([older, recent]), np.concatenate([older + 3000, recent + lags]))
    assert typical_lag(obs, 7) == 45


@pytest.mark.parametrize(
    ("latest", "edge", "expected"),
    [
        pytest.param("2026-09-01", "2024-09-01", 7, id="24-months-before-is-out"),
        pytest.param("2026-09-01", "2024-09-02", 20, id="a-day-later-is-in"),
        pytest.param("2028-02-29", "2026-02-28", 7, id="29-february-counts-back-to-28-february"),
        pytest.param("2028-02-29", "2026-03-01", 20, id="then-1-march-is-in"),
    ],
)
def test_the_window_holds_the_days_after_the_same_date_two_years_before(latest, edge, expected):
    # 11 observations in the latest year and one at the edge: 12 in the window give their median, 11 the default.
    obs_day = np.append(iso_day(latest) - 30 * np.arange(11), iso_day(edge))
    assert typical_lag(observations(obs_day, obs_day + 20), 7) == expected


def test_a_quarterly_series_has_too_few_recent_observations_and_takes_the_default():
    dates = month_starts(2016, 7, 121)[::3]  # 8 of them in the 24 months up to July 2026
    assert typical_lag(observations(dates, dates + 30), 44) == 44


def test_a_median_on_a_half_day_rounds_up():
    dates = month_starts(2025, 1, 12)
    assert typical_lag(observations(dates, dates + np.array([10] * 6 + [11] * 6)), 0) == 11


def test_without_observations_the_typical_lag_is_the_default():
    assert typical_lag(observations([], []), 55) == 55


def pre_archive_series():
    """Monthly observations from January 1990 to December 1999, released 45 days after their date.

    ALFRED's archive starts on 14 June 1996: the observations dated before May 1996 carry that day as their release day.
    """
    dates = month_starts(1990, 1, 120)
    return dates, np.where(dates < day(1996, 5, 1), day(1996, 6, 14), dates + 45)


def test_pre_archive_observations_get_estimated_release_days_and_the_flag():
    dates, release = pre_archive_series()
    obs = observations(dates, release)
    assert typical_lag(obs, 30) == 45
    result = estimate_release_days(obs, 45)
    # 1 March 1996 is 105 = 45 + 60 days before 14 June 1996, which is not more: it keeps its release day.
    flagged = dates < day(1996, 3, 1)
    np.testing.assert_array_equal(result.estimated, flagged)
    np.testing.assert_array_equal(result.release_day, np.where(flagged, dates + 45, release))
    np.testing.assert_array_equal(result.obs_day, obs.obs_day)
    np.testing.assert_array_equal(result.value, obs.value)


def test_a_lag_of_exactly_typical_lag_plus_60_keeps_its_release_day():
    result = estimate_release_days(observations([0, 100, 200], [90, 191, 230]), 30)
    assert (result.release_day.tolist(), result.estimated.tolist()) == ([90, 130, 230], [False, True, False])


def test_flags_already_set_stay_set():
    result = estimate_release_days(observations([0, 100], [30, 130], estimated=[True, False]), 30)
    assert (result.release_day.tolist(), result.estimated.tolist()) == ([30, 130], [True, False])


# The known-value rule (SPEC.md §5.3)


def test_a_late_first_release_of_an_older_observation_is_skipped():
    # January's value was first released a week after February's.
    obs = observations(
        [day(2026, 1, 1), day(2026, 2, 1), day(2026, 3, 1)], [day(2026, 3, 20), day(2026, 3, 13), day(2026, 4, 10)]
    )
    assert columns(known_points(obs)) == (
        [day(2026, 2, 1), day(2026, 3, 1)], [2.0, 3.0], [day(2026, 3, 13), day(2026, 4, 10)], [False, False]
    )


def test_of_two_observations_released_on_one_day_the_newer_becomes_the_point():
    obs = observations([day(2026, 2, 1), day(2026, 1, 1)], [day(2026, 3, 13), day(2026, 3, 13)])
    assert columns(known_points(obs)) == ([day(2026, 2, 1)], [1.0], [day(2026, 3, 13)], [False])


def test_a_release_day_with_only_older_observations_gives_no_point():
    obs = observations(
        [day(2026, 3, 1), day(2026, 1, 1), day(2026, 2, 1)], [day(2026, 4, 10), day(2026, 4, 20), day(2026, 4, 20)]
    )
    assert known_points(obs).obs_day.tolist() == [day(2026, 3, 1)]


def literal_walk(obs):
    """SPEC.md §5.3 as worded, one observation at a time: the indices of the points."""
    points = []
    for i in sorted(range(obs.obs_day.size), key=lambda i: (obs.release_day[i], obs.obs_day[i])):
        if not all(obs.obs_day[i] > obs.obs_day[p] for p in points):
            continue
        if points and obs.release_day[points[-1]] == obs.release_day[i]:
            points.pop()  # of the observations released on one day, the newest is the point
        points.append(i)
    return points


@pytest.mark.parametrize("seed", range(6))
def test_points_strictly_increase_and_hold_the_newest_observation_known(seed):
    rng = np.random.default_rng(seed)
    obs_day = rng.choice(3000, 400, replace=False)
    # Lags of up to 120 days, released on a weekly schedule: shared release days and late older releases are common.
    release_day = obs_day + rng.integers(0, 120, obs_day.size)
    release_day += -release_day % 7
    obs = observations(obs_day, release_day, estimated=rng.random(obs_day.size) < 0.3)
    points = known_points(obs)
    walk = literal_walk(obs)
    assert columns(points) == columns(obs.take(walk))
    assert (np.diff(points.release_day) > 0).all() and (np.diff(points.obs_day) > 0).all()
    # The points sit exactly on the release days where the newest known observation changes, and hold it.
    days = np.unique(obs.release_day)
    newest = np.array([obs.obs_day[obs.release_day <= d].max() for d in days])
    changes = np.append(True, newest[1:] > newest[:-1])
    assert (points.release_day.tolist(), points.obs_day.tolist()) == (days[changes].tolist(), newest[changes].tolist())
    # Both cases of §5.3 occur: shared release days, and release days without a point.
    assert days.size < obs_day.size and len(walk) < days.size


def test_no_observations_give_no_points():
    assert columns(known_points(observations([], []))) == ([], [], [], [])


def test_a_fred_body_with_pre_archive_observations_becomes_points():
    dates, release = pre_archive_series()
    rows = [
        {"realtime_start": iso(r), "realtime_end": "9999-12-31", "date": iso(d), "value": f"{1000 + k}.5"}
        for k, (d, r) in enumerate(zip(dates, release))
    ]
    for k in (30, 100):  # July 1992 and May 1998
        rows[k]["value"] = "."
    obs = fred_observations(fred(*rows))
    points = known_points(estimate_release_days(obs, typical_lag(obs, 30)))
    # 1 March 1996 (74) shares 14 June 1996 with the newer 1 April 1996 (75), which becomes that day's point.
    kept = [k for k in range(120) if k not in (30, 74, 100)]
    assert points.obs_day.tolist() == dates[kept].tolist()
    assert points.value.tolist() == [1000.5 + k for k in kept]
    assert points.estimated.tolist() == [k < 74 for k in kept]
    assert points.release_day[kept.index(75)] == day(1996, 6, 14)
    assert (np.diff(points.release_day) > 0).all()


# Forms (SPEC.md §5.4)


def test_y_is_the_logarithm_in_log_form_and_the_value_in_level_form():
    values = np.array([0.5, 1.0, 250.0])
    np.testing.assert_array_equal(y_values(values, "LOG"), np.log(values))
    np.testing.assert_array_equal(y_values(values, "LEVEL"), values)


INVALID = [
    pytest.param("LOG", [3.0, 0.0, 2.0], True, id="log-with-a-zero"),
    pytest.param("LOG", [3.0, -1e-9, 2.0], True, id="log-with-a-negative-value"),
    pytest.param("LOG", [3.0, 5e-324, 2.0], False, id="log-with-the-smallest-positive-value"),
    pytest.param("LEVEL", [3.0, 0.0, -2.0], False, id="level-with-zero-and-negative-values"),
    pytest.param("LOG", [], False, id="no-values"),
]


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize(("form", "values", "invalid"), INVALID)
def test_a_log_series_with_a_value_that_is_not_positive_has_invalid_values(form, values, invalid):
    assert has_invalid_values(np.array(values), form) is invalid


def test_the_catalog_accepts_exactly_the_forms_of_section_5_4():
    assert catalog.FORMS == FORMS == ("LOG", "LEVEL")


def test_an_unknown_form_is_refused():
    with pytest.raises(KeyError):
        y_values(np.array([1.0]), "SQRT")


@pytest.mark.parametrize(
    ("units", "values", "form"),
    [
        ("Percent", [1.0, 2.0], "LEVEL"),
        ("Percent Change from Year Ago", [1.0], "LEVEL"),
        ("Index 2015=100", [101.5, 99.0], "LOG"),
        ("Billions of Dollars", [5.0, 0.0], "LEVEL"),
        ("Index", [3.0, -0.5], "LEVEL"),
        ("Index", [], "LOG"),
    ],
    ids=["percent", "percent-change", "positive", "a-zero", "a-negative", "no-values-yet"],
)
def test_a_user_series_is_level_for_percent_units_or_a_value_that_is_not_positive(units, values, form):
    assert user_form(units, np.array(values)) == form


def test_joined_observations_keep_both_in_order():
    first = observations([10, 20], [100, 120], [True, False])
    second = observations([15], [130])
    assert columns(joined(first, second)) == ([10, 20, 15], [1.0, 2.0, 1.0], [100, 120, 130], [True, False, False])
    assert columns(joined(second, observations([], []))) == columns(second)


@pytest.mark.parametrize("form", FORMS)
def test_original_values_undo_each_form(form):
    values = np.array([0.5, 2.0, 1234.5678])
    np.testing.assert_allclose(original_values(y_values(values, form), form), values, rtol=1e-15)


def test_original_values_keep_nan():
    assert np.isnan(original_values(np.array([np.nan]), "LOG")).all()
    assert original_values(np.array([np.nan, -0.5]), "LEVEL")[1] == -0.5
