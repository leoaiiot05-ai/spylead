import asyncio
import contextlib
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from aiohttp import web

from spylead import __main__ as spylead_main
from spylead.__main__ import main, setup_logging, take_lock
from spylead.catalog import load_catalog
from spylead.config import Config
from spylead.jobs import Jobs
from spylead.store import Store

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "Sentinel-token-0123456789"
CONFIG = f"""\
[web]
port = 8090
token = "{TOKEN}"

[fred]
api_key = ""

[tiingo]
api_key = ""
"""
# The child's sitecustomize.py: any socket activity ends it at once with code 99.
NO_NETWORK = """\
import os, pathlib, sys

def refuse_network(event, args):
    if event.startswith("socket."):
        sys.stderr.write(f"network access: {event}\\n")
        sys.stderr.flush()
        os._exit(99)

sys.addaudithook(refuse_network)
pathlib.Path(__file__).with_name("guard-active").touch()
"""
# A child that takes the lock, says so, and waits to be killed.
HOLD_LOCK = """\
import pathlib, sys, time
from spylead.__main__ import take_lock

lock = take_lock(pathlib.Path(sys.argv[1]))
print("locked" if lock else "refused", flush=True)
time.sleep(600)
"""


@contextlib.contextmanager
def restored_logging():
    """Removes the handlers and levels that setup_logging installs, so later tests log as before."""
    root, package = logging.getLogger(), logging.getLogger("spylead")
    handlers, levels = list(root.handlers), (root.level, package.level)
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(levels[0])
        package.setLevel(levels[1])


def write_config(folder):
    shutil.copy(ROOT / "catalog.toml", folder / "catalog.toml")
    path = folder / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_a_valid_config_takes_the_lock_then_serves(monkeypatch, tmp_path, capsys):
    config = write_config(tmp_path)
    served = []

    async def serve(config, catalog, data_dir):
        # The lock is held while the app serves.
        assert take_lock(data_dir / "spylead.lock") is None
        served.append((config.port, len(catalog), data_dir))

    monkeypatch.setattr(spylead_main, "serve", serve)
    with restored_logging():
        assert main(["--config", str(config)]) == 0
    assert served == [(8090, 18, tmp_path / "data")]
    log_text = (tmp_path / "logs" / "spylead.log").read_text(encoding="utf-8")
    assert "INFO spylead: Loaded config.toml and catalog.toml: 18 candidates" in log_text
    assert "WARNING spylead: fred.api_key is empty" in log_text
    assert "WARNING spylead: tiingo.api_key is empty" in log_text
    assert "INFO spylead: spylead stopped" in log_text
    assert TOKEN not in log_text + capsys.readouterr().err
    # Released at the end.
    lock = take_lock(tmp_path / "data" / "spylead.lock")
    assert lock is not None
    lock.close()


def test_ctrl_c_stops_cleanly_and_a_failure_is_logged_redacted(monkeypatch, tmp_path):
    config = write_config(tmp_path)

    async def interrupted(config, catalog, data_dir):
        raise KeyboardInterrupt

    async def failing(config, catalog, data_dir):
        raise OSError("https://api.stlouisfed.org/fred/series?api_key=SECRETKEY0123456789")

    monkeypatch.setattr(spylead_main, "serve", interrupted)
    with restored_logging():
        assert main(["--config", str(config)]) == 0
    monkeypatch.setattr(spylead_main, "serve", failing)
    with restored_logging():
        assert main(["--config", str(config)]) == 1
    log_text = (tmp_path / "logs" / "spylead.log").read_text(encoding="utf-8")
    assert "spylead stopped on an error" in log_text and "api_key=***" in log_text
    assert "SECRETKEY0123456789" not in log_text


def test_a_second_process_exits_before_opening_the_database_or_the_port(tmp_path):
    config = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    holder = take_lock(tmp_path / "data" / "spylead.lock")
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(NO_NETWORK, encoding="utf-8")
    result = run_spylead(config, guard)
    holder.close()
    assert (guard / "guard-active").exists()
    assert result.returncode == 3, result.stderr
    assert "Another spylead process holds" in result.stderr
    assert sorted(path.name for path in (tmp_path / "data").iterdir()) == ["spylead.lock"]


