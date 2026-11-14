"""Tests for the TankWise sensor-reading coordinator."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_BOTTOM_TEMPERATURE_SENSOR,
    CONF_MAINS_WATER_TEMPERATURE,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_SENSOR,
    CONF_SURPLUS_SENSOR,
    CONF_WATER_REGION,
    DEFAULT_PARAMETERS,
    UPDATE_INTERVAL,
)
from custom_components.tankwise.coordinator import TankWiseCoordinator

from .conftest import (
    BOTTOM,
    POWER,
    PRESENCE,
    PRICE,
    SURPLUS,
    TEMPERATURE,
    set_input_states,
    set_optional_states,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> TankWiseCoordinator:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


async def test_reads_required_sensors(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Required sensors are read on setup."""
    set_input_states(hass)
    coordinator = await _setup(hass, mock_entry)

    readings = coordinator.data
    assert readings.temperature == 45.5
    assert readings.power == 1450.0
    assert readings.energy == 123.4
    assert readings.required_available
    assert readings.surplus_power is None
    assert readings.presence is None


async def test_units_are_normalised(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """°F, kW and Wh are converted to °C, W and kWh."""
    set_input_states(
        hass,
        temperature="113",
        temperature_unit="°F",
        power="1.45",
        power_unit="kW",
        energy="123400",
        energy_unit="Wh",
    )
    coordinator = await _setup(hass, mock_entry)

    readings = coordinator.data
    assert readings.temperature == pytest.approx(45.0)
    assert readings.power == pytest.approx(1450.0)
    assert readings.energy == pytest.approx(123.4)


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            **DEFAULT_PARAMETERS,
            CONF_SURPLUS_SENSOR: SURPLUS,
            CONF_BOTTOM_TEMPERATURE_SENSOR: BOTTOM,
            CONF_PRICE_SENSOR: PRICE,
            CONF_PRESENCE_ENTITY: PRESENCE,
        }
    ],
)
async def test_reads_optional_sensors(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Optional sensors are read when configured."""
    set_input_states(hass)
    set_optional_states(hass)
    coordinator = await _setup(hass, mock_entry)

    readings = coordinator.data
    assert readings.surplus_power == pytest.approx(800.0)
    assert readings.bottom_temperature == 30.0
    assert readings.price == 0.12
    assert readings.presence is True


async def test_tolerates_unavailable_sensors(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unavailable or unknown sensors give None without failing the update."""
    set_input_states(hass, temperature="unavailable")
    coordinator = await _setup(hass, mock_entry)

    assert coordinator.last_update_success
    assert coordinator.data.temperature is None
    assert not coordinator.data.required_available
    assert caplog.text.count(f"Input {TEMPERATURE} is not usable") == 1

    # A second unusable reading does not log again.
    hass.states.async_set(POWER, "unknown", {"unit_of_measurement": "W"})
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.power is None
    assert caplog.text.count(f"Input {TEMPERATURE} is not usable") == 1

    # Recovery is picked up by the periodic update.
    set_input_states(hass)
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data.temperature == 45.5
    assert coordinator.data.required_available
    assert f"Input {TEMPERATURE} is usable again" in caplog.text


async def test_non_numeric_state(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A non-numeric state is treated as unusable."""
    set_input_states(hass, power="heating")
    coordinator = await _setup(hass, mock_entry)
    assert coordinator.data.power is None


@pytest.mark.parametrize(
    ("entry_options", "expected"),
    [
        ({**DEFAULT_PARAMETERS}, 22.0),
        ({**DEFAULT_PARAMETERS, CONF_WATER_REGION: "mainland"}, 19.0),
        ({**DEFAULT_PARAMETERS, CONF_MAINS_WATER_TEMPERATURE: 16.5}, 16.5),
    ],
)
async def test_mains_water_temperature(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    entry_options: dict[str, Any],
    expected: float,
) -> None:
    """Mains water temperature comes from the region table or the override."""
    freezer.move_to("2026-08-15 12:00:00+00:00")
    set_input_states(hass)
    coordinator = await _setup(hass, mock_entry)
    assert coordinator.mains_water_temperature == expected


async def test_unload(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """The entry can be unloaded."""
    set_input_states(hass)
    await _setup(hass, mock_entry)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.NOT_LOADED
