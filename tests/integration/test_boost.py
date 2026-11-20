"""Boost in Home Assistant (phase 5)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.tankwise.const import (
    CONF_BOOST_AS_SHORTAGE,
    CONF_ENERGY_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
    EVENT_BOOST_FINISHED,
    EVENT_BOOST_STARTED,
)
from custom_components.tankwise.model.planning import Mode

from .conftest import ENERGY, MANUAL, POWER, PRICE, TEMPERATURE, set_input_states

ACTIVE = "binary_sensor.tankwise_boost_active"
BOOST = "button.tankwise_boost"
CANCEL = "button.tankwise_cancel_boost"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    return {**DEFAULT_PARAMETERS, CONF_ENERGY_SENSOR: ENERGY}


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    mode: Mode = Mode.SHADOW,
    temperature: str = "40.0",
) -> list[ServiceCall]:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature=temperature)
    hass.states.async_set(PRICE, "0.20", {"unit_of_measurement": "€/kWh"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        calls.append(call)

    for name in ("turn_on", "turn_off"):
        hass.services.async_register("switch", name, _record)
    await entry.runtime_data.async_set_mode(mode)
    await hass.async_block_till_done()
    return calls


def _manual_orders(calls: list[ServiceCall]) -> list[str]:
    """Orders on the regulator manual mode, in the order they were sent."""
    return [call.service for call in calls if call.data["entity_id"] == MANUAL]


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def _minute(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    minutes: float = 1,
    *,
    temperature: str | None = None,
    power: str | None = None,
) -> None:
    freezer.tick(timedelta(minutes=minutes))
    state = hass.states.get(TEMPERATURE)
    hass.states.async_set(
        TEMPERATURE, temperature or state.state, state.attributes, force_update=True
    )
    if power is not None:
        hass.states.async_set(POWER, power, hass.states.get(POWER).attributes)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_boost_from_cold_reaches_the_target_and_stops(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shadow mode: the boost still acts, and stops at the target."""
    freezer.move_to("2026-11-18 20:00:00+01:00")
    calls = await _setup(hass, mock_entry)
    started = async_capture_events(hass, EVENT_BOOST_STARTED)
    finished = async_capture_events(hass, EVENT_BOOST_FINISHED)

    await _press(hass, BOOST)
    assert hass.states.get(ACTIVE).state == "on"
    attributes = hass.states.get(ACTIVE).attributes
    assert attributes["target_temperature"] == 60.0
    assert attributes["expected_end"] is not None
    assert _manual_orders(calls) == ["turn_on"]
    assert started[0].data["target"] == 60.0
    assert "Boost: heating to 60.0 °C (at most until 21:30)" in caplog.text

    hass.states.async_set(MANUAL, "on")
    await _minute(hass, mock_entry, freezer, power="1500", temperature="50.0")
    await _minute(hass, mock_entry, freezer, temperature="60.2")
    assert hass.states.get(ACTIVE).state == "off"
    assert _manual_orders(calls) == ["turn_on", "turn_off"]
    assert finished[0].data["reason"] == "target"
    assert finished[0].data["energy_kwh"] == pytest.approx(0.05, abs=0.01)
    assert "Boost finished (target reached) at 60.2 °C" in caplog.text


