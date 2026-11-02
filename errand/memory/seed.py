"""Deterministic demo seeds: 56 memories (with traps) and an 84-turn prior conversation for ``u_demo``.

Every trap value carries a unique marker (``ZQ<letter><n>``) so a test can grep any text sent to the model:
  ZQU unconfirmed inferred   ZQF forgotten   ZQS replaced by a correction   ZQI usable but irrelevant to errands
Idempotent: seeding a user that already has memories changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from errand.clock import IST
from errand.core.store import iso
from errand.memory.store import SqliteMemoryStore, TurnLog

PRIOR_SESSION = "s_prev"
_MARK = re.compile(r"ZQ[UFSI]\d+")

# (key, value, tags) — current explicit memories. The first three are the stable "profile" facts.
CURRENT = [
    ("home_area", "HSR Layout", ("profile", "area")),
    ("earliest_time", "10:00", ("profile", "time")),
    ("preferred_technician", "Karthik R.", ("profile", "technician")),
    ("plumber_budget", "Up to ₹1,500 for plumbing jobs", ("plumber", "plumbing", "budget")),
    ("plumber_note", "Recurring kitchen tap issue; likes a plumber who brings spare washers", ("plumber", "plumbing")),
    ("electrician_preferred", "Same electrician as last time: el_02", ("electrician", "electrical", "wiring")),
    ("electrician_timing", "Electrician visits are fine only after 10:00", ("electrician", "electrical")),
    ("ac_servicing_provider", "AC servicing is with ac_01, twice a year", ("ac", "ac_servicing", "servicing")),
    ("ac_servicing_day", "Saturdays suit AC servicing", ("ac", "ac_servicing", "servicing", "saturday")),
    ("cleaning_scope", "2BHK flat, monthly deep cleaning", ("cleaning", "cleaner")),
    ("carpenter_note", "Kitchen cabinet hinges need fixing", ("carpenter", "carpentry")),
    ("contact_pref", "Message on WhatsApp; no calls before 9:00", ("contact",)),
    ("payment_pref", "Pays by UPI after the job", ("payment",)),
    ("language", "Prefers English or Kannada", ("language",)),
]
# Inferred and CONFIRMED by the user: usable.
CONFIRMED_INFERRED = [
    ("slot_habit", "Usually picks morning slots", ("time", "morning")),
    ("rating_habit", "Prefers providers rated 4.5 or higher", ("rating", "provider")),
    ("price_habit", "Dislikes price changes at quote time", ("price", "quote")),
    ("rebook_habit", "Rebooks the same provider when satisfied", ("provider", "rebook")),
]
# Inferred and NOT confirmed: must never reach the model (some carry the "profile" tag to tempt a leak).
UNCONFIRMED = [
    ("slot_habit_guess", "Prefers evening slots after 6 pm (ZQU1)", ("profile", "time")),
    ("plumber_guess", "Prefers plumber pl_03 (ZQU2)", ("plumber", "plumbing")),
    ("area_guess", "Probably lives near Whitefield (ZQU3)", ("profile", "area")),
    ("plumber_budget_guess", "Budget under ₹800 for plumbing (ZQU4)", ("plumber", "plumbing", "budget")),
    ("weekend_guess", "Avoids weekends (ZQU5)", ("time", "weekend")),
    ("price_guess", "Always picks the cheapest provider (ZQU6)", ("price", "provider")),
]
# Said by the user, later forgotten: must never reach the model.
FORGOTTEN = [
    ("gate_code", "4821 (ZQF1)", ("profile", "access")),
    ("old_phone", "98450 11223 (ZQF2)", ("profile", "contact")),
    ("home_area", "Whitefield (ZQF3)", ("profile", "area")),
    ("plumber_dislike", "Avoid plumber pl_02 (ZQF4)", ("plumber", "plumbing")),
    ("electrician_dislike", "Avoid Volt Masters (ZQF5)", ("electrician", "electrical")),
    ("office_ac", "Window AC at the office (ZQF6)", ("ac", "servicing")),
    ("work_email", "me@work.example (ZQF7)", ("profile", "contact")),
    ("allergy", "Allergic to bleach (ZQF8)", ("cleaning", "cleaner")),
]
# Usable but about nothing an errand query is about.
IRRELEVANT = [
    ("hobby_cricket", "Watches cricket on weekends"), ("pet_dog", "Dog named Bruno, a beagle"),
    ("music", "Listens to old Hindi songs"), ("food_dosa", "Loves masala dosa"),
    ("book_genre", "Reads detective novels"), ("coffee", "Filter coffee, no sugar"),
    ("team", "Supports Royal Challengers"), ("commute", "Cycles to work on Fridays"),
    ("garden", "Grows tulsi and curry leaves"), ("festival", "Celebrates Onam at home"),
    ("sleep", "Sleeps by eleven"), ("tv", "Follows a cooking show"),
    ("sibling", "Sister lives in Pune"), ("degree", "Studied mechanical engineering"),
    ("gym", "Yoga twice a week"), ("birthday", "Birthday in March"),
    ("chess", "Plays chess online"), ("photo", "Takes sunrise photographs"),
    ("tea", "Green tea after lunch"), ("movie", "Likes old black and white films"),
    ("walk", "Evening walk in the park"), ("trip", "Plans a Coorg trip"),
]

PRIOR_USER_LINES = [
    "I need a plumber for the kitchen tap again", "Can you find an electrician for a fan installation",
    "My AC needs servicing before summer", "Looking for home cleaning for this weekend",
    "Is there a carpenter free to fix a cabinet hinge", "Show me the cheaper options",
    "What is the rating of the second one", "Make it a morning slot please",
    "Not before 10 a.m., I work late", "Same provider as last time if possible",
    "Can we do Saturday instead", "That price is fine, go ahead",
]
PRIOR_ASSISTANT_LINES = [
    "Here are a few options with ratings and prices.", "I found a few providers with morning slots.",
    "Two of them are free on Saturday morning.", "I can set that up once you approve.",
    "Noted, I will keep that in mind.", "Here is what I found for that slot.",
]


@dataclass
class SeedInfo:
    user_id: str
    current: list[str] = field(default_factory=list)  # usable, errand-relevant
    confirmed_inferred: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)  # ZQU: forbidden
    forgotten: list[str] = field(default_factory=list)  # ZQF: forbidden
    superseded: list[str] = field(default_factory=list)  # ZQS: forbidden
    irrelevant: list[str] = field(default_factory=list)  # ZQI: usable, never relevant
    markers: dict[str, str] = field(default_factory=dict)  # memory id -> marker
    total: int = 0
    prior_session: str = PRIOR_SESSION
    prior_turns: int = 0

    @property
    def forbidden(self) -> list[str]:
        return self.unconfirmed + self.forgotten + self.superseded


def _day(n: int, hour: int = 10) -> str:
    return iso(datetime(2026, 10, 1, hour, 0, tzinfo=IST) + timedelta(days=n))


def seed_demo(memory: SqliteMemoryStore, turns: TurnLog, user_id: str) -> SeedInfo:
    if memory.store.one("SELECT 1 FROM memories WHERE user_id = ? LIMIT 1", (user_id,)) is None:
        _seed(memory, turns, user_id)
    return _info(memory, user_id)


def _seed(memory: SqliteMemoryStore, turns: TurnLog, user_id: str) -> None:
    n = 0

    def at() -> str:
        nonlocal n
        n += 1
        return _day(n % 27)

    # corrected preferences: the OLD value is saved first, then replaced through the real correction path
    memory.add(user_id, "home_area", "Indiranagar (ZQS1)", tags=("profile", "area"), at=at())
    memory.add(user_id, "earliest_time", "08:00 (ZQS2)", tags=("profile", "time"), at=at())
    for key, value, tags in CURRENT:
        memory.add(user_id, key, value, tags=tags, at=at())
    for key, value, tags in CONFIRMED_INFERRED:
        memory.add(user_id, key, value, source="inferred", confirmed=True, tags=tags, at=at())
    for key, value, tags in UNCONFIRMED:
        memory.add(user_id, key, value, source="inferred", confirmed=False, tags=tags, at=at())
    for key, value, tags in FORGOTTEN:
        memory.add(user_id, key, value, tags=tags, forgotten=True, at=at())
    for i, (key, value) in enumerate(IRRELEVANT, 1):
        memory.add(user_id, key, f"{value} (ZQI{i:02d})", tags=("hobby",), at=at())

    # 84 turns (42 exchanges) across October. Two exchanges quote forgotten / replaced values in raw text.
    special = {
        5: "My gate code is 4821 (ZQF1), please pass it on to the technician.",
        9: "I live in Indiranagar (ZQS1), by the way.",
    }
    for i in range(42):
        day = 1 + (i * 27) // 42
        user_text = special.get(i, PRIOR_USER_LINES[i % len(PRIOR_USER_LINES)])
        turns.append(PRIOR_SESSION, user_id, "user", user_text, at=_day(day, 9 + i % 8))
        turns.append(PRIOR_SESSION, user_id, "assistant", PRIOR_ASSISTANT_LINES[i % len(PRIOR_ASSISTANT_LINES)],
                     at=_day(day, 9 + i % 8))


def _info(memory: SqliteMemoryStore, user_id: str) -> SeedInfo:
    info = SeedInfo(user_id)
    usable = {m.id for m in memory.usable(user_id)}
    for r in memory.store.all("SELECT id, value FROM memories WHERE user_id = ? ORDER BY id", (user_id,)):
        info.total += 1
        mark = _MARK.search(r["value"])
        if mark:
            info.markers[r["id"]] = mark.group()
        bucket = {"U": info.unconfirmed, "F": info.forgotten, "S": info.superseded, "I": info.irrelevant}.get(
            mark.group()[2] if mark else "")
        if bucket is not None:
            bucket.append(r["id"])
        elif r["id"] in usable:
            (info.confirmed_inferred if _is_confirmed_inferred(memory, r["id"]) else info.current).append(r["id"])
    info.prior_turns = len(memory.store.all("SELECT 1 FROM turns WHERE session_id = ? AND user_id = ?", (PRIOR_SESSION, user_id)))
    return info


def _is_confirmed_inferred(memory: SqliteMemoryStore, mid: str) -> bool:
    return memory.store.one("SELECT source FROM memories WHERE id = ?", (mid,))["source"] == "inferred"
