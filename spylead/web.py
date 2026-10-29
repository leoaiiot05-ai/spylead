"""The JSON API and the static UI (SPEC.md §11, §13), served on 127.0.0.1 only.

The web layer validates input, then calls the jobs and reads the store. It holds no analysis logic.
"""
import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from aiohttp import web

from spylead import analysis
from spylead.catalog import FRED_ID, SPY_NAME, CatalogEntry
from spylead.config import Config
from spylead.jobs import JobRunning, LastJob, Progress, candidates
from spylead.pit import day_to_date, known_points, original_values, today_at, utc_text
from spylead.sources import redact
from spylead.store import ORIGIN_USER, Counts, SeriesRow, Store

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "spylead_session"
SESSION_MESSAGE = b"spylead-session"
SESSION_MAX_AGE_S = 30 * 24 * 3600
LOGIN_DELAY_S = 1.0
EVENTS_SHOWN = 50
N_LOW, N_HIGH, N_STEPS_PER_UNIT = 0.25, 24, 4
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
STATUS_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}
# Every error code the API answers with (SPEC.md §11), for the UI's locales.
ERROR_CODES = (
    *("LOGIN_FAILED", "UNAUTHORIZED", "UNSUPPORTED_MEDIA_TYPE", "INVALID_JSON", "INVALID_FIELD", "JOB_RUNNING"),
    *("ALREADY_EXISTS", "NOT_FOUND", "NOT_USER_SERIES", "NO_DATA", "NO_F"),
    *("METHOD_NOT_ALLOWED", "PAYLOAD_TOO_LARGE", "INTERNAL_ERROR"),
)


class JobsLike(Protocol):
    """What the web layer needs of spylead.jobs.Jobs."""

    progress: Progress | None
    last: LastJob | None

    def fetch(self) -> None: ...
    def recompute(self, inflections_per_year: float) -> None: ...
    def refit(self, excluded: Sequence[str]) -> None: ...
    def add_series(self, fred_id: str) -> None: ...
    def remove_series(self, name: str) -> None: ...


@dataclass(frozen=True)
class Context:
    config: Config
    store: Store
    jobs: JobsLike
    catalog: Sequence[CatalogEntry]
    clock: Callable[[], Any]
    delay: Callable[[float], Awaitable[None]]
    session: str = dataclasses.field(repr=False)


CONTEXT = web.AppKey("context", Context)


class ApiError(Exception):
    """An answer of the error format of SPEC.md §11. The message is English and holds no secret."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def session_value(token: str) -> str:
    """The session cookie's value: the hex HMAC-SHA256 of spylead-session, keyed with the token (SPEC.md §11)."""
    return hmac.new(token.encode(), SESSION_MESSAGE, hashlib.sha256).hexdigest()


def same_secret(given: str, expected: str) -> bool:
    # JSON can carry a lone surrogate, which plain UTF-8 cannot encode.
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def create_app(
    config: Config,
    store: Store,
    jobs: JobsLike,
    catalog: Sequence[CatalogEntry],
    clock: Callable[[], Any],
    delay: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> web.Application:
    app = web.Application(middlewares=[answer_errors, require_session])
    app[CONTEXT] = Context(config, store, jobs, catalog, clock, delay, session_value(config.token))
    app.on_response_prepare.append(add_headers)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/fetch", fetch)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/selection", selection)
    app.router.add_post("/api/series", add_series)
    app.router.add_delete("/api/series/{name}", remove_series)
    app.router.add_get("/api/charts/spy", spy_chart)
    app.router.add_get("/api/charts/series/{name}", series_chart)
    app.router.add_get("/api/charts/f", f_chart)
    # Each top-level file and folder by name, not one static route at /: that would also answer /api/* paths.
    app.router.add_get("/", file_route(STATIC / "index.html"))
    for path in sorted(STATIC.iterdir()):
        if path.is_dir():
            app.router.add_static(f"/{path.name}", path)
        else:
            app.router.add_get(f"/{path.name}", file_route(path))
    return app


def file_route(path: Path) -> Callable[[web.Request], Awaitable[web.FileResponse]]:
    async def handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(path)

    return handler


def context(request: web.Request) -> Context:
    return request.app[CONTEXT]


def is_api(request: web.Request) -> bool:
    return request.path == "/api" or request.path.startswith("/api/")


# Middlewares and headers


