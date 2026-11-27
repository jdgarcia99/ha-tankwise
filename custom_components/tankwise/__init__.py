"""TankWise: full control of an electric water heater for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import (
    CONF_APPOINTMENTS,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_REGULATOR_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CUTOFF_TEMPERATURE,
    DEFAULT_RATED_POWER,
    DOMAIN,
    ISSUE_MISSING_INPUTS,
    REQUIRED_INPUT_KEYS,
    SETTING_MAINS_WATER_TEMPERATURE,
)
from .coordinator import TankWiseConfigEntry, TankWiseCoordinator
from .entity import tankwise_entity_id
from .model.appointments import load_appointments
from .services import async_setup_services
from .storage import TankWiseStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Version 1 (0.2.x) option keys renamed in version 2.
_V1_RENAMED_OPTIONS = {
    "presence_entity": CONF_OCCUPANCY_ENTITY,
    "surplus_sensor": CONF_EXPORT_POWER_SENSOR,
    "solar_forecast_sensor": CONF_SOLAR_FORECAST_SENSOR,
    "regulator_sensor": CONF_REGULATOR_SENSOR,
}
_V1_KEPT_PARAMETERS = (
    CONF_VOLUME,
    CONF_RATED_POWER,
    CONF_WATER_REGION,
    CONF_CUTOFF_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD,
)
_V1_REMOVED_SENSORS = ("showers_available",)
# Version 0.2 defaults that no longer fit (initial values, learned later).
_V1_DEFAULT_CUTOFF_TEMPERATURE = 47.0
_V1_DEFAULT_RATED_POWER = 1450


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (they work with any loaded entry)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Set up TankWise from a config entry."""
    _async_update_missing_inputs_issue(hass, entry)
    _async_remove_stale_appointment_entities(hass, entry)
    _async_normalise_entity_ids(hass, entry)

    coordinator = TankWiseCoordinator(hass, entry)
    integration = await async_get_integration(hass, DOMAIN)
    coordinator.sw_version = str(integration.version) if integration.version else None
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_listeners()

    # The tank model must keep running even when no entity is listening
    # (e.g. all entities disabled), so keep the coordinator polling.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if coordinator.needs_bootstrap:
        coordinator.async_start_bootstrap()
    coordinator.async_start_compensation_import()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Unload a config entry, saving the model state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save_now()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
    """Delete the persisted state and repair issues when the entry is removed."""
    await TankWiseStore(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, _missing_inputs_issue_id(entry.entry_id))


