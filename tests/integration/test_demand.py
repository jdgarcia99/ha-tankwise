"""Tests for the demand model integration: failures, workdays, import."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_MANUAL_MODE_ENTITY,
    CONF_PERSON_AUTOMATIONS,
    CONF_WORKDAY_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.tankwise.model.demand import DayType, FailureReason

from .conftest import TEMPERATURE, set_input_states

MANUAL = "switch.freeds_57d4_pwm_manual_mode"
BUTTON = "automation.zigbee_maneja_termo_desde_pulsador"
OTHER = "automation.solar_surplus"
WORKDAY = "binary_sensor.workday_sensor"
NEXT_DRAW = "sensor.water_heater_next_expected_draw"
PROFILE = "sensor.water_heater_demand_profile"
MONDAY_NOON = "2026-11-16 12:00:00+00:00"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options with the manual mode entity and the push button automation."""
    return {
        **DEFAULT_PARAMETERS,
        CONF_MANUAL_MODE_ENTITY: MANUAL,
        CONF_PERSON_AUTOMATIONS: [BUTTON],
    }


async def _switch_on(hass: HomeAssistant, context: Context) -> None:
    hass.states.async_set(MANUAL, "on", context=context)
    await hass.async_block_till_done()


def _automation_run(hass: HomeAssistant, entity_id: str) -> Context:
    context = Context(parent_id=Context().id)
    hass.bus.async_fire(
        "automation_triggered",
        {"name": entity_id, "entity_id": entity_id},
        context=context,
    )
    return context


@pytest.mark.parametrize("actor", ["user", "button", "button_via_script"])
async def test_manual_mode_by_a_person_is_a_failure(
    hass: HomeAssistant, mock_entry: MockConfigEntry, actor: str
) -> None:
    """A person (directly or through a listed automation) switching it on."""
    set_input_states(hass)
    hass.states.async_set(MANUAL, "off")
    await _setup(hass, mock_entry)

    if actor == "user":
        context = Context(user_id="a-person")
    else:
        context = _automation_run(hass, BUTTON)
        await hass.async_block_till_done()
        if actor == "button_via_script":
            script = Context(parent_id=context.id)
            hass.bus.async_fire(
                "script_started",
                {"name": "x", "entity_id": "script.heat_now"},
                context=script,
            )
            await hass.async_block_till_done()
            context = Context(parent_id=script.id)
    await _switch_on(hass, context)

    failures = mock_entry.runtime_data.demand.failures
    assert len(failures) == 1
    assert failures[0].reason is FailureReason.MANUAL_MODE