@web.middleware
async def answer_errors(request: web.Request, handler: Callable) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as error:
        return error_response(error.status, error.code, error.message)
    except web.HTTPException as error:
        if error.status < 400 or not is_api(request):
            raise
        return error_response(error.status, STATUS_CODES.get(error.status, f"HTTP_{error.status}"), error.reason)
    except Exception as error:
        # Exception texts can hold anything, so the answer has none, and the log gets the traceback redacted.
        log.error("%s %s failed:\n%s", request.method, request.path, redact("".join(traceback.format_exception(error))))
        if not is_api(request):
            raise
        return error_response(500, "INTERNAL_ERROR", "The server failed to answer this request")


@web.middleware
async def require_session(request: web.Request, handler: Callable) -> web.StreamResponse:
    if is_api(request) and request.path != "/api/login":
        if not same_secret(request.cookies.get(SESSION_COOKIE, ""), context(request).session):
            raise ApiError(401, "UNAUTHORIZED", "Log in first")
    return await handler(request)


async def add_headers(request: web.Request, response: web.StreamResponse) -> None:
    """The headers of SPEC.md §13, on every response."""
    response.headers.update(SECURITY_HEADERS)
    response.headers["Cache-Control"] = "no-store" if is_api(request) else "no-cache"


def error_response(status: int, code: str, message: str) -> web.Response:
    return json_response({"error": code, "message": message}, status)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda value: json.dumps(value, allow_nan=False))


async def json_body(request: web.Request, fields: Sequence[str]) -> dict[str, Any]:
    """The JSON object of a POST body, with exactly `fields` (SPEC.md §11)."""
    if request.content_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "The body must be application/json")
    try:
        body = json.loads(await request.text())
    except ValueError:
        raise ApiError(400, "INVALID_JSON", "The body is not valid JSON") from None
    if not isinstance(body, dict) or sorted(body) != sorted(fields):
        expected = ", ".join(fields) or "no field"
        raise ApiError(400, "INVALID_FIELD", f"The body must be a JSON object with {expected}")
    return body


def finite_number(value: Any) -> bool:
    # JSON true and false arrive as bool, which Python counts as an int; Python's json also reads NaN and Infinity.
    return type(value) in (int, float) and math.isfinite(value)


def start(action: Callable[..., None], *arguments: Any) -> web.Response:
    try:
        action(*arguments)
    except JobRunning:
        raise ApiError(409, "JOB_RUNNING", "Another job is running") from None
    return json_response({}, 202)


# Routes


async def login(request: web.Request) -> web.Response:
    token = (await json_body(request, ("token",)))["token"]
    if not isinstance(token, str):
        raise ApiError(400, "INVALID_FIELD", "token must be a string")
    ctx = context(request)
    if not same_secret(token, ctx.config.token):
        log.warning("A login failed")
        await ctx.delay(LOGIN_DELAY_S)
        raise ApiError(401, "LOGIN_FAILED", "The token does not match")
    response = web.Response(status=204)
    set_session_cookie(request, response, ctx.session, SESSION_MAX_AGE_S)
    return response


async def logout(request: web.Request) -> web.Response:
    await json_body(request, ())
    response = web.Response(status=204)
    set_session_cookie(request, response, "", 0)
    return response


def set_session_cookie(request: web.Request, response: web.Response, value: str, max_age: int) -> None:
    # Tailscale Serve terminates HTTPS and forwards plain HTTP to 127.0.0.1 (SPEC.md §13).
    secure = request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    response.set_cookie(SESSION_COOKIE, value, max_age=max_age, path="/", httponly=True, samesite="Strict", secure=secure)


async def state(request: web.Request) -> web.Response:
    return json_response(state_document(context(request)))


async def fetch(request: web.Request) -> web.Response:
    await json_body(request, ())
    return start(context(request).jobs.fetch)


async def settings(request: web.Request) -> web.Response:
    value = (await json_body(request, ("inflections_per_year",)))["inflections_per_year"]
    if not valid_n(value):
        raise ApiError(400, "INVALID_FIELD", "inflections_per_year must be a number from 0.25 to 24 in steps of 0.25")
    return start(context(request).jobs.recompute, float(value))


def valid_n(value: Any) -> bool:
    """N of SPEC.md §6.3: from 0.25 to 24 in steps of 0.25."""
    return finite_number(value) and N_LOW <= value <= N_HIGH and float(value * N_STEPS_PER_UNIT).is_integer()


