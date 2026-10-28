"""python -m spylead --config <path> (SPEC.md §14, §15)."""
import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from spylead.catalog import load_catalog
from spylead.config import ConfigError, load_config

LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    folder = args.config.parent
    try:
        config = load_config(args.config)
        catalog = load_catalog(folder / "catalog.toml")
    except ConfigError as error:
        # Logging starts only after validation, so a mistyped path never creates a logs folder.
        print(f"spylead: {error}", file=sys.stderr)
        return 2
    setup_logging(folder / "logs")
    log.info("Loaded %s and catalog.toml: %d candidates", args.config.name, len(catalog))
    for key in config.empty_keys:
        log.warning("%s is empty: fetches that need it will fail with MISSING_KEY", key)
    log.info("spylead is not implemented yet; exiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
