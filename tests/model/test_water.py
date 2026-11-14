"""Tests for the mains water temperature table."""

from __future__ import annotations

import pytest
from tankwise_model.water import (
    DEFAULT_REGION,
    MAINS_WATER_TEMPERATURES,
    REGION_BALEARIC_ISLANDS,
    REGION_MAINLAND,
    REGIONS,
    mains_water_temperature,
)


def test_default_region_is_balearic_islands() -> None:
    assert DEFAULT_REGION == REGION_BALEARIC_ISLANDS
    assert DEFAULT_REGION in REGIONS


@pytest.mark.parametrize("region", REGIONS)
def test_every_region_has_twelve_plausible_months(region: str) -> None:
    table = MAINS_WATER_TEMPERATURES[region]
    assert len(table) == 12
    assert all(0 < value < 30 for value in table)
    # Water is warmer in summer than in winter.
    assert table[7] > table[0]


def test_balearic_values() -> None:
    assert mains_water_temperature(REGION_BALEARIC_ISLANDS, 1) == 11.0
    assert mains_water_temperature(REGION_BALEARIC_ISLANDS, 8) == 22.0


def test_unknown_region_falls_back_to_mainland() -> None:
    assert mains_water_temperature("atlantis", 3) == mains_water_temperature(
        REGION_MAINLAND, 3
    )


@pytest.mark.parametrize("month", [0, 13, -1])
def test_invalid_month(month: int) -> None:
    with pytest.raises(ValueError):
        mains_water_temperature(REGION_MAINLAND, month)
