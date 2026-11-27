"""Solar forecast, selling the surplus, power request and anti-legionella."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_capture_events,
    mock_integration,
    mock_platform,
)

from custom_components.tankwise.const import (
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_REGULATOR_OUTPUT_SWITCH,
    CONF_SOLAR_FORECAST_ENTRIES,
    DEFAULT_PARAMETERS,
    EVENT_POWER_REQUEST,
)
from custom_components.tankwise.model.compensation import CompensationTracker
from custom_components.tankwise.model.planning import Mode
from custom_components.tankwise.solar_forecast import (
    async_forecast_entries,
    async_hourly_forecast,
)

from .conftest import (
    ENERGY,
    EXPORT_POWER,
    GRID_EXPORT,
    GRID_IMPORT,
    INJECTION,
    OUTPUT,
    PRICE,
    set_input_states,
)

DAY = "2026-11-18"


def _sunny_day(peak_wh: float) -> dict[str, float]:
    """Forecast.Solar style: Wh of the hour ending at each timestamp."""
    shape = {9: 0.2, 10: 0.5, 11: 0.8, 12: 1.0, 13: 1.0, 14: 0.8, 15: 0.5, 16: 0.2}
    hours = {f"{DAY}T08:05:00+01:00": 0.0}
    for hour, share in shape.items():
        hours[f"{DAY}T{hour:02d}:00:00+01:00"] = peak_wh * share
    return hours


@pytest.fixture
async def forecast_entries(hass: HomeAssistant) -> list[str]:
    """Two config entries of an integration with a solar forecast."""
    forecasts = {"plane_1": _sunny_day(1500), "plane_2": _sunny_day(1000)}

    async def _get(_hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
        return {"wh_hours": forecasts[entry_id]} if entry_id in forecasts else None

    mock_integration(hass, MockModule("forecast_solar"))
    mock_platform(
        hass, "forecast_solar.energy", SimpleNamespace(async_get_solar_forecast=_get)
    )
    for entry_id in forecasts:
        entry = MockConfigEntry(
            domain="forecast_solar", entry_id=entry_id, title="Roof"
        )
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.LOADED)
    return list(forecasts)


@pytest.fixture
def entry_options(forecast_entries: list[str]) -> dict[str, Any]:
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_INJECTION_PRICE_SENSOR: INJECTION,
        CONF_EXPORT_POWER_SENSOR: EXPORT_POWER,
        CONF_GRID_EXPORT_SENSOR: GRID_EXPORT,
        CONF_GRID_IMPORT_SENSOR: GRID_IMPORT,
        CONF_REGULATOR_OUTPUT_SWITCH: OUTPUT,
        CONF_SOLAR_FORECAST_ENTRIES: forecast_entries,
    }


def _hourly(value: float, **hours: float) -> dict[str, Any]:
    attributes = {f"price_{hour:02d}h": value for hour in range(24)}
    attributes |= {f"price_{key[1:]}h": price for key, price in hours.items()}
    attributes["unit_of_measurement"] = "€/kWh"
    return attributes


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    injection: dict[str, Any],
    buy: dict[str, Any],
    temperature: str = "45.0",
    mode: Mode = Mode.AUTO,
) -> list[ServiceCall]:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature=temperature)
    hass.states.async_set(PRICE, "0.20", buy)
    hass.states.async_set(INJECTION, "0.05", injection)
    hass.states.async_set(EXPORT_POWER, "0", {"unit_of_measurement": "W"})
    hass.states.async_set(
        GRID_EXPORT,
        "800.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    hass.states.async_set(OUTPUT, "on")
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


async def test_forecast_entries_are_listed_and_summed(
    hass: HomeAssistant, forecast_entries: list[str]
) -> None:
    entries = await async_forecast_entries(hass)
    assert [entry_id for entry_id, _label in entries] == forecast_entries
    tz = dt_util.get_time_zone("Europe/Madrid")
    total = await async_hourly_forecast(hass, forecast_entries, tz)
    assert total is not None
    noon = datetime(2026, 11, 18, 12, tzinfo=tz)
    # The 13:00 value (hour ending at 13:00) belongs to 12:00-13:00.
    assert total[noon] == pytest.approx(2.5)
    assert await async_hourly_forecast(hass, ["missing"], tz) is None


async def test_options_flow_offers_the_forecast_entries(
    hass: HomeAssistant, mock_entry: MockConfigEntry, forecast_entries: list[str]
) -> None:
    await _setup(hass, mock_entry, injection=_hourly(0.05), buy=_hourly(0.20))
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "inputs"}
    )
    assert "solar_forecast_entries" in [
        str(key) for key in result["data_schema"].schema
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ENERGY_SENSOR: ENERGY, CONF_SOLAR_FORECAST_ENTRIES: ["plane_2"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options[CONF_SOLAR_FORECAST_ENTRIES] == ["plane_2"]


async def test_plan_counts_on_the_surplus_and_requests_power(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sunny day, surplus worth little: no grid heating, power requested."""
    freezer.move_to(f"{DAY} 11:05:00+01:00")
    events = async_capture_events(hass, EVENT_POWER_REQUEST)
    await _setup(hass, mock_entry, injection=_hourly(0.02), buy=_hourly(0.20))
    planner = mock_entry.runtime_data.planner
    assert planner.solar_forecast is not None
    plan = planner.plan
    assert plan is not None
    assert sum(slot.solar_kwh for slot in plan.slots) > 2.0
    assert plan.grid_kwh == pytest.approx(0.0, abs=0.4)
    state = hass.states.get("sensor.tankwise_power_request")
    assert float(state.state) > 0
    assert state.attributes["priority"] in ("high", "low")
    assert events and events[-1].data["power"] == float(state.state)


