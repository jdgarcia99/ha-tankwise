"""Controller in Home Assistant (phase 4): orders, safeties, degraded mode."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.tankwise.const import (
    CONF_APPOINTMENTS,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
    EVENT_PLAN_AT_RISK,
)
from custom_components.tankwise.model.planning import Mode

from .conftest import (
    ENERGY,
    EXPORT_POWER,
    HEATER_SWITCH,
    INJECTION,
    MANUAL,
    POWER,
    PRICE,
    TEMPERATURE,
    set_input_states,
)

EVENING = {
    "id": "e5f6a7b8",
    "name": "Tarde",
    "time": "17:00",
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "liters": 60.0,
    "enabled": True,
    "duration": 180,
}
AT_RISK = "binary_sensor.tankwise_plan_at_risk"
DEGRADED = "binary_sensor.tankwise_degraded_mode"


def _prices(base: float, cheap_hours: tuple[int, ...], cheap: float) -> dict[str, Any]:
    attributes = {f"price_{hour:02d}h": base for hour in range(24)}
    attributes |= {f"price_{hour:02d}h": cheap for hour in cheap_hours}
    attributes["unit_of_measurement"] = "€/kWh"
    return attributes


@pytest.fixture
def entry_options() -> dict[str, Any]:
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_INJECTION_PRICE_SENSOR: INJECTION,
        CONF_EXPORT_POWER_SENSOR: EXPORT_POWER,
        CONF_APPOINTMENTS: [EVENING],
    }


@pytest.fixture
def switch_calls() -> list[list[ServiceCall]]:
    """Switch orders, recorded once TankWise is set up (plain states here)."""
    return []


def _orders(switch_calls: list) -> list[tuple[str, str]]:
    if not switch_calls:
        return []
    on, off = switch_calls
    return sorted(
        [(call.data["entity_id"], call.service) for call in [*on, *off]],
        key=str,
    )


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    calls: list[list[ServiceCall]],
    *,
    temperature: str = "40.0",
    prices: bool = True,
    mode: Mode = Mode.AUTO,
) -> None:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature=temperature)
    if prices:
        hass.states.async_set(PRICE, "0.20", _prices(0.20, (14, 15), 0.08))
    else:
        hass.states.async_set(PRICE, "unavailable", {})
    hass.states.async_set(INJECTION, "0.05", _prices(0.05, (), 0.05))
    hass.states.async_set(EXPORT_POWER, "0", {"unit_of_measurement": "W"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After the switch platform is loaded, or it replaces the mocks.
    calls.extend(
        async_mock_service(hass, "switch", service)
        for service in ("turn_on", "turn_off")
    )
    await entry.runtime_data.async_set_mode(mode)
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
    """Advance the clock; the probe keeps reporting; refresh once."""
    freezer.tick(timedelta(minutes=minutes))
    state = hass.states.get(TEMPERATURE)
    hass.states.async_set(
        TEMPERATURE,
        temperature or state.state,
        state.attributes,
        force_update=True,
    )
    if power is not None:
        hass.states.async_set(POWER, power, hass.states.get(POWER).attributes)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_auto_mode_heats_in_the_planned_slot(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cheap at 14:00, appointment at 17:00: the regulator goes to manual."""
    freezer.move_to("2026-11-18 14:01:00+01:00")
    await _setup(hass, mock_entry, switch_calls)
    assert _orders(switch_calls) == [(MANUAL, "turn_on")]
    assert "Heating from the grid until" in caplog.text
    assert hass.states.get(DEGRADED).state == "off"

    # Back to shadow mode: TankWise gives the regulator back.
    hass.states.async_set(MANUAL, "on")
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.tankwise_mode", "option": "shadow"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _orders(switch_calls) == [(MANUAL, "turn_off"), (MANUAL, "turn_on")]
    assert "mode changed to shadow" in caplog.text


