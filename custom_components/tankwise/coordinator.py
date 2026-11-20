"""Data update coordinator for TankWise.

Every 60 s the coordinator reads and normalises the input entities and feeds
them to the tank model. An input that is ``unavailable``/``unknown`` (or not
numeric) is reported as ``None``; the coordinator never fails because of it.
If the probe or the heater power is missing, the model freezes (safe mode)
and the tank entities become unavailable.

On the first start the tank model is rebuilt in the background from the
recorder history (see :mod:`.bootstrap`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .bootstrap import BootstrapInfo, BootstrapStatus, async_bootstrap
from .const import (
    BOOTSTRAP_DAYS,
    CONF_AMBIENT_SENSOR,
    CONF_AMBIENT_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MANUAL_MODE_SWITCH,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_REGULATOR_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    DEFAULT_PARAMETERS,
    DEFAULT_SETTINGS,
    DOMAIN,
    MAINS_AUTOMATIC,
    MAINS_SOURCE_MANUAL,
    MAINS_SOURCE_TABLE,
    SETTING_COMFORT_MIN_TEMPERATURE,
    SETTING_MAINS_WATER_TEMPERATURE,
    SETTING_SHOWER_TEMPERATURE,
    SHORTAGE_WINDOW,
    UPDATE_INTERVAL,
)
from .inputs import InputError, parse_bool, parse_measurement, parse_numeric, state_unit
from .model.replay import (
    ROLE_AMBIENT,
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_TEMPERATURE,
)
from .model.tank import PowerDropDetector, TankModel, TankParams, TankSample, TankState
from .model.water import mains_water_temperature
from .storage import TankWiseStore

_LOGGER = logging.getLogger(__name__)

type TankWiseConfigEntry = ConfigEntry[TankWiseCoordinator]


@dataclass(frozen=True, slots=True)
class TankReadings:
    """Normalised snapshot of the inputs.

    Temperatures are in °C, power in W and energy in kWh. ``None`` means the
    input is not configured or its state could not be used.
    """

    timestamp: datetime
    temperature: float | None
    power: float | None
    energy: float | None = None
    regulator: float | None = None
    manual_mode: bool | None = None
    heater_switch: bool | None = None
    master: bool | None = None
    occupancy: bool | None = None
    price: float | None = None
    injection_price: float | None = None
    grid_import: float | None = None
    grid_export: float | None = None
    export_power: float | None = None
    solar_forecast: float | None = None
    ambient_temperature: float | None = None

    @property
    def required_available(self) -> bool:
        """Return True when the inputs the model needs have a usable value."""
        return self.temperature is not None and self.power is not None


@dataclass(frozen=True, slots=True)
class TankWiseData:
    """Data published by the coordinator."""

    readings: TankReadings
    tank: TankState
    shortages_7d: int
    mains_source: str


class TankWiseCoordinator(DataUpdateCoordinator[TankWiseData]):
    """Read the TankWise inputs and run the tank model."""

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
        # Integration version from manifest.json, shown on the device.
        self.sw_version: str | None = None
        self._unusable: set[str] = set()
        # The regulator may be switched off seconds after a thermostat
        # cut-off, so its state is captured at the instant the power falls
        # from state change events, not only every 60 s.
        self._drop_detector = PowerDropDetector(
            float(self.parameters[CONF_CUTOFF_POWER_THRESHOLD])
        )
        self._store = TankWiseStore(hass, entry.entry_id)
        self.model = TankModel()
        self.settings: dict[str, float] = dict(DEFAULT_SETTINGS)
        self.bootstrap = BootstrapInfo(BootstrapStatus.PENDING)
        self._last_sample: TankSample | None = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Restore the state saved before the last shutdown."""
        stored = await self._store.async_load() or {}
        for key, value in (stored.get("settings") or {}).items():
            if key in self.settings:
                self.settings[key] = float(value)
        if "bootstrap" in stored:
            try:
                self.bootstrap = BootstrapInfo.from_dict(stored["bootstrap"])
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Discarding stored bootstrap status: %s", err)
        if "tank" in stored:
            try:
                self.model = TankModel.from_dict(
                    stored["tank"], volume_l=float(self.parameters[CONF_VOLUME])
                )
            except ValueError as err:
                _LOGGER.warning("Discarding stored tank state: %s", err)

    def _stored_data(self) -> dict[str, Any]:
        return {
            "tank": self.model.as_dict(),
            "settings": dict(self.settings),
            "bootstrap": self.bootstrap.as_dict(),
        }

    async def async_save_now(self) -> None:
        """Persist the state immediately (e.g. on unload)."""
        await self._store.async_save(self._stored_data())

    # ------------------------------------------------------------------
    # Bootstrap from the recorder
    # ------------------------------------------------------------------

    @property
    def needs_bootstrap(self) -> bool:
        """Return True if the history has not been imported yet."""
        return self.bootstrap.status in (
            BootstrapStatus.PENDING,
            BootstrapStatus.RUNNING,
        )

    async def async_run_bootstrap(self) -> None:
        """Rebuild the model from the recorder history and adopt it."""
        self.bootstrap = BootstrapInfo(BootstrapStatus.RUNNING)
        data = self.config_entry.data
        options = self.config_entry.options
        roles = {
            ROLE_TEMPERATURE: data.get(CONF_TEMPERATURE_SENSOR),
            ROLE_POWER: data.get(CONF_POWER_SENSOR),
            ROLE_ENERGY: options.get(CONF_ENERGY_SENSOR),
            ROLE_REGULATOR: options.get(CONF_REGULATOR_SENSOR),
            ROLE_MANUAL_MODE: data.get(CONF_MANUAL_MODE_SWITCH),
            ROLE_AMBIENT: options.get(CONF_AMBIENT_SENSOR),
        }
        info, result = await async_bootstrap(
            self.hass, roles, self.tank_params_at, days=BOOTSTRAP_DAYS
        )
        self.bootstrap = info
        if result is not None and result.samples:
            self.model = result.model
            await self.async_save_now()
            await self.async_request_refresh()
        else:
            self._store.async_schedule_save(self._stored_data)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @callback
    def async_start_listeners(self) -> None:
        """Follow power, regulator and manual mode changes between samples."""
        detector = self._drop_detector
        power = self.config_entry.data.get(CONF_POWER_SENSOR)
        regulator = self.config_entry.options.get(CONF_REGULATOR_SENSOR)
        manual = self.config_entry.data.get(CONF_MANUAL_MODE_SWITCH)
        now = dt_util.utcnow()
        if regulator:
            detector.update_regulator(now, _numeric(self.hass.states.get(regulator)))
        if manual:
            detector.update_manual(now, _boolean(self.hass.states.get(manual)))
        if power:
            detector.update_power(now, _power_watts(self.hass.states.get(power)))

        @callback
        def _async_changed(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None:
                return
            entity_id = event.data["entity_id"]
            if entity_id == regulator:
                detector.update_regulator(new_state.last_changed, _numeric(new_state))
            elif entity_id == manual:
                detector.update_manual(new_state.last_changed, _boolean(new_state))
            else:
                detector.update_power(new_state.last_changed, _power_watts(new_state))

        tracked = [entity_id for entity_id in (power, regulator, manual) if entity_id]
        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, tracked, _async_changed)
        )

    # ------------------------------------------------------------------
    # Parameters and settings
    # ------------------------------------------------------------------

    @property
    def parameters(self) -> dict[str, Any]:
        """Return the installation parameters, filled with defaults."""
        return {**DEFAULT_PARAMETERS, **self.config_entry.options}

    def mains_water_temperature_at(self, moment: datetime) -> tuple[float, str]:
        """Return the mains water temperature (°C) and its source."""
        manual = self.settings[SETTING_MAINS_WATER_TEMPERATURE]
        if manual != MAINS_AUTOMATIC:
            return manual, MAINS_SOURCE_MANUAL
        local = dt_util.as_local(moment).date()
        return (
            mains_water_temperature(self.parameters[CONF_WATER_REGION], local),
            MAINS_SOURCE_TABLE,
        )

    def tank_params_at(self, moment: datetime) -> TankParams:
        """Return the tank model parameters at a given time."""
        parameters = self.parameters
        mains, _source = self.mains_water_temperature_at(moment)
        return TankParams(
            volume_l=float(parameters[CONF_VOLUME]),
            rated_power_w=float(parameters[CONF_RATED_POWER]),
            mains_temperature=mains,
            ambient_temperature=float(parameters[CONF_AMBIENT_TEMPERATURE]),
            comfort_min_temperature=self.settings[SETTING_COMFORT_MIN_TEMPERATURE],
            shower_temperature=self.settings[SETTING_SHOWER_TEMPERATURE],
            cutoff_temperature=float(parameters[CONF_CUTOFF_TEMPERATURE]),
            power_threshold_w=float(parameters[CONF_CUTOFF_POWER_THRESHOLD]),
        )

    @property
    def tank_params(self) -> TankParams:
        """Return the tank model parameters now."""
        return self.tank_params_at(dt_util.utcnow())

    async def async_set_setting(self, key: str, value: float) -> None:
        """Change a runtime setting (number entity) and refresh the state."""
        self.settings[key] = float(value)
        self._store.async_schedule_save(self._stored_data)
        if self.data is None or self._last_sample is None:
            return
        # Same timestamp as the last sample: the derived values are
        # recomputed with the new settings without advancing the model.
        tank = self.model.update(self._last_sample, self.tank_params)
        _mains, source = self.mains_water_temperature_at(dt_util.utcnow())
        self.async_set_updated_data(replace(self.data, tank=tank, mains_source=source))

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> TankWiseData:
        """Read the inputs and advance the tank model."""
        readings = self._read_inputs()
        sample = TankSample(
            timestamp=readings.timestamp,
            temperature=readings.temperature,
            power=readings.power,
            energy=readings.energy,
            regulator=readings.regulator,
            manual_mode=readings.manual_mode,
            ambient_temperature=readings.ambient_temperature,
            power_drop=self._drop_detector.drop,
        )
        tank = self.model.update(sample, self.tank_params_at(readings.timestamp))
        self._last_sample = sample
        self._store.async_schedule_save(self._stored_data)
        _mains, source = self.mains_water_temperature_at(readings.timestamp)
        return TankWiseData(
            readings=readings,
            tank=tank,
            shortages_7d=self._count_shortages(readings.timestamp),
            mains_source=source,
        )

    def _count_shortages(self, now: datetime) -> int:
        return sum(
            1
            for event in self.model.consumptions
            if event.shortage and now - event.start <= SHORTAGE_WINDOW
        )

    def _read_inputs(self) -> TankReadings:
        """Read all configured inputs."""
        data = self.config_entry.data
        options = self.config_entry.options
        return TankReadings(
            timestamp=dt_util.utcnow(),
            temperature=self._read_measurement(
                data.get(CONF_TEMPERATURE_SENSOR), "temperature"
            ),
            power=self._read_measurement(data.get(CONF_POWER_SENSOR), "power"),
            energy=self._read_measurement(options.get(CONF_ENERGY_SENSOR), "energy"),
            regulator=self._read_float(options.get(CONF_REGULATOR_SENSOR)),
            manual_mode=self._read_bool(data.get(CONF_MANUAL_MODE_SWITCH)),
            heater_switch=self._read_bool(data.get(CONF_HEATER_SWITCH)),
            master=self._read_bool(options.get(CONF_MASTER_ENTITY)),
            occupancy=self._read_bool(options.get(CONF_OCCUPANCY_ENTITY)),
            price=self._read_float(data.get(CONF_PRICE_SENSOR)),
            injection_price=self._read_float(options.get(CONF_INJECTION_PRICE_SENSOR)),
            grid_import=self._read_measurement(
                options.get(CONF_GRID_IMPORT_SENSOR), "energy"
            ),
            grid_export=self._read_measurement(
                options.get(CONF_GRID_EXPORT_SENSOR), "energy"
            ),
            export_power=self._read_measurement(
                options.get(CONF_EXPORT_POWER_SENSOR), "power"
            ),
            solar_forecast=self._read_float(options.get(CONF_SOLAR_FORECAST_SENSOR)),
            ambient_temperature=self._read_measurement(
                options.get(CONF_AMBIENT_SENSOR), "temperature"
            ),
        )

    def _read(self, entity_id: str | None, parse: Callable[[State], Any]) -> Any:
        """Read an entity with a parser; log once when it becomes unusable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            self._mark_unusable(entity_id, "entity not found")
            return None
        try:
            value = parse(state)
        except InputError as err:
            self._mark_unusable(entity_id, str(err))
            return None
        self._mark_usable(entity_id)
        return value

    def _read_measurement(self, entity_id: str | None, kind: str) -> float | None:
        return self._read(
            entity_id,
            lambda state: parse_measurement(state.state, state_unit(state), kind),
        )

    def _read_float(self, entity_id: str | None) -> float | None:
        return self._read(entity_id, lambda state: parse_numeric(state.state))

    def _read_bool(self, entity_id: str | None) -> bool | None:
        return self._read(entity_id, lambda state: parse_bool(state.state))

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


def _numeric(state: State | None) -> float | None:
    """Return the numeric value of a state, or None."""
    if state is None:
        return None
    try:
        return parse_numeric(state.state)
    except InputError:
        return None


def _boolean(state: State | None) -> bool | None:
    """Return the on/off value of a state, or None."""
    if state is None:
        return None
    try:
        return parse_bool(state.state)
    except InputError:
        return None


def _power_watts(state: State | None) -> float | None:
    """Return a power state in W, or None."""
    if state is None:
        return None
    try:
        return parse_measurement(state.state, state_unit(state), "power")
    except InputError:
        return None
