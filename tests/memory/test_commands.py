import pytest

from errand.memory import MemoryCommand, parse

KEYS = ["gate_code", "plumber_budget"]


@pytest.mark.parametrize("text,expected", [
    ("what do you remember", MemoryCommand("inspect")),
    ("What do you remember about me?", MemoryCommand("inspect")),
    ("show my memories", MemoryCommand("inspect")),
    ("forget my area", MemoryCommand("forget", "home_area")),
    ("Forget m_000012", MemoryCommand("forget", "m_000012")),
    ("forget my gate code", MemoryCommand("forget", "gate_code")),
    ("forget plumber budget", MemoryCommand("forget", "plumber_budget")),
    ("correct my area to Jadavpur", MemoryCommand("correct", "home_area", "Jadavpur")),
    ("change earliest time to 9:30", MemoryCommand("correct", "earliest_time", "9:30")),
    ("remember my area is HSR Layout", MemoryCommand("remember", "home_area", "HSR Layout")),
    ("remember my technician: Suresh", MemoryCommand("remember", "preferred_technician", "Suresh")),
    ("remember my dog's name is Bruno", MemoryCommand("remember", "dog_s_name", "Bruno")),
    ("confirm m_000005", MemoryCommand("confirm", "m_000005")),
    ("reject m_000006", MemoryCommand("reject", "m_000006")),
])
def test_commands(text, expected):
    assert parse(text, KEYS) == expected


@pytest.mark.parametrize("text", [
    "forget it, book the first one", "forget it", "forget my wallet", "yes", "find me a plumber",
    "go with the first one", "confirm", "confirm CONFIRM 4821", "I want to correct my plans", "what do you think",
    "forget my area and book the first one",
])
def test_not_commands_go_to_the_model_or_the_guard(text):
    assert parse(text, KEYS) is None
