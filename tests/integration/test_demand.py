"""Demand profile, appointments and services in Home Assistant."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.time import ATTR_TIME
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_APPOINTMENTS,
    CONF_ENERGY_SENSOR,
    CONF_OCCUPANCY_ENTITY,
    CONF_WORKDAY_ENTITY,
    DEFAULT_PARAMETERS,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.tankwise.model.demand import SLOTS_PER_DAY, DayObservation

from .conftest import ENERGY, OCCUPANCY, set_input_states

NOW = "2026-11-18 10:00:00+01:00"  # a Wednesday
WORKDAY = "binary_sensor.workday_sensor"
MORNING = {
    "id": "a1b2c3d4",
    "name": "Ducha mañana",
    "time": "06:30",
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "liters": 60.0,
    "enabled": True,
}
EVENING = {
    "id": "e5f6a7b8",
    "name": "Tarde",
    "time": "17:00",
    "days": ["workday"],
    "liters": 120.0,
    "enabled": True,
}
DEMAND_TODAY = "sensor.tankwise_demand_today"
DEMAND_FORECAST = "sensor.tankwise_demand_forecast"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_OCCUPANCY_ENTITY: OCCUPANCY,
        CONF_APPOINTMENTS: [MORNING, EVENING],
    }


def _observed_days(first: date, days: int) -> list[DayObservation]:
    """Days with a 1.4 kWh shower at 06:30 and 2.6 kWh around 18:00."""
    result = []
    for offset in range(days):
        slots = [0.0] * SLOTS_PER_DAY
        slots[26] = 1.4  # 06:30-06:45
        slots[72] = 1.3  # 18:00-18:15
        slots[73] = 1.3
        result.append(
            DayObservation(first + timedelta(days=offset), tuple(slots), True)
        )
    return result


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, *, history: int = 0
) -> None:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature="60.0")
    hass.states.async_set(OCCUPANCY, "on")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if history:
        demand = entry.runtime_data.demand
        today = dt_util.now().date()
        for observation in _observed_days(today - timedelta(days=history), history):
            demand._observations[observation.day] = observation
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


async def test_demand_sensors(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry, history=14)

    today = hass.states.get(DEMAND_TODAY)
    assert float(today.state) == pytest.approx(4.0)
    assert today.attributes["p50"] == pytest.approx(4.0)
    # 10:00: the morning shower is over, the evening is still to come.
    assert today.attributes["remaining_p90"] == pytest.approx(2.6)
    assert today.attributes["day_type"] == "workday"
    assert today.attributes["observed_days"] == 14
    assert today.attributes["profile_status"] == "ready"

    forecast = hass.states.get(DEMAND_FORECAST)
    # 36 h from 10:00: tonight, tomorrow morning and tomorrow evening.
    assert float(forecast.state) == pytest.approx(2.6 + 4.0)
    starts = [slot["start"] for slot in forecast.attributes["slots"]]
    assert starts[0].startswith("2026-11-18T18:00")
    assert len(forecast.attributes["slots"]) == 5


async def test_unoccupied_home_has_no_demand(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry, history=14)
    hass.states.async_set(OCCUPANCY, "off")
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(hass.states.get(DEMAND_TODAY).state) == 0
    assert float(hass.states.get(DEMAND_FORECAST).state) == 0


async def test_appointment_entities(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each appointment has time, litres, active and suggested litres entities."""
    freezer.move_to(NOW)
    await _setup(hass, mock_entry, history=14)

    time_state = hass.states.get("time.tankwise_ducha_manana_time")
    assert time_state.state == "06:30:00"
    assert time_state.name == "Water heater Ducha mañana time"
    assert float(hass.states.get("number.tankwise_ducha_manana_liters").state) == 60
    assert hass.states.get("switch.tankwise_ducha_manana_enabled").state == "on"
    suggested = hass.states.get("sensor.tankwise_ducha_manana_suggested_liters")
    # 1.4 kWh in 06:30-07:30 with showers at 40 °C.
    mains = mock_entry.runtime_data.tank_params.mains_temperature
    assert float(suggested.state) == pytest.approx(
        1.4 / (0.001163 * (40 - mains)), rel=1e-3
    )
    assert suggested.attributes["days"] == 14
    assert hass.states.get("time.tankwise_tarde_time").state == "17:00:00"
    entry = entity_registry.async_get("time.tankwise_ducha_manana_time")
    assert entry.unique_id == f"{mock_entry.entry_id}_appointment_a1b2c3d4_time"


