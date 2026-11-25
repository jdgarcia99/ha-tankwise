"""Maintenance window in Home Assistant: entity, margin, restarts and outages."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_APPOINTMENTS,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MAINTENANCE_ENTITY,
    DEFAULT_PARAMETERS,
)

from .conftest import ENERGY, EXPORT_POWER, INJECTION, MANUAL, POWER, TEMPERATURE
from .test_control import EVENING, _minute, _setup

SCRIPT = "script.update_esphome_devices"
UPTIME = "sensor.tank_probe_uptime"
MAINTENANCE = "binary_sensor.tankwise_maintenance"
DEGRADED = "binary_sensor.tankwise_degraded_mode"
AT_RISK = "binary_sensor.tankwise_plan_at_risk"
PLAN = "sensor.tankwise_plan"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_INJECTION_PRICE_SENSOR: INJECTION,
        CONF_EXPORT_POWER_SENSOR: EXPORT_POWER,
        CONF_APPOINTMENTS: [EVENING],
        CONF_MAINTENANCE_ENTITY: SCRIPT,
    }


@pytest.fixture
def switch_calls() -> list[list[ServiceCall]]:
    return []


def _probe_on_a_device(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The probe and an uptime sensor (seconds) on the same ESPHome device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "termo_sensor")},
        name="Termo sensor",
    )
    registry = er.async_get(hass)
    for unique_id, entity_id in (("probe", TEMPERATURE), ("uptime", UPTIME)):
        registry.async_get_or_create(
            "sensor",
            "esphome",
            unique_id,
            suggested_object_id=entity_id.split(".")[1],
            device_id=device.id,
            config_entry=entry,
        )
    hass.states.async_set(UPTIME, "31000", {"unit_of_measurement": "s"})


async def _start(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    calls: list,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-25 09:50:00+01:00")
    hass.states.async_set(SCRIPT, "off")
    entry.add_to_hass(hass)
    _probe_on_a_device(hass, entry)
    await _setup(hass, entry, calls)


async def test_script_running_opens_the_window(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    await _start(hass, mock_entry, switch_calls, freezer)
    assert hass.states.get(MAINTENANCE).state == "off"
    hass.states.async_set(SCRIPT, "on")  # the update of all ESPHome devices runs
    await _minute(hass, mock_entry, freezer)
    state = hass.states.get(MAINTENANCE)
    assert state.state == "on"
    assert state.attributes["source"] == "entity" and state.attributes["until"] is None
    assert hass.states.get(PLAN).attributes["maintenance"] == "entity"
    # The probe stops reporting during the update: not degraded, plan not at risk
    freezer.tick(timedelta(minutes=11))
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(DEGRADED).state == "off"
    assert hass.states.get(AT_RISK).state == "off"


async def test_margin_after_the_script(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    await _start(hass, mock_entry, switch_calls, freezer)
    hass.states.async_set(SCRIPT, "on")
    await _minute(hass, mock_entry, freezer)
    hass.states.async_set(SCRIPT, "off")  # 09:56: it ends, the devices reboot after
    await _minute(hass, mock_entry, freezer, 5)  # 10:01, the Shelly without response
    state = hass.states.get(MAINTENANCE)
    assert state.state == "on" and state.attributes["until"] is not None
    await _minute(hass, mock_entry, freezer, 5)  # past the 10 min margin
    assert hass.states.get(MAINTENANCE).state == "off"
    assert hass.states.get(PLAN).attributes["maintenance"] is None
    # Now a probe that stops reporting is a real problem
    freezer.tick(timedelta(minutes=11))
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(DEGRADED).attributes["reason"] == "probe"


async def test_restart_detected_from_the_uptime_sensor(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={
            k: v for k, v in mock_entry.options.items() if k != CONF_MAINTENANCE_ENTITY
        },
    )
    freezer.move_to("2026-11-25 09:50:00+01:00")
    _probe_on_a_device(hass, mock_entry)
    await _setup(hass, mock_entry, switch_calls)
    await _minute(hass, mock_entry, freezer)
    assert hass.states.get(MAINTENANCE).state == "off"
    hass.states.async_set(UPTIME, "15", {"unit_of_measurement": "s"})  # it restarted
    await _minute(hass, mock_entry, freezer)
    state = hass.states.get(MAINTENANCE)
    assert state.state == "on"
    assert state.attributes["source"] == "restart"
    assert state.attributes["devices"] == ["Termo sensor"]
    await _minute(hass, mock_entry, freezer, 10)
    assert hass.states.get(MAINTENANCE).state == "off"


@pytest.mark.parametrize(("minutes", "maintenance"), [(5, True), (20, False)])
async def test_device_unavailable(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
    minutes: int,
    maintenance: bool,
) -> None:
    """Back within 15 minutes it was maintenance; a longer outage is not."""
    await _start(hass, mock_entry, switch_calls, freezer)
    attributes = hass.states.get(TEMPERATURE).attributes
    hass.states.async_set(TEMPERATURE, "unavailable", attributes)
    freezer.tick(timedelta(minutes=minutes))
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(MAINTENANCE)
    assert (state.state == "on") is maintenance
    if maintenance:
        assert state.attributes["source"] == "unavailable"
        assert hass.states.get(DEGRADED).state == "off"
    else:
        assert hass.states.get(DEGRADED).attributes["reason"] == "probe"
    hass.states.async_set(TEMPERATURE, "40.0", attributes)
    await _minute(hass, mock_entry, freezer)
    assert (hass.states.get(MAINTENANCE).state == "on") is maintenance


async def test_heater_power_is_not_learned_in_the_window(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    switch_calls: list,
) -> None:
    await _start(hass, mock_entry, switch_calls, freezer)
    coordinator = mock_entry.runtime_data
    hass.states.async_set(MANUAL, "on")
    hass.states.async_set(SCRIPT, "on")
    before = coordinator.model.as_dict()["heater_power_samples"]
    for _ in range(5):
        await _minute(hass, mock_entry, freezer, power="1500")
    assert coordinator.model.as_dict()["heater_power_samples"] == before
    hass.states.async_set(SCRIPT, "off")
    await _minute(hass, mock_entry, freezer, 11, power="1500")
    for _ in range(3):
        await _minute(hass, mock_entry, freezer, power="1500")
    assert coordinator.model.as_dict()["heater_power_samples"] > before
    assert hass.states.get(POWER) is not None
