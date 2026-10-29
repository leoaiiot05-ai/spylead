"""The web layer with aiohttp's TestServer and TestClient, a temporary store and a stub jobs object (SPEC.md §16.1).

TestServer listens on 127.0.0.1 with a free port, inside the test process; no test reaches another host.
"""
import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import math

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from spylead import web as web_module
from spylead.analysis import Combination, MemberFit, Step3, Step5, Window
from spylead.catalog import CatalogEntry
from spylead.config import Config
from spylead.jobs import JobRunning, LastJob, Progress, payload_of
from spylead.pit import Observations, known_points
from spylead.store import Curve, Store
from spylead.web import create_app

EPOCH = datetime.date(1970, 1, 1)
NOW = datetime.datetime(2026, 10, 28, 15, 0, tzinfo=datetime.UTC)
TOKEN = "Sentinel-token-0123456789"
FRED_KEY = "fredkey0123456789abcdef0123456789"
TIINGO_KEY = "tiingokey0123456789abcdef0123456789abc"
SESSION = hmac.new(TOKEN.encode(), b"spylead-session", hashlib.sha256).hexdigest()
SECRETS = (TOKEN, SESSION, FRED_KEY, TIINGO_KEY)
CONFIG = Config(port=8090, token=TOKEN, fred_api_key=FRED_KEY, tiingo_api_key=TIINGO_KEY)
M2 = CatalogEntry("M2", "M2SL", "LOG", 55, "M2 money stock", "M2 貨幣供給量")
NFCI = CatalogEntry("NFCI", "NFCI", "LEVEL", 5, "Chicago Fed National Financial Conditions Index", "芝加哥聯準銀行全國金融狀況指數")
CATALOG = [M2, NFCI]
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def iso(day):
    return (EPOCH + datetime.timedelta(days=day)).isoformat()


def observations(obs_day, value, release_day, estimated=None):
    obs_day = np.asarray(obs_day, dtype=np.int64)
    flags = np.zeros(obs_day.size, dtype=bool) if estimated is None else np.asarray(estimated, dtype=bool)
    return Observations(obs_day, np.asarray(value, dtype=float), np.asarray(release_day, dtype=np.int64), flags)


class StubJobs:
    """Stands in for spylead.jobs.Jobs: records each start, and refuses all while `running`."""

    def __init__(self):
        self.calls = []
        self.running = False
        self.progress = None
        self.last = None

    def act(self, *call):
        if self.running:
            raise JobRunning
        self.calls.append(call)

    def fetch(self):
        self.act("fetch")

    def recompute(self, inflections_per_year):
        self.act("recompute", inflections_per_year)

    def refit(self, excluded):
        self.act("refit", excluded)

    def add_series(self, fred_id):
        self.act("add_series", fred_id)

    def remove_series(self, name):
        self.act("remove_series", name)


class Delay:
    def __init__(self):
        self.waited = []

    async def __call__(self, seconds):
        self.waited.append(seconds)


def fill(store):
    """Stored series: SPY, the catalog's M2 and NFCI, and a USER series added after the last compute."""
    store.sync_catalog(CATALOG)
    store.set_start_days(8701, 9797)
    store.add_user_series("T5YIE", "LEVEL", "5-Year Breakeven Inflation Rate", "Percent", "D")
    store.set_metadata("M2", "M2", "Billions of Dollars", "M")
    store.set_metadata("NFCI", "National Financial Conditions Index", "Index", "W")
    store.insert_observations("SPY", observations([20750, 20751, 20752], [580.123456789, 581.5, 579.0], [20750, 20751, 20752]))
    store.insert_observations("M2", observations([20600, 20630], [21942.123456, 22088.7], [20655, 20685], [True, False]))
    store.insert_observations("NFCI", observations([20740, 20747], [-0.5123456, -0.48], [20745, 20752]))
    store.insert_observations("T5YIE", observations([20750], [2.31], [20751]))
    store.record_fetch("NFCI", "TIMEOUT: FRED did not answer within 30 s")


