"""The restyle on the warm theme: fonts, where mono is allowed, text contrast in light and dark, and whether the trace
event kinds stay easy to tell apart. Static checks on demo/web/index.html and demo/web/theme.css."""

import itertools
import re
from pathlib import Path
from urllib.parse import urlparse

from tests.web.colors import THEME, contrast, distance, theme_tokens

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "demo/web/index.html").read_text()
STYLE = re.sub(r"/\*.*?\*/", "", re.search(r"<style>(.*?)</style>", PAGE, re.S)[1], flags=re.S)  # comments removed
LIGHT, DARK = theme_tokens()

FONT_LINKS = [
    '<link rel="preconnect" href="https://fonts.googleapis.com">',
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
    '<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">',
]
MONO_ALLOWED = {".pill", ".time", ".terms .raw", ".tr"}  # trace rows, pills, timestamps (the raw slot value is a timestamp)


def rules():
    """(selector, {property: value}) for every rule in the page's style block, @media rules included."""
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", STYLE):
        decls = {p.strip(): v.strip() for p, v in re.findall(r"([a-z-]+)\s*:\s*([^;]+);?", body)}
        out.append((" ".join(selector.split()), decls))
    return out


def first_var(value: str) -> str | None:
    m = re.search(r"var\((--[\w-]+)", value)
    return m[1] if m else None


# ----------------------------------------------------------------------------- fonts


def test_the_page_loads_exactly_poppins_and_ibm_plex_mono_from_google_fonts():
    head = PAGE.split("</head>")[0]
    for link in FONT_LINKS:
        assert link in head, link
    assert re.findall(r"family=([A-Za-z+]+)", head) == ["Poppins", "IBM+Plex+Mono"]  # no other family, no Libre Baskerville
    assert "--font-serif" not in PAGE and "Baskerville" not in PAGE
    hosts = {urlparse(u).netloc for u in re.findall(r"https?://[^\s\"'<>)]+", PAGE)}
    assert hosts == {"fonts.googleapis.com", "fonts.gstatic.com"}  # the only external resources the page names
    assert not re.search(r"\.font-(thin|light|normal|medium|semibold|bold)\b", PAGE)  # no per-weight utility classes


def test_mono_is_used_only_for_trace_rows_pills_and_timestamps():
    using_mono = {sel for sel, d in rules() if any("--font-mono" in v for v in d.values())}
    assert using_mono == MONO_ALLOWED, using_mono
    for needed in ("header.top h1", ".panel h2", ".card-h", "button", ".composer input", ".terms dt"):
        decl = dict(rules())[needed]
        assert "--font-sans" in decl.get("font", decl.get("font-family", "")), needed  # headings, buttons, inputs: Poppins


# ----------------------------------------------------------------------------- contrast


def pairs_in_the_css():
    out = []
    for sel, d in rules():
        bg = first_var(d.get("background", d.get("background-color", "")))
        fg = first_var(d.get("color", ""))
        if bg and fg:
            out.append((sel, bg, fg))
    return out


EXTRA_PAIRS = [  # text whose background is set by a parent rule
    ("sender name on the chat area", "--muted", "--chart-1"), ("timestamp on the chat area", "--muted", "--muted-foreground"),
    ("body text", "--background", "--foreground"), ("card text", "--card", "--card-foreground"),
    ("card labels", "--card", "--muted-foreground"), ("booking details", "--card", "--muted-foreground"),
    ("strip label", "--sidebar", "--muted-foreground"), ("strip buttons", "--card", "--card-foreground"),
    ("typed text", "--background", "--foreground"),
]


def test_every_text_and_background_pair_in_the_page_has_at_least_4_5_to_1_in_light_and_dark():
    found = pairs_in_the_css()
    assert len(found) >= 20, "the check must cover the bubbles, pills, card, buttons and headings"
    failures = []
    for theme_name, tokens in (("light", LIGHT), ("dark", DARK)):
        for sel, bg, fg in found + [(n, b, f) for n, b, f in EXTRA_PAIRS]:
            ratio = contrast(tokens[fg], tokens[bg])
            if ratio < 4.5:
                failures.append(f"{theme_name}: {sel}: {fg} on {bg} = {ratio:.2f}")
    assert failures == []
    # the ones asked about by name
    covered = {sel for sel, _, _ in found}
    assert {".msg.agent .bubble, .msg.notice .bubble", ".msg.user .bubble", ".pill.model", ".pill.tool", ".pill.bad", ".pill.ok",
            ".pill.poll", ".pill.node", ".card-h"} <= covered


# ----------------------------------------------------------------------------- trace kinds stay distinguishable


def pill_fill(kind: str) -> str:
    bg = dict(rules())[f".pill.{kind}"]["background"]
    return first_var(bg)


def test_the_six_trace_kinds_have_six_different_fills_and_the_cool_ones_are_the_two_proposed_tokens():
    kinds = ["model", "tool", "bad", "ok", "poll", "node"]
    fills = {k: pill_fill(k) for k in kinds}
    assert len(set(fills.values())) == 6, fills
    assert {fills["model"], fills["ok"]} == {"--info", "--success"}  # the only non-warm hues on the page
    for tokens in (LIGHT, DARK):
        warm = [t for t in tokens if t.startswith("--chart-") or t in ("--primary", "--destructive", "--secondary", "--accent")]
        for cool in ("--info", "--success"):
            assert min(distance(tokens[cool], tokens[w]) for w in warm) >= 0.17  # why the two tokens are proposed
        # the solid fills that carry meaning (tool, error) are not near-duplicates of each other
        assert distance(tokens[fills["tool"]], tokens[fills["bad"]]) >= 0.09
        for a, b in itertools.combinations(("model", "ok", "bad", "tool"), 2):
            assert distance(tokens[fills[a]], tokens[fills[b]]) >= 0.09, (a, b)


def test_the_proposed_tokens_are_defined_in_both_themes_and_the_page_falls_back_to_warm_ones_without_them():
    assert "--info" in LIGHT and "--success" in LIGHT and "--info" in {k for k in DARK if DARK[k] != LIGHT.get(k)}
    assert "var(--info, var(--chart-4))" in PAGE and "var(--success, var(--chart-5))" in PAGE
    assert "PROPOSED ADDITIONS" in THEME
