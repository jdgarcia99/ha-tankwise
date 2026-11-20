"""Anti-legionella cycle: at least once a week, 60 °C or more for an hour.

The tracker watches the probe: when it stays at or above 60 °C for an hour,
a cycle is recorded. When the next deadline (last cycle + 7 days) comes
close, the plan gets a target temperature at the hour where heating is
cheapest (solar surplus first), so the cycle is usually free.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

CYCLE_TEMPERATURE = 60.0
CYCLE_DURATION = timedelta(hours=1)
CYCLE_PERIOD = timedelta(days=7)
# Planned when the deadline is this close.
PLAN_AHEAD = timedelta(days=2)
# Without any known cycle, the first deadline is this far from the start.
FIRST_DEADLINE = timedelta(days=3)
# Target at the planned time: the tank then stays above 60 °C for an hour
# (standing losses are 0.3-0.4 °C/h at that temperature).
TARGET_TEMPERATURE = 61.5
CUTOFF_MARGIN = 0.5
# Probe noise: a reading this far below 60 °C does not break the hour.
NOISE = 0.5


@dataclass(frozen=True, slots=True)
class LegionellaTarget:
    """A planned cycle: the tank must be at ``temperature`` at ``time``."""

    time: datetime
    temperature: float


class LegionellaTracker:
    """Record the cycles really done and compute the next deadline."""

    def __init__(self) -> None:
        """Create a tracker with no known cycle."""
        self.last_cycle: datetime | None = None
        self.first_seen: datetime | None = None
        self._hot_since: datetime | None = None

    def update(self, now: datetime, temperature: float | None) -> bool:
        """Account a probe reading; return True when a cycle completes."""
        self.first_seen = self.first_seen or now
        if temperature is None:
            return False
        if temperature >= CYCLE_TEMPERATURE - (NOISE if self._hot_since else 0.0):
            self._hot_since = self._hot_since or now
            if now - self._hot_since >= CYCLE_DURATION and (
                self.last_cycle is None or self.last_cycle < self._hot_since
            ):
                self.last_cycle = now
                return True
        else:
            self._hot_since = None
        return False

    def deadline(self, now: datetime) -> datetime:
        """Return when the next cycle must be done."""
        if self.last_cycle is not None:
            return self.last_cycle + CYCLE_PERIOD
        return (self.first_seen or now) + FIRST_DEADLINE

    def due(self, now: datetime) -> bool:
        """Return True when the next cycle should be planned."""
        return self.deadline(now) - now <= PLAN_AHEAD

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "last_cycle": self.last_cycle.isoformat() if self.last_cycle else None,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegionellaTracker:
        """Rebuild from :meth:`as_dict` output (raises ValueError if invalid)."""
        tracker = cls()
        try:
            if value := data.get("last_cycle"):
                tracker.last_cycle = datetime.fromisoformat(value)
            if value := data.get("first_seen"):
                tracker.first_seen = datetime.fromisoformat(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"invalid anti-legionella data: {err}") from err
        return tracker


def target_temperature(cutoff: float, safety_max: float) -> float | None:
    """Return the cycle target, or None if the tank cannot reach 60 °C."""
    target = min(TARGET_TEMPERATURE, cutoff - CUTOFF_MARGIN, safety_max - 1.0)
    return target if target >= CYCLE_TEMPERATURE else None


def choose_time(
    start: datetime,
    deadline: datetime,
    *,
    slot_minutes: int,
    buy: Sequence[float],
    surplus_value: Sequence[float],
    solar_kwh: Sequence[float],
    heater_slot_kwh: float,
) -> datetime | None:
    """Choose when the tank must be hot: after the cheapest hour to heat.

    The hour before each candidate time is scored with its energy price:
    the surplus value when the expected surplus covers most of the heater
    power, otherwise the purchase price. Candidates are whole hours in the
    horizon, at least an hour from now and before the deadline.
    """
    per_hour = 60 // slot_minutes
    best: tuple[float, int] | None = None
    for first in range(len(buy) - per_hour + 1):
        moment = start + timedelta(minutes=slot_minutes * (first + per_hour))
        if moment.minute != 0:
            continue
        if moment > deadline:
            break
        window = range(first, first + per_hour)
        scores = []
        for index in window:
            sunny = solar_kwh[index] >= 0.7 * heater_slot_kwh
            scores.append(surplus_value[index] if sunny else buy[index])
        score = sum(scores) / len(scores)
        if best is None or score < best[0] - 1e-9:
            best = (score, first + per_hour)
    if best is None:
        return None
    return start + timedelta(minutes=slot_minutes * best[1])
