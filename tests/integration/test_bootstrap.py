"""Cold start: the tank model is rebuilt from the recorder history."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.tankwise.bootstrap import BootstrapStatus, states_to_changes
from custom_components.tankwise.const import DOMAIN
from custom_components.tankwise.model.replay import (
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_TEMPERATURE,
)

from .conftest import ENERGY, MANUAL, POWER, TEMPERATURE, set_input_states


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Any, enable_custom_integrations: None
) -> None:
    """The recorder must be set up before Home Assistant in these tests."""


async def _record_history(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Two days of history: a heating to the cut-off and an evening shower."""
    start = dt_util.utcnow() - timedelta(days=2)
    freezer.move_to(start)
    temperature = 40.0
    energy = 100.0
    set_input_states(hass, temperature=f"{temperature}", energy=f"{energy}")
    await hass.async_block_till_done()

    def minute(power: float, manual: str, draw_kw: float = 0.0) -> None:
        nonlocal temperature, energy
        heat = power / 1000 / 60
        energy += heat
        temperature += (0.9 * heat - draw_kw / 60) / 0.1163
        temperature -= (temperature - 20) * 1.0 / 116.3 / 60
        freezer.tick(timedelta(minutes=1))
        set_input_states(
            hass,
            temperature=f"{temperature:.1f}",
            power=f"{power}",
            energy=f"{energy:.4f}",
            manual=manual,
        )

    for _ in range(60):
        minute(0, "off")
    while temperature < 62.0:  # heating in manual mode until the cut-off
        minute(1480, "on")
    for _ in range(4):
        minute(0, "on")  # thermostat open, manual mode still on
    for _ in range(600):
        minute(0, "off")
    for _ in range(12):
        minute(0, "off", draw_kw=9.0)  # shower
    for _ in range(600):
        minute(0, "off")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(days=2))


async def test_history_is_imported_on_first_start(
    recorder_mock: Any,
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await _record_history(hass, freezer)
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    assert coordinator.bootstrap.samples > 1000
    assert coordinator.bootstrap.calibrations == 1
    assert coordinator.bootstrap.draws >= 1
    assert coordinator.model.as_dict()["cutoff_samples"]
    assert any(event.energy_kwh > 1.0 for event in coordinator.model.consumptions)
    assert coordinator.data.tank.last_consumption is not None
    # The demand profile was rebuilt from the same history.
    assert coordinator.bootstrap.days == len(coordinator.demand.observations)

    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]
    assert stored["bootstrap"]["status"] == "done"


async def test_history_is_not_imported_twice(
    recorder_mock: Any,
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{mock_entry.entry_id}",
        "data": {"bootstrap": {"status": "done", "samples": 10}},
    }
    set_input_states(hass)
    mock_entry.add_to_hass(hass)
    with patch("custom_components.tankwise.coordinator.async_bootstrap") as bootstrap:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    bootstrap.assert_not_called()
    assert mock_entry.runtime_data.bootstrap.samples == 10


async def test_empty_history(
    recorder_mock: Any, hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Entities without recorded states: nothing imported, live model kept."""
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        data={**mock_entry.data, "temperature_sensor": "sensor.never_recorded"},
    )
    set_input_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_entry.runtime_data.bootstrap.status is BootstrapStatus.NO_HISTORY


async def test_failure_does_not_break_setup(
    recorder_mock: Any,
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_input_states(hass)
    mock_entry.add_to_hass(hass)
    with patch(
        "custom_components.tankwise.bootstrap._fetch_history",
        side_effect=RuntimeError("database locked"),
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    info = mock_entry.runtime_data.bootstrap
    assert info.status is BootstrapStatus.FAILED
    assert info.error == "database locked"
    assert "Could not import the tank history" in caplog.text
    assert hass.states.get("sensor.tankwise_stored_energy").state != "unavailable"


def test_states_to_changes_converts_units_and_bad_values() -> None:
    from homeassistant.core import State

    moment = dt_util.utcnow()
    states = [
        State(TEMPERATURE, "113", last_changed=moment),
        State(TEMPERATURE, "unavailable", last_changed=moment + timedelta(minutes=1)),
    ]
    changes = states_to_changes(ROLE_TEMPERATURE, states, "°F")
    assert changes[0].value == pytest.approx(45.0)
    assert changes[1].value is None

    energy = states_to_changes(
        ROLE_ENERGY, [State(ENERGY, "1500", last_changed=moment)], "Wh"
    )
    assert energy[0].value == pytest.approx(1.5)

    manual = states_to_changes(
        ROLE_MANUAL_MODE, [State(MANUAL, "on", last_changed=moment)], None
    )
    assert manual[0].value is True
    assert POWER  # imported for symmetry with the other tests
