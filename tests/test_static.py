"""The static UI (SPEC.md §12, AC-19): the locales, markup without a way in for HTML, the PWA files, and ECharts."""
import datetime
import hashlib
import importlib.util
import json
import re
import struct
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest

from spylead import analysis, jobs, sources, store, web
from spylead.config import Config
from spylead.store import Store

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "spylead" / "static"
OWN_FILES = sorted(path for path in STATIC.rglob("*") if path.is_file() and "vendor" not in path.relative_to(STATIC).parts)
TEXT_FILES = [path for path in OWN_FILES if path.suffix != ".png"]
APP = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
ECHARTS = STATIC / "vendor" / "echarts"
ECHARTS_SHA256 = "b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The backend's codes, which the page localizes as family.CODE (SPEC.md §12).
FAMILIES = {
    "status": analysis.STATUSES,
    "reason": analysis.STATUSES,
    "f_status": analysis.F_STATUSES,
    "outcome": jobs.OUTCOMES,
    "kind": jobs.KINDS,
    "phase": jobs.PHASES,
    "event": store.EVENT_KINDS,
    "level": store.LEVELS,
    "error": web.ERROR_CODES,
    "source_error": sources.ERROR_CODES,
}
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Ways for text to become markup or code, which the CSP of §13 also blocks.
UNSAFE = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write",
    "eval": r"\beval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "inline script": r"<script(?![^>]*\ssrc=)[^>]*>",
    "style element": r"<style\b",
    "style attribute": r"\sstyle\s*=",
    "CSSOM style": r"\.style\b|\bcssText\b|setAttribute\(\s*[\"']style",
    "inline event handler": r"\son[a-z]+\s*=|\.on[a-z]+\s*=(?!=)|setAttribute\(\s*[\"']on",
    "javascript: URL": r"javascript:",
}


class Tags(HTMLParser):
    """Every start tag of a page, with its attributes, template contents included."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def find(self, tag: str, **attributes: str) -> list[dict[str, str | None]]:
        return [found for name, found in self.tags if name == tag and attributes.items() <= found.items()]


def locale(name: str) -> dict[str, str]:
    return json.loads((STATIC / "locales" / f"{name}.json").read_text(encoding="utf-8"))


def static_path(url: str) -> Path:
    return STATIC / url.removeprefix("/")


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR", path.name
    return struct.unpack(">II", data[16:24])


def pixels(png: bytes) -> tuple[bytes, bytes]:
    """A PNG's header and its scanlines. zlib builds may compress differently; the pixels they hold cannot differ."""
    header, data = b"", b""
    position = len(PNG_SIGNATURE)
    while position < len(png):
        (length,) = struct.unpack(">I", png[position : position + 4])
        kind, body = png[position + 4 : position + 8], png[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            data += body
        position += 12 + length  # length, kind, body and CRC
    return header, zlib.decompress(data)


def named_keys() -> set[str]:
    """The locale keys the page names: in data-i18n attributes, and as string literals in app.js."""
    attributes = set(re.findall(r'data-i18n(?:-aria|-placeholder)?="([^"]+)"', INDEX))
    return attributes | set(re.findall(r'"([a-z][A-Za-z_]*\.[A-Za-z0-9]+)"', APP))


# Locales


def test_both_locales_are_flat_and_define_the_same_keys_with_the_same_placeholders():
    english, chinese = locale("en"), locale("zh-TW")
    assert sorted(english) == sorted(chinese)
    for key, text in english.items():
        assert isinstance(text, str) and text and isinstance(chinese[key], str) and chinese[key], key
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(chinese[key])), key


@pytest.mark.parametrize("name", ["en", "zh-TW"])
@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_covers_exactly_the_codes_the_backend_emits(name, family):
    codes = {key.removeprefix(f"{family}.") for key in locale(name) if key.startswith(f"{family}.")}
    assert codes == set(FAMILIES[family])


def test_every_key_the_page_names_is_defined():
    assert sorted(named_keys() - set(locale("en"))) == []


def test_every_key_outside_the_families_is_named_by_the_page():
    assert sorted(key for key in locale("en") if key.split(".")[0] not in FAMILIES and key not in named_keys()) == []


def test_the_page_localizes_the_families_by_code():
    assert set(re.findall(r'\blabel\("(\w+)"', APP)) == set(FAMILIES)