async def test_pressing_twice_cancels(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    calls = await _setup(hass, mock_entry, mode=Mode.HOLIDAY)
    finished = async_capture_events(hass, EVENT_BOOST_FINISHED)
    await _press(hass, BOOST)
    hass.states.async_set(MANUAL, "on")
    await hass.async_block_till_done()
    await _press(hass, BOOST)
    assert hass.states.get(ACTIVE).state == "off"
    assert finished[0].data["reason"] == "cancelled"
    assert _manual_orders(calls) == ["turn_on", "turn_off"]

    # The cancel button and service do nothing without a boost.
    await _press(hass, CANCEL)
    await hass.services.async_call(DOMAIN, "cancel_boost", {}, blocking=True)
    assert len(finished) == 1


async def test_boost_ends_at_the_thermostat_cutoff(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    await _setup(hass, mock_entry, mode=Mode.AUTO, temperature="55.0")
    finished = async_capture_events(hass, EVENT_BOOST_FINISHED)
    await hass.services.async_call(DOMAIN, "boost", {"temperature": 75}, blocking=True)
    # Limited to the safety maximum.
    assert hass.states.get(ACTIVE).attributes["target_temperature"] == 72.0
    hass.states.async_set(MANUAL, "on")
    await _minute(hass, mock_entry, freezer, power="1.6")
    await _minute(hass, mock_entry, freezer, 3)
    assert finished[0].data["reason"] == "thermostat"
    assert hass.states.get(ACTIVE).state == "off"


async def test_boost_maximum_duration_and_litres(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    await _setup(hass, mock_entry)
    finished = async_capture_events(hass, EVENT_BOOST_FINISHED)
    await hass.services.async_call(
        DOMAIN, "boost", {"liters": 80, "duration": 2}, blocking=True
    )
    target = hass.states.get(ACTIVE).attributes["target_temperature"]
    required = mock_entry.runtime_data.controller.boost_target(liters=80)
    assert target == pytest.approx(required, abs=0.1)
    hass.states.async_set(MANUAL, "on")
    await _minute(hass, mock_entry, freezer, power="1500", temperature="41.0")
    await _minute(hass, mock_entry, freezer, temperature="42.0")
    assert finished[0].data["reason"] == "duration"


async def test_boost_not_needed_and_not_in_off_mode(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    calls = await _setup(hass, mock_entry, temperature="62.0")
    started = async_capture_events(hass, EVENT_BOOST_STARTED)
    await _press(hass, BOOST)
    assert not started
    assert hass.states.get(ACTIVE).state == "off"
    assert "Boost not needed: the water is already at 62.0 °C" in caplog.text
    assert _manual_orders(calls) == []

    await mock_entry.runtime_data.async_set_mode(Mode.OFF)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "boost", {"temperature": 70}, blocking=True
        )


async def test_switching_off_cancels_the_boost(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    calls = await _setup(hass, mock_entry)
    finished = async_capture_events(hass, EVENT_BOOST_FINISHED)
    await _press(hass, BOOST)
    hass.states.async_set(MANUAL, "on")
    await hass.async_block_till_done()
    await mock_entry.runtime_data.async_set_mode(Mode.OFF)
    await hass.async_block_till_done()
    assert finished[0].data["reason"] == "cancelled"
    assert _manual_orders(calls) == ["turn_on", "turn_off"]


@pytest.mark.parametrize("as_shortage", [False, True])
async def test_boost_and_the_demand_profile(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    as_shortage: bool,
) -> None:
    """By default a boost does not change the usage profile."""
    freezer.move_to("2026-11-18 20:00:00+01:00")
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, CONF_BOOST_AS_SHORTAGE: as_shortage},
    )
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature="40.0")
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    async_mock_service(hass, "switch", "turn_on")
    await _press(hass, BOOST)
    marks = mock_entry.runtime_data.demand.marks
    assert [mark.reason for mark in marks] == (["boost"] if as_shortage else [])


async def test_boost_survives_a_restart(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    await _setup(hass, mock_entry)
    await _press(hass, BOOST)
    assert await hass.config_entries.async_reload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ACTIVE).state == "on"
    assert hass.states.get(ACTIVE).attributes["target_temperature"] == 60.0


BLUEPRINT = (
    Path(__file__).parents[2]
    / "blueprints"
    / "automation"
    / "tankwise"
    / "boost_from_button.yaml"
)


async def _blueprint_automation(
    hass: HomeAssistant, tmp_path: Path, **inputs: Any
) -> None:
    folder = tmp_path / "blueprints" / "automation" / "tankwise"
    folder.mkdir(parents=True)
    shutil.copy(BLUEPRINT, folder / BLUEPRINT.name)
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "Boost button",
                "use_blueprint": {
                    "path": f"tankwise/{BLUEPRINT.name}",
                    "input": {
                        "button": [{"trigger": "event", "event_type": "button_pushed"}],
                        **inputs,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()


async def _push(hass: HomeAssistant) -> None:
    hass.bus.async_fire("button_pushed")
    await hass.async_block_till_done()


async def test_blueprint_toggles_the_boost(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    tmp_path: Path,
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    await _setup(hass, mock_entry)
    await _blueprint_automation(hass, tmp_path)
    await _push(hass)
    assert hass.states.get(ACTIVE).state == "on"
    assert hass.states.get(ACTIVE).attributes["target_temperature"] == 60.0
    await _push(hass)
    assert hass.states.get(ACTIVE).state == "off"


async def test_blueprint_start_only_with_temperature_and_duration(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    tmp_path: Path,
) -> None:
    freezer.move_to("2026-11-18 20:00:00+01:00")
    await _setup(hass, mock_entry)
    await _blueprint_automation(
        hass, tmp_path, action="start", temperature=55, duration=30
    )
    await _push(hass)
    await _push(hass)  # start only: still running
    attributes = hass.states.get(ACTIVE).attributes
    assert hass.states.get(ACTIVE).state == "on"
    assert attributes["target_temperature"] == 55.0
    assert attributes["deadline"].startswith("2026-11-18T19:30")
