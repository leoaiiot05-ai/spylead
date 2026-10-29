import asyncio
import datetime
import json
import logging
import socket
import traceback
from dataclasses import dataclass

import aiohttp
import pytest

from spylead.sources import Fred, FredSeries, SourceError, Tiingo, redact

EPOCH = datetime.date(1970, 1, 1)
FRED_KEY = "abcdef0123456789abcdef0123456789"
TIINGO_KEY = "0123456789abcdef0123456789abcdef01234567"

TIINGO_META = {
    "ticker": "SPY",
    "name": "SPDR S&P 500 ETF Trust",
    "exchangeCode": "NYSE ARCA",
    "startDate": "1993-01-29",
    "endDate": "2026-10-27",
    "description": "...",
}
TIINGO_ROWS = [
    {"date": "2026-10-26T00:00:00.000Z", "close": 580.1, "high": 581.0, "low": 576.2, "adjClose": 575.4, "volume": 1},
    {"date": "2026-10-27T00:00:00.000Z", "close": 581.5, "high": 583.0, "low": 579.9, "adjClose": 576.8, "volume": 2},
]
FRED_SERIES_BODY = {
    "realtime_start": "2026-10-28",
    "realtime_end": "2026-10-28",
    "seriess": [
        {
            "id": "M2SL",
            "realtime_start": "2026-10-28",
            "realtime_end": "2026-10-28",
            "title": "M2",
            "observation_start": "1959-01-01",
            "observation_end": "2026-09-01",
            "frequency": "Monthly",
            "frequency_short": "M",
            "units": "Billions of Dollars",
            "units_short": "Bil. of $",
            "seasonal_adjustment": "Seasonally Adjusted",
            "seasonal_adjustment_short": "SA",
            "last_updated": "2026-10-22 12:01:02-05",
            "popularity": 80,
            "notes": "...",
        }
    ],
}
FRED_OBSERVATIONS_BODY = {
    "realtime_start": "1776-07-04",
    "realtime_end": "9999-12-31",
    "observation_start": "2026-07-01",
    "observation_end": "9999-12-31",
    "units": "lin",
    "output_type": 4,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 3,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {"realtime_start": "2026-08-26", "realtime_end": "9999-12-31", "date": "2026-07-01", "value": "21942.1"},
        {"realtime_start": "2026-09-23", "realtime_end": "9999-12-31", "date": "2026-08-01", "value": "."},
        {"realtime_start": "2026-10-22", "realtime_end": "9999-12-31", "date": "2026-09-01", "value": "22088.7"},
    ],
}


def iso_day(text):
    return (datetime.date.fromisoformat(text) - EPOCH).days


@dataclass(frozen=True)
class Answer:
    """One scripted answer: a status with a body and headers, or an exception that the request raises."""

    status: int = 200
    body: str | BaseException = ""
    headers: tuple[tuple[str, str], ...] = ()
    raises: BaseException | None = None


def ok(body):
    return Answer(200, json.dumps(body))


def status(code, retry_after=None):
    return Answer(code, "{}", () if retry_after is None else (("Retry-After", retry_after),))


def raising(error):
    return Answer(raises=error)


@dataclass(frozen=True)
class Request:
    url: str
    params: dict | None
    headers: dict
    timeout: aiohttp.ClientTimeout


class FakeResponse:
    def __init__(self, answer):
        self.answer = answer
        self.status = answer.status
        self.headers = dict(answer.headers)

    async def __aenter__(self):
        if self.answer.raises is not None:
            raise self.answer.raises
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        if isinstance(self.answer.body, BaseException):
            raise self.answer.body
        return self.answer.body


