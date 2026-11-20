"""Runtime settings (number entities)."""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import DOMAIN
from custom_components.tankwise.model.water import mains_water_temperature

from .conftest import set_input_states

SHOWER = "number.water_heater_shower_temperature"
COMFORT = "number.water_heater_comfort_min_temperature"
MAINS = "number.water_heater_mains_water_temperature"
LITERS = "sensor.water_heater_available_liters"
USEFUL = "sensor.water_heater_useful_energy"
MAINS_SENSOR = "sensor.water_heater_mains_water_temperature"
NOW = "2026-11-15 12:00:00+00:00"


async def _set(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    set_input_states(hass, temperature="60.0")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_default_values(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_entry)
    assert float(hass.states.get(SHOWER).state) == 40.0
    assert float(hass.states.get(COMFORT).state) == 38.0
    assert float(hass.states.get(MAINS).state) == 0.0


async def test_settings_update_derived_values_at_once(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    table = mains_water_temperature("balearic_islands", date(2026, 11, 15))
    assert float(hass.states.get(MAINS_SENSOR).state) == pytest.approx(table)

    await _set(hass, SHOWER, 45.0)
    assert float(hass.states.get(LITERS).state) == pytest.approx(
        100 * (60 - table) / (45 - table)
    )

    await _set(hass, COMFORT, 40.0)
    assert float(hass.states.get(USEFUL).state) == pytest.approx(0.1163 * 20)

    await _set(hass, MAINS, 18.0)
    assert float(hass.states.get(MAINS_SENSOR).state) == 18.0
    assert hass.states.get(MAINS_SENSOR).attributes["source"] == "manual"
    assert float(hass.states.get(LITERS).state) == pytest.approx(100 * 42 / 27)

    await _set(hass, MAINS, 0.0)
    assert hass.states.get(MAINS_SENSOR).attributes["source"] == "table"

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    settings = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]["settings"]
    assert settings == {
        "shower_temperature": 45.0,
        "comfort_min_temperature": 40.0,
        "mains_water_temperature": 0.0,
    }


async def test_settings_are_restored(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{mock_entry.entry_id}",
        "data": {
            "settings": {"shower_temperature": 42.0, "unknown_setting": 1},
            "bootstrap": {"status": "done"},
        },
    }
    await _setup(hass, mock_entry)
    assert float(hass.states.get(SHOWER).state) == 42.0
    assert "unknown_setting" not in mock_entry.runtime_data.settings
