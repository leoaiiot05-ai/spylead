import contextlib
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from pathlib import Path

from spylead.__main__ import main, setup_logging

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


def test_valid_config_logs_warnings_and_exits_not_implemented(tmp_path, capsys):
    config = write_config(tmp_path)
    with restored_logging():
        assert main(["--config", str(config)]) == 1
    log_text = (tmp_path / "logs" / "spylead.log").read_text(encoding="utf-8")
    assert "INFO spylead: Loaded config.toml and catalog.toml: 18 candidates" in log_text
    assert "WARNING spylead: fred.api_key is empty" in log_text
    assert "WARNING spylead: tiingo.api_key is empty" in log_text
    assert "INFO spylead: spylead is not implemented yet" in log_text
    console = capsys.readouterr().err
    assert "spylead is not implemented yet" in console
    assert TOKEN not in log_text + console


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