class FakeSession:
    """Stands in for aiohttp.ClientSession.get: records each request and plays the scripted answers in order."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.requests.append(Request(url, params, headers, timeout))
        return FakeResponse(self.answers.pop(0))


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def clock():
    return FakeTime()


def tiingo(session, clock, key=TIINGO_KEY):
    return Tiingo(session, key, clock.sleep, clock.monotonic)


def fred(session, clock, key=FRED_KEY):
    return Fred(session, key, clock.sleep, clock.monotonic)


def run(coroutine):
    return asyncio.run(coroutine)


def failure(call):
    with pytest.raises(SourceError) as caught:
        run(call)
    return caught.value


# Each request (SPEC.md §4.1, §4.2)


def test_the_tiingo_meta_request_carries_the_key_in_a_header_and_gives_end_date(clock):
    session = FakeSession(ok(TIINGO_META))
    assert run(tiingo(session, clock).end_day()) == iso_day("2026-10-27")
    (request,) = session.requests
    assert request.url == "https://api.tiingo.com/tiingo/daily/SPY"
    assert request.params is None
    assert request.headers == {"Authorization": f"Token {TIINGO_KEY}"}
    assert request.timeout == aiohttp.ClientTimeout(total=30)


def test_the_tiingo_price_request_gives_closes_on_their_trading_days(clock):
    session = FakeSession(ok(TIINGO_ROWS))
    closes = run(tiingo(session, clock).prices(iso_day("2026-10-20"), iso_day("2026-10-27")))
    (request,) = session.requests
    assert request.url == "https://api.tiingo.com/tiingo/daily/SPY/prices"
    assert list(request.params.items()) == [("startDate", "2026-10-20"), ("endDate", "2026-10-27"), ("format", "json")]
    assert request.headers == {"Authorization": f"Token {TIINGO_KEY}"}
    assert TIINGO_KEY not in request.url + json.dumps(request.params)
    assert closes.obs_day.tolist() == closes.release_day.tolist() == [iso_day("2026-10-26"), iso_day("2026-10-27")]
    assert closes.value.tolist() == [580.1, 581.5]


def test_the_fred_series_request_carries_the_key_in_the_query_and_gives_last_updated(clock):
    session = FakeSession(ok(FRED_SERIES_BODY))
    assert run(fred(session, clock).series("M2SL")) == FredSeries(
        "M2", "Billions of Dollars", "M", "2026-10-22 12:01:02-05", iso_day("1959-01-01"), iso_day("2026-09-01")
    )
    (request,) = session.requests
    assert request.url == "https://api.stlouisfed.org/fred/series"
    assert list(request.params.items()) == [("series_id", "M2SL"), ("api_key", FRED_KEY), ("file_type", "json")]
    assert request.headers == {}
    assert request.timeout == aiohttp.ClientTimeout(total=30)


def test_the_fred_observations_request_asks_for_initial_releases(clock):
    session = FakeSession(ok(FRED_OBSERVATIONS_BODY))
    releases = run(fred(session, clock).observations("M2SL", iso_day("2026-07-01")))
    (request,) = session.requests
    assert request.url == "https://api.stlouisfed.org/fred/series/observations"
    assert list(request.params.items()) == [
        ("series_id", "M2SL"),
        ("api_key", FRED_KEY),
        ("file_type", "json"),
        ("realtime_start", "1776-07-04"),
        ("realtime_end", "9999-12-31"),
        ("output_type", "4"),
        ("observation_start", "2026-07-01"),
        ("limit", "100000"),
    ]
    assert releases.obs_day.tolist() == [iso_day("2026-07-01"), iso_day("2026-09-01")]
    assert releases.release_day.tolist() == [iso_day("2026-08-26"), iso_day("2026-10-22")]
    assert releases.value.tolist() == [21942.1, 22088.7]


# Retries (SPEC.md §8.4)

FAILURES = [
    pytest.param(status(503), "HTTP_503", "Tiingo answered HTTP 503", id="http-503"),
    pytest.param(status(500), "HTTP_500", "Tiingo answered HTTP 500", id="http-500"),
    pytest.param(raising(TimeoutError()), "TIMEOUT", "Tiingo did not answer within 30 s", id="timeout"),
    pytest.param(raising(aiohttp.ServerTimeoutError()), "TIMEOUT", "Tiingo did not answer within 30 s", id="read-timeout"),
    pytest.param(
        raising(aiohttp.ClientConnectionError()), "CONNECTION_FAILED", "Tiingo could not be reached", id="connection"
    ),
    pytest.param(
        Answer(200, aiohttp.ClientPayloadError()), "CONNECTION_FAILED", "Tiingo could not be reached", id="cut-off-body"
    ),
]


@pytest.mark.parametrize(("answer", "code", "text"), FAILURES)
@pytest.mark.parametrize("count", [1, 3])
def test_failures_are_retried_after_2_4_and_8_seconds(clock, answer, code, text, count):
    session = FakeSession(*[answer] * count, ok(TIINGO_META))
    assert run(tiingo(session, clock).end_day()) == iso_day("2026-10-27")
    assert clock.slept == [2, 4, 8][:count]
    assert len(session.requests) == count + 1


@pytest.mark.parametrize(("answer", "code", "text"), FAILURES)
def test_a_fourth_failure_ends_the_request(clock, answer, code, text):
    session = FakeSession(*[answer] * 4)
    error = failure(tiingo(session, clock).end_day())
    assert (error.code, error.text, str(error)) == (code, text, f"{code}: {text}")
    assert clock.slept == [2, 4, 8]
    assert len(session.requests) == 4


RETRY_AFTER = [
    pytest.param("7", 7, id="seconds"),
    pytest.param("120", 60, id="capped-at-60"),
    pytest.param(None, 60, id="missing"),
    pytest.param("Wed, 28 Oct 2026 14:05:00 GMT", 60, id="http-date"),
    pytest.param("1.5", 60, id="not-an-integer"),
    pytest.param("-5", 0, id="negative"),
]


@pytest.mark.parametrize(("value", "wait"), RETRY_AFTER)
def test_a_429_waits_for_retry_after_then_retries(clock, value, wait):
    session = FakeSession(status(429, value), ok(TIINGO_META))
    assert run(tiingo(session, clock).end_day()) == iso_day("2026-10-27")
    assert clock.slept == [wait]
    assert len(session.requests) == 2


def test_a_429_is_retried_once(clock):
    session = FakeSession(status(429, "3"), status(429, "3"))
    error = failure(tiingo(session, clock).end_day())
    assert (error.code, error.text) == ("HTTP_429", "Tiingo answered HTTP 429")
    assert clock.slept == [3]
    assert len(session.requests) == 2


def test_a_429_does_not_use_up_the_server_error_retries(clock):
    session = FakeSession(status(503), status(429, "3"), status(503), status(503), ok(TIINGO_META))
    assert run(tiingo(session, clock).end_day()) == iso_day("2026-10-27")
    assert clock.slept == [2, 3, 4, 8]


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_other_4xx_answers_are_not_retried(clock, code):
    session = FakeSession(status(code))
    error = failure(fred(session, clock).series("NOT_A_SERIES"))
    assert (error.code, error.text) == (f"HTTP_{code}", f"FRED answered HTTP {code}")
    assert clock.slept == []
    assert len(session.requests) == 1


# FRED's pacing (SPEC.md §8.4)


def test_fred_waits_half_a_second_between_requests(clock):
    session = FakeSession(ok(FRED_SERIES_BODY), ok(FRED_SERIES_BODY), ok(FRED_OBSERVATIONS_BODY))
    client = fred(session, clock)
    run(client.series("M2SL"))
    run(client.series("ICSA"))
    run(client.observations("ICSA", iso_day("2026-01-01")))
    assert clock.slept == [0.5, 0.5]


def test_fred_waits_only_for_the_rest_of_the_half_second(clock):
    session = FakeSession(ok(FRED_SERIES_BODY), ok(FRED_SERIES_BODY), ok(FRED_SERIES_BODY))
    client = fred(session, clock)
    run(client.series("M2SL"))
    clock.now += 0.3
    run(client.series("ICSA"))
    clock.now += 2.0
    run(client.series("VIXCLS"))
    assert clock.slept == [pytest.approx(0.2)]


def test_fred_retries_count_as_requests_but_already_wait_longer(clock):
    session = FakeSession(status(503), ok(FRED_SERIES_BODY))
    run(fred(session, clock).series("M2SL"))
    assert clock.slept == [2]


def test_tiingo_is_not_paced(clock):
    session = FakeSession(ok(TIINGO_META), ok(TIINGO_ROWS))
    client = tiingo(session, clock)
    run(client.end_day())
    run(client.prices(iso_day("2026-10-20"), iso_day("2026-10-27")))
    assert clock.slept == []


# MISSING_KEY and bad payloads (SPEC.md §8.3)

CALLS = {
    "tiingo-meta": lambda client: client.end_day(),
    "tiingo-prices": lambda client: client.prices(iso_day("2026-10-20"), iso_day("2026-10-27")),
    "fred-series": lambda client: client.series("M2SL"),
    "fred-observations": lambda client: client.observations("M2SL", iso_day("2026-07-01")),
}


def client_for(name, session, clock, key=None):
    if name.startswith("tiingo"):
        return tiingo(session, clock, TIINGO_KEY if key is None else key)
    return fred(session, clock, FRED_KEY if key is None else key)


@pytest.mark.parametrize("name", CALLS)
def test_an_empty_key_fails_with_missing_key_before_any_request(clock, name):
    session = FakeSession()
    error = failure(CALLS[name](client_for(name, session, clock, key="")))
    source = "Tiingo" if name.startswith("tiingo") else "FRED"
    assert (error.code, error.text) == ("MISSING_KEY", f"the {source} API key is empty in config.toml")
    assert session.requests == []


BAD_PAYLOADS = [
    pytest.param("tiingo-meta", Answer(200, "<html>Service page</html>"), "Tiingo sent a body that is not JSON", id="html"),
    pytest.param("tiingo-meta", ok([TIINGO_META]), "Tiingo sent an unexpected body: metadata must be an object", id="list"),
    pytest.param("tiingo-meta", ok({"ticker": "SPY"}), "Tiingo sent an unexpected body: missing key endDate", id="no-end-date"),
    pytest.param(
        "tiingo-meta", ok({"endDate": "yesterday"}), "Tiingo sent an unexpected body: endDate must be a date", id="bad-date"
    ),
    pytest.param("tiingo-prices", ok({"rows": []}), "Tiingo sent an unexpected body: prices must be a list", id="prices"),
    pytest.param(
        "fred-series", ok({"seriess": []}), "FRED sent an unexpected body: seriess must be a list with a series", id="none"
    ),
    pytest.param(
        "fred-series",
        ok({"seriess": [{"title": "M2"}]}),
        "FRED sent an unexpected body: series 1: missing key units",
        id="series-without-units",
    ),
    pytest.param(
        "fred-observations",
        ok({"observations": [{"date": "2026-07-01", "value": "n/a", "realtime_start": "2026-08-26"}]}),
        "FRED sent an unexpected body: observation 1: value must be a finite number",
        id="bad-value",
    ),
    pytest.param("fred-observations", Answer(200, ""), "FRED sent a body that is not JSON", id="empty"),
]


@pytest.mark.parametrize(("name", "answer", "text"), BAD_PAYLOADS)
def test_a_bad_payload_fails_with_bad_payload_and_no_retry(clock, name, answer, text):
    session = FakeSession(answer)
    error = failure(CALLS[name](client_for(name, session, clock)))
    assert (error.code, error.text) == ("BAD_PAYLOAD", text)
    assert len(session.requests) == 1
    assert clock.slept == []


# No key in an error or a log line (AC-17, redaction part)

SUCCESSES = {
    "tiingo-meta": TIINGO_META,
    "tiingo-prices": TIINGO_ROWS,
    "fred-series": FRED_SERIES_BODY,
    "fred-observations": FRED_OBSERVATIONS_BODY,
}
# aiohttp's own messages can hold a URL, and with it FRED's key.
LEAKY = aiohttp.ClientConnectionError(f"Cannot connect: https://x/y?api_key={FRED_KEY} Token {TIINGO_KEY}")
FAILING = {
    "server-errors": [status(503)] * 4,
    "timeouts": [raising(TimeoutError())] * 4,
    "refused": [raising(LEAKY)] * 4,
    "rate-limited": [status(429), status(429)],
    "not-found": [status(404)],
    "bad-json": [Answer(200, "not json")],
    "bad-shape": [ok({"unexpected": True})],
}
SCENARIOS = [pytest.param(name, [ok(body)], id=f"{name}-ok") for name, body in SUCCESSES.items()] + [
    pytest.param(name, answers, id=f"{name}-{label}") for label, answers in FAILING.items() for name in CALLS
]


@pytest.mark.parametrize(("name", "answers"), SCENARIOS)
def test_no_key_reaches_an_error_text_or_a_log_line(caplog, clock, name, answers):
    texts = []
    with caplog.at_level(logging.DEBUG, logger="spylead"):
        try:
            run(CALLS[name](client_for(name, FakeSession(*answers), clock)))
        except SourceError as error:
            # A logged traceback shows no other exception, such as aiohttp's with its URL.
            assert error.__cause__ is None
            assert error.__context__ is None or error.__suppress_context__
            texts = [str(error), error.text, "".join(traceback.format_exception(error))]
    lines = [record.getMessage() for record in caplog.records]
    assert lines
    for text in texts + lines:
        assert FRED_KEY not in text
        assert TIINGO_KEY not in text
    for text in texts[:2]:
        assert "://" not in text


def test_the_logged_fred_url_is_redacted(caplog, clock):
    with caplog.at_level(logging.DEBUG, logger="spylead"):
        run(fred(FakeSession(ok(FRED_SERIES_BODY)), clock).series("M2SL"))
    assert [record.getMessage() for record in caplog.records] == [
        "FRED request: GET https://api.stlouisfed.org/fred/series?series_id=M2SL&api_key=***&file_type=json",
        "FRED answered HTTP 200",
    ]


def test_retries_are_logged_as_warnings(caplog, clock):
    with caplog.at_level(logging.DEBUG, logger="spylead"):
        run(fred(FakeSession(status(503), ok(FRED_SERIES_BODY)), clock).series("M2SL"))
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["FRED: FRED answered HTTP 503; retrying in 2 s"]


REDACTIONS = [
    pytest.param(
        "https://api.stlouisfed.org/fred/series?series_id=M2SL&api_key=abc123&file_type=json",
        "https://api.stlouisfed.org/fred/series?series_id=M2SL&api_key=***&file_type=json",
        id="middle",
    ),
    pytest.param("https://x/y?series_id=M2SL&api_key=abc123", "https://x/y?series_id=M2SL&api_key=***", id="last"),
    pytest.param("a?api_key=one&b=2 and c?api_key=two", "a?api_key=***&b=2 and c?api_key=***", id="two-urls"),
    pytest.param("https://x/y?API_KEY=abc123#part", "https://x/y?API_KEY=***#part", id="upper-case-and-fragment"),
    pytest.param("url='https://x/y?api_key=abc123' failed", "url='https://x/y?api_key=***' failed", id="in-a-message"),
    pytest.param("https://x/y?series_id=M2SL", "https://x/y?series_id=M2SL", id="no-key"),
]


@pytest.mark.parametrize(("text", "expected"), REDACTIONS)
def test_redact_replaces_every_api_key_value(text, expected):
    assert redact(text) == expected