async def test_appointment_entities_update_the_options(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    await hass.services.async_call(
        "time",
        "set_value",
        {ATTR_ENTITY_ID: "time.tankwise_ducha_manana_time", ATTR_TIME: time(7, 15)},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: "number.tankwise_ducha_manana_liters", ATTR_VALUE: 80},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.tankwise_ducha_manana_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()
    morning = mock_entry.options[CONF_APPOINTMENTS][0]
    assert morning == {**MORNING, "time": "07:15", "liters": 80, "enabled": False}
    assert mock_entry.options[CONF_APPOINTMENTS][1] == EVENING
    assert hass.states.get("time.tankwise_ducha_manana_time").state == "07:15:00"
    assert hass.states.get("switch.tankwise_ducha_manana_enabled").state == "off"
    # Changing an appointment from its entities does not reload the entry.
    assert mock_entry.runtime_data is not None


async def test_entities_of_removed_appointments_are_deleted(
    hass: HomeAssistant, mock_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    await _setup(hass, mock_entry)
    assert entity_registry.async_get("switch.tankwise_tarde_enabled")
    hass.config_entries.async_update_entry(
        mock_entry, options={**mock_entry.options, CONF_APPOINTMENTS: [MORNING]}
    )
    assert await hass.config_entries.async_reload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get("switch.tankwise_tarde_enabled") is None
    assert entity_registry.async_get("switch.tankwise_ducha_manana_enabled")


async def test_diagnostic_sensors_are_disabled_by_default(
    hass: HomeAssistant, mock_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    await _setup(hass, mock_entry)
    for key in (
        "heat_loss_coefficient",
        "heater_power",
        "cutoff_temperature",
        "capacity_factor",
    ):
        entry = entity_registry.async_get(f"sensor.tankwise_{key}")
        assert (
            entry is not None
            and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        )
        assert hass.states.get(f"sensor.tankwise_{key}") is None


async def test_workday_sensor_and_check_date(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Tomorrow is asked to the workday integration; today comes from the sensor."""
    freezer.move_to(NOW)
    checked: list[date] = []

    async def check_date(call: Any) -> ServiceResponse:
        day = call.data["check_date"]
        checked.append(day)
        return {WORKDAY: {"workday": day != date(2026, 11, 19)}}  # Thu is a holiday

    hass.services.async_register(
        "workday", "check_date", check_date, supports_response=SupportsResponse.ONLY
    )
    hass.states.async_set(WORKDAY, "on")
    options = {**mock_entry.options, CONF_WORKDAY_ENTITY: WORKDAY}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        version=2,
        data=dict(mock_entry.data),
        options=options,
    )
    await _setup(hass, entry, history=14)

    assert date(2026, 11, 19) in checked and date(2026, 11, 20) in checked
    coordinator = entry.runtime_data
    assert coordinator._workdays[date(2026, 11, 19)] is False
    assert coordinator.data.readings.workday is True
    # Workday appointment: not due on the holiday, so next on Friday.
    evening = coordinator.appointments[1]
    due = evening.next_due(dt_util.now(), coordinator._workdays)
    assert due.date() == date(2026, 11, 18)
    later = datetime(2026, 11, 18, 18, 0, tzinfo=dt_util.get_default_time_zone())
    assert evening.next_due(later, coordinator._workdays).date() == date(2026, 11, 20)


async def test_reset_learning_service(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry, history=5)
    coordinator = mock_entry.runtime_data
    coordinator.model._ua = 2.0

    await hass.services.async_call(
        DOMAIN, "reset_learning", {"part": "losses"}, blocking=True
    )
    assert coordinator.model.ua == 1.0
    assert len(coordinator.demand.observations) == 5

    await hass.services.async_call(
        DOMAIN,
        "reset_learning",
        {"part": "demand", "config_entry_id": mock_entry.entry_id},
        blocking=True,
    )
    assert coordinator.demand.observations == []

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "reset_learning", {"config_entry_id": "nope"}, blocking=True
        )

    with patch.object(coordinator, "async_reset_learning", AsyncMock()) as reset:
        await hass.services.async_call(DOMAIN, "reset_learning", {}, blocking=True)
    reset.assert_awaited_once_with("all")


async def test_demand_profile_is_persisted(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, mock_entry, history=3)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]["demand"]
    assert len(stored["observations"]) == 3

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert len(mock_entry.runtime_data.demand.observations) == 3


async def test_required_temperature_and_rescue(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The evening needs more than a full tank: the rescue recharge covers it."""
    freezer.move_to(NOW)
    big_evening = {
        **EVENING,
        "liters": 150.0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "duration": 180,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        version=2,
        minor_version=2,
        data=dict(mock_entry.data),
        options={**mock_entry.options, CONF_APPOINTMENTS: [MORNING, big_evening]},
    )
    await _setup(hass, entry, history=14)
    coordinator = entry.runtime_data
    mains = coordinator.data.tank.mains_temperature

    morning = hass.states.get("sensor.tankwise_ducha_manana_required_temperature")
    assert float(morning.state) == pytest.approx(38 + 60 * (40 - mains) / 100, abs=0.05)
    assert morning.attributes["liters_source"] == "minimum"
    assert morning.attributes["feasible"] is True

    evening = hass.states.get("sensor.tankwise_tarde_required_temperature")
    assert float(evening.state) == pytest.approx(
        38 + 150 * (40 - mains) / 100, abs=0.05
    )
    assert evening.attributes["feasible"] is False  # beyond a full tank...
    # ... but the rescue recharge inside the block covers the deficit.
    assert hass.states.get("binary_sensor.tankwise_plan_at_risk").state == "off"
    plan = coordinator.planner.plan
    assert plan is not None and plan.rescue_kwh > 0


async def test_strict_mode_is_at_risk(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Without rescue (strict mode) the same appointment puts the plan at risk."""
    freezer.move_to(NOW)
    big_evening = {
        **EVENING,
        "liters": 150.0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        version=2,
        minor_version=2,
        data=dict(mock_entry.data),
        options={
            **mock_entry.options,
            CONF_APPOINTMENTS: [big_evening],
            "rescue": False,
        },
    )
    await _setup(hass, entry)
    risk = hass.states.get("binary_sensor.tankwise_plan_at_risk")
    assert risk.state == "on"
    assert risk.attributes["appointments"][0]["name"] == "Tarde"
    assert risk.attributes["cutoff_temperature"] == 68.0

    # Suspending the appointment clears the risk.
    await hass.services.async_call(
        "switch",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.tankwise_tarde_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.tankwise_plan_at_risk").state == "off"


async def test_plan_not_at_risk_with_nobody_home(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    big = {
        **EVENING,
        "liters": 150.0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        version=2,
        minor_version=2,
        data=dict(mock_entry.data),
        options={**mock_entry.options, CONF_APPOINTMENTS: [big], "rescue": False},
    )
    await _setup(hass, entry)
    assert hass.states.get("binary_sensor.tankwise_plan_at_risk").state == "on"
    hass.states.async_set(OCCUPANCY, "off")
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.tankwise_plan_at_risk").state == "off"
