"""Fixtures for TankWise integration tests."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_ENERGY_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_MANUAL_MODE_SWITCH,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONFIG_ENTRY_VERSION,
    DEFAULT_PARAMETERS,
    DOMAIN,
)

TEMPERATURE = "sensor.tank_temperature"
POWER = "sensor.heater_power"
ENERGY = "sensor.heater_energy"
HEATER_SWITCH = "switch.water_heater"
MANUAL = "switch.diverter_manual_mode"
PRICE = "sensor.pvpc"
REGULATOR = "sensor.diverter_pwm"
MASTER = "input_boolean.diverter_master"
OCCUPANCY = "input_boolean.home_active"
INJECTION = "sensor.injection_price"
GRID_IMPORT = "sensor.grid_import_energy"
GRID_EXPORT = "sensor.grid_export_energy"
EXPORT_POWER = "sensor.grid_export_power"
SOLAR = "sensor.solar_forecast"
AMBIENT = "sensor.boiler_room_temperature"

REQUIRED_INPUT = {
    "name": "Water heater",
    CONF_TEMPERATURE_SENSOR: TEMPERATURE,
    CONF_POWER_SENSOR: POWER,
    CONF_HEATER_SWITCH: HEATER_SWITCH,
    CONF_MANUAL_MODE_SWITCH: MANUAL,
    CONF_PRICE_SENSOR: PRICE,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration in every test."""


def set_input_states(
    hass: HomeAssistant,
    *,
    temperature: str = "45.5",
    temperature_unit: str = "°C",
    power: str = "0",
    power_unit: str = "W",
    energy: str = "123.4",
    energy_unit: str = "kWh",
    energy_state_class: str | None = "total_increasing",
    manual: str = "off",
    heater: str = "on",
    price: str = "0.12",
) -> None:
    """Create the states of the required inputs and the energy sensor."""
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
    hass.states.async_set(HEATER_SWITCH, heater)
    hass.states.async_set(MANUAL, manual)
    hass.states.async_set(PRICE, price, {"unit_of_measurement": "€/kWh"})


def set_optional_states(hass: HomeAssistant) -> None:
    """Create the states of the optional inputs."""
    total = {"unit_of_measurement": "kWh", "state_class": "total_increasing"}
    hass.states.async_set(REGULATOR, "0", {"unit_of_measurement": "%"})
    hass.states.async_set(MASTER, "on")
    hass.states.async_set(OCCUPANCY, "on")
    hass.states.async_set(INJECTION, "0.05", {"unit_of_measurement": "€/kWh"})
    hass.states.async_set(GRID_IMPORT, "1500.5", total)
    hass.states.async_set(GRID_EXPORT, "800.25", total)
    hass.states.async_set(EXPORT_POWER, "0.8", {"unit_of_measurement": "kW"})
    hass.states.async_set(SOLAR, "6.4", {"unit_of_measurement": "kWh"})
    hass.states.async_set(AMBIENT, "18.0", {"unit_of_measurement": "°C"})


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Default options of a config entry (with the energy sensor)."""
    return {**DEFAULT_PARAMETERS, CONF_ENERGY_SENSOR: ENERGY}


@pytest.fixture
def mock_entry(entry_options: dict[str, Any]) -> MockConfigEntry:
    """Return a TankWise config entry (not yet added to hass)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        unique_id=TEMPERATURE,
        version=CONFIG_ENTRY_VERSION,
        data=dict(REQUIRED_INPUT),
        options=entry_options,
    )