def compute(store, f=None):
    """Curves and results in the jobs' format: M2 KEPT and in F, NFCI NOT_LEADING."""
    rows = {row.name: row for row in store.all_series()}
    points = {name: known_points(store.observations(name)) for name in ("SPY", "M2", "NFCI")}
    step3s = {
        "M2": Step3(Window(20000, 20700), -120, 0.83, 0.051, 0.074, 0.4),
        "NFCI": Step3(Window(20000, 20690), 35, -0.2, 0.09, 0.1, -0.3),
    }
    if f is None:
        combination = Combination(
            (MemberFit("M2", -95, 0.7),), 0.1, 0.043, 0.061, 0.1, Window(20749, 20752), 20751,
            np.array([1.0, 1.0123456789, 0.99, 1.0]), np.array([580.0, 581.0, 582.0, 583.123456]),
        )
        f = Step5("OK", combination)
    payload = payload_of(20754, 2.0, points, [rows["M2"], rows["NFCI"]], {"M2": "KEPT", "NFCI": "NOT_LEADING"}, step3s, f)
    curves = {
        "SPY": Curve(20748, np.log([np.nan, 580.0, 581.25, 580.5, 579.875]), 20750, np.log([580.1, 581.0, 579.5])),
        "M2": Curve(20655, np.log([np.nan, 22000.0]), 20655, np.log([21950.0, 22080.0])),
        "NFCI": Curve(20745, np.array([np.nan, -0.5, -0.49]), 20745, np.array([-0.51, -0.48])),
    }
    return store.save_compute(curves, payload)


@pytest.fixture
def store(tmp_path):
    opened = Store.open(tmp_path / "spylead.db", lambda: NOW)
    fill(opened)
    yield opened
    opened.close()


@pytest.fixture
def jobs():
    return StubJobs()


@pytest.fixture
def delay():
    return Delay()


@pytest.fixture
def app(store, jobs, delay):
    return create_app(CONFIG, store, jobs, CATALOG, lambda: NOW, delay)


def served(app, check):
    async def run():
        async with TestClient(TestServer(app)) as client:
            return await check(client)

    return asyncio.run(run())


async def login(client):
    response = await client.post("/api/login", json={"token": TOKEN})
    assert response.status == 204
    return response


async def answer(response):
    return response.status, await response.json()


ROUTES = [
    ("POST", "/api/logout", {}),
    ("GET", "/api/state", None),
    ("POST", "/api/fetch", {}),
    ("POST", "/api/settings", {"inflections_per_year": 2}),
    ("POST", "/api/selection", {"excluded": []}),
    ("POST", "/api/series", {"fred_id": "DGS10"}),
    ("DELETE", "/api/series/T5YIE", None),
    ("GET", "/api/charts/spy", None),
    ("GET", "/api/charts/series/M2", None),
    ("GET", "/api/charts/f", None),
]


# AC-17


@pytest.mark.parametrize("cookie", [None, "0" * 64, SESSION[:-1], f"{SESSION}0"], ids=["none", "wrong", "short", "long"])
def test_every_api_route_but_login_needs_the_session(app, jobs, cookie):
    async def check(client):
        if cookie is not None:
            client.session.cookie_jar.update_cookies({"spylead_session": cookie})
        return [await answer(await client.request(method, path, json=body)) for method, path, body in ROUTES]

    expected = (401, {"error": "UNAUTHORIZED", "message": "Log in first"})
    assert served(app, check) == [expected] * len(ROUTES)
    assert jobs.calls == []


def test_logging_out_clears_the_session(app):
    async def check(client):
        await login(client)
        assert (await client.get("/api/state")).status == 200
        response = await client.post("/api/logout", json={})
        cookie = response.cookies["spylead_session"]
        after = await client.get("/api/state")
        return response.status, cookie.value, cookie["max-age"], after.status

    assert served(app, check) == (204, "", "0", 401)


@pytest.mark.parametrize("path", ["/api/login", "/api/logout", "/api/fetch", "/api/settings", "/api/selection", "/api/series"])
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_a_post_that_is_not_json_gets_415(app, path, content_type):
    async def check(client):
        await login(client)
        headers = {} if content_type is None else {"Content-Type": content_type}
        return await answer(await client.post(path, data=b'{"token": "x"}', headers=headers))

    assert served(app, check) == (415, {"error": "UNSUPPORTED_MEDIA_TYPE", "message": "The body must be application/json"})


