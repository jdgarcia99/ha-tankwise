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
CONFIG_ENTRY_MINOR_VERSION: Final = 3

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
# Config entries whose hourly solar forecast is used (summed).
CONF_SOLAR_FORECAST_ENTRIES: Final = "solar_forecast_entries"
# Switch that enables the regulator output (FreeDS "PWM enabled"): off, the
# surplus goes to the grid instead of the water heater.
CONF_REGULATOR_OUTPUT_SWITCH: Final = "regulator_output_switch"
# Until 0.8: a daily forecast sensor, not usable for an hourly plan.
CONF_SOLAR_FORECAST_SENSOR: Final = "solar_forecast_sensor"
CONF_AMBIENT_SENSOR: Final = "ambient_sensor"
CONF_WORKDAY_ENTITY: Final = "workday_entity"
# Regulator health: a power drop while the regulator is unavailable,
# restarting, in a mode that does not heat or at 0 % is not a cut-off.
CONF_REGULATOR_MODE_ENTITY: Final = "regulator_mode_entity"
CONF_REGULATOR_ACTIVE_MODES: Final = "regulator_active_modes"
CONF_REGULATOR_UPTIME_SENSOR: Final = "regulator_uptime_sensor"
CONF_REGULATOR_RESTART_REASON_SENSOR: Final = "regulator_restart_reason_sensor"
# Hot water meter at the outlet of the tank: cumulative volume (L or m³) and,
# optionally, the flow rate. With it, draws are measured in litres.
CONF_HOT_WATER_VOLUME_SENSOR: Final = "hot_water_volume_sensor"
CONF_HOT_WATER_FLOW_SENSOR: Final = "hot_water_flow_sensor"
# Maintenance window: while this entity is on (a firmware update script, a
# helper...) and for the margin after, readings are not learned from.
CONF_MAINTENANCE_ENTITY: Final = "maintenance_entity"
CONF_MAINTENANCE_MARGIN: Final = "maintenance_margin"
DEFAULT_MAINTENANCE_MARGIN: Final = 10  # min

OPTIONAL_INPUT_KEYS: Final = (
    CONF_ENERGY_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_REGULATOR_OUTPUT_SWITCH,
    CONF_AMBIENT_SENSOR,
    CONF_WORKDAY_ENTITY,
    CONF_REGULATOR_MODE_ENTITY,
    CONF_REGULATOR_ACTIVE_MODES,
    CONF_REGULATOR_UPTIME_SENSOR,
    CONF_REGULATOR_RESTART_REASON_SENSOR,
    CONF_HOT_WATER_VOLUME_SENSOR,
    CONF_HOT_WATER_FLOW_SENSOR,
    CONF_MAINTENANCE_ENTITY,
    CONF_MAINTENANCE_MARGIN,
)

# ---------------------------------------------------------------------------
# Installation parameters (config entry options)
# ---------------------------------------------------------------------------
CONF_VOLUME: Final = "volume"
CONF_RATED_POWER: Final = "rated_power"
CONF_WATER_REGION: Final = "water_region"
# Where the probe is: at the top (it sees the water leaving) or in the
# thermostat's sheath, low in the tank next to the element.
CONF_PROBE_POSITION: Final = "probe_position"
PROBE_TOP: Final = "top"
PROBE_SHEATH: Final = "sheath"
PROBE_POSITIONS: Final = (PROBE_TOP, PROBE_SHEATH)
CONF_AMBIENT_TEMPERATURE: Final = "ambient_temperature"
CONF_CUTOFF_TEMPERATURE: Final = "cutoff_temperature"
CONF_CUTOFF_POWER_THRESHOLD: Final = "cutoff_power_threshold"
CONF_PROFILE_MODE: Final = "profile_mode"
CONF_PROFILE_DAYS: Final = "profile_days"
# Rescue recharge inside appointment blocks (off: strict mode).
CONF_RESCUE: Final = "rescue"
# A boost raises the demand forecast like a hot water shortage.
CONF_BOOST_AS_SHORTAGE: Final = "boost_as_shortage"
# Minutes the regulator may be unavailable before the heater switch is opened.
CONF_REGULATOR_MARGIN: Final = "regulator_margin"
# k_use while none is learned, with the probe in the thermostat's sheath.
CONF_USABLE_FACTOR_DEFAULT: Final = "usable_factor_default"
# Share of the volume below a probe in the thermostat's sheath (%).
CONF_PROBE_VOLUME_BELOW: Final = "probe_volume_below"
# Day of the month the billing period starts (1: the calendar month).
CONF_BILLING_DAY: Final = "billing_day"
# Factor applied to the computed energy term for the compensation cap (the
# purchase price may include tolls and charges the cap does not count).
CONF_ENERGY_TERM_FACTOR: Final = "energy_term_factor"

