"""Tests for the mains water temperature table and its interpolation."""

from __future__ import annotations

from datetime import date

import pytest
from tankwise_model.water import (
    DEFAULT_REGION,
    MAINS_WATER_TEMPERATURES,
    REGION_BALEARIC_ISLANDS,
    REGION_MAINLAND,
    REGIONS,
    mains_water_temperature,
    monthly_mains_temperature,
)


def test_default_region_is_balearic_islands() -> None:
    assert DEFAULT_REGION == REGION_BALEARIC_ISLANDS
    assert DEFAULT_REGION in REGIONS


def test_every_region_has_twelve_months() -> None:
    for table in MAINS_WATER_TEMPERATURES.values():
        assert len(table) == 12


def test_monthly_values() -> None:
    assert monthly_mains_temperature(REGION_BALEARIC_ISLANDS, 1) == 11
    assert monthly_mains_temperature(REGION_BALEARIC_ISLANDS, 8) == 22
    assert monthly_mains_temperature("atlantis", 1) == 6  # mainland fallback


@pytest.mark.parametrize("month", [0, 13, -1])
def test_invalid_month(month: int) -> None:
    with pytest.raises(ValueError):
        monthly_mains_temperature(REGION_MAINLAND, month)


def test_middle_of_month_is_the_table_value() -> None:
    # November has 30 days: its middle is between the 15th and the 16th.
    assert mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2026, 8, 16)) == 22
    value = mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2026, 11, 15))
    assert value == pytest.approx(14.0, abs=0.1)


def test_interpolation_between_months() -> None:
    """November (14 °C) to December (12 °C): the 1st of December is ~13 °C."""
    value = mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2026, 12, 1))
    assert 12.9 < value < 13.1
    nov_20 = mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2026, 11, 20))
    assert 13.6 < nov_20 < 14.0


def test_interpolation_across_the_new_year() -> None:
    """December (12 °C) to January (11 °C)."""
    dec_31 = mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2026, 12, 31))
    jan_1 = mains_water_temperature(REGION_BALEARIC_ISLANDS, date(2027, 1, 1))
    assert 11.4 < jan_1 < dec_31 < 11.6


def test_interpolation_is_continuous_over_a_year() -> None:
    day = date(2026, 1, 1)
    previous = mains_water_temperature(REGION_MAINLAND, day)
    for offset in range(1, 366):
        current = mains_water_temperature(
            REGION_MAINLAND, date.fromordinal(day.toordinal() + offset)
        )
        assert abs(current - previous) < 0.2
        previous = current