@pytest.mark.parametrize("body", [b"{", b"", b"\xff\xfe", b"{'single': 'quotes'}"], ids=["cut-off", "empty", "not-utf-8", "quotes"])
def test_a_body_that_is_not_json_gets_400_invalid_json(app, body):
    async def check(client):
        await login(client)
        return await answer(await client.post("/api/fetch", data=body, headers={"Content-Type": "application/json"}))

    assert served(app, check) == (400, {"error": "INVALID_JSON", "message": "The body is not valid JSON"})


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "true", "false", "null", '"2"', "[2]"])
def test_numbers_must_be_finite_json_numbers(app, jobs, text):
    async def check(client):
        await login(client)
        body = f'{{"inflections_per_year": {text}}}'.encode()
        return await answer(await client.post("/api/settings", data=body, headers={"Content-Type": "application/json"}))

    status, error = served(app, check)
    assert (status, error["error"]) == (400, "INVALID_FIELD")
    assert jobs.calls == []


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/api/state", "api"),
        ("/api/charts/f", "api-error"),
        ("/api/charts/series/NOPE", "api-error"),
        ("/api/nope", "api-error"),
        ("/", "static"),
        ("/index.html", "static"),
        ("/vendor/echarts/echarts.min.js", "static"),
        ("/missing.js", "static-error"),
    ],
)
def test_every_response_carries_the_security_headers(app, path, kind):
    async def check(client):
        await login(client)
        response = await client.get(path)
        await response.read()
        return response.status, dict(response.headers)

    status, headers = served(app, check)
    assert status == (404 if kind.endswith("error") else 200)
    for name, value in HEADERS.items():
        assert headers[name] == value
    assert headers["Cache-Control"] == ("no-store" if kind.startswith("api") else "no-cache")


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/app.js", "text/javascript"),
        ("/style.css", "text/css"),
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/locales/zh-TW.json", "application/json"),
        ("/icons/apple-touch-icon.png", "image/png"),
        ("/vendor/echarts/echarts.min.js", "text/javascript"),
    ],
)
def test_static_files_are_served_with_their_content_types(app, path, content_type):
    # nosniff makes a browser refuse a script or a stylesheet served with another type.
    async def check(client):
        response = await client.get(path)
        await response.read()
        return response.status, response.content_type

    assert served(app, check) == (200, content_type)


def test_an_unauthorized_answer_carries_the_security_headers(app):
    async def check(client):
        response = await client.get("/api/state")
        return response.status, dict(response.headers)

    status, headers = served(app, check)
    assert status == 401
    assert {name: headers[name] for name in HEADERS} == HEADERS
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("proto", "secure"), [(None, ""), ("https", True), ("http", "")])
def test_the_session_cookie_is_httponly_strict_for_30_days_and_secure_behind_https(app, proto, secure):
    async def check(client):
        headers = {} if proto is None else {"X-Forwarded-Proto": proto}
        response = await client.post("/api/login", json={"token": TOKEN}, headers=headers)
        return response.cookies["spylead_session"]

    cookie = served(app, check)
    assert cookie.value == SESSION
    assert (cookie["httponly"], cookie["samesite"], cookie["max-age"], cookie["path"]) == (True, "Strict", "2592000", "/")
    assert cookie["secure"] == secure


def test_no_secret_reaches_a_response_a_log_line_or_an_event(app, store, jobs, caplog):
    compute(store)
    jobs.progress = Progress("FETCH", "2026-10-28T14:59:00Z", "STEP3", 7, 18, "M2")
    jobs.last = LastJob("FETCH", "UPDATED", "2026-10-28T14:00:00Z", "2 series fetched, 0 up to date, 0 failed")

    async def check(client):
        bodies = [await (await client.post("/api/login", json={"token": TOKEN + "x"})).text()]
        await login(client)
        for method, path, body in ROUTES[1:] + [("GET", "/api/nope", None), ("PUT", "/api/state", None)]:
            bodies.append(await (await client.request(method, path, json=body)).text())
        bodies.append(await (await client.post("/api/logout", json={})).text())
        return bodies

    with caplog.at_level(logging.DEBUG):
        bodies = served(app, check)
    texts = bodies + [record.getMessage() for record in caplog.records] + [str(event) for event in store.events(500)]
    assert any("GET /api/state" in text for text in texts)
    for secret in SECRETS:
        assert all(secret not in text for text in texts)


# Login and logout


