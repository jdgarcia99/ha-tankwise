"""Data update coordinator for TankWise.

Every 60 s the coordinator reads and normalises the input sensors and feeds
them to the tank model. A sensor that is ``unavailable``/``unknown`` (or not
numeric) is reported as ``None``; the coordinator never fails because of it.
If a required sensor is missing, the model freezes (safe mode) and the tank
entities become unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

from .const import (
    CONF_BOTTOM_TEMPERATURE_SENSOR,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_MAINS_WATER_TEMPERATURE,
    CONF_MIN_USEFUL_TEMPERATURE,
    CONF_POWER_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_REGULATOR_SENSOR,
    CONF_SHOWER_ENERGY,
    CONF_SURPLUS_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    DEFAULT_PARAMETERS,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .model.tank import TankModel, TankParams, TankSample, TankState
from .model.water import mains_water_temperature
from .storage import TankWiseStore

_LOGGER = logging.getLogger(__name__)

type TankWiseConfigEntry = ConfigEntry[TankWiseCoordinator]


@dataclass(frozen=True, slots=True)
class TankReadings:
    """Normalised snapshot of the input sensors.

    Temperatures are in °C, power in W and energy in kWh. ``None`` means the
    sensor is not configured or its state could not be used.
    """

    timestamp: datetime
    temperature: float | None
    power: float | None
    energy: float | None
    bottom_temperature: float | None = None
    surplus_power: float | None = None
    price: float | None = None
    presence: bool | None = None
    regulator: float | None = None

    @property
    def required_available(self) -> bool:
        """Return True when all required sensors have a usable value."""
        return (
            self.temperature is not None
            and self.power is not None
            and self.energy is not None
        )


@dataclass(frozen=True, slots=True)
class TankWiseData:
    """Data published by the coordinator."""

    readings: TankReadings
    tank: TankState


class TankWiseCoordinator(DataUpdateCoordinator[TankWiseData]):
    """Read the TankWise input sensors and run the tank model."""

    config_entry: TankWiseConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._unusable: set[str] = set()
        self._store = TankWiseStore(hass, entry.entry_id)
        self.model = TankModel()

    async def _async_setup(self) -> None:
        """Restore the model state saved before the last shutdown."""
        stored = await self._store.async_load()
        if not stored or "tank" not in stored:
            return
        try:
            self.model = TankModel.from_dict(stored["tank"])
        except ValueError as err:
            _LOGGER.warning("Discarding stored tank state: %s", err)

    def _stored_data(self) -> dict[str, Any]:
        return {"tank": self.model.as_dict()}

    async def async_save_now(self) -> None:
        """Persist the model state immediately (e.g. on unload)."""
        await self._store.async_save(self._stored_data())

    async def async_remove_storage(self) -> None:
        """Delete the persisted state."""
        await self._store.async_remove()

    @property
    def parameters(self) -> dict[str, Any]:
        """Return the physical parameters, filled with defaults."""
        return {**DEFAULT_PARAMETERS, **self.config_entry.options}

    @property
    def mains_water_temperature(self) -> float:
        """Return the mains water temperature (°C) for the current month."""
        override = self.config_entry.options.get(CONF_MAINS_WATER_TEMPERATURE)
        if override is not None:
            return float(override)
        return mains_water_temperature(
            self.parameters[CONF_WATER_REGION], dt_util.now().month
        )

    @property
    def tank_params(self) -> TankParams:
        """Return the tank model parameters from the options."""
        parameters = self.parameters
        return TankParams(
            volume_l=float(parameters[CONF_VOLUME]),
            rated_power_w=float(parameters[CONF_RATED_POWER]),
            mains_temperature=self.mains_water_temperature,
            min_useful_temperature=float(parameters[CONF_MIN_USEFUL_TEMPERATURE]),
            cutoff_temperature=float(parameters[CONF_CUTOFF_TEMPERATURE]),
            power_threshold_w=float(parameters[CONF_CUTOFF_POWER_THRESHOLD]),
            shower_energy_kwh=float(parameters[CONF_SHOWER_ENERGY]),
        )

    async def _async_update_data(self) -> TankWiseData:
        """Read the sensors and advance the tank model."""
        readings = self._read_inputs()
        tank = self.model.update(
            TankSample(
                timestamp=readings.timestamp,
                temperature=readings.temperature,
                power=readings.power,
                energy=readings.energy,
                regulator=readings.regulator,
            ),
            self.tank_params,
        )
        self._store.async_schedule_save(self._stored_data)
        return TankWiseData(readings=readings, tank=tank)

    def _read_inputs(self) -> TankReadings:
        """Read all configured sensors."""
        data = self.config_entry.data
        options = self.config_entry.options
        return TankReadings(
            timestamp=dt_util.utcnow(),
            temperature=self._read_measurement(
                data[CONF_TEMPERATURE_SENSOR],
                TemperatureConverter,
                UnitOfTemperature.CELSIUS,
            ),
            power=self._read_measurement(
                data[CONF_POWER_SENSOR], PowerConverter, UnitOfPower.WATT
            ),
            energy=self._read_measurement(
                data[CONF_ENERGY_SENSOR],
                EnergyConverter,
                UnitOfEnergy.KILO_WATT_HOUR,
            ),
            bottom_temperature=self._read_measurement(
                options.get(CONF_BOTTOM_TEMPERATURE_SENSOR),
                TemperatureConverter,
                UnitOfTemperature.CELSIUS,
            ),
            surplus_power=self._read_measurement(
                options.get(CONF_SURPLUS_SENSOR), PowerConverter, UnitOfPower.WATT
            ),
            price=self._read_float(options.get(CONF_PRICE_SENSOR)),
            presence=self._read_bool(options.get(CONF_PRESENCE_ENTITY)),
            regulator=self._read_float(options.get(CONF_REGULATOR_SENSOR)),
        )

    def _read_measurement(
        self,
        entity_id: str | None,
        converter: type[BaseUnitConverter],
        target_unit: str,
    ) -> float | None:
        """Read a numeric sensor and convert it to ``target_unit``."""
        if entity_id is None:
            return None
        value = self._read_float(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
        if unit not in converter.VALID_UNITS:
            self._mark_unusable(entity_id, f"unsupported unit {unit!r}")
            return None
        return converter.convert(value, unit, target_unit)

    def _read_float(self, entity_id: str | None) -> float | None:
        """Return the numeric state of an entity, or None if unusable."""
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            self._mark_unusable(entity_id, "entity not found")
            return None
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._mark_unusable(entity_id, state.state)
            return None
        try:
            value = float(state.state)
        except ValueError:
            self._mark_unusable(entity_id, f"non-numeric state {state.state!r}")
            return None
        self._mark_usable(entity_id)
        return value

    def _read_bool(self, entity_id: str | None) -> bool | None:
        """Return the on/off state of an entity, or None if unusable."""
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            self._mark_unusable(entity_id, state.state if state else "entity not found")
            return None
        self._mark_usable(entity_id)
        return state.state == STATE_ON

    def _mark_unusable(self, entity_id: str, reason: str) -> None:
        """Log once when an input becomes unusable."""
        if entity_id not in self._unusable:
            self._unusable.add(entity_id)
            _LOGGER.warning("Input %s is not usable (%s)", entity_id, reason)

    def _mark_usable(self, entity_id: str) -> None:
        """Log once when an input recovers."""
        if entity_id in self._unusable:
            self._unusable.discard(entity_id)
            _LOGGER.info("Input %s is usable again", entity_id)
