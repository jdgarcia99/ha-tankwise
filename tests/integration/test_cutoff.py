"""Thermostat cut-off detection between samples, through state listeners."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_REGULATOR_SENSOR,
    DEFAULT_PARAMETERS,
    UPDATE_INTERVAL,
)

from .conftest import ENERGY, MANUAL, POWER, REGULATOR, TEMPERATURE, set_input_states

START = "2026-11-15 21:58:30+00:00"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options with the energy sensor, the PWM output and a 47 °C cut-off."""
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_REGULATOR_SENSOR: REGULATOR,
        CONF_CUTOFF_TEMPERATURE: 47.0,
    }


class Heater:
    """Drive the input entities like the real installation."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
        self.hass = hass
        self.freezer = freezer
        self.energy = 500.0
        self.power = 1441.0

    def set_power(self, watts: float) -> None:
        self.power = watts
        self.hass.states.async_set(POWER, f"{watts}", {"unit_of_measurement": "W"})

    def set_manual(self, on: bool) -> None:
        self.hass.states.async_set(MANUAL, "on" if on else "off")
        self.hass.states.async_set(
            REGULATOR, "100" if on else "0", {"unit_of_measurement": "%"}
        )

    async def wait(self, seconds: float) -> None:
        """Advance time; the coordinator polls when its interval is due."""
        self.freezer.tick(timedelta(seconds=seconds))
        self.energy += self.power / 1000 * seconds / 3600
        self.hass.states.async_set(
            ENERGY,
            f"{self.energy:.4f}",
            {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
        )
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()


async def _heating(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry
) -> Heater:
    freezer.move_to(START)
    set_input_states(
        hass, temperature="46.5", power="1441", energy="500.0", manual="on"
    )
    hass.states.async_set(REGULATOR, "100", {"unit_of_measurement": "%"})
    hass.states.async_set(TEMPERATURE, "46.5", {"unit_of_measurement": "°C"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    heater = Heater(hass, freezer)
    for _ in range(10):
        await heater.wait(UPDATE_INTERVAL.total_seconds())
    return heater


async def test_cutoff_then_manual_mode_switched_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """15/11 23:08:57: cut-off in manual mode, switched off three minutes later."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(27)
    heater.set_power(1.8)  # thermostat cut-off between two samples
    await heater.wait(181)
    heater.set_manual(False)  # an automation switches the manual mode off
    for _ in range(5):
        await heater.wait(60)

    data = mock_entry.runtime_data.data
    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 1
    assert data.tank.cutoff_samples[-1] == pytest.approx(46.5, abs=0.2)


async def test_manual_mode_switched_off_first_is_not_full(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """Manual mode off at :03, power falls at :04: not a full tank."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(3)
    heater.set_manual(False)
    await heater.wait(1)
    heater.set_power(0.5)
    for _ in range(40):
        await heater.wait(60)

    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 0
