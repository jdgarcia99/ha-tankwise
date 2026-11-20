"""Hot water appointments: "hot water ready by" times set by the user.

An appointment opens a block of usage (e.g. 17:00 for 3 hours) on some days
of the week, or on workdays/holidays when a workday sensor is configured.
Its minimum litres (at the shower temperature, 40 °C by default) must be
available at the start of the block and cover the whole block without
reheating within it. The optimiser treats it as a hard constraint (phase 3).

To help choosing the litres, the water actually drawn during the block is
measured on the observed days and summarised as P50/P90 of the last weeks
(the suggested litres).

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any
import unicodedata

from .demand import (
    SLOT_MINUTES,
    SLOTS_PER_DAY,
    WEEKDAY_NAMES,
    DemandModel,
    DemandParams,
    is_workday,
    slot_of,
    weighted_quantile,
)
from .tank import WATER_HEAT_CAPACITY_KWH_PER_L_K

MAX_APPOINTMENTS = 6
DAY_WORKDAY = "workday"
DAY_HOLIDAY = "holiday"
DAY_OPTIONS = (*WEEKDAY_NAMES, DAY_WORKDAY, DAY_HOLIDAY)
DEFAULT_DURATION = timedelta(hours=1)
MIN_DURATION = timedelta(minutes=15)
MAX_DURATION = timedelta(hours=12)
USAGE_WEEKS = 4
DEFAULT_LITERS = 50.0


@dataclass(frozen=True, slots=True)
class Appointment:
    """Water ready by a time on some days."""

    id: str
    name: str
    time: time
    days: frozenset[str]
    liters: float
    enabled: bool = True
    duration: timedelta = DEFAULT_DURATION

    @property
    def slug(self) -> str:
        """Return an ASCII identifier derived from the name (for entity IDs)."""
        return slugify(self.name) or self.id

    def applies(self, day: date, workday: bool | None = None) -> bool:
        """Return True if the appointment is due on ``day``.

        ``workday`` comes from the workday sensor; without it, workdays are
        Monday to Friday.
        """
        if WEEKDAY_NAMES[day.weekday()] in self.days:
            return True
        working = is_workday(day, workday)
        return (DAY_WORKDAY in self.days and working) or (
            DAY_HOLIDAY in self.days and not working
        )

    def next_due(
        self,
        now: datetime,
        workdays: Mapping[date, bool] | None = None,
        *,
        days_ahead: int = 8,
    ) -> datetime | None:
        """Return the next time (local, ``now``'s zone) the water must be ready."""
        if not self.enabled or not self.days:
            return None
        workdays = workdays or {}
        for offset in range(days_ahead):
            day = now.date() + timedelta(days=offset)
            due = datetime.combine(day, self.time, now.tzinfo)
            if due > now and self.applies(day, workdays.get(day)):
                return due
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return the representation stored in the config entry options."""
        return {
            "id": self.id,
            "name": self.name,
            "time": self.time.strftime("%H:%M"),
            "days": [day for day in DAY_OPTIONS if day in self.days],
            "liters": self.liters,
            "enabled": self.enabled,
            "duration": int(self.duration.total_seconds() // 60),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Appointment:
        """Build from :meth:`as_dict` output. Raises ValueError if invalid."""
        try:
            days = frozenset(str(day) for day in data["days"])
            if not days <= set(DAY_OPTIONS):
                raise ValueError(f"unknown days {sorted(days - set(DAY_OPTIONS))}")
            minutes = float(data.get("duration", DEFAULT_DURATION.total_seconds() / 60))
            duration = timedelta(minutes=minutes)
            if not MIN_DURATION <= duration <= MAX_DURATION:
                raise ValueError(f"duration out of range: {minutes} min")
            return cls(
                id=str(data["id"]),
                name=str(data["name"]),
                time=time.fromisoformat(str(data["time"])),
                days=days,
                liters=float(data["liters"]),
                enabled=bool(data.get("enabled", True)),
                duration=duration,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"invalid appointment: {err}") from err


def load_appointments(items: Sequence[Mapping[str, Any]]) -> list[Appointment]:
    """Build the valid appointments of a stored list (invalid ones skipped)."""
    result = []
    for item in items[:MAX_APPOINTMENTS]:
        try:
            result.append(Appointment.from_dict(item))
        except ValueError:
            continue
    return result


def slugify(text: str) -> str:
    """Return a lowercase ASCII slug (``Ducha mañana`` → ``ducha_manana``)."""
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def liters_from_energy(
    energy_kwh: float, shower_temperature: float, mains_temperature: float
) -> float:
    """Return the litres at the shower temperature equivalent to an energy."""
    span = shower_temperature - mains_temperature
    if span <= 0:
        return 0.0
    return energy_kwh / (WATER_HEAT_CAPACITY_KWH_PER_L_K * span)


def energy_from_liters(
    liters: float, shower_temperature: float, mains_temperature: float
) -> float:
    """Return the energy (kWh) of some litres at the shower temperature."""
    span = max(0.0, shower_temperature - mains_temperature)
    return liters * WATER_HEAT_CAPACITY_KWH_PER_L_K * span


@dataclass(frozen=True, slots=True)
class AppointmentUsage:
    """Water drawn during the block of an appointment on the observed days."""

    p50: float | None
    p90: float | None
    days: int


def appointment_usage(
    appointment: Appointment,
    demand: DemandModel,
    params: DemandParams,
    now: datetime,
    *,
    shower_temperature: float,
    mains_temperature: float,
    weeks: int = USAGE_WEEKS,
) -> AppointmentUsage:
    """Measure the litres drawn during the block of the appointment.

    Only the observed days of the last ``weeks`` weeks on which the
    appointment applies are used, weighted by recency like the profile.
    """
    today = now.astimezone(demand.tz).date()
    oldest = today - timedelta(weeks=weeks)
    first = slot_of(datetime.combine(today, appointment.time))
    last = first + max(
        1, int(appointment.duration.total_seconds() // 60 // SLOT_MINUTES)
    )
    days = [
        obs
        for obs in demand.observations
        if oldest <= obs.day < today and appointment.applies(obs.day, obs.workday)
    ]
    if not days:
        return AppointmentUsage(None, None, 0)
    # The same shortage uplift as the plan, so both agree on the litres.
    factors = demand.slot_factors(now)
    liters = [
        liters_from_energy(
            sum(
                obs.slots[index] * factors[index]
                for index in range(max(0, first), min(SLOTS_PER_DAY, last))
            ),
            shower_temperature,
            mains_temperature,
        )
        for obs in days
    ]
    weights = [demand.weight(obs, now, params) for obs in days]
    return AppointmentUsage(
        p50=weighted_quantile(liters, weights, 0.5),
        p90=weighted_quantile(liters, weights, 0.9),
        days=len(days),
    )


@dataclass(frozen=True, slots=True)
class AppointmentRequirement:
    """What the tank needs when an appointment block starts."""

    liters: float  # useful litres at the shower temperature
    source: str  # "minimum" (set by the user) or "usage" (P90 of the block)
    temperature: float  # tank temperature needed at the start of the block
    feasible: bool  # reachable below the learned thermostat cut-off


def required_temperature(
    liters: float,
    *,
    volume_l: float,
    comfort_temperature: float,
    shower_temperature: float,
    mains_temperature: float,
    usable_factor: float,
) -> float:
    """Return the tank temperature that gives ``liters`` useful litres.

    Inverse of the useful litres: L = k_use * V * (T - T_comfort) /
    (T_shower - T_mains).
    """
    span = max(0.0, shower_temperature - mains_temperature)
    return comfort_temperature + liters * span / (volume_l * usable_factor)


def appointment_requirement(
    appointment: Appointment,
    usage: AppointmentUsage | None,
    *,
    volume_l: float,
    comfort_temperature: float,
    shower_temperature: float,
    mains_temperature: float,
    usable_factor: float,
    max_temperature: float,
) -> AppointmentRequirement:
    """Litres and temperature needed at the start of the appointment block.

    The litres are the larger of the minimum set by the user and the P90 of
    the litres used during the block (a quantile of the whole block, never a
    sum of hourly quantiles).
    """
    measured = usage.p90 if usage is not None and usage.p90 is not None else 0.0
    liters, source = (
        (measured, "usage")
        if measured > appointment.liters
        else (appointment.liters, "minimum")
    )
    temperature = required_temperature(
        liters,
        volume_l=volume_l,
        comfort_temperature=comfort_temperature,
        shower_temperature=shower_temperature,
        mains_temperature=mains_temperature,
        usable_factor=usable_factor,
    )
    return AppointmentRequirement(
        liters=liters,
        source=source,
        temperature=temperature,
        feasible=temperature <= max_temperature,
    )