# Markup


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda path: path.relative_to(STATIC).as_posix())
def test_no_static_file_turns_text_into_markup_or_code(path):
    text = path.read_text(encoding="utf-8")
    assert [name for name, pattern in UNSAFE.items() if re.search(pattern, text)] == []


def test_the_page_has_no_inline_script_style_or_handler():
    tags = Tags(INDEX).tags
    assert all("src" in attributes for name, attributes in tags if name == "script")
    assert [name for name, _ in tags if name == "style"] == []
    assert [(name, key) for name, attributes in tags for key in attributes if key == "style" or key.startswith("on")] == []


def test_a_native_login_submit_never_puts_the_token_in_a_url():
    # The access log records request lines (SPEC.md §13).
    (form,) = Tags(INDEX).find("form", id="login-form")
    assert (form["method"], form["action"]) == ("post", "/api/login")


def test_app_js_sets_no_attribute_that_could_carry_a_url_or_code():
    assert set(re.findall(r"setAttribute\(\s*\"([^\"]+)\"", APP)) == {"aria-label"}


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda path: path.relative_to(STATIC).as_posix())
def test_no_static_file_names_another_host(path):
    assert re.findall(r"https?://|[\"'(=]\s*//\w", path.read_text(encoding="utf-8")) == []


def test_every_file_the_page_loads_exists():
    tags = Tags(INDEX)
    urls = [attributes["src"] for attributes in tags.find("script")] + [attributes["href"] for attributes in tags.find("link")]
    assert urls and all(url.startswith("/") and static_path(url).is_file() for url in urls)


def test_the_page_calls_only_routes_of_the_api(tmp_path):
    clock = lambda: datetime.datetime(2026, 10, 28, 15, 0, tzinfo=datetime.UTC)  # noqa: E731
    opened = Store.open(tmp_path / "spylead.db", clock)
    config = Config(port=8090, token="Sentinel-token-0123456789", fred_api_key="", tiingo_api_key="")
    routes = {resource.canonical for resource in web.create_app(config, opened, None, [], clock).router.resources()}
    opened.close()
    called = set(re.findall(r"[\"`](/api/[^\"`$]*)", APP))
    # A path that ends in / takes the series name as its last part.
    assert called and all(path in routes or f"{path}{{name}}" in routes for path in called)


# The PWA


def test_the_manifest_has_the_fields_and_icons_of_spec_12():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == manifest["short_name"] == "spylead"
    assert (manifest["start_url"], manifest["display"]) == ("/", "standalone")
    assert manifest["theme_color"] and manifest["background_color"]
    icons = {icon["sizes"]: icon for icon in manifest["icons"]}
    assert sorted(icons) == ["192x192", "512x512"]
    # Edge installs a site only with an icon of purpose "any" of at least 144 px.
    assert (icons["192x192"].get("purpose", "any"), icons["512x512"].get("purpose")) == ("any", "maskable")
    for size, icon in icons.items():
        assert icon["type"] == "image/png"
        assert "x".join(map(str, png_size(static_path(icon["src"])))) == size


def test_the_page_links_the_manifest_the_icons_and_the_home_screen_tags():
    tags = Tags(INDEX)
    assert [link["href"] for link in tags.find("link", rel="manifest")] == ["/manifest.webmanifest"]
    (touch,) = tags.find("link", rel="apple-touch-icon")
    assert touch["sizes"] == "180x180" and png_size(static_path(touch["href"])) == (180, 180)
    (icon,) = tags.find("link", rel="icon")
    assert png_size(static_path(icon["href"])) == (192, 192)
    for name in ("apple-mobile-web-app-capable", "mobile-web-app-capable"):
        assert tags.find("meta", name=name, content="yes"), name
    assert "viewport-fit=cover" in tags.find("meta", name="viewport")[0]["content"]


def test_the_committed_icons_are_what_the_script_makes():
    spec = importlib.util.spec_from_file_location("make_icons", ROOT / "scripts" / "make_icons.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.ICONS == STATIC / "icons"
    assert sorted(script.ICON_FILES) == sorted(path.name for path in (STATIC / "icons").iterdir())
    for name, make in script.ICON_FILES.items():
        assert pixels((STATIC / "icons" / name).read_bytes()) == pixels(make()), name


# ECharts


def test_the_vendored_echarts_matches_its_pinned_sha256():
    assert hashlib.sha256((ECHARTS / "echarts.min.js").read_bytes()).hexdigest() == ECHARTS_SHA256
    assert (ECHARTS / "LICENSE").is_file() and (ECHARTS / "NOTICE").is_file()