# Options flow section with the parameters most users never change.
SECTION_ADVANCED: Final = "advanced"
ADVANCED_PARAMETERS: Final = (
    "rated_power",
    "ambient_temperature",
    "cutoff_temperature",
    "cutoff_power_threshold",
    "profile_mode",
    "profile_days",
    "regulator_margin",
    "usable_factor_default",
    "probe_volume_below",
    "billing_day",
    "energy_term_factor",
)

# Hot water appointments (list of dicts in the options).
CONF_APPOINTMENTS: Final = "appointments"

DEFAULT_NAME: Final = "TankWise"
DEFAULT_VOLUME: Final = 100  # L
DEFAULT_RATED_POWER: Final = 1500  # W, initial value (then learned)
DEFAULT_WATER_REGION: Final = DEFAULT_REGION
DEFAULT_PROBE_POSITION: Final = PROBE_TOP
DEFAULT_AMBIENT_TEMPERATURE: Final = 20.0  # °C, without an ambient sensor
DEFAULT_CUTOFF_TEMPERATURE: Final = 68.0  # °C, initial value (then learned)
DEFAULT_CUTOFF_POWER_THRESHOLD: Final = 50  # W
DEFAULT_PROFILE_MODE: Final = "workday_weekend"
DEFAULT_PROFILE_DAYS: Final = 16
DEFAULT_RESCUE: Final = True
DEFAULT_REGULATOR_MARGIN: Final = 2  # min
DEFAULT_BOOST_AS_SHORTAGE: Final = False
DEFAULT_USABLE_FACTOR_DEFAULT: Final = 0.85
DEFAULT_PROBE_VOLUME_BELOW: Final = 30  # %
DEFAULT_BILLING_DAY: Final = 1
DEFAULT_ENERGY_TERM_FACTOR: Final = 1.0

DEFAULT_PARAMETERS: Final = {
    CONF_VOLUME: DEFAULT_VOLUME,
    CONF_RATED_POWER: DEFAULT_RATED_POWER,
    CONF_WATER_REGION: DEFAULT_WATER_REGION,
    CONF_PROBE_POSITION: DEFAULT_PROBE_POSITION,
    CONF_AMBIENT_TEMPERATURE: DEFAULT_AMBIENT_TEMPERATURE,
    CONF_CUTOFF_TEMPERATURE: DEFAULT_CUTOFF_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD: DEFAULT_CUTOFF_POWER_THRESHOLD,
    CONF_PROFILE_MODE: DEFAULT_PROFILE_MODE,
    CONF_PROFILE_DAYS: DEFAULT_PROFILE_DAYS,
    CONF_RESCUE: DEFAULT_RESCUE,
    CONF_REGULATOR_MARGIN: DEFAULT_REGULATOR_MARGIN,
    CONF_BOOST_AS_SHORTAGE: DEFAULT_BOOST_AS_SHORTAGE,
    CONF_USABLE_FACTOR_DEFAULT: DEFAULT_USABLE_FACTOR_DEFAULT,
    CONF_PROBE_VOLUME_BELOW: DEFAULT_PROBE_VOLUME_BELOW,
    CONF_BILLING_DAY: DEFAULT_BILLING_DAY,
    CONF_ENERGY_TERM_FACTOR: DEFAULT_ENERGY_TERM_FACTOR,
}
INTEGER_PARAMETERS: Final = (
    CONF_VOLUME,
    CONF_PROBE_VOLUME_BELOW,
    CONF_RATED_POWER,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_PROFILE_DAYS,
    CONF_REGULATOR_MARGIN,
    CONF_BILLING_DAY,
)

