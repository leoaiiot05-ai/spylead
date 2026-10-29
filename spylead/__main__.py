"""python -m spylead --config <path> (SPEC.md §14, §15)."""
import argparse
import asyncio
import contextlib
import datetime
import functools
import logging
import logging.handlers
import os
import sys
import traceback
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import IO

from aiohttp import web

from spylead.catalog import CatalogEntry, load_catalog
from spylead.config import Config, ConfigError, load_config
from spylead.jobs import Jobs, open_sources
from spylead.sources import redact
from spylead.store import Store
from spylead.web import create_app

LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
HOST = "127.0.0.1"  # never another interface: Tailscale Serve publishes it inside the tailnet (SPEC.md §13)
EXIT_CONFIG = 2
EXIT_LOCKED = 3

# Named explicitly: under `python -m`, __name__ is "__main__", outside the spylead logger tree.
log = logging.getLogger("spylead")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m spylead", description="How many days US leading indicators lead SPY.")
    parser.add_argument(
        "--config", type=Path, required=True, metavar="PATH", help="config.toml; catalog.toml, data/ and logs/ sit next to it"
    )
    return parser.parse_args(argv)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(exist_ok=True)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logfile = logging.handlers.RotatingFileHandler(
        log_dir / "spylead.log", maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    logfile.setLevel(logging.DEBUG)
    root = logging.getLogger()
    for handler in (console, logfile):
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Only spylead's own records go down to DEBUG: libraries' debug output can carry request details.
    log.setLevel(logging.DEBUG)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def take_lock(path: Path) -> IO[bytes] | None:
    """The operating-system lock on data/spylead.lock (SPEC.md §15): the open file, which holds it while it stays
    open, or None when another process holds it. The lock dies with its process; the file itself means nothing."""
    lock = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


async def wait_forever(runner: web.AppRunner) -> None:
    await asyncio.Event().wait()


async def serve(
    config: Config,
    catalog: Sequence[CatalogEntry],
    data_dir: Path,
    until: Callable[[web.AppRunner], Awaitable[None]] = wait_forever,
) -> None:
    """The store, the pool, the jobs and the web server, until `until` returns or Ctrl+C cancels it.

    The exit stack shuts down in the order of SPEC.md §15: stop accepting requests, interrupt the running job,
    shut the pool down, close the store.
    """
    async with contextlib.AsyncExitStack() as stack:
        store = Store.open(data_dir / "spylead.db", now)
        stack.callback(store.close)
        store.sync_catalog(catalog)
        pool = ProcessPoolExecutor(max(1, (os.cpu_count() or 1) - 1))
        stack.callback(pool.shutdown, wait=True, cancel_futures=True)
        jobs = Jobs(store, catalog, pool, now, functools.partial(open_sources, config, asyncio.sleep))
        stack.push_async_callback(jobs.shutdown)
        runner = web.AppRunner(create_app(config, store, jobs, catalog, now))
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.TCPSite(runner, HOST, config.port).start()
        log.info("spylead is ready on http://%s:%d", HOST, config.port)
        await until(runner)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    folder = args.config.parent
    try:
        config = load_config(args.config)
        catalog = load_catalog(folder / "catalog.toml")
    except ConfigError as error:
        # Logging starts only after validation, so a mistyped path never creates a logs folder.
        print(f"spylead: {error}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(folder / "logs")
    log.info("Loaded %s and catalog.toml: %d candidates", args.config.name, len(catalog))
    for key in config.empty_keys:
        log.warning("%s is empty: fetches that need it will fail with MISSING_KEY", key)
    data_dir = folder / "data"
    data_dir.mkdir(exist_ok=True)
    # The lock comes before the database and the port (SPEC.md §15).
    lock = take_lock(data_dir / "spylead.lock")
    if lock is None:
        log.error("Another spylead process holds %s; this one exits", data_dir / "spylead.lock")
        return EXIT_LOCKED
    try:
        asyncio.run(serve(config, catalog, data_dir))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log.error("spylead stopped on an error:\n%s", redact("".join(traceback.format_exception(error))))
        return 1
    finally:
        lock.close()
    log.info("spylead stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
