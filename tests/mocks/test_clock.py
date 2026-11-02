from datetime import timedelta

import pytest

from errand.clock import DEFAULT_START, IST, FakeClock, SystemClock


def test_fake_clock_only_moves_when_told():
    c = FakeClock()
    assert c.now() == DEFAULT_START
    assert c.now() == c.now()
    c.advance(minutes=11)
    assert c.now() - DEFAULT_START == timedelta(minutes=11)
    assert c.monotonic() == pytest.approx(660)


def test_fake_sleep_advances_instantly():
    c = FakeClock()
    c.sleep(3)
    assert c.monotonic() == pytest.approx(3)
    assert c.now() == DEFAULT_START + timedelta(seconds=3)


def test_fake_clock_cannot_go_backwards():
    c = FakeClock()
    with pytest.raises(ValueError):
        c.advance(-1)
    with pytest.raises(ValueError):
        c.set(DEFAULT_START - timedelta(hours=1))


def test_clocks_are_ist_aware():
    assert FakeClock().now().utcoffset() == timedelta(hours=5, minutes=30)
    assert SystemClock().now().tzinfo == IST