async def selection(request: web.Request) -> web.Response:
    excluded = (await json_body(request, ("excluded",)))["excluded"]
    ctx = context(request)
    if not isinstance(excluded, list) or not all(isinstance(name, str) for name in excluded):
        raise ApiError(400, "INVALID_FIELD", "excluded must be a list of names")
    results = ctx.store.results()
    kept = {entry["name"] for entry in results.payload["series"] if entry["status"] == analysis.KEPT} if results else set()
    if not set(excluded) <= kept:
        raise ApiError(400, "INVALID_FIELD", "excluded may name KEPT series only")
    return start(ctx.jobs.refit, excluded)


async def add_series(request: web.Request) -> web.Response:
    fred_id = (await json_body(request, ("fred_id",)))["fred_id"]
    if not isinstance(fred_id, str) or not FRED_ID.fullmatch(fred_id.upper()):
        raise ApiError(400, "INVALID_FIELD", f"fred_id must match {FRED_ID.pattern} once uppercased")
    ctx = context(request)
    fred_id = fred_id.upper()
    # A second series of one FRED ID would give step 5 two identical members.
    if any(fred_id in (row.name, row.source_id) for row in ctx.store.all_series()):
        raise ApiError(409, "ALREADY_EXISTS", "That series is already listed")
    return start(ctx.jobs.add_series, fred_id)


async def remove_series(request: web.Request) -> web.Response:
    ctx = context(request)
    name = request.match_info["name"]
    row = ctx.store.series(name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "There is no such series")
    if row.origin != ORIGIN_USER:
        raise ApiError(400, "NOT_USER_SERIES", "Only a series you added can be removed; catalog.toml holds the others")
    return start(ctx.jobs.remove_series, name)


# The state document (SPEC.md §11)


def state_document(ctx: Context) -> dict[str, Any]:
    store = ctx.store
    results = store.results()
    payload = results.payload if results is not None else None
    entries = {entry["name"]: entry for entry in payload["series"]} if payload else {}
    counts = store.counts()
    rows = store.all_series()
    excluded = set(store.excluded())
    titles = {entry.name: entry for entry in ctx.catalog}
    start_days = store.start_days()
    spy = next(row for row in rows if row.name == SPY_NAME)
    now = ctx.clock()
    return {
        "app": {
            "server_time": utc_text(now),
            "today": iso(today_at(now)),
            "fetch_start": iso(start_days[0]) if start_days else None,
            "analysis_start": iso(start_days[1]) if start_days else None,
            "inflections_per_year": store.inflections_per_year(),
            "keys": {"fred": bool(ctx.config.fred_api_key), "tiingo": bool(ctx.config.tiingo_api_key)},
            "job": None if ctx.jobs.progress is None else dataclasses.asdict(ctx.jobs.progress),
            "last_job": None if ctx.jobs.last is None else dataclasses.asdict(ctx.jobs.last),
            "computed_at": results.computed_at if results is not None else None,
        },
        "spy": {
            "last_obs_day": iso(counts[SPY_NAME].last_obs_day) if SPY_NAME in counts else None,
            "points": payload["spy"]["points"] if payload else (counts[SPY_NAME].observations if SPY_NAME in counts else 0),
            "last_fetch_error": spy.last_fetch_error,
        },
        "series": [
            series_state(row, counts.get(row.name), entries.get(row.name), excluded, titles.get(row.name))
            for row in candidates(rows, ctx.catalog)
        ],
        "f": None if payload is None else f_state(payload["f"]),
        "events": [dataclasses.asdict(event) for event in store.events(EVENTS_SHOWN)],
    }