def test_a_matching_token_logs_in(app, delay):
    async def check(client):
        response = await client.post("/api/login", json={"token": TOKEN})
        return response.status, await response.read(), (await client.get("/api/state")).status

    assert served(app, check) == (204, b"", 200)
    assert delay.waited == []


@pytest.mark.parametrize("token", ["wrong-token-0123456789", TOKEN[:-1], TOKEN + " ", "", "\ud800"])
def test_a_wrong_token_waits_a_second_and_fails(app, delay, caplog, token):
    async def check(client):
        response = await client.post("/api/login", data=json.dumps({"token": token}), headers={"Content-Type": "application/json"})
        return (*await answer(response), "spylead_session" in response.cookies, (await client.get("/api/state")).status)

    with caplog.at_level(logging.WARNING, logger="spylead"):
        result = served(app, check)
    assert result == (401, {"error": "LOGIN_FAILED", "message": "The token does not match"}, False, 401)
    assert delay.waited == [1.0]
    assert [record.getMessage() for record in caplog.records if record.name == "spylead.web"] == ["A login failed"]


@pytest.mark.parametrize("body", [{}, {"token": 5}, {"token": TOKEN, "remember": True}, [TOKEN]])
def test_a_login_body_without_exactly_a_token_string_is_invalid(app, delay, body):
    async def check(client):
        return await answer(await client.post("/api/login", json=body))

    status, error = served(app, check)
    assert (status, error["error"]) == (400, "INVALID_FIELD")
    assert delay.waited == []


# Routes


def accepted(app, method, path, body):
    async def check(client):
        await login(client)
        return await answer(await client.request(method, path, json=body))

    return served(app, check)


def test_fetch_starts_a_fetch(app, jobs):
    assert accepted(app, "POST", "/api/fetch", {}) == (202, {})
    assert jobs.calls == [("fetch",)]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/fetch", {}),
        ("POST", "/api/settings", {"inflections_per_year": 3}),
        ("POST", "/api/selection", {"excluded": ["M2"]}),
        ("POST", "/api/series", {"fred_id": "DGS10"}),
        ("DELETE", "/api/series/T5YIE", None),
    ],
)
def test_a_job_request_while_a_job_runs_gets_409(app, store, jobs, method, path, body):
    compute(store)
    jobs.running = True
    assert accepted(app, method, path, body) == (409, {"error": "JOB_RUNNING", "message": "Another job is running"})


@pytest.mark.parametrize("value", [0.25, 2, 2.75, 24])
def test_a_valid_n_recomputes(app, jobs, value):
    assert accepted(app, "POST", "/api/settings", {"inflections_per_year": value}) == (202, {})
    assert jobs.calls == [("recompute", float(value))]


@pytest.mark.parametrize("value", [0, 0.2, 24.25, 2.1, -2, 1e300])
def test_an_n_outside_the_range_or_the_steps_is_invalid(app, jobs, value):
    status, error = accepted(app, "POST", "/api/settings", {"inflections_per_year": value})
    message = "inflections_per_year must be a number from 0.25 to 24 in steps of 0.25"
    assert (status, error) == (400, {"error": "INVALID_FIELD", "message": message})
    assert jobs.calls == []


@pytest.mark.parametrize("body", [{}, {"inflections_per_year": 2, "extra": 1}])
def test_a_settings_body_needs_exactly_its_field(app, body):
    assert accepted(app, "POST", "/api/settings", body)[1]["error"] == "INVALID_FIELD"


@pytest.mark.parametrize("excluded", [["M2"], [], ["M2", "M2"]])
def test_unchecking_kept_series_refits_f(app, store, jobs, excluded):
    compute(store)
    assert accepted(app, "POST", "/api/selection", {"excluded": excluded}) == (202, {})
    assert jobs.calls == [("refit", excluded)]


@pytest.mark.parametrize("excluded", [["NFCI"], ["T5YIE"], ["NOPE"], "M2", [5], [["M2"]], None])
def test_unchecking_anything_but_a_kept_series_is_invalid(app, store, jobs, excluded):
    compute(store)
    status, error = accepted(app, "POST", "/api/selection", {"excluded": excluded})
    assert (status, error["error"]) == (400, "INVALID_FIELD")
    assert jobs.calls == []


