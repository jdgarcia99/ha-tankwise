"""Mains (cold) water temperature by region and month.

Values are monthly averages in °C, approximated from the reference tables used
for domestic hot water sizing in Spain (UNE 94002 / CTE DB-HE4). They are a
starting point only: users can override the value in the options flow.
"""

from __future__ import annotations

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


def mains_water_temperature(region: str, month: int) -> float:
    """Return the mains water temperature (°C) for a region and month (1-12).

    Unknown regions fall back to the generic mainland table.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be in 1..12, got {month}")
    table = MAINS_WATER_TEMPERATURES.get(
        region, MAINS_WATER_TEMPERATURES[REGION_MAINLAND]
    )
    return float(table[month - 1])
