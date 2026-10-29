"""The Tiingo and FRED clients (SPEC.md §4.1, §4.2), under the request rules of §8.4, and their translation helpers.

Error texts never hold a URL or a key, and a FRED URL reaches a log only through `redact`.
"""
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from spylead.pit import Observations, PayloadError, day_field, day_to_date, field, fred_observations, tiingo_observations

log = logging.getLogger(__name__)

TIINGO_META = "https://api.tiingo.com/tiingo/daily/SPY"
TIINGO_PRICES = "https://api.tiingo.com/tiingo/daily/SPY/prices"
FRED_SERIES = "https://api.stlouisfed.org/fred/series"
FRED_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT_S = 30
FRED_INTERVAL_S = 0.5
RETRY_DELAYS_S = (2, 4, 8)
MAX_RETRY_AFTER_S = 60
# The value runs to the next parameter, the fragment, or the end of a URL quoted inside a message.
API_KEY = re.compile(r"(api_key=)[^&#\s'\"]*", re.IGNORECASE)

Sleep = Callable[[float], Awaitable[None]]


class SourceError(Exception):
    """A failed request (SPEC.md §8.3). The text is English and holds no URL and no key.

    code: TIMEOUT, CONNECTION_FAILED, HTTP_<status>, BAD_PAYLOAD or MISSING_KEY.
    """

    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


@dataclass(frozen=True)
class FredSeries:
    """FRED's metadata of a series, from seriess[0] (SPEC.md §4.2)."""

    title: str
    units: str
    frequency: str  # frequency_short
    last_updated: str  # as given
    observation_start: int
    observation_end: int


def redact(text: str) -> str:
    """`text` with the value of every api_key parameter replaced by ***: for any FRED URL that reaches a log (§13)."""
    return API_KEY.sub(r"\1***", text)


class Source:
    """One source's requests through the job's ClientSession, under the rules of SPEC.md §8.4."""

    name = ""
    interval = 0.0  # the least time between two requests, in seconds

    def __init__(
        self, session: aiohttp.ClientSession, key: str, sleep: Sleep, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self.session = session
        self.key = key
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_request = -math.inf

    def headers(self) -> dict[str, str]:
        return {}

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        """The decoded JSON body of a GET request, after the retries of SPEC.md §8.4."""
        if not self.key:
            raise SourceError("MISSING_KEY", f"the {self.name} API key is empty in config.toml")
        retries = 0
        rate_limited = False
        while True:
            await self.pace()
            log.debug("%s request: GET %s", self.name, redact(f"{url}?{urlencode(params)}" if params else url))
            try:
                async with self.session.get(
                    url, params=params, headers=self.headers(), timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)
                ) as response:
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
                    body = await response.text() if 200 <= status < 300 else None
            except TimeoutError:
                error = SourceError("TIMEOUT", f"{self.name} did not answer within {TIMEOUT_S} s")
            except aiohttp.ClientError:
                error = SourceError("CONNECTION_FAILED", f"{self.name} could not be reached")
            else:
                log.debug("%s answered HTTP %d", self.name, status)
                if body is not None:
                    return decode(self.name, body)
                error = SourceError(f"HTTP_{status}", f"{self.name} answered HTTP {status}")
                if status == 429 and not rate_limited:
                    rate_limited = True
                    delay = retry_delay(retry_after)
                    log.warning("%s: %s; retrying in %g s", self.name, error.text, delay)
                    await self.sleep(delay)
                    continue
                if status < 500:
                    raise error
            if retries == len(RETRY_DELAYS_S):
                raise error
            delay = RETRY_DELAYS_S[retries]
            retries += 1
            log.warning("%s: %s; retrying in %g s", self.name, error.text, delay)
            await self.sleep(delay)

    async def pace(self) -> None:
        wait = self.last_request + self.interval - self.monotonic()
        if wait > 0:
            await self.sleep(wait)
        self.last_request = self.monotonic()


class Tiingo(Source):
    """SPY's closes from Tiingo (SPEC.md §4.1). The key travels in a header, never in a URL."""

    name = "Tiingo"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.key}"}

    async def end_day(self) -> int:
        """The latest trading day Tiingo has (endDate): SPY's up-to-date test (SPEC.md §8.2)."""
        return translated(self.name, tiingo_end_day, await self.get_json(TIINGO_META))

    async def prices(self, first_day: int, last_day: int) -> Observations:
        params = {
            "startDate": day_to_date(first_day).isoformat(),
            "endDate": day_to_date(last_day).isoformat(),
            "format": "json",
        }
        return translated(self.name, tiingo_observations, await self.get_json(TIINGO_PRICES, params))


class Fred(Source):
    """FRED's metadata and first releases (SPEC.md §4.2). FRED requires the key in the query string."""

    name = "FRED"
    interval = FRED_INTERVAL_S

    async def series(self, series_id: str) -> FredSeries:
        params = {"series_id": series_id, "api_key": self.key, "file_type": "json"}
        return translated(self.name, fred_series, await self.get_json(FRED_SERIES, params))

    async def observations(self, series_id: str, first_day: int) -> Observations:
        """Each observation's initial release, from first_day on."""
        params = {
            "series_id": series_id,
            "api_key": self.key,
            "file_type": "json",
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
            "output_type": "4",
            "observation_start": day_to_date(first_day).isoformat(),
            "limit": "100000",
        }
        return translated(self.name, fred_observations, await self.get_json(FRED_OBSERVATIONS, params))


def retry_delay(value: str | None) -> float:
    """The wait that Retry-After asks for, at most 60 s. Without a number of seconds, the most."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return MAX_RETRY_AFTER_S
    return min(max(seconds, 0), MAX_RETRY_AFTER_S)


def decode(name: str, body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError("BAD_PAYLOAD", f"{name} sent a body that is not JSON") from None


def translated(name: str, translate: Callable[[Any], Any], body: Any) -> Any:
    try:
        return translate(body)
    except PayloadError as error:
        raise SourceError("BAD_PAYLOAD", f"{name} sent an unexpected body: {error}") from None


def tiingo_end_day(body: Any) -> int:
    """endDate of Tiingo's SPY metadata, as a day (SPEC.md §4.1). Raises PayloadError."""
    if not isinstance(body, dict):
        raise PayloadError("metadata must be an object")
    return day_field(body, "endDate", "")


def fred_series(body: Any) -> FredSeries:
    """The fields of seriess[0] that SPEC.md §4.2 uses. Raises PayloadError."""
    rows = body.get("seriess") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise PayloadError("seriess must be a list with a series")
    row, where = rows[0], "series 1: "
    return FredSeries(
        title=field(row, "title", (str,), where),
        units=field(row, "units", (str,), where),
        frequency=field(row, "frequency_short", (str,), where),
        last_updated=field(row, "last_updated", (str,), where),
        observation_start=day_field(row, "observation_start", where),
        observation_end=day_field(row, "observation_end", where),
    )
