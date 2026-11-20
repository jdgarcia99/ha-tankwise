"""Constants for the TankWise integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .model.water import DEFAULT_REGION

DOMAIN: Final = "tankwise"

MANUFACTURER: Final = "jdgarcia99"
MODEL: Final = "TankWise"
CONFIGURATION_URL: Final = "https://github.com/jdgarcia99/ha-tankwise"

UPDATE_INTERVAL: Final = timedelta(seconds=60)
CONFIG_ENTRY_VERSION: Final = 2
CONFIG_ENTRY_MINOR_VERSION: Final = 2

# ---------------------------------------------------------------------------
# Required inputs (config entry data, changed with "Reconfigure")
# ---------------------------------------------------------------------------
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_POWER_SENSOR: Final = "power_sensor"
CONF_HEATER_SWITCH: Final = "heater_switch"
CONF_MANUAL_MODE_SWITCH: Final = "manual_mode_switch"
CONF_PRICE_SENSOR: Final = "price_sensor"

REQUIRED_INPUT_KEYS: Final = (
    CONF_TEMPERATURE_SENSOR,
    CONF_POWER_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_MANUAL_MODE_SWITCH,
    CONF_PRICE_SENSOR,
)

# ---------------------------------------------------------------------------
# Recommended and optional inputs (config entry options)
# ---------------------------------------------------------------------------
CONF_ENERGY_SENSOR: Final = "energy_sensor"
CONF_REGULATOR_SENSOR: Final = "regulator_sensor"
CONF_MASTER_ENTITY: Final = "master_entity"
CONF_OCCUPANCY_ENTITY: Final = "occupancy_entity"
CONF_INJECTION_PRICE_SENSOR: Final = "injection_price_sensor"
CONF_GRID_IMPORT_SENSOR: Final = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR: Final = "grid_export_sensor"
CONF_EXPORT_POWER_SENSOR: Final = "export_power_sensor"
CONF_SOLAR_FORECAST_SENSOR: Final = "solar_forecast_sensor"
CONF_AMBIENT_SENSOR: Final = "ambient_sensor"
CONF_WORKDAY_ENTITY: Final = "workday_entity"

OPTIONAL_INPUT_KEYS: Final = (
    CONF_ENERGY_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_AMBIENT_SENSOR,
    CONF_WORKDAY_ENTITY,
)

# ---------------------------------------------------------------------------
# Installation parameters (config entry options)
# ---------------------------------------------------------------------------
CONF_VOLUME: Final = "volume"
CONF_RATED_POWER: Final = "rated_power"
CONF_WATER_REGION: Final = "water_region"
CONF_AMBIENT_TEMPERATURE: Final = "ambient_temperature"
CONF_CUTOFF_TEMPERATURE: Final = "cutoff_temperature"
CONF_CUTOFF_POWER_THRESHOLD: Final = "cutoff_power_threshold"
CONF_PROFILE_MODE: Final = "profile_mode"
CONF_PROFILE_DAYS: Final = "profile_days"

# Options flow section with the parameters most users never change.
SECTION_ADVANCED: Final = "advanced"
ADVANCED_PARAMETERS: Final = (
    "rated_power",
    "ambient_temperature",
    "cutoff_temperature",
    "cutoff_power_threshold",
    "profile_mode",
    "profile_days",
)

# Hot water appointments (list of dicts in the options).
CONF_APPOINTMENTS: Final = "appointments"

DEFAULT_NAME: Final = "TankWise"
DEFAULT_VOLUME: Final = 100  # L
DEFAULT_RATED_POWER: Final = 1500  # W, initial value (then learned)
DEFAULT_WATER_REGION: Final = DEFAULT_REGION
DEFAULT_AMBIENT_TEMPERATURE: Final = 20.0  # °C, without an ambient sensor
DEFAULT_CUTOFF_TEMPERATURE: Final = 68.0  # °C, initial value (then learned)
DEFAULT_CUTOFF_POWER_THRESHOLD: Final = 50  # W
DEFAULT_PROFILE_MODE: Final = "workday_weekend"
DEFAULT_PROFILE_DAYS: Final = 16

DEFAULT_PARAMETERS: Final = {
    CONF_VOLUME: DEFAULT_VOLUME,
    CONF_RATED_POWER: DEFAULT_RATED_POWER,
    CONF_WATER_REGION: DEFAULT_WATER_REGION,
    CONF_AMBIENT_TEMPERATURE: DEFAULT_AMBIENT_TEMPERATURE,
    CONF_CUTOFF_TEMPERATURE: DEFAULT_CUTOFF_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD: DEFAULT_CUTOFF_POWER_THRESHOLD,
    CONF_PROFILE_MODE: DEFAULT_PROFILE_MODE,
    CONF_PROFILE_DAYS: DEFAULT_PROFILE_DAYS,
}
INTEGER_PARAMETERS: Final = (
    CONF_VOLUME,
    CONF_RATED_POWER,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_PROFILE_DAYS,
)

# ---------------------------------------------------------------------------
# Runtime settings (number entities, persisted in the store)
# ---------------------------------------------------------------------------
SETTING_SHOWER_TEMPERATURE: Final = "shower_temperature"
SETTING_COMFORT_MIN_TEMPERATURE: Final = "comfort_min_temperature"
SETTING_MAINS_WATER_TEMPERATURE: Final = "mains_water_temperature"

MAINS_AUTOMATIC: Final = 0.0  # mains water number value meaning "use the table"

DEFAULT_SETTINGS: Final[dict[str, float]] = {
    SETTING_SHOWER_TEMPERATURE: 40.0,
    SETTING_COMFORT_MIN_TEMPERATURE: 38.0,
    SETTING_MAINS_WATER_TEMPERATURE: MAINS_AUTOMATIC,
}

MAINS_SOURCE_TABLE: Final = "table"
MAINS_SOURCE_MANUAL: Final = "manual"

# ---------------------------------------------------------------------------
# Storage, bootstrap and repairs
# ---------------------------------------------------------------------------
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 300  # s

BOOTSTRAP_DAYS: Final = 10
SHORTAGE_WINDOW: Final = timedelta(days=7)

ISSUE_MISSING_INPUTS: Final = "missing_required_inputs"

# Services
SERVICE_RESET_LEARNING: Final = "reset_learning"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_PART: Final = "part"
PART_LOSSES: Final = "losses"
PART_DEMAND: Final = "demand"
PART_ALL: Final = "all"
RESET_PARTS: Final = (PART_LOSSES, PART_DEMAND, PART_ALL)

# Forecast horizon
FORECAST_HOURS: Final = 36
