"""Heating plan in Home Assistant (phase 3: shadow mode)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_APPOINTMENTS,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
)

from .conftest import (
    ENERGY,
    EXPORT_POWER,
    INJECTION,
    MANUAL,
    PRICE,
    set_input_states,
)

NOW = "2026-11-18 10:02:00+01:00"  # a Wednesday
PLAN = "sensor.tankwise_plan"
NEXT = "sensor.tankwise_next_heating"
MODE = "select.tankwise_mode"
EVENING = {
    "id": "e5f6a7b8",
    "name": "Tarde",
    "time": "17:00",
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "liters": 60.0,
    "enabled": True,
    "duration": 180,
}


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


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, temperature: str = "45.0"
) -> None:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature=temperature)
    hass.states.async_set(PRICE, "0.20", _prices(0.20, (14, 15), 0.08))
    hass.states.async_set(INJECTION, "0.05", _prices(0.05, (), 0.05))
    hass.states.async_set(EXPORT_POWER, "0", {"unit_of_measurement": "W"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 60
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_plan_in_shadow_mode(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """45 °C at 10:00, block at 17:00: heat in the cheap 14:00-16:00 hours."""
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)

    assert hass.states.get(MODE).state == "shadow"
    plan = hass.states.get(PLAN)
    assert float(plan.state) > 0
    assert plan.attributes["mode"] == "shadow"
    assert plan.attributes["tomorrow_prices"] is False
    # Compact summary only: the slots would exceed the recorder size limit.
    assert "slots" not in plan.attributes
    # 17:00 today and tomorrow are inside the 36-hour horizon.
    assert [block["name"] for block in plan.attributes["blocks"]] == ["Tarde"] * 2
    assert dt_util.as_local(
        dt_util.parse_datetime(plan.attributes["heatings"][0]["start"])
    ).hour in (14, 15)
    assert plan.attributes["at_risk"] is False

    next_heating = hass.states.get(NEXT)
    start = dt_util.parse_datetime(next_heating.state)
    assert dt_util.as_local(start).hour in (14, 15)
    assert next_heating.attributes["cost"] == pytest.approx(
        next_heating.attributes["energy_kwh"] * 0.08, abs=1e-3
    )

    predicted = hass.states.get("sensor.tankwise_tarde_predicted_temperature")
    required = float(predicted.attributes["required_temperature"])
    assert float(predicted.state) >= required - 0.5
    assert predicted.attributes["at_risk"] is False
    assert hass.states.get("binary_sensor.tankwise_planned_grid_heating").state == "off"


async def test_mode_off_and_holiday(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: MODE, "option": "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLAN).state == "unknown"
    assert hass.states.get(NEXT).state == "unknown"
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]["control"]
    assert stored["mode"] == "off"

    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: MODE, "option": "holiday"},
        blocking=True,
    )
    await hass.async_block_till_done()
    plan = hass.states.get(PLAN)
    assert float(plan.state) == 0
    assert plan.attributes["grid_kwh"] == 0


async def test_compensation_cap_and_replan_service(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    await hass.services.async_call(
        "switch",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.tankwise_compensation_cap"},
        blocking=True,
    )
    await hass.async_block_till_done()
    planner = mock_entry.runtime_data.planner
    assert planner.compensation_cap
    assert set(planner.prices.surplus) == {0.0}
    assert hass.states.get(PLAN).attributes["reason"] == "compensation_cap"

    await hass.services.async_call(DOMAIN, "replan", {}, blocking=True)
    assert hass.states.get(PLAN).attributes["reason"] == "service"
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.tankwise_replan"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLAN).attributes["reason"] == "button"


async def test_real_cost_from_grid_and_surplus(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    # 0.5 kWh from the grid at 0.20 €/kWh.
    set_input_states(hass, temperature="47.0", energy="123.9", power="1500")
    hass.states.async_set(PRICE, "0.20", _prices(0.20, (14, 15), 0.08))
    await _tick(hass, freezer)
    # 0.5 kWh while exporting: surplus at 0.05 €/kWh.
    hass.states.async_set(EXPORT_POWER, "800", {"unit_of_measurement": "W"})
    set_input_states(hass, temperature="49.0", energy="124.4", power="1500")
    hass.states.async_set(PRICE, "0.20", _prices(0.20, (14, 15), 0.08))
    await _tick(hass, freezer)

    assert float(hass.states.get("sensor.tankwise_cost_today").state) == pytest.approx(
        0.125
    )
    assert float(hass.states.get("sensor.tankwise_cost_month").state) == pytest.approx(
        0.125
    )
    totals = mock_entry.runtime_data.data.totals_today
    assert totals.real_grid_kwh == pytest.approx(0.5)
    assert totals.real_solar_kwh == pytest.approx(0.5)


async def test_planned_cost_is_committed_slot_by_slot(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """At 14:00 the plan heats: its slot cost becomes the planned cost."""
    freezer.move_to("2026-11-18 13:58:00+01:00")
    await _setup(hass, mock_entry, temperature="40.0")
    for _ in range(4):
        await _tick(hass, freezer)
    # The coordinator jitters its refreshes: make sure one ran after 14:00.
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    slot = mock_entry.runtime_data.planner.current_slot(dt_util.utcnow())
    assert slot is not None and slot.grid_kwh > 0
    assert hass.states.get("binary_sensor.tankwise_planned_grid_heating").state == "on"
    planned = float(hass.states.get("sensor.tankwise_planned_cost_today").state)
    assert planned == pytest.approx(slot.cost, rel=0.2)
    savings = float(hass.states.get("sensor.tankwise_estimated_savings_month").state)
    assert savings == pytest.approx(-planned, abs=0.01)  # nothing really heated


async def test_external_order_against_the_plan_is_logged(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    hass.states.async_set(MANUAL, "on")  # the plan does not heat at 10:00
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert hass.states.get(PLAN).attributes["deviations_today"] == 1
    assert "manual mode was switched on outside the plan" in caplog.text


async def test_no_plan_without_prices(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry)
    hass.states.async_set(PRICE, "unavailable", {})
    mock_entry.runtime_data.planner.request("test")
    await _tick(hass, freezer)
    assert hass.states.get(PLAN).state == "unknown"
