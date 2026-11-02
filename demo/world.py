"""The mock world plus the simulator hooks the demo commands use. These are NOT agent tools: the agent can only
learn what happened by calling its normal tools (e.g. polling ``list_bookings``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from errand.clock import Clock
from errand.mocks import DEMO_USER_ID, MockWorld, ScenarioConfig
from errand.wiring import tools_for

SERVICE_ALIASES = {
    "cleaning": "home_cleaning", "clean": "home_cleaning", "home_cleaning": "home_cleaning",
    "ac": "ac_servicing", "ac_servicing": "ac_servicing", "plumber": "plumber", "electrician": "electrician",
    "carpenter": "carpenter",
}


class DemoError(Exception):
    """A demo command could not be carried out (shown to the user as one line)."""


class DemoWorld:
    def __init__(self, data_dir: str | Path, clock: Clock, *, random: bool = False) -> None:
        path = Path(data_dir) / "world.db"
        existed = path.exists()
        # A new world gets the chosen mode; an existing one keeps the mode it was created with.
        config = None if existed else ScenarioConfig(mode="seeded" if random else "forced")
        self.world = MockWorld(path, clock, config)
        self.clock, self.sim = clock, self.world.sim
        self.mode: str = self.world.config.mode
        self.mode_mismatch = existed and random != (self.mode == "seeded")
        self.tools = tools_for(self.world, DEMO_USER_ID)

    def close(self) -> None:
        self.world.close()

    # ------------------------------------------------------------------ simulator events

    def provider_cancel(self, target: str = "cleaning", reason: str = "Technician unavailable") -> dict[str, Any]:
        """The provider cancels an upcoming confirmed booking: by service ("cleaning", "ac", ...) or booking id."""
        now = self.clock.now().strftime("%Y-%m-%dT%H:%M")
        live = [b for b in self.sim.bookings() if b["status"] == "confirmed" and b["slot"] > now]
        if target.startswith("bk_"):
            pick = [b for b in live if b["booking_id"] == target]
        elif target.lower() in SERVICE_ALIASES:
            pick = [b for b in live if b["service"] == SERVICE_ALIASES[target.lower()]]
        else:
            raise DemoError(f"don't know {target!r}: try a service (cleaning, ac, plumber, electrician) or a booking id")
        if not pick:
            raise DemoError(f"no upcoming confirmed booking matches {target!r}")
        booking = min(pick, key=lambda b: b["slot"])
        self.sim.provider_cancels(booking["booking_id"], reason)
        return booking

    def arm_timeout(self, when: str = "after") -> str:
        kind = {"after": "timeout_after_commit", "before": "timeout_before_commit"}.get(when)
        if kind is None:
            raise DemoError("use /timeout after (booked, then times out) or /timeout before (nothing booked)")
        self.sim.force("book", kind)
        return kind

    def arm_price(self, delta: int) -> None:
        self.sim.force("hold_slot", "price_change", delta=delta)

    def force(self, tool: str, kind: str, times: int = 1) -> None:
        try:
            self.sim.force(tool, kind, times=times)
        except ValueError as e:
            raise DemoError(str(e)) from None

    def take_slot(self, provider_id: str, slot: str) -> str:
        return self.sim.take_slot(provider_id, slot)