def test_a_lock_left_by_a_killed_process_does_not_block_a_restart(tmp_path):
    lock_path = tmp_path / "spylead.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(lock_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline() == "locked\n"
        assert take_lock(lock_path) is None
    finally:
        holder.kill()
        holder.wait(timeout=60)
    lock = take_lock(lock_path)
    assert lock is not None
    lock.close()
    assert lock_path.exists()


def test_serve_listens_on_127_0_0_1_only_and_shuts_down_in_order(monkeypatch, tmp_path):
    order = []
    for owner, name, label in [
        (web.AppRunner, "cleanup", "stop accepting requests"),
        (Jobs, "shutdown", "interrupt the job"),
        (ProcessPoolExecutor, "shutdown", "shut the pool down"),
        (Store, "close", "close the store"),
    ]:
        monkeypatch.setattr(owner, name, recording(getattr(owner, name), label, order))
    addresses = []

    async def until(runner):
        addresses.extend(runner.addresses)
        order.append("ready")

    config = Config(port=0, token=TOKEN, fred_api_key="", tiingo_api_key="")
    catalog = load_catalog(ROOT / "catalog.toml")
    asyncio.run(spylead_main.serve(config, catalog, tmp_path, until))
    assert [host for host, _ in addresses] == ["127.0.0.1"]
    assert order == ["ready", "stop accepting requests", "interrupt the job", "shut the pool down", "close the store"]
    assert (tmp_path / "spylead.db").exists()


def test_ctrl_c_cancels_serve_which_shuts_down_in_order(monkeypatch, tmp_path):
    order = []
    for owner, name, label in [(web.AppRunner, "cleanup", "runner"), (Jobs, "shutdown", "jobs"), (Store, "close", "store")]:
        monkeypatch.setattr(owner, name, recording(getattr(owner, name), label, order))
    config = Config(port=0, token=TOKEN, fred_api_key="", tiingo_api_key="")
    catalog = load_catalog(ROOT / "catalog.toml")
    ready = []

    async def forever(runner):
        ready.append(runner)
        await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(spylead_main.serve(config, catalog, tmp_path, forever))
        while not ready:
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert order == ["runner", "jobs", "store"]


def recording(original, label, order):
    if asyncio.iscoroutinefunction(original):

        async def wrapper(*args, **kwargs):
            order.append(label)
            return await original(*args, **kwargs)

    else:

        def wrapper(*args, **kwargs):
            order.append(label)
            return original(*args, **kwargs)

    return wrapper


def run_spylead(config, guard):
    return subprocess.run(
        [sys.executable, "-m", "spylead", "--config", str(config)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(guard), "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_invalid_config_is_reported_before_logging_starts(tmp_path, capsys):
    config = write_config(tmp_path)
    config.write_text(CONFIG.replace(TOKEN, "short"), encoding="utf-8")
    assert main(["--config", str(config)]) == 2
    assert capsys.readouterr().err == f"spylead: {config}: web.token must have at least 16 characters\n"
    assert not (tmp_path / "logs").exists()


def test_invalid_catalog_is_reported_before_logging_starts(tmp_path, capsys):
    config = write_config(tmp_path)
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(catalog.read_text(encoding="utf-8").replace('"LOG"', '"LOGARITHM"', 1), encoding="utf-8")
    assert main(["--config", str(config)]) == 2
    assert capsys.readouterr().err == f"spylead: {catalog}: entry 1: form must be LOG or LEVEL\n"
    assert not (tmp_path / "logs").exists()


def test_file_takes_spylead_debug_and_rotates_console_takes_info(tmp_path, capsys):
    with restored_logging():
        setup_logging(tmp_path / "logs")
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert [(h.maxBytes, h.backupCount) for h in files] == [(5 * 1024 * 1024, 5)]
        logging.getLogger("spylead.smoothing").debug("refit detail")
        logging.getLogger("spylead.jobs").info("phase summary")
        logging.getLogger("aiohttp.client").debug("library detail")
    log_text = (tmp_path / "logs" / "spylead.log").read_text(encoding="utf-8")
    assert "refit detail" in log_text and "phase summary" in log_text
    assert "library detail" not in log_text
    console = capsys.readouterr().err
    assert "phase summary" in console and "refit detail" not in console


def test_example_config_fails_on_the_placeholder_token_without_network(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(NO_NETWORK, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "spylead", "--config", "config.example.toml"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert (tmp_path / "guard-active").exists()
    assert result.returncode == 2, result.stderr
    assert result.stderr == "spylead: config.example.toml: web.token must be changed from the placeholder\n"
