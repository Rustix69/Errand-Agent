"""OKLCH -> sRGB and WCAG contrast, to check the page's colour pairings against the theme's real token values."""

from __future__ import annotations

import math
import re
from pathlib import Path

THEME = (Path(__file__).resolve().parents[2] / "demo/web/theme.css").read_text()
Color = tuple[float, float, float]


def _tokens(block: str) -> dict[str, Color]:
    return {k: tuple(float(x) for x in v.split()[:3]) for k, v in re.findall(r"(--[\w-]+):\s*oklch\(([^)]*)\)", block)}  # type: ignore[misc]


def theme_tokens() -> tuple[dict[str, Color], dict[str, Color]]:
    """(light, dark). Dark inherits any token it does not override. Proposed tokens are included."""
    light: dict[str, Color] = {}
    dark: dict[str, Color] = {}
    for sel, body in re.findall(r"(:root|\.dark)\s*\{(.*?)\n\}", THEME, re.S):
        (light if sel == ":root" else dark).update(_tokens(body))
    return light, {**light, **dark}


def _linear(c: Color) -> tuple[float, float, float]:
    L, C, h = c
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l_, m_, s_ = L + .3963377774 * a + .2158037573 * b, L - .1055613458 * a - .0638541728 * b, L - .0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * l - 3.3077115913 * m + .2309699292 * s, -1.2684380046 * l + 2.6097574011 * m - .3413193965 * s,
            -.0041960863 * l - .7034186147 * m + 1.7076147010 * s)


def luminance(c: Color) -> float:
    r, g, b = (min(1.0, max(0.0, x)) for x in _linear(c))
    return .2126 * r + .7152 * g + .0722 * b


def contrast(a: Color, b: Color) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def distance(a: Color, b: Color) -> float:
    """Euclidean distance in OKLab: about 0.02 is 'barely different', 0.2+ is 'clearly another colour'."""
    def lab(c: Color) -> Color:
        return (c[0], c[1] * math.cos(math.radians(c[2])), c[1] * math.sin(math.radians(c[2])))
    return math.dist(lab(a), lab(b))
