"""Full-tank detection between samples, through state change listeners."""

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
    CONF_REGULATOR_SENSOR,
    DEFAULT_PARAMETERS,
    UPDATE_INTERVAL,
)

from .conftest import ENERGY, POWER, REGULATOR, TEMPERATURE, set_input_states

START = "2026-11-15 21:58:30+00:00"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options with the regulator output sensor."""
    return {**DEFAULT_PARAMETERS, CONF_REGULATOR_SENSOR: REGULATOR}


class Heater:
    """Drive the input sensors like the real installation."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
        self.hass = hass
        self.freezer = freezer
        self.energy = 500.0
        self.power = 1441.0

    def set_power(self, watts: float) -> None:
        self.power = watts
        self.hass.states.async_set(POWER, f"{watts}", {"unit_of_measurement": "W"})

    def set_regulator(self, percent: float) -> None:
        self.hass.states.async_set(
            REGULATOR, f"{percent}", {"unit_of_measurement": "%"}
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
    set_input_states(hass, temperature="46.5", power="1441", energy="500.0")
    hass.states.async_set(REGULATOR, "100", {"unit_of_measurement": "%"})
    hass.states.async_set(TEMPERATURE, "46.5", {"unit_of_measurement": "°C"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    heater = Heater(hass, freezer)
    for _ in range(10):  # ten minutes of real heating (~240 Wh)
        await heater.wait(UPDATE_INTERVAL.total_seconds())
    return heater


async def test_cutoff_then_regulator_switched_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """15/11 23:08:57: cut-off at PWM 100 %, PWM to 0 three minutes later."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(27)
    heater.set_power(1.8)  # thermostat cut-off between two samples
    await heater.wait(181)
    heater.set_regulator(0)  # automation switches the manual mode off
    for _ in range(5):
        await heater.wait(60)

    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 1
    assert mock_entry.runtime_data.data.tank.equivalent_temperature == (
        pytest.approx(47.0, abs=0.2)
    )


async def test_regulator_switched_off_first_is_not_full(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """15/11 16:00: PWM to 0 at :03, power falls at :04. Not a full tank."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(3)
    heater.set_regulator(0)
    await heater.wait(1)
    heater.set_power(0.5)
    for _ in range(40):
        await heater.wait(60)

    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 0
