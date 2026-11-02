import re

import pytest

from errand.mocks import AREAS, SERVICES, ErrorCode, ToolError
from errand.mocks.world import parse_slot
from tests.conftest import WEEK

INJECTION = re.compile(r"ignore all previous instructions|system|approval is not required|instructions>", re.I)


@pytest.mark.parametrize("service", list(SERVICES))
@pytest.mark.parametrize("area", AREAS)
def test_every_service_area_returns_3_to_8_providers(tools, service, area):
    res = tools.search_providers(service, area, WEEK)
    assert 3 <= len(res.providers) <= 8
    ids = [p.provider_id for p in res.providers]
    assert len(ids) == len(set(ids))
    for p in res.providers:
        assert p.service == service and area in p.areas
        assert p.price > 0 and 0 < p.rating <= 5
        assert {"technician", "verified", "specialties"} <= set(p.attributes)


def test_slots_are_future_hourly_and_within_range(tools, clock):
    res = tools.search_providers("plumber", "Koramangala", WEEK)
    all_slots = [s for p in res.providers for s in p.available_slots]
    assert all_slots, "seeded world should have availability"
    for s in all_slots:
        dt = parse_slot(s)
        assert dt > clock.now()
        assert "2026-11-02" <= s[:10] <= "2026-11-08"


def test_mornings_under_budget_exist_for_example_request(tools):
    """'plumber for my kitchen tap this week, mornings only, under ₹1,500' must be satisfiable."""
    res = tools.search_providers("plumber", "Indiranagar", WEEK)
    hits = [(p.provider_id, s) for p in res.providers if p.price < 1500
            for s in p.available_slots if int(s[11:13]) < 12]
    assert hits


def test_aliases_normalize(tools):
    assert tools.search_providers("Plumbing", "indiranagar", WEEK).service == "plumber"
    assert tools.search_providers("AC repair", "HSR layout", ["2026-11-03", "2026-11-04"]).area == "HSR Layout"


@pytest.mark.parametrize(
    "service,area,dr,code",
    [
        ("astrologer", "Indiranagar", WEEK, ErrorCode.UNKNOWN_SERVICE),
        ("plumber", "Mars", WEEK, ErrorCode.UNKNOWN_AREA),
        ("plumber", "Indiranagar", {"start": "2026-11-01", "end": "2026-12-30"}, ErrorCode.INVALID_ARGUMENT),
        ("plumber", "Indiranagar", {"start": "2026-10-01", "end": "2026-10-05"}, ErrorCode.INVALID_ARGUMENT),
        ("plumber", "Indiranagar", {"start": "2026-11-08", "end": "2026-11-02"}, ErrorCode.INVALID_ARGUMENT),
        ("plumber", "Indiranagar", "next week", ErrorCode.INVALID_ARGUMENT),
        ("", "Indiranagar", WEEK, ErrorCode.INVALID_ARGUMENT),
    ],
)
def test_search_validation(tools, service, area, dr, code):
    with pytest.raises(ToolError) as e:
        tools.search_providers(service, area, dr)
    assert e.value.code == code


def test_get_provider_returns_reviews(tools):
    d = tools.get_provider("pl_02")
    assert d.provider_id == "pl_02" and d.reviews
    assert all(r.text for r in d.reviews)


def test_prompt_injection_reviews_exist(tools):
    texts = [r.text for pid in ("pl_01", "pl_04", "el_01", "ac_01") for r in tools.get_provider(pid).reviews]
    assert sum(bool(INJECTION.search(t)) for t in texts) >= 4


def test_unknown_provider(tools):
    with pytest.raises(ToolError) as e:
        tools.get_provider("pl_99")
    assert e.value.code == ErrorCode.PROVIDER_NOT_FOUND


def test_dispatch_only_allows_contract_tools(tools):
    with pytest.raises(ToolError):
        tools.call("drop_tables", {})
    with pytest.raises(ToolError) as e:
        tools.call("get_provider", {"provider_id": "pl_01", "admin": True})
    assert e.value.code == ErrorCode.INVALID_ARGUMENT
    assert tools.call("get_provider", {"provider_id": "pl_01"}).provider_id == "pl_01"