async def test_manual_mode_by_other_automation_is_ignored(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Automations not acting for a person do not count."""
    set_input_states(hass)
    hass.states.async_set(MANUAL, "off")
    await _setup(hass, mock_entry)

    context = _automation_run(hass, OTHER)
    await hass.async_block_till_done()
    await _switch_on(hass, context)
    await _switch_on(hass, Context())  # no user, unknown origin
    assert mock_entry.runtime_data.demand.failures == []


async def test_manual_mode_staying_on_counts_once(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    set_input_states(hass)
    hass.states.async_set(MANUAL, "off")
    await _setup(hass, mock_entry)
    person = Context(user_id="a-person")
    await _switch_on(hass, person)
    hass.states.async_set(MANUAL, "on", {"changed": True}, context=person)
    await hass.async_block_till_done()
    assert len(mock_entry.runtime_data.demand.failures) == 1


async def test_detected_draw_feeds_the_demand_model(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A strong shower detected live is recorded, and it is not a failure."""
    freezer.move_to(MONDAY_NOON)
    set_input_states(hass, temperature="46.0", power="0")
    await _setup(hass, mock_entry)

    temperatures = [46.0] * 15 + [46.0 - 1.3 * i for i in range(1, 13)] + [30.4] * 15
    for temperature in temperatures:
        hass.states.async_set(
            TEMPERATURE, f"{temperature:.1f}", {"unit_of_measurement": "°C"}
        )
        freezer.tick(UPDATE_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    draws = mock_entry.runtime_data.demand.draws
    assert len(draws) == 1
    assert draws[0].energy_kwh == pytest.approx(1.8, rel=0.2)
    assert not draws[0].failure
    assert draws[0].day_type is DayType.WORKDAY


@pytest.mark.parametrize(
    "entry_options", [{**DEFAULT_PARAMETERS, CONF_WORKDAY_SENSOR: WORKDAY}]
)
async def test_workday_sensor_marks_holidays(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Today from the sensor state; other dates from workday.check_date."""
    freezer.move_to(MONDAY_NOON)
    calls: list[str] = []

    async def check_date(call: ServiceCall) -> ServiceResponse:
        calls.append(str(call.data["check_date"]))
        return {WORKDAY: {"workday": False}}

    hass.services.async_register(
        "workday", "check_date", check_date, supports_response=SupportsResponse.ONLY
    )
    hass.states.async_set(WORKDAY, "off")
    set_input_states(hass)
    await _setup(hass, mock_entry)

    coordinator = mock_entry.runtime_data
    today = dt_util.now().date()
    assert coordinator.data.demand.today_type is DayType.SUNDAY_HOLIDAY
    assert coordinator.day_type_of(today + timedelta(days=1)) is (
        DayType.SUNDAY_HOLIDAY
    )
    assert calls
    assert hass.states.get(PROFILE).attributes["today_type"] == "sunday_holiday"


async def test_demand_sensors(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Cold start: learning profile and a next draw from the default profile."""
    freezer.move_to(MONDAY_NOON)
    set_input_states(hass)
    await _setup(hass, mock_entry)

    profile = hass.states.get(PROFILE)
    assert profile.state == "learning"
    assert profile.attributes["days_with_data"] == 0
    assert profile.attributes["shower_energy_kwh"] == 1.8

    next_draw = hass.states.get(NEXT_DRAW)
    moment = dt_util.parse_datetime(next_draw.state)
    assert moment is not None
    assert moment > dt_util.utcnow()
    assert next_draw.attributes["energy_kwh"] > 0

    # Demand sensors stay available in safe mode.
    hass.states.async_set(TEMPERATURE, "unavailable")
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(PROFILE).state == "learning"
    assert hass.states.get("sensor.water_heater_useful_energy").state == "unavailable"


def _history_csv(first_day: datetime, days: int) -> str:
    """Minute readings: idle at 46 °C, a strong shower at 08:00, reheating."""
    lines = ["timestamp,temperature,power,energy,pwm,manual_mode"]
    energy = 100.0
    for day in range(days):
        temperature = 46.0
        start = first_day + timedelta(days=day)
        for minute in range(24 * 60):
            power = 0.0
            if 8 * 60 <= minute < 8 * 60 + 12:
                temperature -= 1.3
            elif minute >= 10 * 60 and temperature < 46.0:
                power = 1450.0
                energy += 1.45 / 60
                temperature = min(46.0, temperature + 0.208)
            moment = start + timedelta(minutes=minute)
            lines.append(
                f"{moment.strftime('%Y-%m-%d %H:%M:%S')},{temperature:.2f},"
                f"{power},{energy:.4f},{100 if power else 0},off"
            )
    return "\n".join(lines) + "\n"


async def _import(hass: HomeAssistant, entry: MockConfigEntry, path: str) -> Any:
    return await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"config_entry_id": entry.entry_id, "file_path": path},
        blocking=True,
        return_response=True,
    )


async def test_import_history(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    tmp_path: Path,
) -> None:
    """The CSV is replayed through the tank model; importing twice is safe."""
    freezer.move_to(MONDAY_NOON)
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    set_input_states(hass)
    await _setup(hass, mock_entry)

    first_day = datetime(2026, 11, 2)
    csv_file = tmp_path / "history.csv"
    csv_file.write_text(_history_csv(first_day, 3), encoding="utf-8")

    response = await _import(hass, mock_entry, str(csv_file))
    assert response["imported_days"] == 3
    assert response["draws"] == 3
    assert response["failures"] == 0
    demand = mock_entry.runtime_data.demand
    assert all(draw.imported for draw in demand.draws)

    again = await _import(hass, mock_entry, str(csv_file))
    assert again["imported_days"] == 0
    assert again["skipped_days"] == 3
    assert len(demand.draws) == 3


@pytest.mark.parametrize(
    ("name", "allowed", "key"),
    [
        ("history.txt", True, "invalid_file_type"),
        ("missing.csv", True, "file_not_found"),
        ("history.csv", False, "path_not_allowed"),
    ],
)
async def test_import_history_rejects_bad_paths(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    tmp_path: Path,
    name: str,
    allowed: bool,
    key: str,
) -> None:
    hass.config.allowlist_external_dirs = {str(tmp_path)} if allowed else set()
    set_input_states(hass)
    await _setup(hass, mock_entry)
    target = tmp_path / name
    if name != "missing.csv":
        target.write_text("timestamp,temperature,power,energy\n", encoding="utf-8")

    with pytest.raises(ServiceValidationError) as err:
        await _import(hass, mock_entry, str(target))
    assert err.value.translation_key == key


async def test_import_history_rejects_invalid_csv(
    hass: HomeAssistant, mock_entry: MockConfigEntry, tmp_path: Path
) -> None:
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    set_input_states(hass)
    await _setup(hass, mock_entry)
    target = tmp_path / "bad.csv"
    target.write_text("foo,bar\n1,2\n", encoding="utf-8")

    with pytest.raises(ServiceValidationError) as err:
        await _import(hass, mock_entry, str(target))
    assert err.value.translation_key == "invalid_file"
