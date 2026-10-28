# Prompt 1: Scaffold, lock file, config and catalog

## Steps

1. `requirements.in`: exactly `numpy`, `scipy`, `aiohttp`, `tzdata`, `pytest`, one per line.
2. Venv and lock (SPEC.md §13), with Python 3.13.7, the only install on this PC:
   `python -m venv .venv`, `.venv/Scripts/python -m pip install --upgrade pip`,
   `.venv/Scripts/python -m pip lock -r requirements.in -o pylock.toml`, `.venv/Scripts/python -m pip install -r pylock.toml`.
3. `.gitignore`: `config.toml`, `data/`, `logs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`.
4. `spylead/__init__.py`: a docstring only.
5. `spylead/config.py`:
   - `ConfigError`, the one error type for `config.toml` and `catalog.toml`. Messages start with the file's path.
   - `read_toml(path)`: accepts a UTF-8 BOM (PowerShell 5.1 writes one). Errors for a missing or unreadable file, invalid UTF-8 and invalid TOML.
     A TOML error reports only `(at line L, column C)`: tomllib's own messages can quote a character of the file.
   - `check_table(table, fields, where, prefix)`: unknown keys, then missing keys, then types. A boolean is not an integer.
   - `load_config(path) -> Config` (§14): `web.port` from 1 to 65535 and not an IB API port; `web.token` not the placeholder and at least 16 characters; empty API keys accepted.
   - `Config`: frozen dataclass; the token and the keys are left out of its `repr`. `Config.empty_keys` names the empty API keys.
6. `spylead/catalog.py` (§4.3):
   - `CatalogEntry` frozen dataclass; `load_catalog(path) -> tuple[CatalogEntry, ...]` in file order.
   - `[[series]]` tables with exactly `name`, `fred_id`, `form`, `default_lag_days`, `title_en`, `title_zh`.
   - `name` matches `[A-Za-z0-9_]{1,40}` and is unique; `fred_id` matches `[A-Z0-9_]{1,40}` (§11); `form` is `LOG` or `LEVEL`; `default_lag_days` is 0 or more.
   - Entries are named by position (`entry 3`): an entry's `name` is itself a value from the file.
7. `spylead/__main__.py`, `python -m spylead --config PATH`:
   - parse the arguments; load the config, then `catalog.toml` next to it;
   - on a `ConfigError`, print it to stderr and exit 2, before `logs/` exists;
   - set up logging (§15): console at INFO, rotating `logs/spylead.log` at DEBUG, 5 MB, 5 backups;
   - log a warning per empty API key, log that the app is not implemented yet, and exit 1.
8. `catalog.toml`: the 18 entries of §4.3 in order, English titles from the table, Traditional Chinese (Taiwan usage) titles.
9. `config.example.toml`: the file of §14, plus a one-line recipe for a random token.
10. Tests:
    - `tests/conftest.py`: an `echoed` fixture listing the values of a TOML text that appear in a message (AC-20).
    - `tests/test_config.py`: a valid file, a missing file, invalid TOML and UTF-8, a BOM, unknown, missing and wrongly typed keys, the placeholder token, a short token (16 characters accepted), the port range and IB ports, empty API keys (accepted and listed), secrets out of `repr`. Every error names the key and echoes no value.
    - `tests/test_catalog.py`: the committed catalog against the §4.3 table and its titles; a duplicate name, a bad name, a bad form, a missing key, an unknown key, wrong types, a negative lag, a bad `fred_id`, a bad structure. Every error names the entry and echoes no value.
    - `tests/test_main.py`:
      - a valid config: the log file, one warning per empty key, "not implemented yet", exit 1;
      - an invalid config or catalog: exit 2 and no `logs/`;
      - log levels and rotation settings;
      - `python -m spylead --config config.example.toml` in a subprocess fails on the placeholder token. A `sitecustomize.py` audit hook kills the child on any `socket.*` event, which proves it never touches the network.
    - `tests/test_lock.py`: `requirements.in` lists exactly the five packages; every `pylock.toml` package has a sha256 hash.
11. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- `web.port` must not be 4001, 4002, 7496 or 7497 (§13 coexistence): Tailscale Serve or a browser pointed at spylead's port must never reach IB's API. The message does not list the ports, so it cannot echo the value.
- `fred_id` must match §11's FRED ID pattern: it goes into a FRED URL.
- `default_lag_days` must be 0 or more: a negative lag would date an estimated release before its observation (look-ahead).
- Logging: root logger at INFO, `spylead` logger at DEBUG. spylead's own detail reaches the file; libraries' debug records, which can carry request details, stay out.
- The empty-key warning is logged by `__main__` once logging is set up, so it reaches the log file too. `load_config` does no logging.
- Exit codes: 2 for an invalid config or catalog, 1 for "not implemented yet".
- Validation stops at the first problem.
- No `pytest.ini` and no `tests/__init__.py`: `python -m pytest` from the root puts the root on `sys.path`.

## Extension points

- A new config key: `config.SCHEMA` and `Config`. Value rules sit next to the port and token rules in `load_config`.
- A new catalog field: `catalog.FIELDS` and `CatalogEntry`. A new form: `catalog.FORMS`; the form mapping of prompt 3 should use it.
- `__main__.main`: prompt 8 replaces the "not implemented" exit with the lock, store, jobs and web wiring, in §15's order.

## Refactor seams

- `read_toml` and `check_table` live in `config.py` and serve `catalog.py`. Move them to their own module if a third TOML file appears.
- `setup_logging` is the one place that installs handlers; aiohttp's access-log rules of §13 (prompt 8) attach there.
- `Config` keeps its secrets out of `repr`; sources (prompt 6) and web (prompt 8) read them from it. The state document's `keys` flags (§11) derive from the same fields as `empty_keys`.

## Deferred items (appended to TODO.md)

- Report every `config.toml` and `catalog.toml` error at once; startup stops at the first today. Seam: `config.check_table`.
- Under `pythonw.exe` (autostart) a config or catalog error is invisible: it is printed before logging starts, and pythonw has no console. Seam: `__main__.main`.

## Result

- The venv has pip 26.2.2. `pip lock` and installing from `pylock.toml` are experimental in that version: pip warns, and both worked.
- `pylock.toml` holds 18 packages: numpy 2.5.4, scipy 1.18.1, aiohttp 3.14.5, tzdata 2026.6 and pytest 9.2.0, plus their dependencies. Each is pinned to one wheel with a sha256 hash (`tests/test_lock.py`).
  pip locks for the running interpreter and platform, CPython 3.13 on win_amd64, so the lock is valid on this PC only (CLOUD.md).
- `.venv/Scripts/python -m pip install -r pylock.toml` installed all 18 packages.
- AC-20:
  - 15 config cases and 17 catalog cases check the exact message, and check that no value from the file appears in it;
  - the subprocess test checks the CLI end to end. Its audit hook was checked by hand: a child that opens a socket exits with 99.
- `.venv/Scripts/python -m pytest -q`: 52 passed.
