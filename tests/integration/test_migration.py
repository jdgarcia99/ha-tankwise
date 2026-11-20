"""Migration of 0.2 entries and the repair issue for missing inputs."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    DOMAIN,
)

from .conftest import (
    ENERGY,
    OCCUPANCY,
    POWER,
    PRICE,
    REGULATOR,
    TEMPERATURE,
    set_input_states,
)

V1_DATA = {
    "name": "Water heater",
    "temperature_sensor": TEMPERATURE,
    "power_sensor": POWER,
    "energy_sensor": ENERGY,
}
V1_OPTIONS = {
    "volume": 100,
    "rated_power": 1450,
    "water_region": "balearic_islands",
    "mains_water_temperature": 16.5,
    "min_useful_temperature": 32.0,
    "cutoff_power_threshold": 50,
    "cutoff_temperature": 47.0,
    "shower_energy": 1.8,
    "comfort_level": "p90",
    "price_sensor": PRICE,
    "presence_entity": OCCUPANCY,
    "surplus_sensor": "sensor.solar_surplus",
    "bottom_temperature_sensor": "sensor.tank_bottom",
    "regulator_sensor": REGULATOR,
}
V1_TANK = {
    "format": 1,
    "equivalent_temperature": 45.0,
    "loss_coefficient": 0.0086,
    "loss_windows": 6,
    "cutoff_samples": [47.0, 47.1, 46.9],
    "uncertain": False,
    "calibration_count": 9,
    "last_calibration": "2026-11-15T08:00:00+00:00",
    "last_valid_time": "2026-11-15T11:58:00+00:00",
    "last_energy": 123.4,
    "consumptions": [],
}


@pytest.fixture
def v1_entry(hass: HomeAssistant, hass_storage: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        unique_id=TEMPERATURE,
        version=1,
        data=V1_DATA,
        options=V1_OPTIONS,
    )
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"tank": V1_TANK},
    }
    return entry


async def test_v1_entry_is_migrated(
    hass: HomeAssistant,
    v1_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    showers = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{v1_entry.entry_id}_showers_available",
        config_entry=v1_entry,
    )
    set_input_states(hass)
    assert await hass.config_entries.async_setup(v1_entry.entry_id)
    await hass.async_block_till_done()

    assert v1_entry.state is ConfigEntryState.LOADED
    assert v1_entry.version == 2
    assert v1_entry.data == {
        "name": "Water heater",
        CONF_TEMPERATURE_SENSOR: TEMPERATURE,
        CONF_POWER_SENSOR: POWER,
        CONF_PRICE_SENSOR: PRICE,
    }
    assert v1_entry.options == {
        CONF_VOLUME: 100,
        "rated_power": 1500,  # 0.2 default replaced
        "water_region": "balearic_islands",
        "cutoff_power_threshold": 50,
        CONF_CUTOFF_TEMPERATURE: 68.0,  # 0.2 default replaced
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_OCCUPANCY_ENTITY: OCCUPANCY,
        CONF_EXPORT_POWER_SENSOR: "sensor.solar_surplus",
        CONF_REGULATOR_SENSOR: REGULATOR,
    }
    assert entity_registry.async_get(showers.entity_id) is None

    coordinator = v1_entry.runtime_data
    # The mains override became the number setting; learned data was kept.
    assert coordinator.settings["mains_water_temperature"] == 16.5
    assert coordinator.model.ua == pytest.approx(0.0086 * 116.3, rel=1e-3)
    assert coordinator.model.as_dict()["calibration_count"] == 9
    # Cut-offs learned around 47 °C cannot be thermostat cut-offs at 68 °C.
    assert coordinator.model.as_dict()["cutoff_samples"] == []
    assert v1_entry.minor_version == 2

    issue = issue_registry.async_get_issue(
        DOMAIN, f"missing_required_inputs_{v1_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "name": "Water heater",
        "inputs": "heater_switch, manual_mode_switch",
    }


async def test_issue_is_removed_when_inputs_are_complete(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"missing_required_inputs_{mock_entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing_required_inputs",
    )
    set_input_states(hass)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"missing_required_inputs_{mock_entry.entry_id}"
        )
        is None
    )


async def test_future_version_is_not_loaded(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=V1_DATA)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_custom_cutoff_is_kept(hass: HomeAssistant) -> None:
    """Only the 0.2 defaults are replaced, not values chosen by the user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={
            "name": "x",
            CONF_TEMPERATURE_SENSOR: TEMPERATURE,
            CONF_POWER_SENSOR: POWER,
        },
        options={CONF_CUTOFF_TEMPERATURE: 62.0, "rated_power": 2000},
    )
    entry.add_to_hass(hass)
    set_input_states(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options[CONF_CUTOFF_TEMPERATURE] == 62.0
    assert entry.options["rated_power"] == 2000
    assert entry.minor_version == 2


async def test_entity_ids_with_area_prefix_are_renamed(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """sensor.coladuria_tankwise_x becomes sensor.tankwise_x; custom IDs stay."""
    mock_entry.add_to_hass(hass)
    prefixed = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{mock_entry.entry_id}_stored_energy",
        config_entry=mock_entry,
        suggested_object_id="coladuria_tankwise_stored_energy",
    )
    custom = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{mock_entry.entry_id}_charge",
        config_entry=mock_entry,
        suggested_object_id="my_tank_charge",
    )
    assert prefixed.entity_id == "sensor.coladuria_tankwise_stored_energy"
    set_input_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get("sensor.coladuria_tankwise_stored_energy") is None
    renamed = entity_registry.async_get("sensor.tankwise_stored_energy")
    assert renamed is not None and renamed.id == prefixed.id  # same entry
    assert entity_registry.async_get(custom.entity_id) is not None
    assert hass.states.get("sensor.tankwise_stored_energy") is not None