def test_before_a_compute_no_series_can_be_unchecked(app, jobs):
    async def check(client):
        await login(client)
        named = await client.post("/api/selection", json={"excluded": ["M2"]})
        empty = await client.post("/api/selection", json={"excluded": []})
        return named.status, empty.status

    assert served(app, check) == (400, 202)
    assert jobs.calls == [("refit", [])]


@pytest.mark.parametrize(("given", "fred_id"), [("DGS10", "DGS10"), ("dgs10", "DGS10"), ("T10y_2Y", "T10Y_2Y"), ("A" * 40, "A" * 40)])
def test_a_new_fred_id_is_uppercased_and_added(app, jobs, given, fred_id):
    assert accepted(app, "POST", "/api/series", {"fred_id": given}) == (202, {})
    assert jobs.calls == [("add_series", fred_id)]


@pytest.mark.parametrize("given", ["DGS-10", "", "A" * 41, "DGS 10", "ÄBC", 10, None])
def test_a_fred_id_outside_the_pattern_is_invalid(app, jobs, given):
    status, error = accepted(app, "POST", "/api/series", {"fred_id": given})
    assert (status, error) == (400, {"error": "INVALID_FIELD", "message": "fred_id must match [A-Z0-9_]{1,40} once uppercased"})
    assert jobs.calls == []


@pytest.mark.parametrize("given", ["T5YIE", "t5yie", "M2", "M2SL", "NFCI", "SPY"])
def test_a_series_that_is_already_listed_by_name_or_fred_id_gets_409(app, jobs, given):
    assert accepted(app, "POST", "/api/series", {"fred_id": given}) == (
        409, {"error": "ALREADY_EXISTS", "message": "That series is already listed"}
    )
    assert jobs.calls == []


def test_a_user_series_can_be_removed(app, jobs):
    assert accepted(app, "DELETE", "/api/series/T5YIE", None) == (202, {})
    assert jobs.calls == [("remove_series", "T5YIE")]


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [("M2", 400, "NOT_USER_SERIES"), ("SPY", 400, "NOT_USER_SERIES"), ("NOPE", 404, "NOT_FOUND"), ("t5yie", 404, "NOT_FOUND")],
)
def test_only_a_user_series_can_be_removed(app, jobs, name, status, code):
    answered, error = accepted(app, "DELETE", f"/api/series/{name}", None)
    assert (answered, error["error"]) == (status, code)
    assert jobs.calls == []


def test_an_unknown_api_path_or_method_gets_a_json_error(app):
    async def check(client):
        await login(client)
        return [await answer(await client.get("/api/nope")), await answer(await client.put("/api/state", json={}))]

    assert served(app, check) == [
        (404, {"error": "NOT_FOUND", "message": "Not Found"}),
        (405, {"error": "METHOD_NOT_ALLOWED", "message": "Method Not Allowed"}),
    ]


def test_an_unexpected_failure_answers_500_without_its_text(monkeypatch, app, store, caplog):
    def broken():
        raise RuntimeError(f"https://api.stlouisfed.org/fred/series?api_key={FRED_KEY}")

    monkeypatch.setattr(store, "results", broken)
    with caplog.at_level(logging.ERROR, logger="spylead"):
        result = accepted(app, "GET", "/api/state", None)
    assert result == (500, {"error": "INTERNAL_ERROR", "message": "The server failed to answer this request"})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "api_key=***" in logged and FRED_KEY not in logged


# The state document (SPEC.md §11)


