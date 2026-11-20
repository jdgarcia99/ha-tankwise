"""Mains (cold) water temperature by region and month.

Values are monthly averages in °C, approximated from the reference tables used
for domestic hot water sizing in Spain (UNE 94002 / CTE DB-HE4). Each value is
taken as the temperature in the middle of its month and interpolated linearly
between months. They are a starting point only: the temperature can be fixed
by hand with a number entity.
"""

from __future__ import annotations

import calendar
from datetime import date

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