async def test_surplus_is_sold_when_injection_pays_more(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Injection 0.30 €/kWh at noon, grid 0.05 at night: sell, heat at night."""
    freezer.move_to(f"{DAY} 12:05:00+01:00")
    calls = await _setup(
        hass,
        mock_entry,
        injection=_hourly(0.02, h11=0.30, h12=0.30, h13=0.30, h14=0.30),
        buy=_hourly(0.20, h00=0.05, h01=0.05, h02=0.05, h03=0.05),
        temperature="50.0",
    )
    slot = mock_entry.runtime_data.planner.current_slot(dt_util.utcnow())
    assert slot is not None and not slot.solar_allowed
    assert [(call.data["entity_id"], call.service) for call in calls] == [
        (OUTPUT, "turn_off")
    ]
    assert "Selling the solar surplus (0.300 €/kWh" in caplog.text

    # Back to shadow mode: the output is given back.
    hass.states.async_set(OUTPUT, "off")
    await mock_entry.runtime_data.async_set_mode(Mode.SHADOW)
    await hass.async_block_till_done()
    assert (OUTPUT, "turn_on") in [
        (call.data["entity_id"], call.service) for call in calls
    ]


def _fill_month(
    tracker: CompensationTracker, *, export_kwh: float, now: datetime
) -> None:
    """The month so far: 0.4 kWh/h imported at 0.20, ``export_kwh`` in the sun."""
    tz = dt_util.get_time_zone("Europe/Madrid")
    hour = datetime(2026, 11, 1, tzinfo=tz)
    while hour < now - timedelta(hours=1):
        sunny = 10 <= hour.hour < 16
        tracker.add_hour(
            dt_util.as_utc(hour), 0.4, export_kwh if sunny else 0.0, 0.20, 0.15
        )
        hour += timedelta(hours=1)


async def test_automatic_compensation_cap(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Below the cap the surplus is sold; over it, the plan takes it for free."""
    freezer.move_to(f"{DAY} 12:05:00+01:00")
    calls = await _setup(
        hass,
        mock_entry,
        injection=_hourly(0.02, h11=0.30, h12=0.30, h13=0.30, h14=0.30),
        buy=_hourly(0.20, h00=0.05, h01=0.05, h02=0.05, h03=0.05),
        temperature="50.0",
    )
    coordinator = mock_entry.runtime_data
    planner = coordinator.planner
    margin = "sensor.tankwise_compensation_margin"

    # A month below the cap: 1 kWh/h exported in the sun, 0.4 kWh/h imported.
    _fill_month(planner.compensation, export_kwh=1.0, now=dt_util.now())
    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(margin)
    assert float(state.state) > 0
    assert state.attributes["free_surplus"] == 0
    assert state.attributes["period_start"].startswith("2026-11-01T00:00:00")
    assert state.attributes["energy_term_factor"] == 1.0
    assert float(hass.states.get("sensor.tankwise_compensation_period").state) > 0
    assert float(hass.states.get("sensor.tankwise_energy_term_period").state) > 0
    slot = planner.current_slot(dt_util.utcnow())
    assert slot is not None and not slot.solar_allowed  # still sold
    assert calls[-1].service == "turn_off" and calls[-1].data["entity_id"] == OUTPUT
    hass.states.async_set(OUTPUT, "off")

    # A month over the cap from the middle: 6 kWh/h exported in the sun since
    # the 10th. The surplus over the cap is free: the plan takes it.
    planner.compensation = CompensationTracker()
    _fill_month(planner.compensation, export_kwh=1.0, now=dt_util.now())
    for start, totals in planner.compensation.hours.items():
        if start >= datetime(2026, 11, 10, tzinfo=UTC) and totals.export_kwh:
            totals.export_kwh = 6.0
            totals.compensation_eur = 6.0 * 0.15
    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(margin)
    assert float(state.state) < 0
    assert state.attributes["free_surplus"] == pytest.approx(-float(state.state))
    assert state.attributes["margin_now"] < 0
    assert planner.plan_reason == "compensation"
    slot = planner.current_slot(dt_util.utcnow())
    assert slot is not None and slot.solar_allowed and slot.solar_kwh > 0
    assert calls[-1].service == "turn_on" and calls[-1].data["entity_id"] == OUTPUT

    # The switch still forces the cap by hand (all the surplus free).
    await coordinator.async_set_compensation_cap(True)
    await hass.async_block_till_done()
    assert hass.states.get(margin).attributes["free_surplus"] == 0


async def test_anti_legionella_is_planned_when_due(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(f"{DAY} 09:05:00+01:00")
    await _setup(
        hass,
        mock_entry,
        injection=_hourly(0.02),
        buy=_hourly(0.20),
        mode=Mode.SHADOW,
    )
    coordinator = mock_entry.runtime_data
    sensor = "sensor.tankwise_last_anti_legionella"
    assert hass.states.get("switch.tankwise_anti_legionella").state == "on"
    assert hass.states.get(sensor).attributes["planned_at"] is None

    coordinator.planner.legionella.first_seen = dt_util.utcnow() - timedelta(days=2)
    await coordinator.async_replan("test")
    await hass.async_block_till_done()
    attributes = hass.states.get(sensor).attributes
    assert attributes["planned_at"] is not None
    planned = dt_util.parse_datetime(attributes["planned_at"])
    # On the sunny day the cycle goes where the surplus is.
    assert 10 <= dt_util.as_local(planned).hour <= 17
    assert attributes["planned_temperature"] == 61.5

    # (switch.turn_off is replaced by the recorder of orders in these tests)
    await coordinator.async_set_anti_legionella(False)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).attributes["planned_at"] is None
