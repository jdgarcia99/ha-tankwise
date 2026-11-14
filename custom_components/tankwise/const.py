"""Constants for the TankWise integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .model.water import DEFAULT_REGION

DOMAIN: Final = "tankwise"

UPDATE_INTERVAL: Final = timedelta(seconds=60)

# Required sensors (config entry data)
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_POWER_SENSOR: Final = "power_sensor"
CONF_ENERGY_SENSOR: Final = "energy_sensor"

# Optional sensors (config entry options)
CONF_SOLAR_FORECAST_SENSOR: Final = "solar_forecast_sensor"
CONF_SURPLUS_SENSOR: Final = "surplus_sensor"
CONF_PRICE_SENSOR: Final = "price_sensor"
CONF_PRESENCE_ENTITY: Final = "presence_entity"
CONF_BOTTOM_TEMPERATURE_SENSOR: Final = "bottom_temperature_sensor"

OPTIONAL_SENSOR_KEYS: Final = (
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_SURPLUS_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_BOTTOM_TEMPERATURE_SENSOR,
)

# Physical parameters (config entry options)
CONF_VOLUME: Final = "volume"
CONF_RATED_POWER: Final = "rated_power"
CONF_WATER_REGION: Final = "water_region"
CONF_MAINS_WATER_TEMPERATURE: Final = "mains_water_temperature"
CONF_MIN_USEFUL_TEMPERATURE: Final = "min_useful_temperature"
CONF_CUTOFF_POWER_THRESHOLD: Final = "cutoff_power_threshold"
CONF_COMFORT_LEVEL: Final = "comfort_level"

DEFAULT_NAME: Final = "TankWise"
DEFAULT_VOLUME: Final = 100  # L
DEFAULT_RATED_POWER: Final = 1450  # W
DEFAULT_WATER_REGION: Final = DEFAULT_REGION
DEFAULT_MIN_USEFUL_TEMPERATURE: Final = 38.0  # °C
DEFAULT_CUTOFF_POWER_THRESHOLD: Final = 50  # W

COMFORT_P80: Final = "p80"
COMFORT_P90: Final = "p90"
COMFORT_P95: Final = "p95"
COMFORT_LEVELS: Final = (COMFORT_P80, COMFORT_P90, COMFORT_P95)
DEFAULT_COMFORT_LEVEL: Final = COMFORT_P90

DEFAULT_PARAMETERS: Final = {
    CONF_VOLUME: DEFAULT_VOLUME,
    CONF_RATED_POWER: DEFAULT_RATED_POWER,
    CONF_WATER_REGION: DEFAULT_WATER_REGION,
    CONF_MIN_USEFUL_TEMPERATURE: DEFAULT_MIN_USEFUL_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD: DEFAULT_CUTOFF_POWER_THRESHOLD,
    CONF_COMFORT_LEVEL: DEFAULT_COMFORT_LEVEL,
}