def series_state(
    row: SeriesRow, counts: Counts | None, entry: dict | None, excluded: set[str], listed: CatalogEntry | None
) -> dict[str, Any]:
    if entry is not None:
        status, step3 = entry["status"], entry["step3"]
        points, estimated = entry["points"], entry["estimated_points"]
    else:
        # Not computed yet: only §7.6's first two statuses can be told apart.
        failed = counts is None and row.last_fetch_error is not None
        status, step3 = analysis.FETCH_FAILED if failed else analysis.PENDING, None
        points, estimated = (counts.observations, counts.estimated) if counts else (0, 0)
    kept = status == analysis.KEPT
    report = step3 or {}
    return {
        "name": row.name,
        "origin": row.origin,
        "source_id": row.source_id,
        "title": listed.title_en if listed else row.title,
        "title_zh": listed.title_zh if listed else None,
        "units": row.units,
        "frequency": row.frequency,
        "form": row.form,
        "last_obs_day": iso(counts.last_obs_day) if counts else None,
        "last_release_day": iso(counts.last_release_day) if counts else None,
        "points": points,
        "estimated_points": estimated,
        "last_fetch_error": row.last_fetch_error,
        "status": status,
        "lead_name": analysis.lead_name(row.name) if kept else None,
        **{key: report.get(key) for key in ("a_star", "beta", "score", "score_oos", "slope_pct_per_year")},
        "window": iso_window(step3["window"]) if step3 else None,
        "selected": row.name not in excluded if kept else None,
    }


def f_state(f: dict[str, Any]) -> dict[str, Any]:
    """F's status, and its report when it is OK (SPEC.md §11)."""
    if f["status"] != analysis.OK:
        return {"status": f["status"]}
    return {
        "status": f["status"],
        "members": members(f),
        "score": f["score"],
        "score_oos": f["score_oos"],
        "slope_pct_per_year": f["slope_pct_per_year"],
        "window": iso_window(f["window"]),
        "split_day": iso(f["split_day"]),
    }


def members(f: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**member, "lead_name": analysis.lead_name(member["name"])} for member in f["members"]]


def iso(day: int) -> str:
    return day_to_date(day).isoformat()


def iso_window(window: dict[str, int]) -> dict[str, str]:
    return {"start": iso(window["start"]), "end": iso(window["end"])}


# Chart payloads (SPEC.md §11)


async def spy_chart(request: web.Request) -> web.Response:
    return json_response(chart(context(request), SPY_NAME))


async def series_chart(request: web.Request) -> web.Response:
    ctx = context(request)
    name = request.match_info["name"]
    row = ctx.store.series(name)
    if row is None or name == SPY_NAME:
        raise ApiError(404, "NOT_FOUND", "There is no such series")
    return json_response(chart(ctx, name))


def chart(ctx: Context, name: str) -> dict[str, Any]:
    """Points at release days and the one-sided and reference series, all in original units."""
    curve = ctx.store.curve(name)
    if curve is None:
        raise ApiError(404, "NO_DATA", "Nothing is computed for this series yet")
    row = ctx.store.series(name)
    points = known_points(ctx.store.observations(name))
    results = ctx.store.results()
    statuses = {entry["name"]: entry["status"] for entry in results.payload["series"]} if results else {}
    return {
        "name": name,
        "lead_name": analysis.lead_name(name) if statuses.get(name) == analysis.KEPT else None,
        "units": row.units,
        "raw": {"days": points.release_day.tolist(), "values": rounded(points.value)},
        "rt": {"first_day": curve.rt_first_day, "values": rounded(original_values(curve.rt, row.form))},
        "ref": {"first_day": curve.ref_first_day, "values": rounded(original_values(curve.ref, row.form))},
    }


async def f_chart(request: web.Request) -> web.Response:
    """F, SPYL1 and the composite over W5 (SPEC.md §7.4, §11)."""
    ctx = context(request)
    results = ctx.store.results()
    f = results.payload["f"] if results is not None else None
    if f is None or f["status"] != analysis.OK:
        raise ApiError(404, "NO_F", "There is no F")
    spy, form = ctx.store.curve(SPY_NAME), ctx.store.series(SPY_NAME).form
    first_day, last_day = f["window"]["start"], f["window"]["end"]
    spyl1 = original_values(spy.rt[first_day - spy.rt_first_day : last_day - spy.rt_first_day + 1], form)
    return json_response(
        {
            "f": {"first_day": first_day, "values": rounded(f["f"])},
            "spyl1": {"first_day": first_day, "values": rounded(spyl1)},
            "composite": {"first_day": first_day, "values": rounded(f["composite"])},
            "split_day": f["split_day"],
            "members": members(f),
        }
    )


def rounded(values: Sequence[float] | np.ndarray) -> list[float | None]:
    """Each value to 6 significant digits, and null where it is undefined (SPEC.md §11)."""
    return [float(f"{value:.6g}") if math.isfinite(value) else None for value in np.asarray(values, dtype=float).tolist()]
