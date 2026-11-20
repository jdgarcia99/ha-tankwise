"""Cold start: the tank model is rebuilt from the recorder history."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
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
from custom_components.tankwise.const import BOOTSTRAP_VERSION, DOMAIN
from custom_components.tankwise.model.replay import (
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_TEMPERATURE,
)

from .conftest import (
    ENERGY,
    HEATER_SWITCH,
    MANUAL,
    POWER,
    PRICE,
    TEMPERATURE,
    set_input_states,
)

STORE_0_5 = Path(__file__).parents[1] / "fixtures" / "store_0_5.json"


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
        "data": {
            "bootstrap": {
                "status": "done",
                "samples": 10,
                "version": BOOTSTRAP_VERSION,
            }
        },
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
    set_input_states(hass)
    with patch("custom_components.tankwise.bootstrap._fetch_history", return_value={}):
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


async def test_history_imported_by_0_3_is_imported_again(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """0.3 imported the history without the demand profile: import it again."""
    await _record_history(hass, freezer)
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{mock_entry.entry_id}",
        "data": {"bootstrap": {"status": "done", "samples": 10}},  # no version
    }
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    assert coordinator.bootstrap.samples > 1000
    assert coordinator.bootstrap.version == BOOTSTRAP_VERSION
    assert not coordinator.needs_bootstrap
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]["bootstrap"]
    assert stored["version"] == BOOTSTRAP_VERSION


def _store_0_5(entry: MockConfigEntry) -> dict[str, Any]:
    """Store saved by TankWise 0.5 (built with the 0.5 code, synthetic data)."""
    store = json.loads(STORE_0_5.read_text(encoding="utf-8"))
    store["key"] = f"{DOMAIN}.{entry.entry_id}"
    return store


def _assert_learning_kept(coordinator: Any) -> None:
    tank = coordinator.data.tank
    assert tank.ua_w_per_k == pytest.approx(0.94)
    assert tank.loss_windows == 12
    assert tank.heater_power_w == pytest.approx(1505.0)
    assert tank.usable_factor == pytest.approx(0.89)
    assert tank.capacity_samples == 5
    assert list(tank.cutoff_samples) == [68.5, 69.1, 68.8, 68.2]


async def test_upgrade_from_0_5_keeps_learning(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """The history is imported again on upgrade and merged, never replacing."""
    await _record_history(hass, freezer)
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = _store_0_5(mock_entry)
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    assert coordinator.bootstrap.version == BOOTSTRAP_VERSION
    assert coordinator.bootstrap.draws >= 1
    _assert_learning_kept(coordinator)
    # The draw learned by 0.5 (before the imported period) is kept too.
    starts = [event.start.isoformat() for event in coordinator.model.consumptions]
    assert "2026-11-19T05:30:00+00:00" in starts
    assert len(starts) > 1

    await hass.config_entries.async_unload(mock_entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]
    assert stored["tank"]["ua"] == pytest.approx(0.94)
    assert stored["tank"]["cutoff_samples"] == [68.5, 69.1, 68.8, 68.2]


async def test_import_waits_for_inputs_that_appear_late(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Home Assistant starting: the probe and power do not exist yet."""
    await _record_history(hass, freezer)
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = _store_0_5(mock_entry)
    for entity_id in (TEMPERATURE, POWER, ENERGY):
        hass.states.async_remove(entity_id)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(5):
        await asyncio.sleep(0)

    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.DONE  # the 0.5 one
    assert coordinator.bootstrap.version == 2  # not imported yet
    assert coordinator.model.as_dict()["ua"] == pytest.approx(0.94)

    set_input_states(hass, temperature="50.0")  # the sensors appear
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    assert coordinator.bootstrap.version == BOOTSTRAP_VERSION
    assert coordinator.bootstrap.calibrations == 1
    _assert_learning_kept(coordinator)


async def test_import_waits_for_a_complete_entry(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Required inputs missing (repair issue): the import is postponed."""
    mock_entry.add_to_hass(hass)
    data = {
        key: value
        for key, value in mock_entry.data.items()
        if key not in ("heater_switch", "manual_mode_switch", "price_sensor")
    }
    hass.config_entries.async_update_entry(mock_entry, data=data)
    set_input_states(hass)
    with patch("custom_components.tankwise.coordinator.async_bootstrap") as bootstrap:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    bootstrap.assert_not_called()
    assert mock_entry.runtime_data.needs_bootstrap
    assert HEATER_SWITCH and PRICE  # the inputs the entry lacks


async def test_import_that_learns_nothing_is_not_adopted(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """0.6 bug: units unknown, thousands of empty samples marked as done."""
    await _record_history(hass, freezer)
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = _store_0_5(mock_entry)
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    with patch("custom_components.tankwise.bootstrap.state_unit", return_value=None):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.EMPTY
    assert coordinator.bootstrap.samples > 1000
    assert coordinator.needs_bootstrap  # imported again at the next start
    assert "learned nothing" in caplog.text
    _assert_learning_kept(coordinator)


async def test_reimport_history_service(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await _record_history(hass, freezer)
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = {
        **_store_0_5(mock_entry),
    }
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]["bootstrap"]["version"] = (
        BOOTSTRAP_VERSION
    )
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.samples == 14000  # not imported again

    await hass.services.async_call(DOMAIN, "reimport_history", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.bootstrap.samples != 14000
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    _assert_learning_kept(coordinator)
    assert hass.states.get("button.tankwise_reimport_history") is not None


async def test_upgrade_from_0_6_with_an_empty_import(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """0.6.0 → this version directly: the empty import of 0.6 is redone."""
    await _record_history(hass, freezer)
    store = _store_0_5(mock_entry)
    data = store["data"]
    # What 0.6.0 left: an import marked done that learned nothing, which
    # replaced the learned model with an empty one.
    data["bootstrap"] = {
        "status": "done",
        "samples": 14401,
        "draws": 0,
        "calibrations": 0,
        "days": 0,
        "version": 2,
    }
    data["tank"] = {
        **data["tank"],
        "ua": 1.0,
        "loss_windows": 0,
        "capacity_samples": [],
        "usable_samples": [],
        "heater_power_w": None,
        "heater_power_samples": 0,
        "cutoff_samples": [],
        "calibration_count": 0,
        "consumptions": [],
    }
    hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"] = store
    set_input_states(hass, temperature="50.0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = mock_entry.runtime_data
    assert coordinator.bootstrap.status is BootstrapStatus.DONE
    assert coordinator.bootstrap.version == BOOTSTRAP_VERSION
    assert coordinator.bootstrap.calibrations == 1
    assert coordinator.bootstrap.draws >= 1
    assert coordinator.data.tank.cutoff_samples
    assert coordinator.data.tank.heater_power_w != 1500.0
