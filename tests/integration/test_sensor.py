"""Tests for the TankWise tank sensors, safe mode and persistence."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import DOMAIN, UPDATE_INTERVAL

from .conftest import set_input_states

NOW = "2026-11-15 12:00:00+00:00"  # November: Balearic mains water at 14 °C
CAPACITY = 100 * 4.186 / 3600  # kWh/K of a 100 L tank

USEFUL_ENERGY = "sensor.water_heater_useful_energy"
CHARGE = "sensor.water_heater_charge"
SHOWERS = "sensor.water_heater_showers_available"
STATUS = "sensor.water_heater_model_status"
ALL_SENSORS = (USEFUL_ENERGY, CHARGE, SHOWERS, STATUS)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _stored_tank(**overrides: Any) -> dict[str, Any]:
    tank = {
        "format": 1,
        "equivalent_temperature": 40.0,
        "loss_coefficient": 0.01,
        "loss_windows": 5,
        "cutoff_samples": [47.2, 47.0, 46.8],
        "uncertain": False,
        "calibration_count": 3,
        "last_calibration": "2026-11-15T08:00:00+00:00",
        "last_valid_time": "2026-11-15T11:58:00+00:00",
        "last_energy": 123.4,
        "consumptions": [],
    }
    tank.update(overrides)
    return tank


def _store(
    hass_storage: dict[str, Any], entry: MockConfigEntry, tank: dict[str, Any]
) -> None:
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"tank": tank},
    }


async def test_sensors_from_first_reading(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Without stored state the model starts from the probe, uncertain."""
    freezer.move_to(NOW)
    set_input_states(hass, temperature="45.5", power="0")
    await _setup(hass, mock_entry)

    useful = CAPACITY * (45.5 - 32)
    assert float(hass.states.get(USEFUL_ENERGY).state) == pytest.approx(useful)
    assert hass.states.get(USEFUL_ENERGY).attributes["unit_of_measurement"] == "kWh"
    assert float(hass.states.get(CHARGE).state) == pytest.approx(90.0)
    assert float(hass.states.get(SHOWERS).state) == pytest.approx(useful / 1.8)
    assert hass.states.get(STATUS).state == "uncertain"


async def test_safe_mode(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A missing required sensor makes the tank sensors unavailable."""
    freezer.move_to(NOW)
    set_input_states(hass, power="0")
    await _setup(hass, mock_entry)
    useful_before = hass.states.get(USEFUL_ENERGY).state

    hass.states.async_set(
        "sensor.heater_energy", "unavailable", {"unit_of_measurement": "kWh"}
    )
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    for entity_id in ALL_SENSORS:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    set_input_states(hass, power="0")
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # Back from safe mode: frozen state resumes, no draw invented.
    assert float(hass.states.get(USEFUL_ENERGY).state) == pytest.approx(
        float(useful_before), abs=0.01
    )
    assert mock_entry.runtime_data.model.consumptions == []


async def test_restores_state_after_short_restart(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A restart of a few minutes keeps the stored state and its status."""
    freezer.move_to(NOW)
    _store(hass_storage, mock_entry, _stored_tank())
    set_input_states(hass, temperature="40.0", power="0", energy="123.4")
    await _setup(hass, mock_entry)

    assert hass.states.get(STATUS).state == "calibrated"
    useful = float(hass.states.get(USEFUL_ENERGY).state)
    assert useful == pytest.approx(CAPACITY * (40.0 - 32), abs=0.02)
    model = mock_entry.runtime_data.model
    assert model.as_dict()["calibration_count"] == 3


async def test_long_restart_makes_state_uncertain(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a 6 h outage the state is uncertain; heat comes from the meter."""
    freezer.move_to(NOW)
    stored_time = dt_util.utcnow() - timedelta(hours=6)
    _store(
        hass_storage,
        mock_entry,
        _stored_tank(
            equivalent_temperature=30.0,
            last_valid_time=stored_time.isoformat(),
            last_energy=122.4,
        ),
    )
    # 1 kWh of heating while Home Assistant was down.
    set_input_states(hass, temperature="38.0", power="0", energy="123.4")
    await _setup(hass, mock_entry)

    assert hass.states.get(STATUS).state == "uncertain"
    equivalent = mock_entry.runtime_data.data.tank.equivalent_temperature
    assert 36.0 < equivalent < 40.0


async def test_corrupt_storage_is_discarded(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unusable stored data starts a fresh model instead of failing."""
    _store(hass_storage, mock_entry, {"format": 99})
    set_input_states(hass)
    await _setup(hass, mock_entry)

    assert "Discarding stored tank state" in caplog.text
    assert hass.states.get(STATUS).state == "uncertain"


async def test_unload_saves_and_remove_deletes_storage(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The state is saved on unload and deleted with the integration."""
    set_input_states(hass)
    await _setup(hass, mock_entry)
    key = f"{DOMAIN}.{mock_entry.entry_id}"

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["tank"]["equivalent_temperature"] == 45.5

    assert await hass.config_entries.async_remove(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
