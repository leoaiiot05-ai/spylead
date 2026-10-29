"""Writes the PWA icons in spylead/static/icons/ (SPEC.md §12) with the standard library only: zlib and struct.

Run from the repository root: python scripts/make_icons.py. The output is deterministic, and the PNGs are committed.

The drawing: two rising waves on the theme colour, a light one ahead of a white one, as an indicator leads SPY.
"""
import math
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

ICONS = Path(__file__).resolve().parents[1] / "spylead" / "static" / "icons"
BACKGROUND = (31, 78, 121)  # the manifest's theme_color
LEAD = (126, 182, 232)
SPY = (255, 255, 255)
LEAD_SHIFT = 0.17  # how far the light wave runs ahead, in widths of the drawing
LINE_WIDTH = 0.085  # in widths of the drawing
CORNER = 0.2  # the corner radius of the icon with transparent corners, in icon widths
SEGMENTS = 256
Color = tuple[int, int, int]
Point = tuple[float, float]


def wave(s: float) -> float:
    """The height of the wave at s, both from 0 to 1 across the drawing; 0 is the top."""
    return 0.6 - 0.36 * (s - 0.5) - 0.13 * math.sin(2 * math.pi * 1.1 * (s - 0.1))


def polyline(shift: float) -> list[Point]:
    """The wave shifted left by `shift`, as SEGMENTS straight pieces across the drawing."""
    return [(i / SEGMENTS, wave(i / SEGMENTS + shift)) for i in range(SEGMENTS + 1)]


def segment_distance(s: float, t: float, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    k = min(max(((s - a[0]) * dx + (t - a[1]) * dy) / (dx * dx + dy * dy), 0.0), 1.0)
    return math.hypot(s - a[0] - k * dx, t - a[1] - k * dy)


def steepness(points: list[Point]) -> float:
    return max(abs((b[1] - a[1]) / (b[0] - a[0])) for a, b in zip(points, points[1:]))


def line_distance(s: float, t: float, points: list[Point], steep: float, reach: float) -> float:
    """The distance from (s, t) to the polyline when it is within `reach`, else infinity. The line has round caps.

    A point of the line within reach lies within reach across, so only that stretch is searched. A pixel farther from
    the line's height than its steepest slope `steep` allows has no such point.
    """
    end = min(max(s, 0.0), 1.0)
    i = min(int(end * SEGMENTS), SEGMENTS - 1)
    height = points[i][1] + (end * SEGMENTS - i) * (points[i + 1][1] - points[i][1])
    if abs(s - end) > reach or abs(t - height) > reach * (1 + steep) + steep * abs(s - end):
        return math.inf
    first = min(max(math.floor((s - reach) * SEGMENTS), 0), SEGMENTS - 1)
    last = max(min(math.ceil((s + reach) * SEGMENTS), SEGMENTS), first + 1)
    return min(segment_distance(s, t, points[k], points[k + 1]) for k in range(first, last))


def coverage(distance: float, half_width: float) -> float:
    """How much of a pixel a line covers, from the distance of its centre to the line's middle, in pixels."""
    return min(max(half_width - distance + 0.5, 0.0), 1.0)


def blend(under: Color, over: Color, alpha: float) -> Color:
    return tuple(round(a + (b - a) * alpha) for a, b in zip(under, over))


def rounded_square(x: float, y: float, size: int) -> float:
    """The coverage of the pixel centred at (x, y) by the icon's square with rounded corners."""
    radius = CORNER * size
    half = size / 2
    qx = abs(x - half) - (half - radius)
    qy = abs(y - half) - (half - radius)
    distance = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
    return min(max(0.5 - distance, 0.0), 1.0)


def pixel(x: float, y: float, size: int, margin: float) -> Color:
    """The colour at the pixel centred at (x, y): the drawing fills the square left inside `margin` (icon widths)."""
    extent = size * (1 - 2 * margin)
    s = (x - size * margin) / extent
    t = (y - size * margin) / extent
    half_width = LINE_WIDTH * extent / 2
    reach = (half_width + 1) / extent
    color = BACKGROUND
    for points, steep, line_color in ((LEAD_LINE, LEAD_STEEP, LEAD), (SPY_LINE, SPY_STEEP, SPY)):
        distance = line_distance(s, t, points, steep, reach)
        color = blend(color, line_color, coverage(distance * extent, half_width))
    return color


def image(size: int, margin: float, alpha: Callable[[float, float, int], float] | None) -> bytes:
    """The PNG scanlines: RGB, or RGBA when `alpha` gives each pixel's opacity."""
    rows = []
    for row in range(size):
        line = bytearray(b"\x00")  # filter type None
        for column in range(size):
            x, y = column + 0.5, row + 0.5
            line += bytes(pixel(x, y, size, margin))
            if alpha is not None:
                line.append(round(255 * alpha(x, y, size)))
        rows.append(bytes(line))
    return b"".join(rows)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(size: int, scanlines: bytes, with_alpha: bool) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 6 if with_alpha else 2, 0, 0, 0)
    return b"".join(
        (b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", header), chunk(b"IDAT", zlib.compress(scanlines, 9)), chunk(b"IEND", b""))
    )


def icon(size: int, margin: float, rounded: bool) -> bytes:
    alpha = rounded_square if rounded else None
    return png(size, image(size, margin, alpha), rounded)


LEAD_LINE = polyline(LEAD_SHIFT)
SPY_LINE = polyline(0.0)
LEAD_STEEP = steepness(LEAD_LINE)
SPY_STEEP = steepness(SPY_LINE)
# The maskable icon keeps its drawing inside the central circle of 80 % that every mask shows.
ICON_FILES = {
    "icon-192.png": lambda: icon(192, 0.16, rounded=True),
    "icon-512-maskable.png": lambda: icon(512, 0.24, rounded=False),
    "apple-touch-icon.png": lambda: icon(180, 0.16, rounded=False),
}


def main() -> None:
    ICONS.mkdir(exist_ok=True)
    for name, make in ICON_FILES.items():
        (ICONS / name).write_bytes(make())
        print(f"wrote {ICONS / name}")


if __name__ == "__main__":
    main()
