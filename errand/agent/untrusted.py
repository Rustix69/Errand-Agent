"""Free text from tools (reviews, provider blurbs, cancel reasons) is DATA, never instructions.

``wrap_untrusted`` delimits it, escapes angle brackets so it cannot close the delimiter or open
its own tags, caps its length, and scans it for injection patterns. Detections are only logged;
the structural rule (the model cannot book, approvals come from code) is the real defence.
"""

from __future__ import annotations

import re

TAG = "untrusted_data"
DEFAULT_CAP = 400

PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|rules)", re.I),
    "fake_system_notice": re.compile(r"(system notice|###\s*system|new policy from the platform)", re.I),
    "addresses_the_assistant": re.compile(r"\b(to (any )?(ai|llm)( agent| assistant)?|ai assistant)\b", re.I),
    "tool_invocation": re.compile(r"\b(call|invoke|run)\s+(book|cancel|reschedule|hold_slot)\s*\(", re.I),
    "approval_bypass": re.compile(
        r"(skip the confirmation|already approved|approval is not required|without asking|do not mention)", re.I
    ),
    "markup_breakout": re.compile(r"</?\s*(review|instructions?|system|untrusted_data)\b", re.I),
    "exfiltration": re.compile(r"reveal (their|the user'?s?) (saved )?(address|preferences)", re.I),
}


def scan(text: str) -> list[str]:
    return sorted(name for name, pat in PATTERNS.items() if pat.search(text))


def _safe_source(source: str) -> str:
    return re.sub(r"[^A-Za-z0-9_:.\-]", "_", source)[:80]


def wrap_untrusted(text: str, source: str, cap: int = DEFAULT_CAP) -> tuple[str, list[str]]:
    """Returns (wrapped text, detections). The scan runs on the FULL text so truncation cannot hide a hit."""
    detections = scan(text)
    body = text.replace("<", "&lt;").replace(">", "&gt;")
    if len(body) > cap:
        body = body[:cap] + "…[truncated]"
    return f'<{TAG} source="{_safe_source(source)}">{body}</{TAG}>', detections
