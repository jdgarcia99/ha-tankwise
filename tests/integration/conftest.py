"""Fixtures for TankWise integration tests."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_ENERGY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
)

TEMPERATURE = "sensor.tank_temperature"
POWER = "sensor.heater_power"
ENERGY = "sensor.heater_energy"
SURPLUS = "sensor.solar_surplus"
BOTTOM = "sensor.tank_bottom_temperature"
PRICE = "sensor.pvpc"
PRESENCE = "input_boolean.home_active"

REQUIRED_INPUT = {
    "name": "Water heater",
    CONF_TEMPERATURE_SENSOR: TEMPERATURE,
    CONF_POWER_SENSOR: POWER,
    CONF_ENERGY_SENSOR: ENERGY,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration in every test."""


def set_input_states(
    hass: HomeAssistant,
    *,
    temperature: str = "45.5",
    temperature_unit: str = "°C",
    power: str = "1450",
    power_unit: str = "W",
    energy: str = "123.4",
    energy_unit: str = "kWh",
    energy_state_class: str | None = "total_increasing",
) -> None:
    """Create the states of the required input sensors."""
    hass.states.async_set(
        TEMPERATURE,
        temperature,
        {"unit_of_measurement": temperature_unit, "device_class": "temperature"},
    )
    hass.states.async_set(
        POWER, power, {"unit_of_measurement": power_unit, "device_class": "power"}
    )
    energy_attributes: dict[str, Any] = {
        "unit_of_measurement": energy_unit,
        "device_class": "energy",
    }
    if energy_state_class is not None:
        energy_attributes["state_class"] = energy_state_class
    hass.states.async_set(ENERGY, energy, energy_attributes)


def set_optional_states(hass: HomeAssistant) -> None:
    """Create the states of the optional input sensors."""
    hass.states.async_set(SURPLUS, "0.8", {"unit_of_measurement": "kW"})
    hass.states.async_set(BOTTOM, "30.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(PRICE, "0.12", {"unit_of_measurement": "€/kWh"})
    hass.states.async_set(PRESENCE, "on")


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Default options of a config entry."""
    return dict(DEFAULT_PARAMETERS)


@pytest.fixture
def mock_entry(entry_options: dict[str, Any]) -> MockConfigEntry:
    """Return a TankWise config entry (not yet added to hass)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        unique_id=TEMPERATURE,
        data=dict(REQUIRED_INPUT),
        options=entry_options,
    )