async def async_migrate_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Migrate a config entry from an older version."""
    if entry.version > CONFIG_ENTRY_VERSION:
        return False  # downgraded: not supported
    if entry.version == 1:
        await _async_migrate_v1(hass, entry)
    if entry.version == 2 and entry.minor_version < 2:
        _async_migrate_v2_1(hass, entry)
    if entry.version == 2 and entry.minor_version < 3:
        _async_migrate_v2_2(hass, entry)
    return True


def _async_migrate_v2_2(hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
    """0.9 → 0.10: forget the daily solar forecast sensor (no longer used).

    0.9 replaced it with the hourly forecast of config entries.
    """
    options = {
        key: value
        for key, value in entry.options.items()
        if key != CONF_SOLAR_FORECAST_SENSOR
    }
    hass.config_entries.async_update_entry(
        entry, options=options, minor_version=CONFIG_ENTRY_MINOR_VERSION
    )
    _LOGGER.info("Migrated TankWise entry %s to version 2.3", entry.title)


def _async_migrate_v2_1(hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
    """0.3 → 0.4: replace the 0.2 defaults of the initial values.

    A cut-off temperature of 47 °C made heating stopped by an automation at
    ~45 °C look like thermostat cut-offs.
    """
    options = dict(entry.options)
    if options.get(CONF_CUTOFF_TEMPERATURE) == _V1_DEFAULT_CUTOFF_TEMPERATURE:
        options[CONF_CUTOFF_TEMPERATURE] = DEFAULT_CUTOFF_TEMPERATURE
    if options.get(CONF_RATED_POWER) == _V1_DEFAULT_RATED_POWER:
        options[CONF_RATED_POWER] = DEFAULT_RATED_POWER
    hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    _LOGGER.info("Migrated TankWise entry %s to version 2.2", entry.title)


async def _async_migrate_v1(hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
    """Version 0.2 → 0.3: new inputs, energy optional, settings as numbers."""
    old_data = dict(entry.data)
    old_options = dict(entry.options)

    data: dict[str, Any] = {
        key: old_data[key]
        for key in (CONF_NAME, CONF_TEMPERATURE_SENSOR, CONF_POWER_SENSOR)
        if key in old_data
    }
    if price := old_options.get(CONF_PRICE_SENSOR):
        data[CONF_PRICE_SENSOR] = price

    options: dict[str, Any] = {
        key: old_options[key] for key in _V1_KEPT_PARAMETERS if key in old_options
    }
    if energy := old_data.get(CONF_ENERGY_SENSOR):
        options[CONF_ENERGY_SENSOR] = energy
    for old_key, new_key in _V1_RENAMED_OPTIONS.items():
        if value := old_options.get(old_key):
            options[new_key] = value

    # The mains water override is now a number entity (a runtime setting).
    if (mains := old_options.get("mains_water_temperature")) is not None:
        store = TankWiseStore(hass, entry.entry_id)
        stored = await store.async_load() or {}
        settings = dict(stored.get("settings") or {})
        settings[SETTING_MAINS_WATER_TEMPERATURE] = float(mains)
        await store.async_save({**stored, "settings": settings})

    registry = er.async_get(hass)
    for key in _V1_REMOVED_SENSORS:
        unique_id = f"{entry.entry_id}_{key}"
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
            registry.async_remove(entity_id)

    hass.config_entries.async_update_entry(
        entry, data=data, options=options, version=CONFIG_ENTRY_VERSION, minor_version=1
    )
    _LOGGER.info("Migrated TankWise entry %s to version 2", entry.title)


def _async_remove_stale_appointment_entities(
    hass: HomeAssistant, entry: TankWiseConfigEntry
) -> None:
    """Remove the entities of appointments deleted in the options."""
    current = {item.get("id") for item in entry.options.get(CONF_APPOINTMENTS, [])}
    prefix = f"{entry.entry_id}_appointment_"
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registry_entry.unique_id
        if not unique_id.startswith(prefix):
            continue
        appointment_id = unique_id.removeprefix(prefix).split("_", 1)[0]
        if appointment_id not in current:
            registry.async_remove(registry_entry.entity_id)


def _async_normalise_entity_ids(
    hass: HomeAssistant, entry: TankWiseConfigEntry
) -> None:
    """Give generated entity IDs the ``<platform>.tankwise_<key>`` form.

    Home Assistant adds the device area (or name) to generated entity IDs,
    e.g. ``sensor.coladuria_tankwise_stored_energy``. Those are renamed; the
    recorder keeps their history. Entity IDs renamed by the user to anything
    else are left alone.
    """
    object_ids = {
        f"appointment_{appointment.id}_{kind}": f"{appointment.slug}_{kind}"
        for appointment in load_appointments(entry.options.get(CONF_APPOINTMENTS, []))
        for kind in (
            "time",
            "liters",
            "enabled",
            "suggested_liters",
            "required_temperature",
            "predicted_temperature",
        )
    }
    prefix = f"{entry.entry_id}_"
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not registry_entry.unique_id.startswith(prefix):
            continue
        key = registry_entry.unique_id.removeprefix(prefix)
        object_id = object_ids.get(key, key)
        desired = tankwise_entity_id(registry_entry.domain, object_id)
        current = registry_entry.entity_id
        if current == desired or not current.endswith(f"_{desired.split('.', 1)[1]}"):
            continue
        if registry.async_get(desired) is not None or hass.states.get(desired):
            continue
        registry.async_update_entity(current, new_entity_id=desired)
        _LOGGER.info("Renamed %s to %s", current, desired)


def _missing_inputs_issue_id(entry_id: str) -> str:
    return f"{ISSUE_MISSING_INPUTS}_{entry_id}"


def _async_update_missing_inputs_issue(
    hass: HomeAssistant, entry: TankWiseConfigEntry
) -> None:
    """Ask the user to complete the required inputs (e.g. after an upgrade)."""
    issue_id = _missing_inputs_issue_id(entry.entry_id)
    missing = [key for key in REQUIRED_INPUT_KEYS if not entry.data.get(key)]
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_MISSING_INPUTS,
        translation_placeholders={"name": entry.title, "inputs": ", ".join(missing)},
    )
