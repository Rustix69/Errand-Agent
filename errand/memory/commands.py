"""Deterministic memory handling. The model is never in the write path.

* ``parse``: explicit commands ("what do you remember", "forget my area", "correct my area to X", ...).
  A forget / correct / confirm target must be a memory id (m_000012), a known alias ("area"), or the key
  of one of the user's own memories, so "forget it, book the first one" is NOT a command.
* ``capture``: plain statements ("I'm in Jadavpur", "not before 10") saved as EXPLICIT memories.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

ID = r"m_\d+"

KEY_ALIASES = {
    "area": "home_area", "home area": "home_area", "location": "home_area", "my area": "home_area",
    "earliest time": "earliest_time", "earliest": "earliest_time", "start time": "earliest_time",
    "technician": "preferred_technician", "preferred technician": "preferred_technician",
    "technician preference": "preferred_technician",
}


PROFILE_TAGS = {"home_area": ("profile", "area"), "earliest_time": ("profile", "time"),
                "preferred_technician": ("profile", "technician")}


@dataclass(frozen=True)
class MemoryCommand:
    kind: str  # inspect | remember | correct | forget | confirm | reject
    target: str | None = None  # memory id or key
    value: str | None = None


@dataclass(frozen=True)
class Capture:
    key: str
    value: str
    tags: tuple[str, ...]
    label: str  # how the confirmation names it ("your area")
    alias: str  # what the user can say to remove it ("forget my <alias>")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _target(raw: str, known_keys: Iterable[str]) -> str | None:
    t = raw.strip().lower().strip(".!?\"' ")
    t = re.sub(r"^(my|the)\s+", "", t)
    if re.fullmatch(ID, t):
        return t
    if t in KEY_ALIASES:
        return KEY_ALIASES[t]
    k = slug(t)
    return k if k in set(known_keys) else None


_INSPECT = re.compile(
    r"^(what (do|did) you (remember|know)( about me)?|show( me)?( my)?( saved)? (memories|memory|facts|preferences)|"
    r"list( my)?( saved)? (memories|facts|preferences))\s*[?.!]*$", re.I)
_REMEMBER = re.compile(r"^remember( that)?( my)? (?P<key>[a-z][a-z' ]{1,30}?)\s*(?: is | are |:)\s*(?P<value>.+?)\s*[.!]?$", re.I)
_CORRECT = re.compile(r"^(correct|change|update) (?P<target>.+?) to (?P<value>.+?)\s*[.!]?$", re.I)
_FORGET = re.compile(r"^forget (?P<target>[^,.;!?]+?)\s*[.!]?$", re.I)
_CONFIRM = re.compile(rf"^(?P<kind>confirm|reject) (?P<target>{ID})\s*[.!]?$", re.I)


def parse(text: str, known_keys: Iterable[str] = ()) -> MemoryCommand | None:
    t = " ".join(text.strip().split())
    if _INSPECT.match(t):
        return MemoryCommand("inspect")
    if m := _CONFIRM.match(t):
        return MemoryCommand(m["kind"].lower(), m["target"].lower())
    if m := _REMEMBER.match(t):
        key = KEY_ALIASES.get(m["key"].lower(), slug(m["key"]))
        return MemoryCommand("remember", key, m["value"].strip())
    keys = list(known_keys)
    if m := _CORRECT.match(t):
        tgt = _target(m["target"], keys)
        return MemoryCommand("correct", tgt, m["value"].strip()) if tgt else None
    if m := _FORGET.match(t):
        tgt = _target(m["target"], keys)
        return MemoryCommand("forget", tgt) if tgt else None
    return None


# ------------------------------------------------------------------------------------------ statements

_BLOCK = {"Need", "Trouble", "Love", "Hurry", "Serious", "Doubt", "Charge", "Fact", "Short", "Deep", "India"}
_PLACE = r"[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2}"
_AREA = re.compile(rf"(?i:\bI['’]?m in|\bI am in|\bI live in|\bmy area is|\bmy home area is)\s+(?P<v>{_PLACE})")
_TIME = re.compile(
    r"(?i:\bnot before|\bno earlier than|\bnothing before)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?"
    r"(?![\d/:.\-]*\d)(?!\s*(?i:days?|weeks?|months?|hours?|hrs?|mins?|minutes?)\b)")
_TECH = re.compile(
    r"(?i:\bmy preferred technician is|\bprefer(?:red)? technician|\balways send|\bI prefer technician)\s+"
    r"(?P<v>[A-Z][a-z]+(?: [A-Z][a-z]*\.?)?)")


def _hhmm(h: int, m: int, ap: str | None) -> str | None:
    if ap:
        pm = ap.lower().startswith("p")
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if pm else 0)
    elif 1 <= h <= 7:  # "not before 2" means the afternoon
        h += 12
    return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else None


def capture(text: str) -> list[Capture]:
    out: list[Capture] = []
    if (m := _AREA.search(text)) and m["v"].split()[0] not in _BLOCK:
        out.append(Capture("home_area", m["v"], ("profile", "area"), "your area", "area"))
    if m := _TIME.search(text):
        hhmm = _hhmm(int(m["h"]), int(m["m"] or 0), m["ap"])
        if hhmm:
            out.append(Capture("earliest_time", hhmm, ("profile", "time"), "your earliest time", "earliest time"))
    if m := _TECH.search(text):
        out.append(Capture("preferred_technician", m["v"].strip(), ("profile", "technician"), "your preferred technician", "technician"))
    return out
