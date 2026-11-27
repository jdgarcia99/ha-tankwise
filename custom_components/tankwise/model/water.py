"""Mains (cold) water temperature by region and month.

Values are monthly averages in °C, approximated from the reference tables used
for domestic hot water sizing in Spain (UNE 94002 / CTE DB-HE4). Each value is
taken as the temperature in the middle of its month and interpolated linearly
between months. They are a starting point only: the temperature can be fixed
by hand with a number entity.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import math
from typing import Any

REGION_BALEARIC_ISLANDS = "balearic_islands"
REGION_MAINLAND = "mainland"
REGION_CANARY_ISLANDS = "canary_islands"

DEFAULT_REGION = REGION_BALEARIC_ISLANDS

# January .. December
MAINS_WATER_TEMPERATURES: dict[str, tuple[float, ...]] = {
    # Palma de Mallorca
    REGION_BALEARIC_ISLANDS: (11, 11, 12, 13, 15, 18, 21, 22, 20, 17, 14, 12),
    # Generic mainland Spain (Madrid reference, conservative for inland areas)
    REGION_MAINLAND: (6, 7, 9, 11, 14, 17, 20, 19, 16, 12, 9, 7),
    # Las Palmas de Gran Canaria
    REGION_CANARY_ISLANDS: (15, 15, 16, 16, 17, 18, 19, 19, 19, 18, 17, 16),
}

REGIONS: tuple[str, ...] = tuple(MAINS_WATER_TEMPERATURES)


def _table(region: str) -> tuple[float, ...]:
    return MAINS_WATER_TEMPERATURES.get(
        region, MAINS_WATER_TEMPERATURES[REGION_MAINLAND]
    )


def monthly_mains_temperature(region: str, month: int) -> float:
    """Return the tabulated temperature (°C) of a region for a month (1-12).

    Unknown regions fall back to the generic mainland table.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be in 1..12, got {month}")
    return float(_table(region)[month - 1])


def _mid_month(year: int, month: int) -> float:
    """Return the ordinal (with fraction) of the middle of a month."""
    days = calendar.monthrange(year, month)[1]
    return date(year, month, 1).toordinal() + (days - 1) / 2


def mains_water_temperature(region: str, day: date) -> float:
    """Return the mains water temperature (°C) for a region and day.

    The monthly values are interpolated linearly between the middles of
    consecutive months.
    """
    table = _table(region)
    ordinal = day.toordinal()
    middle = _mid_month(day.year, day.month)
    if ordinal >= middle:
        year, month = (
            (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        )
        before, after = (day.year, day.month), (year, month)
    else:
        year, month = (
            (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
        )
        before, after = (year, month), (day.year, day.month)
    start = _mid_month(*before)
    end = _mid_month(*after)
    fraction = (ordinal - start) / (end - start)
    low = table[before[1] - 1]
    high = table[after[1] - 1]
    return float(low + (high - low) * fraction)


# ---------------------------------------------------------------------------
# Measured mains water temperature
# ---------------------------------------------------------------------------

# Where the cold water sensor is.
PLACEMENT_CISTERN = "cistern"  # in the cistern (aljibe): the water itself
PLACEMENT_PIPE = "pipe"  # on the inlet pipe: the water only while it flows
PLACEMENTS = (PLACEMENT_CISTERN, PLACEMENT_PIPE)
# Readings outside this range are not cold mains water (sensor error).
MEASURED_MIN = 2.0
MEASURED_MAX = 35.0
# Cistern: a slow mean of the readings (the water changes slowly).
CISTERN_TAU = timedelta(hours=1)
# Pipe: at rest the pipe takes the room temperature, so only the readings
# taken after the water has flowed this long count; until a draw is seen, a
# very slow mean of every reading.
PIPE_SETTLE = timedelta(seconds=60)
PIPE_SLOW_TAU = timedelta(hours=24)


def _ema(
    previous: float | None, value: float, elapsed: timedelta, tau: timedelta
) -> float:
    if previous is None:
        return value
    alpha = 1.0 - math.exp(-max(elapsed.total_seconds(), 0.0) / tau.total_seconds())
    return previous + alpha * (value - previous)


class MainsSensor:
    """Mains water temperature from a sensor in the cistern or on the pipe."""

    def __init__(self, placement: str = PLACEMENT_CISTERN) -> None:
        """Start without a value."""
        self.placement = placement if placement in PLACEMENTS else PLACEMENT_CISTERN
        self._value: float | None = None  # cistern mean, or last draw (pipe)
        self._slow: float | None = None  # pipe: slow mean of every reading
        self._last: datetime | None = None
        self._draw_since: datetime | None = None
        self._draw_readings: list[float] = []

    @property
    def temperature(self) -> float | None:
        """Return the mains water temperature, or None if not known yet."""
        if self.placement == PLACEMENT_PIPE and self._value is None:
            return self._slow
        return self._value

    @property
    def from_draw(self) -> bool:
        """Pipe: the value comes from the water flowing in a draw."""
        return self.placement == PLACEMENT_PIPE and self._value is not None

    def update(self, now: datetime, reading: float | None, drawing: bool) -> None:
        """Take a reading (None: unavailable) and whether water is drawn now."""
        if self.placement == PLACEMENT_PIPE:
            if drawing:
                self._draw_since = self._draw_since or now
            elif self._draw_since is not None:
                # The draw is over: its coldest reading is the mains water.
                if self._draw_readings:
                    self._value = min(self._draw_readings)
                self._draw_since = None
                self._draw_readings = []
        if reading is None or not MEASURED_MIN <= reading <= MEASURED_MAX:
            return
        elapsed = now - self._last if self._last is not None else timedelta(0)
        self._last = now
        if self.placement == PLACEMENT_CISTERN:
            self._value = _ema(self._value, reading, elapsed, CISTERN_TAU)
            return
        self._slow = _ema(self._slow, reading, elapsed, PIPE_SLOW_TAU)
        if self._draw_since is not None and now - self._draw_since >= PIPE_SETTLE:
            self._draw_readings.append(reading)

    def as_dict(self) -> dict[str, Any]:
        """Return the state that must survive a restart."""
        return {
            "placement": self.placement,
            "value": self._value,
            "slow": self._slow,
            "last": self._last.isoformat() if self._last else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], placement: str) -> MainsSensor:
        """Restore the state; a change of placement starts again."""
        sensor = cls(placement)
        if data.get("placement") != sensor.placement:
            return sensor
        value, slow, last = data.get("value"), data.get("slow"), data.get("last")
        sensor._value = None if value is None else float(value)
        sensor._slow = None if slow is None else float(slow)
        sensor._last = datetime.fromisoformat(last) if last else None
        return sensor