# ---------------------------------------------------------------------------
# Runtime settings (number entities, persisted in the store)
# ---------------------------------------------------------------------------
SETTING_SHOWER_TEMPERATURE: Final = "shower_temperature"
SETTING_COMFORT_MIN_TEMPERATURE: Final = "comfort_min_temperature"
SETTING_MAINS_WATER_TEMPERATURE: Final = "mains_water_temperature"
SETTING_RESERVE_LITERS: Final = "reserve_liters"
SETTING_SURPLUS_VALUE: Final = "surplus_value"
SETTING_MAX_TEMPERATURE: Final = "max_temperature"
SETTING_BOOST_TARGET: Final = "boost_target"
SETTING_BOOST_DURATION: Final = "boost_duration"  # min, 0: no limit

MAINS_AUTOMATIC: Final = 0.0  # mains water number value meaning "use the table"

DEFAULT_SETTINGS: Final[dict[str, float]] = {
    SETTING_SHOWER_TEMPERATURE: 40.0,
    SETTING_COMFORT_MIN_TEMPERATURE: 38.0,
    SETTING_MAINS_WATER_TEMPERATURE: MAINS_AUTOMATIC,
    # Useful litres always kept for unplanned taps.
    SETTING_RESERVE_LITERS: 10.0,
    # €/kWh of the surplus when there is no injection price sensor.
    SETTING_SURPLUS_VALUE: 0.0,
    # Safety: the manual mode ends above this for one minute. Above the
    # thermostat cut-off (68-70 °C on the reference tank, the probe reads up
    # to ~70 °C then), so the thermostat stays the normal stop.
    SETTING_MAX_TEMPERATURE: 72.0,
    SETTING_BOOST_TARGET: 60.0,
    SETTING_BOOST_DURATION: 90.0,
}

# Control state (select and switch entities, persisted in the store)
DEFAULT_MODE: Final = "shadow"

MAINS_SOURCE_TABLE: Final = "table"
MAINS_SOURCE_MANUAL: Final = "manual"

# ---------------------------------------------------------------------------
# Storage, bootstrap and repairs
# ---------------------------------------------------------------------------
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 300  # s

BOOTSTRAP_DAYS: Final = 10
# Raised when the models change enough for the history to be imported again
# (2: demand profile, energy reconciliation and stricter cut-off detection;
# 3: imports of 0.6 that ran before the inputs existed learned nothing;
# 4: rest windows ended by a slow draw no longer inflate the heat losses).
BOOTSTRAP_VERSION: Final = 6
# The import waits this long for the probe and the power after a start.
BOOTSTRAP_INPUT_WAIT: Final = timedelta(minutes=30)
SHORTAGE_WINDOW: Final = timedelta(days=7)

ISSUE_MISSING_INPUTS: Final = "missing_required_inputs"
ISSUE_HEATER_SWITCH_OFF: Final = "heater_switch_off"
ISSUE_REGULATOR_LOST: Final = "regulator_lost"
ISSUE_CONFLICTING_AUTOMATIONS: Final = "conflicting_automations"

# Events
EVENT_PLAN_AT_RISK: Final = "tankwise_plan_at_risk"
EVENT_POWER_REQUEST: Final = "tankwise_power_request"
EVENT_BOOST_STARTED: Final = "tankwise_boost_started"
EVENT_BOOST_FINISHED: Final = "tankwise_boost_finished"

# Services
SERVICE_RESET_LEARNING: Final = "reset_learning"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_PART: Final = "part"
PART_LOSSES: Final = "losses"
PART_DEMAND: Final = "demand"
PART_ALL: Final = "all"
RESET_PARTS: Final = (PART_LOSSES, PART_DEMAND, PART_ALL)

SERVICE_REPLAN: Final = "replan"
SERVICE_GET_PLAN: Final = "get_plan"
SERVICE_REIMPORT_HISTORY: Final = "reimport_history"
SERVICE_BOOST: Final = "boost"
SERVICE_CANCEL_BOOST: Final = "cancel_boost"
SERVICE_DISCARD_LAST_CUTOFF_SAMPLE: Final = "discard_last_cutoff_sample"
SERVICE_DISCARD_CUTOFF_SAMPLE: Final = "discard_cutoff_sample"
SERVICE_RESET_USABLE_FACTOR: Final = "reset_usable_factor"
ATTR_INDEX: Final = "index"
ATTR_VALUE: Final = "value"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_LITERS: Final = "liters"
ATTR_DURATION: Final = "duration"

# Forecast horizon
FORECAST_HOURS: Final = 36

# Planning
REPLAN_DEVIATION: Final = 3.0  # °C between the plan and the tank
SURPLUS_EXPORT_W: Final = 50.0  # exporting above this: heating with surplus
