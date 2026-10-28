"""Load and validate config.toml (SPEC.md §14).

Error messages name the file and the key, never a value: the file holds the web token and the API keys.
"""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLACEHOLDER_TOKEN = "change-me"
MIN_TOKEN_LENGTH = 16
# IB Gateway and TWS API ports (SPEC.md §13): a browser or Tailscale Serve aimed at spylead must never reach them.
IB_PORTS = frozenset({4001, 4002, 7496, 7497})
SCHEMA: dict[str, dict[str, type]] = {
    "web": {"port": int, "token": str},
    "fred": {"api_key": str},
    "tiingo": {"api_key": str},
}
KIND_NAMES = {int: "an integer", str: "a string", dict: "a table", list: "an array"}
TOML_POSITION = re.compile(r"\(at (?:line \d+, column \d+|end of document)\)")


class ConfigError(Exception):
    """A problem in config.toml or catalog.toml, worded without any value from the file."""


@dataclass(frozen=True)
class Config:
    port: int
    # Secrets stay out of repr, so a logged or printed Config cannot leak them.
    token: str = field(repr=False)
    fred_api_key: str = field(repr=False)
    tiingo_api_key: str = field(repr=False)

    @property
    def empty_keys(self) -> tuple[str, ...]:
        """The API keys left empty, by their names in config.toml."""
        keys = {"fred.api_key": self.fred_api_key, "tiingo.api_key": self.tiingo_api_key}
        return tuple(name for name, value in keys.items() if not value)


def load_config(path: Path) -> Config:
    data = read_toml(path)
    where = f"{path}: "
    check_table(data, dict.fromkeys(SCHEMA, dict), where)
    for section, fields in SCHEMA.items():
        check_table(data[section], fields, where, f"{section}.")
    port, token = data["web"]["port"], data["web"]["token"]
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}web.port must be between 1 and 65535")
    if port in IB_PORTS:
        raise ConfigError(f"{where}web.port must not be an IB Gateway or TWS API port")
    if token == PLACEHOLDER_TOKEN:
        raise ConfigError(f"{where}web.token must be changed from the placeholder")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"{where}web.token must have at least {MIN_TOKEN_LENGTH} characters")
    return Config(
        port=port, token=token, fred_api_key=data["fred"]["api_key"], tiingo_api_key=data["tiingo"]["api_key"]
    )


def read_toml(path: Path) -> dict[str, Any]:
    try:
        # PowerShell 5.1 and older editors start UTF-8 files with a byte order mark.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read ({error.strerror})") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not valid UTF-8") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        # tomllib's messages can quote a character of the file, so only the position is kept.
        position = TOML_POSITION.search(str(error))
        suffix = f" {position.group()}" if position else ""
        raise ConfigError(f"{path}: not valid TOML{suffix}") from None


def check_table(table: dict[str, Any], fields: dict[str, type], where: str, prefix: str = "") -> None:
    """Require exactly `fields` in `table`, each of its type. `where` starts each message; `prefix` each key."""
    for key in table:
        if key not in fields:
            raise ConfigError(f"{where}unknown key {prefix}{key}")
    for key, kind in fields.items():
        if key not in table:
            raise ConfigError(f"{where}missing key {prefix}{key}")
        # TOML booleans arrive as bool, which Python counts as an int.
        if type(table[key]) is bool or not isinstance(table[key], kind):
            raise ConfigError(f"{where}{prefix}{key} must be {KIND_NAMES[kind]}")