def test_the_state_document_with_results(app, store, jobs):
    computed_at = compute(store)
    store.set_excluded([])
    store.append_event("INFO", "FETCH_STARTED", "Fetch started")
    jobs.progress = Progress("FETCH", "2026-10-28T14:59:00Z", "STEP3", 7, 18, "M2")
    jobs.last = LastJob("FETCH", "UP_TO_DATE", "2026-10-27T22:10:05Z", "Every series is up to date")
    status, document = accepted(app, "GET", "/api/state", None)
    assert status == 200
    assert document["app"] == {
        "server_time": "2026-10-28T15:00:00Z",
        "today": "2026-10-28",
        "fetch_start": "1993-10-28",
        "analysis_start": "1996-10-28",
        "inflections_per_year": 2,
        "keys": {"fred": True, "tiingo": True},
        "job": {"kind": "FETCH", "started_at": "2026-10-28T14:59:00Z", "phase": "STEP3", "done": 7, "total": 18, "series": "M2"},
        "last_job": {"kind": "FETCH", "outcome": "UP_TO_DATE", "finished_at": "2026-10-27T22:10:05Z", "message": "Every series is up to date"},
        "computed_at": computed_at,
    }
    assert document["spy"] == {"last_obs_day": iso(20752), "points": 3, "last_fetch_error": None}
    m2, nfci, t5yie = document["series"]
    assert m2 == {
        "name": "M2", "origin": "CATALOG", "source_id": "M2SL", "title": "M2 money stock", "title_zh": "M2 貨幣供給量",
        "units": "Billions of Dollars", "frequency": "M", "form": "LOG",
        "last_obs_day": iso(20630), "last_release_day": iso(20685), "points": 2, "estimated_points": 1,
        "last_fetch_error": None, "status": "KEPT", "lead_name": "LEADL1_M2",
        "a_star": -120, "beta": 0.83, "score": 0.051, "score_oos": 0.074, "slope_pct_per_year": 0.4,
        "window": {"start": iso(20000), "end": iso(20700)}, "selected": True,
    }
    assert (nfci["status"], nfci["lead_name"], nfci["selected"], nfci["a_star"]) == ("NOT_LEADING", None, None, 35)
    assert nfci["last_fetch_error"] == "TIMEOUT: FRED did not answer within 30 s"
    assert (t5yie["origin"], t5yie["title"], t5yie["title_zh"], t5yie["status"]) == ("USER", "5-Year Breakeven Inflation Rate", None, "PENDING")
    assert (t5yie["points"], t5yie["a_star"], t5yie["window"], t5yie["selected"]) == (1, None, None, None)
    assert document["f"] == {
        "status": "OK",
        "members": [{"name": "M2", "b": -95, "beta": 0.7, "lead_name": "LEADL1_M2"}],
        "score": 0.043, "score_oos": 0.061, "slope_pct_per_year": 0.1,
        "window": {"start": iso(20749), "end": iso(20752)}, "split_day": iso(20751),
    }
    assert document["events"][0] == {
        "id": 1, "ts": "2026-10-28T15:00:00Z", "level": "INFO", "kind": "FETCH_STARTED", "series": None, "message": "Fetch started"
    }


def test_an_unchecked_member_reads_as_not_selected(app, store):
    compute(store)
    store.set_excluded(["M2"])
    assert accepted(app, "GET", "/api/state", None)[1]["series"][0]["selected"] is False


def test_the_state_document_without_results(app, store, tmp_path):
    store.record_fetch("NFCI", "TIMEOUT: FRED did not answer within 30 s")
    status, document = accepted(app, "GET", "/api/state", None)
    assert status == 200
    assert (document["app"]["computed_at"], document["app"]["job"], document["app"]["last_job"], document["f"]) == (None,) * 4
    assert [entry["status"] for entry in document["series"]] == ["PENDING", "PENDING", "PENDING"]
    assert document["series"][0]["points"] == 2 and document["series"][0]["estimated_points"] == 1
    for entry in document["series"]:
        assert {entry[key] for key in ("a_star", "beta", "score", "score_oos", "slope_pct_per_year", "window")} == {None}
    assert document["spy"] == {"last_obs_day": iso(20752), "points": 3, "last_fetch_error": None}


def test_a_series_without_data_after_a_failed_fetch_reads_as_fetch_failed(tmp_path, jobs, delay):
    store = Store.open(tmp_path / "empty.db", lambda: NOW)
    store.sync_catalog(CATALOG)
    store.record_fetch("M2", "HTTP_500: FRED answered HTTP 500")
    status, document = accepted(create_app(CONFIG, store, jobs, CATALOG, lambda: NOW, delay), "GET", "/api/state", None)
    assert [entry["status"] for entry in document["series"]] == ["FETCH_FAILED", "PENDING"]
    assert (document["app"]["fetch_start"], document["spy"]) == (None, {"last_obs_day": None, "points": 0, "last_fetch_error": None})
    store.close()


def test_the_keys_say_only_whether_each_key_is_set(store, jobs, delay):
    config = Config(port=8090, token=TOKEN, fred_api_key="", tiingo_api_key=TIINGO_KEY)
    document = accepted(create_app(config, store, jobs, CATALOG, lambda: NOW, delay), "GET", "/api/state", None)[1]
    assert document["app"]["keys"] == {"fred": False, "tiingo": True}