async def test_shadow_mode_does_not_act(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    freezer.move_to("2026-11-18 14:01:00+01:00")
    await _setup(hass, mock_entry, switch_calls, mode=Mode.SHADOW)
    await _minute(hass, mock_entry, freezer)
    assert hass.states.get("binary_sensor.tankwise_planned_grid_heating").state == "on"
    assert _orders(switch_calls) == []
    assert hass.states.get(DEGRADED).state == "off"


async def test_thermostat_cutoff_ends_the_manual_mode(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Safeties apply to a manual mode switched on by someone else too."""
    freezer.move_to("2026-11-18 10:00:00+01:00")
    await _setup(hass, mock_entry, switch_calls, temperature="66.0")
    hass.states.async_set(MANUAL, "on")  # external order, the element is off
    await hass.async_block_till_done()
    await _minute(hass, mock_entry, freezer, power="1.6")
    await _minute(hass, mock_entry, freezer, 2)
    assert _orders(switch_calls) == []
    await _minute(hass, mock_entry, freezer, 1)
    assert _orders(switch_calls) == [(MANUAL, "turn_off")]
    assert "Thermostat cut-off at 66.0 °C" in caplog.text
    assert mock_entry.runtime_data.controller.state.full_latch == pytest.approx(66.0)


async def test_degraded_mode_heats_before_the_appointment(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No prices: the simple rule heats for the 17:00 appointment."""
    freezer.move_to("2026-11-18 10:00:00+01:00")
    await _setup(hass, mock_entry, switch_calls, prices=False)
    # Right after a start the prices may still be loading.
    assert hass.states.get(DEGRADED).state == "off"
    await _minute(hass, mock_entry, freezer, 5)
    degraded = hass.states.get(DEGRADED)
    assert degraded.state == "on"
    assert degraded.attributes["reason"] == "no_plan"
    assert degraded.attributes["appointment"] == "Tarde"
    target = degraded.attributes["target_temperature"]
    required = float(
        hass.states.get("sensor.tankwise_tarde_required_temperature").state
    )
    assert target == pytest.approx(min(required, 68.0), abs=0.1)
    assert _orders(switch_calls) == []  # too early

    freezer.move_to("2026-11-18 16:30:00+01:00")
    await _minute(hass, mock_entry, freezer, 0)
    assert _orders(switch_calls) == [(MANUAL, "turn_on")]
    assert "Degraded mode: heating to" in caplog.text


async def test_probe_lost_is_degraded_and_at_risk(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    freezer.move_to("2026-11-18 10:00:00+01:00")
    await _setup(hass, mock_entry, switch_calls)
    events = async_capture_events(hass, EVENT_PLAN_AT_RISK)
    freezer.tick(timedelta(minutes=11))  # the probe stops reporting
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(DEGRADED).attributes["reason"] == "probe"
    at_risk = hass.states.get(AT_RISK)
    assert at_risk.state == "on"
    assert at_risk.attributes["reasons"] == ["probe_unavailable"]
    assert [event.data["new_reasons"] for event in events] == [["probe_unavailable"]]
    await _minute(hass, mock_entry, freezer)
    assert len(events) == 1  # announced once
    assert _orders(switch_calls) == []


async def test_regulator_lost_while_heating(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to("2026-11-18 14:01:00+01:00")
    await _setup(hass, mock_entry, switch_calls)
    assert _orders(switch_calls) == [(MANUAL, "turn_on")]
    events = async_capture_events(hass, EVENT_PLAN_AT_RISK)

    hass.states.async_set(MANUAL, "unavailable")
    await _minute(hass, mock_entry, freezer, power="1500")
    assert _orders(switch_calls) == [(MANUAL, "turn_on")]  # within the margin
    await _minute(hass, mock_entry, freezer, 2)
    assert (HEATER_SWITCH, "turn_off") in _orders(switch_calls)
    assert "heater switch opened as a last resort" in caplog.text
    at_risk = hass.states.get(AT_RISK)
    assert at_risk.state == "on"
    assert "regulator_unavailable" in at_risk.attributes["reasons"]
    assert events and "regulator_unavailable" in events[-1].data["new_reasons"]

    # The regulator is back: automatic, then the heater switch again.
    hass.states.async_set(HEATER_SWITCH, "off")
    hass.states.async_set(MANUAL, "on")
    await _minute(hass, mock_entry, freezer)
    orders = _orders(switch_calls)
    assert (MANUAL, "turn_off") in orders
    assert (HEATER_SWITCH, "turn_on") in orders
    assert "The regulator is back" in caplog.text
    issues = ir.async_get(hass)
    assert (
        issues.async_get_issue(DOMAIN, f"heater_switch_off_{mock_entry.entry_id}")
        is None
    )


async def test_heater_switch_off_creates_a_repair_issue(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    freezer.move_to("2026-11-18 10:00:00+01:00")
    await _setup(hass, mock_entry, switch_calls)
    issue = f"heater_switch_off_{mock_entry.entry_id}"
    hass.states.async_set(HEATER_SWITCH, "off")
    await _minute(hass, mock_entry, freezer)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue) is not None
    hass.states.async_set(HEATER_SWITCH, "on")
    await _minute(hass, mock_entry, freezer)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue) is None


async def test_full_plan_from_the_service_and_compact_attributes(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    freezer.move_to("2026-11-18 10:02:00+01:00")
    await _setup(hass, mock_entry, switch_calls, mode=Mode.SHADOW)
    response = await hass.services.async_call(
        DOMAIN, "get_plan", {}, blocking=True, return_response=True
    )
    (plan,) = response["plans"]
    assert plan["name"] == "Water heater"
    assert len(plan["slots"]) == 144
    assert {"start", "grid_kwh", "temperature", "liters"} <= set(plan["slots"][0])

    # What the recorder stores stays well below its 16 KiB limit.
    for entity_id in ("sensor.tankwise_plan", "sensor.tankwise_demand_forecast"):
        state = hass.states.get(entity_id)
        unrecorded = state.state_info["unrecorded_attributes"]
        recorded = {
            key: value
            for key, value in state.attributes.items()
            if key not in unrecorded
        }
        assert len(json.dumps(recorded, default=str)) < 4096
    assert (
        "slots"
        in hass.states.get("sensor.tankwise_demand_forecast").state_info[
            "unrecorded_attributes"
        ]
    )
