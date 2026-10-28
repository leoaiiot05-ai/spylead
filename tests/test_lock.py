import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVED = ["numpy", "scipy", "aiohttp", "tzdata", "pytest"]


def test_requirements_list_exactly_the_approved_packages():
    assert (ROOT / "requirements.in").read_text(encoding="utf-8").split() == APPROVED


def test_every_locked_package_is_pinned_with_a_sha256_hash():
    packages = tomllib.loads((ROOT / "pylock.toml").read_text(encoding="utf-8"))["packages"]
    assert set(APPROVED) <= {package["name"] for package in packages}
    for package in packages:
        files = package.get("wheels", []) + [package[kind] for kind in ("sdist", "archive") if kind in package]
        assert package.get("version") and files, package["name"]
        for file in files:
            assert re.fullmatch(r"[0-9a-f]{64}", file.get("hashes", {}).get("sha256", "")), package["name"]