# Chart payloads (AC-18)


def test_the_spy_chart(app, store):
    compute(store)
    assert accepted(app, "GET", "/api/charts/spy", None) == (
        200,
        {
            "name": "SPY", "lead_name": None, "units": "USD",
            "raw": {"days": [20750, 20751, 20752], "values": [580.123, 581.5, 579.0]},
            "rt": {"first_day": 20748, "values": [None, 580.0, 581.25, 580.5, 579.875]},
            "ref": {"first_day": 20750, "values": [580.1, 581.0, 579.5]},
        },
    )


def test_a_log_series_chart_is_in_original_units_with_points_at_release_days(app, store):
    compute(store)
    assert accepted(app, "GET", "/api/charts/series/M2", None) == (
        200,
        {
            "name": "M2", "lead_name": "LEADL1_M2", "units": "Billions of Dollars",
            "raw": {"days": [20655, 20685], "values": [21942.1, 22088.7]},
            "rt": {"first_day": 20655, "values": [None, 22000.0]},
            "ref": {"first_day": 20655, "values": [21950.0, 22080.0]},
        },
    )


def test_a_level_series_chart_keeps_its_values(app, store):
    compute(store)
    status, chart = accepted(app, "GET", "/api/charts/series/NFCI", None)
    assert (status, chart["lead_name"], chart["units"]) == (200, None, "Index")
    assert chart["raw"] == {"days": [20745, 20752], "values": [-0.512346, -0.48]}
    assert chart["rt"] == {"first_day": 20745, "values": [None, -0.5, -0.49]}


def test_the_f_chart(app, store):
    compute(store)
    assert accepted(app, "GET", "/api/charts/f", None) == (
        200,
        {
            "f": {"first_day": 20749, "values": [1.0, 1.01235, 0.99, 1.0]},
            "spyl1": {"first_day": 20749, "values": [580.0, 581.25, 580.5, 579.875]},
            "composite": {"first_day": 20749, "values": [580.0, 581.0, 582.0, 583.123]},
            "split_day": 20751,
            "members": [{"name": "M2", "b": -95, "beta": 0.7, "lead_name": "LEADL1_M2"}],
        },
    )


@pytest.mark.parametrize("path", ["/api/charts/spy", "/api/charts/series/M2"])
def test_charts_before_a_compute_get_no_data(app, path):
    assert accepted(app, "GET", path, None) == (404, {"error": "NO_DATA", "message": "Nothing is computed for this series yet"})


def test_a_series_without_a_curve_gets_no_data(app, store):
    compute(store)
    assert accepted(app, "GET", "/api/charts/series/T5YIE", None)[1]["error"] == "NO_DATA"


@pytest.mark.parametrize("name", ["NOPE", "SPY"])
def test_a_chart_of_no_candidate_gets_not_found(app, store, name):
    compute(store)
    assert accepted(app, "GET", f"/api/charts/series/{name}", None) == (404, {"error": "NOT_FOUND", "message": "There is no such series"})


@pytest.mark.parametrize("computed", [False, True])
def test_the_f_chart_without_f_gets_no_f(app, store, computed):
    if computed:
        compute(store, f=Step5("NO_MEMBERS"))
    assert accepted(app, "GET", "/api/charts/f", None) == (404, {"error": "NO_F", "message": "There is no F"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(123456789.0, 123457000.0), (0.000123456789, 0.000123457), (-1.23456789, -1.23457), (2.5, 2.5), (math.nan, None), (math.inf, None)],
)
def test_values_are_rounded_to_6_significant_digits_and_nan_becomes_null(value, expected):
    assert web_module.rounded([value]) == [expected]


def test_tokens_and_cookies_are_compared_in_constant_time(monkeypatch, app):
    compared = []
    compare = hmac.compare_digest

    def recording(given, expected):
        compared.append((given, expected))
        return compare(given, expected)

    monkeypatch.setattr(web_module.hmac, "compare_digest", recording)

    async def check(client):
        await login(client)
        return (await client.get("/api/state")).status

    assert served(app, check) == 200
    assert (TOKEN.encode(), TOKEN.encode()) in compared
    assert (SESSION.encode(), SESSION.encode()) in compared
