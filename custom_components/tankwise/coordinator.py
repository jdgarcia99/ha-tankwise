"""Data update coordinator for TankWise.

Every 60 s the coordinator reads and normalises the input sensors and feeds
them to the tank model; the draws it detects feed the demand model. A sensor
that is ``unavailable``/``unknown`` (or not numeric) is reported as ``None``;
the coordinator never fails because of it. If a required sensor is missing,
the tank model freezes (safe mode) and the tank entities become unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
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
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

from .actors import ActorTracker
from .const import (
    CONF_BOTTOM_TEMPERATURE_SENSOR,
    CONF_COMFORT_LEVEL,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_MAINS_WATER_TEMPERATURE,
    CONF_MANUAL_MODE_ENTITY,
    CONF_MIN_USEFUL_TEMPERATURE,
    CONF_PERSON_AUTOMATIONS,
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
    CONF_WORKDAY_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
    FORECAST_HORIZON_HOURS,
    FORECAST_REFRESH,
    UPDATE_INTERVAL,
)
from .model.demand import (
    DayType,
    DemandModel,
    DemandStatus,
    FailureReason,
    NextDraw,
    comfort_percentile,
    day_type_for,
)
from .model.history import (
    ImportSummary,
    apply_to_demand,
    parse_history_csv,
    replay_history,
)
from .model.tank import (
    PowerDropDetector,
    TankModel,
    TankParams,
    TankSample,
    TankState,
)
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
class DemandSnapshot:
    """Published state of the demand model."""

    status: DemandStatus
    days_with_data: int
    today_type: DayType
    shower_energy_kwh: float
    learned_shower_energy_kwh: float | None
    next_draw: NextDraw | None


@dataclass(frozen=True, slots=True)
class TankWiseData:
    """Data published by the coordinator."""

    readings: TankReadings
    tank: TankState
    demand: DemandSnapshot


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
        # The regulator may be switched off seconds after a thermostat
        # cut-off, so its value is captured at the instant the power falls
        # from state change events, not only every 60 s.
        self._drop_detector: PowerDropDetector | None = (
            PowerDropDetector(float(self.parameters[CONF_CUTOFF_POWER_THRESHOLD]))
            if entry.options.get(CONF_REGULATOR_SENSOR)
            else None
        )
        self._store = TankWiseStore(hass, entry.entry_id)
        self.model = TankModel()
        self.demand = DemandModel()
        self._last_draw_end: datetime | None = None
        self._workdays: dict[date, bool | None] = {}
        self._actors = ActorTracker()
        self._next_draw: NextDraw | None = None
        self._forecast_at: datetime | None = None
        self._demand_changed = True

    async def _async_setup(self) -> None:
        """Restore the model state saved before the last shutdown."""
        stored = await self._store.async_load()
        if not stored:
            return
        if "tank" in stored:
            try:
                self.model = TankModel.from_dict(stored["tank"])
            except ValueError as err:
                _LOGGER.warning("Discarding stored tank state: %s", err)
        if "demand" in stored:
            try:
                self.demand = DemandModel.from_dict(stored["demand"])
            except ValueError as err:
                _LOGGER.warning("Discarding stored demand state: %s", err)
        if last_draw_end := stored.get("last_draw_end"):
            self._last_draw_end = datetime.fromisoformat(last_draw_end)
        elif (last := self.model.last_consumption) is not None:
            # Draws already in the tank model were seen before the demand
            # model existed; do not feed them again.
            self._last_draw_end = last.end

    def _stored_data(self) -> dict[str, Any]:
        return {
            "tank": self.model.as_dict(),
            "demand": self.demand.as_dict(),
            "last_draw_end": (
                self._last_draw_end.isoformat() if self._last_draw_end else None
            ),
        }

    @callback
    def async_start_listeners(self) -> None:
        """Start the state change listeners.

        Power and regulator changes feed the cut-off detector between
        samples; the regulator manual mode is tracked to attribute
        activations to people.
        """
        self._async_start_power_drop_listener()
        entry = self.config_entry
        entry.async_on_unload(self._actors.async_start(self.hass))
        if manual := entry.options.get(CONF_MANUAL_MODE_ENTITY):
            entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, [manual], self._async_manual_mode_changed
                )
            )

    @callback
    def _async_manual_mode_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if new_state is None or new_state.state != STATE_ON:
            return
        if old_state is not None and old_state.state == STATE_ON:
            return
        person_automations = self.config_entry.options.get(CONF_PERSON_AUTOMATIONS, [])
        if not self._actors.is_person(new_state.context, person_automations):
            _LOGGER.debug("Manual mode switched on by an automation; ignored")
            return
        now = dt_util.now()
        self.demand.add_failure(
            now, self.day_type_of(now.date()), FailureReason.MANUAL_MODE
        )
        self._demand_changed = True
        self._store.async_schedule_save(self._stored_data)
        _LOGGER.info("Manual mode switched on by a person: forecast failure recorded")

    @callback
    def _async_start_power_drop_listener(self) -> None:
        """Follow power and regulator changes between samples."""
        detector = self._drop_detector
        if detector is None:
            return
        power = self.config_entry.data[CONF_POWER_SENSOR]
        regulator = self.config_entry.options[CONF_REGULATOR_SENSOR]
        now = dt_util.utcnow()
        detector.update_regulator(now, _numeric(self.hass.states.get(regulator)))
        detector.update_power(now, _power_watts(self.hass.states.get(power)))

        @callback
        def _async_changed(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None:
                return
            if event.data["entity_id"] == regulator:
                detector.update_regulator(new_state.last_changed, _numeric(new_state))
            else:
                detector.update_power(new_state.last_changed, _power_watts(new_state))

        self.config_entry.async_on_unload(
            async_track_state_change_event(
                self.hass, [power, regulator], _async_changed
            )
        )

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
    def shower_energy(self) -> float:
        """Return the learned shower energy, or the configured one."""
        learned = self.demand.shower_energy(dt_util.now().date())
        if learned is not None:
            return learned
        return float(self.parameters[CONF_SHOWER_ENERGY])

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
            shower_energy_kwh=self.shower_energy,
        )

    def day_type_of(self, day: date) -> DayType:
        """Return the day type, using the workday sensor when known."""
        return day_type_for(day, self._workdays.get(day))

    async def _async_is_workday(self, day: date, *, today: date) -> bool | None:
        """Ask the workday sensor whether a date is a workday."""
        sensor = self.config_entry.options.get(CONF_WORKDAY_SENSOR)
        if sensor is None:
            return None
        if day == today:
            state = self.hass.states.get(sensor)
            if state is None or state.state not in (STATE_ON, STATE_OFF):
                return None
            return state.state == STATE_ON
        try:
            response = await self.hass.services.async_call(
                "workday",
                "check_date",
                {"check_date": day},
                target={"entity_id": sensor},
                blocking=True,
                return_response=True,
            )
            return bool(response[sensor]["workday"])  # type: ignore[index]
        except (HomeAssistantError, KeyError, TypeError) as err:
            _LOGGER.debug("Could not check workday for %s: %s", day, err)
            return None

    async def _async_refresh_workdays(self, today: date, days: list[date]) -> None:
        for day in days:
            if day == today or self._workdays.get(day) is None:
                self._workdays[day] = await self._async_is_workday(day, today=today)
        oldest = today - timedelta(days=120)
        self._workdays = {d: v for d, v in self._workdays.items() if d >= oldest}

    async def _async_update_data(self) -> TankWiseData:
        """Read the sensors and advance the tank and demand models."""
        readings = self._read_inputs()
        params = self.tank_params
        tank = self.model.update(
            TankSample(
                timestamp=readings.timestamp,
                temperature=readings.temperature,
                power=readings.power,
                energy=readings.energy,
                regulator=readings.regulator,
                power_drop=(self._drop_detector.drop if self._drop_detector else None),
            ),
            params,
        )
        now = dt_util.as_local(readings.timestamp)
        today = now.date()
        await self._async_refresh_workdays(
            today, [today + timedelta(days=offset) for offset in range(3)]
        )
        if tank.available:
            self.demand.observe(now, self.day_type_of(today))
        self._feed_draws(params)
        demand = self._demand_snapshot(now)
        self._store.async_schedule_save(self._stored_data)
        return TankWiseData(readings=readings, tank=tank, demand=demand)

    def _feed_draws(self, params: TankParams) -> None:
        """Pass a newly detected draw from the tank model to the demand model."""
        event = self.model.last_consumption
        if event is None or (
            self._last_draw_end is not None and event.end <= self._last_draw_end
        ):
            return
        self._last_draw_end = event.end
        start = dt_util.as_local(event.start)
        useful = max(
            0.0,
            params.heat_capacity
            * (event.start_temperature - params.min_useful_temperature),
        )
        draw = self.demand.add_draw(
            start,
            event.energy_kwh,
            self.day_type_of(start.date()),
            useful_at_start_kwh=useful,
            shower_energy_kwh=params.shower_energy_kwh,
        )
        self._demand_changed = True
        if draw.failure:
            _LOGGER.info("Draw at %s started without enough hot water", start)

    def _demand_snapshot(self, now: datetime) -> DemandSnapshot:
        today = now.date()
        if (
            self._demand_changed
            or self._forecast_at is None
            or now - self._forecast_at >= FORECAST_REFRESH
            or today != self._forecast_at.date()
        ):
            self._next_draw = self.demand.next_draw(
                now,
                comfort_percentile(self.parameters[CONF_COMFORT_LEVEL]),
                self.day_type_of,
                hours=FORECAST_HORIZON_HOURS,
            )
            self._forecast_at = now
            self._demand_changed = False
        return DemandSnapshot(
            status=self.demand.status(today),
            days_with_data=self.demand.days_with_data(today),
            today_type=self.day_type_of(today),
            shower_energy_kwh=self.shower_energy,
            learned_shower_energy_kwh=self.demand.shower_energy(today),
            next_draw=self._next_draw,
        )

    async def async_import_history(
        self, text: str, *, manual_mode_is_failure: bool
    ) -> tuple[ImportSummary, int]:
        """Import raw tank readings, replayed through a fresh tank model.

        Returns the import summary and the number of skipped CSV rows.
        """
        local_tz = dt_util.get_default_time_zone()
        base = self.tank_params
        override = self.config_entry.options.get(CONF_MAINS_WATER_TEMPERATURE)
        region = self.parameters[CONF_WATER_REGION]

        def params_for(moment: datetime) -> TankParams:
            if override is not None:
                return base
            month = moment.astimezone(local_tz).month
            return replace(
                base, mains_temperature=mains_water_temperature(region, month)
            )

        parsed = await self.hass.async_add_executor_job(
            parse_history_csv, text, local_tz
        )
        result = await self.hass.async_add_executor_job(
            replay_history, parsed.rows, params_for, local_tz
        )
        today = dt_util.now().date()
        await self._async_refresh_workdays(
            today, sorted(day for day in result.observed_days if day != today)
        )
        summary = apply_to_demand(
            result,
            self.demand,
            self.day_type_of,
            self.shower_energy,
            local_tz,
            manual_mode_is_failure=manual_mode_is_failure,
        )
        _LOGGER.info(
            "Imported %s days (%s draws, %s failures); %s rows skipped",
            summary.days,
            summary.draws,
            summary.failures,
            parsed.skipped_rows,
        )
        self._demand_changed = True
        await self.async_save_now()
        await self.async_request_refresh()
        return summary, parsed.skipped_rows

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


def _numeric(state: State | None) -> float | None:
    """Return the numeric value of a state, or None."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _power_watts(state: State | None) -> float | None:
    """Return a power state in W, or None."""
    value = _numeric(state)
    if value is None or state is None:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit not in PowerConverter.VALID_UNITS:
        return None
    return PowerConverter.convert(value, unit, UnitOfPower.WATT)
