"""Boost: the user wants hot water now, whatever the plan and the price.

A boost puts the regulator in manual mode at once and ends when the tank
reaches the boost target, when the thermostat cuts off, after the optional
maximum duration, when a safety stops the heating, or when it is cancelled.
It works in every mode except off. The safeties of the controller are never
skipped.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

# Energy is integrated over at most this long between two updates.
MAX_STEP = timedelta(minutes=5)


class BoostEnd(StrEnum):
    """Why a boost ended."""

    TARGET = "target"
    THERMOSTAT = "thermostat"
    DURATION = "duration"
    SAFETY = "safety"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Boost:
    """A boost in progress."""

    start: datetime
    target: float  # °C
    deadline: datetime | None  # maximum duration, None: no limit
    start_temperature: float | None
    energy_kwh: float = 0.0
    cost: float = 0.0
    last_update: datetime | None = None

    def end_reason(self, now: datetime, temperature: float | None) -> BoostEnd | None:
        """Return why the boost ends now (target or duration), if it does."""
        if temperature is not None and temperature >= self.target:
            return BoostEnd.TARGET
        if self.deadline is not None and now >= self.deadline:
            return BoostEnd.DURATION
        return None

    def account(
        self, now: datetime, power_w: float | None, price: float | None
    ) -> Boost:
        """Add the energy (and its cost) taken since the last update."""
        last = self.last_update or self.start
        if power_w is None or now <= last:
            return replace(self, last_update=max(now, last))
        hours = min(now - last, MAX_STEP).total_seconds() / 3600
        energy = max(0.0, power_w) / 1000 * hours
        return replace(
            self,
            energy_kwh=self.energy_kwh + energy,
            cost=self.cost + energy * (price or 0.0),
            last_update=now,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "start": self.start.isoformat(),
            "target": self.target,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "start_temperature": self.start_temperature,
            "energy_kwh": self.energy_kwh,
            "cost": self.cost,
            "last_update": self.last_update.isoformat() if self.last_update else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Boost:
        """Rebuild from :meth:`as_dict` output (raises ValueError if invalid)."""
        try:
            deadline = data.get("deadline")
            last = data.get("last_update")
            start_temperature = data.get("start_temperature")
            return cls(
                start=datetime.fromisoformat(data["start"]),
                target=float(data["target"]),
                deadline=datetime.fromisoformat(deadline) if deadline else None,
                start_temperature=(
                    None if start_temperature is None else float(start_temperature)
                ),
                energy_kwh=float(data.get("energy_kwh", 0.0)),
                cost=float(data.get("cost", 0.0)),
                last_update=datetime.fromisoformat(last) if last else None,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"invalid boost data: {err}") from err
