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

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .bootstrap import BootstrapInfo, BootstrapStatus, async_bootstrap
from .compensation_history import async_import_compensation
from .const import (
    BOOTSTRAP_DAYS,
    BOOTSTRAP_INPUT_WAIT,
    BOOTSTRAP_VERSION,
    CONF_AMBIENT_SENSOR,
    CONF_AMBIENT_TEMPERATURE,
    CONF_APPOINTMENTS,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_HOT_WATER_FLOW_SENSOR,
    CONF_HOT_WATER_VOLUME_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MAINTENANCE_ENTITY,
    CONF_MAINTENANCE_MARGIN,
    CONF_MANUAL_MODE_SWITCH,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PROBE_POSITION,
    CONF_PROBE_VOLUME_BELOW,
    CONF_PROFILE_DAYS,
    CONF_PROFILE_MODE,
    CONF_RATED_POWER,
    CONF_REGULATOR_ACTIVE_MODES,
    CONF_REGULATOR_MODE_ENTITY,
    CONF_REGULATOR_OUTPUT_SWITCH,
    CONF_REGULATOR_RESTART_REASON_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_REGULATOR_UPTIME_SENSOR,
    CONF_RESCUE,
    CONF_TEMPERATURE_SENSOR,
    CONF_USABLE_FACTOR_DEFAULT,
    CONF_VOLUME,
    CONF_WATER_REGION,
    CONF_WORKDAY_ENTITY,
    DEFAULT_MAINTENANCE_MARGIN,
    DEFAULT_PARAMETERS,
    DEFAULT_SETTINGS,
    DOMAIN,
    EVENT_PLAN_AT_RISK,
    EVENT_POWER_REQUEST,
    FORECAST_HOURS,
    MAINS_AUTOMATIC,
    MAINS_SOURCE_MANUAL,
    MAINS_SOURCE_TABLE,
    PART_ALL,
    PART_DEMAND,
    PART_LOSSES,
    PROBE_SHEATH,
    REQUIRED_INPUT_KEYS,
    SETTING_COMFORT_MIN_TEMPERATURE,
    SETTING_MAINS_WATER_TEMPERATURE,
    SETTING_SHOWER_TEMPERATURE,
    SHORTAGE_WINDOW,
    UPDATE_INTERVAL,
)
from .controller import TankWiseController
from .inputs import (
    InputError,
    parse_bool,
    parse_measurement,
    parse_numeric,
    parse_text,
    state_unit,
)
from .model.appointments import (
    Appointment,
    AppointmentRequirement,
    AppointmentUsage,
    appointment_requirement,
    appointment_usage,
    load_appointments,
)
from .model.compensation import CompensationEstimate
from .model.demand import DayTypeMode, DemandForecast, DemandModel, DemandParams
from .model.ledger import Totals
from .model.maintenance import MaintenanceStatus, MaintenanceTracker
from .model.optimizer import Plan
from .model.planning import Mode
from .model.replay import (
    ROLE_AMBIENT,
    ROLE_ENERGY,
    ROLE_HOT_WATER_FLOW,
    ROLE_HOT_WATER_VOLUME,
    ROLE_MANUAL_MODE,
    ROLE_OCCUPANCY,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_REGULATOR_MODE,
    ROLE_REGULATOR_OUTPUT,
    ROLE_REGULATOR_RESTART_REASON,
    ROLE_REGULATOR_UPTIME,
    ROLE_TEMPERATURE,
    ROLE_WORKDAY,
)
from .model.tank import (
    DEFAULT_REGULATOR_ACTIVE_MODES,
    PowerDropDetector,
    TankModel,
    TankParams,
    TankSample,
    TankState,
)
from .model.water import mains_water_temperature
from .planner import Planner
from .storage import TankWiseStore

_LOGGER = logging.getLogger(__name__)

type TankWiseConfigEntry = ConfigEntry[TankWiseCoordinator]

RISK_APPOINTMENTS = "appointments"
RISK_DEMAND = "demand"


@dataclass(frozen=True, slots=True)
class TankReadings:
    """Normalised snapshot of the inputs.

    Temperatures are in °C, power in W and energy in kWh. ``None`` means the
    input is not configured or its state could not be used.
    """

    timestamp: datetime
    temperature: float | None
    power: float | None
    # Last time the probe reported a value (even an unchanged one).
    temperature_updated: datetime | None = None
    energy: float | None = None
    regulator: float | None = None
    manual_mode: bool | None = None
    heater_switch: bool | None = None
    regulator_output: bool | None = None
    master: bool | None = None
    occupancy: bool | None = None
    price: float | None = None
    injection_price: float | None = None
    grid_import: float | None = None
    grid_export: float | None = None
    export_power: float | None = None
    ambient_temperature: float | None = None
    workday: bool | None = None
    # Hot water meter: cumulative litres and flow rate (L/min).
    hot_water_volume: float | None = None
    hot_water_flow: float | None = None

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
    forecast: DemandForecast
    # P50 and P90 (kWh) of today: what was drawn so far plus the rest of
    # today; and of the rest of today alone.
    demand_today: tuple[float, float]
    demand_remaining: tuple[float, float]
    # Drawn so far today: kWh and litres (None: no hot water meter).
    observed_today: tuple[float, float | None] = (0.0, None)
    # P50 and P90 of a whole usual day of today's type (profile): kWh, litres.
    profile_today: tuple[float, float] = (0.0, 0.0)
    profile_liters_today: tuple[float, float] | None = None
    appointment_usage: dict[str, AppointmentUsage] = field(default_factory=dict)
    appointment_requirements: dict[str, AppointmentRequirement] = field(
        default_factory=dict
    )
    # Active appointments the tank cannot cover even with the rescue.
    at_risk: list[str] = field(default_factory=list)
    # Surplus power TankWise wants now (W) and its priority (SolarSplit).
    power_request: tuple[float, str] = (0.0, "none")
    # Last anti-legionella cycle really done (60 °C or more for an hour).
    legionella_last: datetime | None = None
    # Compensation and energy term of the billing period (None: no grid meters
    # or no injection price).
    compensation: CompensationEstimate | None = None
    # Why hot water cannot be guaranteed: "appointments", "demand",
    # "regulator_unavailable", "probe_unavailable" (empty: plan not at risk).
    risk_reasons: list[str] = field(default_factory=list)
    plan: Plan | None = None
    totals_today: Totals = field(default_factory=Totals)
    totals_month: Totals = field(default_factory=Totals)
    # Maintenance window: devices updated or restarting, readings not learned.
    maintenance: MaintenanceStatus = field(default_factory=MaintenanceStatus)


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
            float(self.parameters[CONF_CUTOFF_POWER_THRESHOLD]),
            self.regulator_active_modes,
        )
        self._store = TankWiseStore(hass, entry.entry_id)
        self.model = TankModel()
        self.settings: dict[str, float] = dict(DEFAULT_SETTINGS)
        self.bootstrap = BootstrapInfo(BootstrapStatus.PENDING)
        self._last_sample: TankSample | None = None
        self.demand = DemandModel(dt_util.get_default_time_zone())
        # Workday status of the coming days, from the workday integration.
        self._workdays: dict[date, bool] = {}
        self.planner = Planner(self)
        self._bootstrap_task: asyncio.Task | None = None
        # Stored sections that could not be read: kept as they were, never
        # overwritten, so an upgrade can never erase what was learned.
        self._unreadable: dict[str, Any] = {}
        self.controller = TankWiseController(self)
        self.maintenance = MaintenanceTracker(
            timedelta(
                minutes=float(
                    entry.options.get(
                        CONF_MAINTENANCE_MARGIN, DEFAULT_MAINTENANCE_MARGIN
                    )
                )
            )
        )
        # Device ID -> name, of the devices whose restarts open a window.
        self.maintenance_devices: dict[str, str] = {}

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
                _LOGGER.warning("Stored tank state not readable, kept aside: %s", err)
                self._unreadable["tank"] = stored["tank"]
            else:
                if removed := self.model.purge_cutoffs(self.tank_params):
                    _LOGGER.info(
                        "Forgot %d learned cut-offs below the plausible range", removed
                    )
        if "demand" in stored:
            try:
                self.demand = DemandModel.from_dict(stored["demand"], self.demand.tz)
            except ValueError as err:
                _LOGGER.warning(
                    "Stored demand profile not readable, kept aside: %s", err
                )
                self._unreadable["demand"] = stored["demand"]
        self._unreadable |= stored.get("unreadable") or {}
        self.planner.restore(stored.get("control") or {})
        self.controller.restore(stored.get("controller") or {})

    def _stored_data(self) -> dict[str, Any]:
        return {
            "tank": self.model.as_dict(),
            "demand": self.demand.as_dict(),
            "settings": dict(self.settings),
            "bootstrap": self.bootstrap.as_dict(),
            "control": self.planner.as_dict(),
            "controller": self.controller.as_dict(),
            **({"unreadable": self._unreadable} if self._unreadable else {}),
        }

    async def async_save_now(self) -> None:
        """Persist the state immediately (e.g. on unload)."""
        await self._store.async_save(self._stored_data())

    # ------------------------------------------------------------------
    # Bootstrap from the recorder
    # ------------------------------------------------------------------

    @property
    def needs_bootstrap(self) -> bool:
        """Return True if the history must be imported (again).

        It is imported again when the last import was made with older models
        (e.g. TankWise 0.3 had no demand profile nor energy reconciliation),
        when it did not learn anything, or when a hot water meter was added
        (the draws of the imported days are measured again in litres).
        """
        if self.bootstrap.status in (
            BootstrapStatus.PENDING,
            BootstrapStatus.RUNNING,
            BootstrapStatus.EMPTY,
        ):
            return True
        if self.bootstrap.status is BootstrapStatus.NO_RECORDER:
            return False
        return self.bootstrap.version < BOOTSTRAP_VERSION or (
            self.meter_configured and not self.bootstrap.metered
        )

    @callback
    def async_start_compensation_import(self) -> None:
        """Add the missing past hours of the compensation from the statistics."""
        data = self.config_entry.data
        options = self.config_entry.options
        sensors = (
            options.get(CONF_GRID_IMPORT_SENSOR),
            options.get(CONF_GRID_EXPORT_SENSOR),
            data.get(CONF_PRICE_SENSOR),
            options.get(CONF_INJECTION_PRICE_SENSOR),
        )
        if not all(sensors):
            return

        async def _import() -> None:
            added = await async_import_compensation(
                self.hass,
                self.planner.compensation,
                import_sensor=sensors[0],
                export_sensor=sensors[1],
                buy_sensor=sensors[2],
                injection_sensor=sensors[3],
            )
            if added:
                _LOGGER.debug("Compensation: %d past hours imported", added)
                self._store.async_schedule_save(self._stored_data)
                await self.async_request_refresh()

        self.config_entry.async_create_background_task(
            self.hass, _import(), f"{DOMAIN} compensation import"
        )

    @callback
    def async_start_bootstrap(self) -> bool:
        """Import the history in the background; False if already running."""
        if self._bootstrap_task is not None and not self._bootstrap_task.done():
            return False
        self._bootstrap_task = self.config_entry.async_create_background_task(
            self.hass, self.async_run_bootstrap(), f"{DOMAIN} history import"
        )
        return True

    def _inputs_ready(self) -> bool:
        """Return True when the probe and the power have a usable value."""
        data = self.config_entry.data
        for key, kind in (
            (CONF_TEMPERATURE_SENSOR, "temperature"),
            (CONF_POWER_SENSOR, "power"),
        ):
            state = self.hass.states.get(data.get(key) or "")
            if state is None:
                return False
            try:
                parse_measurement(state.state, state_unit(state), kind)
            except InputError:
                return False
        return True

    async def _async_wait_for_inputs(self) -> bool:
        """Wait until the entry is complete and the inputs report values.

        Right after Home Assistant starts, the sensors of other integrations
        may not exist yet: their units would be unknown and the import would
        learn nothing.
        """
        if any(not self.config_entry.data.get(key) for key in REQUIRED_INPUT_KEYS):
            _LOGGER.info("History import postponed: the entry is not complete")
            return False
        if not self.hass.is_running:
            started = self.hass.loop.create_future()

            @callback
            def _started(_hass: HomeAssistant) -> None:
                if not started.done():
                    started.set_result(None)

            cancel_started = async_at_started(self.hass, _started)
            try:
                await started
            finally:
                cancel_started()
        ready = asyncio.Event()

        @callback
        def _check(_event: Event[EventStateChangedData] | None = None) -> None:
            if self._inputs_ready():
                ready.set()

        _check()
        if ready.is_set():
            return True
        data = self.config_entry.data
        unsubscribe = async_track_state_change_event(
            self.hass,
            [data[CONF_TEMPERATURE_SENSOR], data[CONF_POWER_SENSOR]],
            _check,
        )
        try:
            try:
                async with asyncio.timeout(BOOTSTRAP_INPUT_WAIT.total_seconds()):
                    await ready.wait()
            except TimeoutError:
                # Keep waiting: the import must not be lost until the next
                # restart because a sensor came back late.
                _LOGGER.warning(
                    "History import waiting: the probe or the power is not available"
                )
                await ready.wait()
        finally:
            unsubscribe()
        return True

    async def async_run_bootstrap(self) -> None:
        """Import the recorder history and merge it with what was learned."""
        if not await self._async_wait_for_inputs():
            return
        previous = self.bootstrap
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
            ROLE_OCCUPANCY: options.get(CONF_OCCUPANCY_ENTITY),
            ROLE_WORKDAY: options.get(CONF_WORKDAY_ENTITY),
            ROLE_REGULATOR_MODE: options.get(CONF_REGULATOR_MODE_ENTITY),
            ROLE_REGULATOR_UPTIME: options.get(CONF_REGULATOR_UPTIME_SENSOR),
            ROLE_REGULATOR_RESTART_REASON: options.get(
                CONF_REGULATOR_RESTART_REASON_SENSOR
            ),
            ROLE_REGULATOR_OUTPUT: options.get(CONF_REGULATOR_OUTPUT_SWITCH),
            ROLE_HOT_WATER_VOLUME: options.get(CONF_HOT_WATER_VOLUME_SENSOR),
            ROLE_HOT_WATER_FLOW: options.get(CONF_HOT_WATER_FLOW_SENSOR),
        }
        info, result, demand = await async_bootstrap(
            self.hass,
            roles,
            self.tank_params_at,
            days=BOOTSTRAP_DAYS,
            tz=self.demand.tz,
            active_modes=self.regulator_active_modes,
        )
        if info.status is BootstrapStatus.DONE and result is not None:
            # Never lose learning: keep the better learned of each parameter.
            result.model.merge_learning(self.model)
            self.model = result.model
            if demand is not None:
                demand.merge(self.demand)
                self.demand = demand
            self.bootstrap = replace(info, version=BOOTSTRAP_VERSION)
            await self.async_save_now()
            await self.async_request_refresh()
            return
        if info.status is BootstrapStatus.EMPTY:
            # Nothing adopted; imported again at the next start.
            _LOGGER.warning(
                "The history import learned nothing from %d samples; "
                "keeping the current model",
                info.samples,
            )
            self.bootstrap = info
        elif previous.status is BootstrapStatus.DONE and info.status in (
            BootstrapStatus.NO_HISTORY,
            BootstrapStatus.FAILED,
        ):
            self.bootstrap = previous  # a failed re-import changes nothing
        else:
            self.bootstrap = replace(info, version=BOOTSTRAP_VERSION)
        self._store.async_schedule_save(self._stored_data)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @callback
    def async_start_listeners(self) -> None:
        """Follow power, regulator and manual mode changes between samples."""
        detector = self._drop_detector
        options = self.config_entry.options
        power = self.config_entry.data.get(CONF_POWER_SENSOR)
        regulator = options.get(CONF_REGULATOR_SENSOR)
        manual = self.config_entry.data.get(CONF_MANUAL_MODE_SWITCH)
        occupancy = options.get(CONF_OCCUPANCY_ENTITY)
        # Regulator health: only its availability, restarts and mode matter.
        health: dict[str, Callable[[datetime, State | None], None]] = {}
        for key, update in (
            (
                CONF_REGULATOR_MODE_ENTITY,
                lambda moment, state: detector.update_mode(moment, _text(state)),
            ),
            (
                CONF_REGULATOR_UPTIME_SENSOR,
                lambda moment, state: detector.update_uptime(moment, _numeric(state)),
            ),
            (
                CONF_REGULATOR_RESTART_REASON_SENSOR,
                lambda moment, state: detector.update_restart_reason(
                    moment, _text(state)
                ),
            ),
            (
                CONF_REGULATOR_OUTPUT_SWITCH,
                lambda moment, state: detector.update_output(moment, _boolean(state)),
            ),
        ):
            if entity_id := options.get(key):
                health[entity_id] = update
        now = dt_util.utcnow()
        for entity_id, update in health.items():
            update(now, self.hass.states.get(entity_id))
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
                old_state = event.data["old_state"]
                old, new = _boolean(old_state), _boolean(new_state)
                moment = new_state.last_changed
                if (
                    old is not None
                    and new is not None
                    and old != new
                    # A regulator restarting in a maintenance window is not
                    # an external order nor a deviation from the plan.
                    and not self.maintenance_status(moment).active
                    and not self.controller.on_manual_change(moment, new)
                ):
                    self.planner.on_manual_mode(moment, new)
            elif entity_id == occupancy:
                self.async_request_replan("occupancy")
            elif (update := health.get(entity_id)) is not None:
                # last_updated: a restart may report the same reason again.
                update(new_state.last_updated, new_state)
            else:
                detector.update_power(new_state.last_changed, _power_watts(new_state))

        tracked = [
            entity_id
            for entity_id in (power, regulator, manual, occupancy, *health)
            if entity_id
        ]
        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, tracked, _async_changed)
        )
        self._async_start_maintenance()

    # ------------------------------------------------------------------
    # Maintenance window
    # ------------------------------------------------------------------

    def _async_start_maintenance(self) -> None:
        """Follow the maintenance entity and the devices of the inputs.

        A device that becomes unavailable, or whose uptime sensor goes back
        (a restart), opens a maintenance window. Its uptime sensors are found
        in the device registry: sensors of the device called uptime.
        """
        tracker = self.maintenance
        entity_id = self.config_entry.options.get(CONF_MAINTENANCE_ENTITY)
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        config = {**self.config_entry.data, **self.config_entry.options}
        inputs: dict[str, str] = {}  # entity -> device
        for key in MAINTENANCE_INPUT_KEYS:
            if (
                (input_id := config.get(key))
                and (entry := entities.async_get(input_id)) is not None
                and entry.device_id
            ):
                inputs[input_id] = entry.device_id
        uptimes: dict[str, str] = {}
        for device_id in set(inputs.values()):
            for entry in er.async_entries_for_device(entities, device_id):
                if _is_uptime(entry):
                    uptimes[entry.entity_id] = device_id
            if (device := devices.async_get(device_id)) is not None:
                self.maintenance_devices[device_id] = (
                    device.name_by_user or device.name or device_id
                )

        def _availability(device_id: str, moment: datetime) -> None:
            available = not any(
                (state := self.hass.states.get(item)) is not None
                and state.state == STATE_UNAVAILABLE
                for item, device in inputs.items()
                if device == device_id
            )
            tracker.availability(device_id, moment, available)

        now = dt_util.utcnow()
        if entity_id:
            tracker.entity(now, _is_on(self.hass.states.get(entity_id)))
        for device_id in set(inputs.values()):
            _availability(device_id, now)
        for item, device_id in uptimes.items():
            tracker.uptime(device_id, now, _uptime(self.hass.states.get(item)))

        @callback
        def _async_changed(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            item = event.data["entity_id"]
            moment = new_state.last_updated if new_state else dt_util.utcnow()
            if item == entity_id:
                tracker.entity(moment, _is_on(new_state))
            if item in inputs:
                _availability(inputs[item], moment)
            if item in uptimes:
                tracker.uptime(uptimes[item], moment, _uptime(new_state))

        tracked = {*inputs, *uptimes, *([entity_id] if entity_id else [])}
        if tracked:
            self.config_entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(tracked), _async_changed
                )
            )

    def maintenance_status(self, now: datetime) -> MaintenanceStatus:
        """The maintenance window open now, if any."""
        return self.maintenance.status(now)

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
            probe_at_top=parameters[CONF_PROBE_POSITION] != PROBE_SHEATH,
            usable_factor_default=float(parameters[CONF_USABLE_FACTOR_DEFAULT]),
            probe_volume_below=float(parameters[CONF_PROBE_VOLUME_BELOW]) / 100,
        )

    @property
    def tank_params(self) -> TankParams:
        """Return the tank model parameters now."""
        return self.tank_params_at(dt_util.utcnow())

    @property
    def demand_params(self) -> DemandParams:
        """Return the settings of the demand profile."""
        parameters = self.parameters
        try:
            mode = DayTypeMode(parameters[CONF_PROFILE_MODE])
        except ValueError:
            mode = DayTypeMode.WORKDAY_WEEKEND
        return DemandParams(mode=mode, observations=int(parameters[CONF_PROFILE_DAYS]))

    @property
    def appointments(self) -> list[Appointment]:
        """Return the hot water appointments of the options."""
        return load_appointments(self.config_entry.options.get(CONF_APPOINTMENTS, []))

    def async_update_appointment(self, appointment_id: str, **changes: Any) -> None:
        """Change an appointment from its entities and store it in the options."""
        items = []
        for item in self.config_entry.options.get(CONF_APPOINTMENTS, []):
            if item.get("id") == appointment_id:
                item = {**item, **changes}
            items.append(item)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={**self.config_entry.options, CONF_APPOINTMENTS: items},
        )
        self.async_request_replan("appointment")
        if self.data is not None:
            usage = self._appointment_usage()
            requirements, at_risk = self._appointment_requirements(
                usage, self.data.tank, self.data.readings.occupancy
            )
            self.async_set_updated_data(
                replace(
                    self.data,
                    appointment_usage=usage,
                    appointment_requirements=requirements,
                    at_risk=at_risk,
                )
            )

    @property
    def meter_configured(self) -> bool:
        """Return True if a hot water meter measures the draws."""
        return bool(self.config_entry.options.get(CONF_HOT_WATER_VOLUME_SENSOR))

    @property
    def regulator_active_modes(self) -> tuple[str, ...]:
        """Regulator modes that heat (a drop in another mode is not a cut-off)."""
        modes = self.config_entry.options.get(CONF_REGULATOR_ACTIVE_MODES)
        return tuple(modes) if modes else DEFAULT_REGULATOR_ACTIVE_MODES

    async def async_reset_usable_factor(self) -> int:
        """Forget the learned useful litres factor; return how many samples it had."""
        count = self.model.reset_usable_factor()
        _LOGGER.info("Useful litres factor reset (%d samples forgotten)", count)
        if self._last_sample is not None and self.data is not None:
            tank = self.model.update(self._last_sample, self.tank_params)
            self.async_set_updated_data(replace(self.data, tank=tank))
        await self.async_save_now()
        await self.async_replan("usable_factor")
        return count

    async def async_discard_cutoff_sample(
        self, *, index: int | None = None, value: float | None = None
    ) -> float:
        """Forget one learned cut-off sample; the cut-off is computed again.

        Raises ValueError if no sample matches.
        """
        removed = self.model.discard_cutoff_sample(index=index, value=value)
        _LOGGER.info(
            "Cut-off sample %.2f °C discarded; %d left",
            removed,
            len(self.model.cutoff_samples),
        )
        if self._last_sample is not None and self.data is not None:
            # Same timestamp as the last sample: the derived values (cut-off,
            # charge, status) are recomputed without advancing the model.
            tank = self.model.update(self._last_sample, self.tank_params)
            self.async_set_updated_data(replace(self.data, tank=tank))
        await self.async_save_now()
        await self.async_replan("cutoff")
        return removed

    async def async_reset_learning(self, part: str) -> None:
        """Forget learned data: losses, the demand profile or everything."""
        if part == PART_LOSSES:
            self.model.reset_losses()
        elif part == PART_DEMAND:
            self.demand.reset()
        elif part == PART_ALL:
            self.model.reset_learning()
            self.demand.reset()
        _LOGGER.info("Learned data reset: %s", part)
        await self.async_save_now()
        await self.async_request_refresh()

    @callback
    def async_request_replan(self, reason: str) -> None:
        """Compute a new plan soon (debounced refresh)."""
        self.planner.request(reason)
        self.hass.async_create_task(self.async_request_refresh())

    async def async_replan(self, reason: str = "manual") -> None:
        """Compute a new plan now (button and service)."""
        self.planner.request(reason)
        await self.async_refresh()

    async def async_set_mode(self, mode: Mode) -> None:
        """Change the operating mode."""
        self.planner.mode = mode
        await self.async_save_now()
        await self.async_replan("mode")

    async def async_set_anti_legionella(self, enabled: bool) -> None:
        """Enable or disable the weekly anti-legionella cycle."""
        self.planner.anti_legionella = enabled
        await self.async_save_now()
        await self.async_replan("anti_legionella")

    async def async_set_compensation_cap(self, reached: bool) -> None:
        """Mark whether the compensation cap of the month was reached."""
        self.planner.compensation_cap = reached
        await self.async_save_now()
        await self.async_replan("compensation_cap")

    async def async_set_setting(self, key: str, value: float) -> None:
        """Change a runtime setting (number entity) and refresh the state."""
        self.settings[key] = float(value)
        self._store.async_schedule_save(self._stored_data)
        self.async_request_replan("setting")
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
        maintenance = self.maintenance_status(readings.timestamp)
        sample = TankSample(
            timestamp=readings.timestamp,
            temperature=readings.temperature,
            power=readings.power,
            energy=readings.energy,
            regulator=readings.regulator,
            manual_mode=readings.manual_mode,
            ambient_temperature=readings.ambient_temperature,
            power_drop=self._drop_detector.drop,
            regulator_configured=bool(
                self.config_entry.data.get(CONF_MANUAL_MODE_SWITCH)
                or self.config_entry.options.get(CONF_REGULATOR_SENSOR)
            ),
            hot_water_volume=readings.hot_water_volume,
            hot_water_flow=readings.hot_water_flow,
            meter_configured=self.meter_configured,
            maintenance=maintenance.active,
        )
        tank = self.model.update(sample, self.tank_params_at(readings.timestamp))
        self._last_sample = sample
        now = readings.timestamp
        self.demand.update(
            now,
            # The window is not covered time: a day is learned without it.
            available=tank.available and not maintenance.active,
            occupied=readings.occupancy,
            draws=self.model.consumptions,
            workday=readings.workday,
            metered=self.meter_configured and readings.hot_water_volume is not None,
        )
        workdays = await self._async_workdays(now, readings.workday)
        params = self.demand_params
        forecast = self.demand.forecast(
            now,
            params,
            hours=FORECAST_HOURS,
            occupied=readings.occupancy,
            workdays=workdays,
        )
        today = dt_util.as_local(now).date()
        midnight = dt_util.start_of_local_day(today + timedelta(days=1))
        if readings.occupancy is False:
            profile_today: tuple[float, float] = (0.0, 0.0)
            profile_liters = None
        else:
            profile_today = self.demand.day_totals(
                today, now, params, workdays.get(today)
            )
            profile_liters = self.demand.day_liters(
                today, now, params, workdays.get(today)
            )
        observed_today = self._observed_today(today)
        remaining = (
            forecast.window(now, midnight, 0.5),
            forecast.window(now, midnight, 0.9),
        )
        await self.planner.async_update(
            now, readings, tank, forecast, workdays, maintenance=maintenance.active
        )
        _mains, source = self.mains_water_temperature_at(now)
        usage = self._appointment_usage()
        requirements, at_risk = self._appointment_requirements(
            usage, tank, readings.occupancy
        )
        await self.controller.async_update(
            now,
            readings,
            tank,
            requirements,
            workdays,
            maintenance=maintenance.active,
            at_risk=bool(at_risk)
            or bool(self.planner.plan and self.planner.plan.at_risk),
        )
        self._store.async_schedule_save(self._stored_data)
        risk_reasons = self._risk_reasons(at_risk)
        power_request = self.planner.power_request(now, tank)
        if self.data is None or power_request != self.data.power_request:
            self.hass.bus.async_fire(
                EVENT_POWER_REQUEST,
                {
                    "entry_id": self.config_entry.entry_id,
                    "name": self.config_entry.title,
                    "power": power_request[0],
                    "priority": power_request[1],
                },
            )
        totals_today, totals_month = self.planner.ledger.current(now)
        return TankWiseData(
            plan=self.planner.plan,
            totals_today=replace(totals_today),
            totals_month=replace(totals_month),
            readings=readings,
            tank=tank,
            shortages_7d=self._count_shortages(now),
            mains_source=source,
            forecast=forecast,
            # What was drawn today counts: the demand of today is never below it.
            demand_today=(
                observed_today[0] + remaining[0],
                observed_today[0] + remaining[1],
            ),
            demand_remaining=remaining,
            observed_today=observed_today,
            profile_today=profile_today,
            profile_liters_today=profile_liters,
            appointment_usage=usage,
            appointment_requirements=requirements,
            at_risk=at_risk,
            risk_reasons=risk_reasons,
            power_request=power_request,
            legionella_last=self.planner.legionella.last_cycle,
            maintenance=maintenance,
            compensation=self.planner.compensation_estimate,
        )

    def _risk_reasons(self, at_risk: list[str]) -> list[str]:
        """Collect why the plan is at risk; fire an event on new reasons."""
        reasons: list[str] = []
        plan = self.planner.plan
        if at_risk:
            reasons.append(RISK_APPOINTMENTS)
        elif plan is not None and plan.at_risk:
            reasons.append(RISK_DEMAND)
        reasons += self.controller.risk_reasons
        announced = self.controller.announced_risks
        new = [reason for reason in reasons if reason not in announced]
        self.controller.announced_risks = list(reasons)
        if new:
            names = {item.id: item.name for item in self.appointments}
            self.hass.bus.async_fire(
                EVENT_PLAN_AT_RISK,
                {
                    "entry_id": self.config_entry.entry_id,
                    "name": self.config_entry.title,
                    "reasons": reasons,
                    "new_reasons": new,
                    "appointments": [names.get(item, item) for item in at_risk],
                },
            )
        return reasons

    def _appointment_requirements(
        self,
        usage: dict[str, AppointmentUsage],
        tank: TankState,
        occupancy: bool | None,
    ) -> tuple[dict[str, AppointmentRequirement], list[str]]:
        """Temperature each appointment needs, and those beyond a full tank."""
        params = self.tank_params
        requirements = {
            appointment.id: appointment_requirement(
                appointment,
                usage.get(appointment.id),
                volume_l=params.volume_l,
                comfort_temperature=tank.useful_floor_temperature,
                shower_temperature=params.shower_temperature,
                mains_temperature=tank.mains_temperature,
                usable_factor=tank.usable_factor,
                max_temperature=tank.cutoff_temperature,
            )
            for appointment in self.appointments
        }
        at_risk: list[str] = []
        if occupancy is False or self.planner.mode is Mode.HOLIDAY:
            return requirements, at_risk  # appointments are suspended
        plan = self.planner.plan
        if plan is not None:
            # The plan knows the rescue recharge and the time left to heat.
            at_risk = sorted(
                {block.id.split("@", 1)[0] for block in plan.blocks if block.at_risk}
            )
            return requirements, at_risk
        rescue = bool(self.parameters[CONF_RESCUE])
        for appointment in self.appointments:
            requirement = requirements[appointment.id]
            if not appointment.enabled or requirement.feasible:
                continue
            # Beyond a full tank: the rescue recharge may cover the rest.
            missing_kwh = params.heat_capacity * (
                requirement.temperature - tank.cutoff_temperature
            )
            rescue_kwh = (
                tank.capacity_factor
                * tank.heater_power_w
                / 1000
                * appointment.duration.total_seconds()
                / 3600
            )
            if not rescue or missing_kwh > rescue_kwh:
                at_risk.append(appointment.id)
        return requirements, at_risk

    def _appointment_usage(self) -> dict[str, AppointmentUsage]:
        """Measure the litres used in the hour after each appointment."""
        now = dt_util.utcnow()
        params = self.tank_params_at(now)
        return {
            appointment.id: appointment_usage(
                appointment,
                self.demand,
                self.demand_params,
                now,
                shower_temperature=params.shower_temperature,
                mains_temperature=params.mains_temperature,
            )
            for appointment in self.appointments
        }

    async def _async_workdays(
        self, now: datetime, today_workday: bool | None
    ) -> dict[date, bool]:
        """Return the workday status of today and the next two days."""
        entity_id = self.config_entry.options.get(CONF_WORKDAY_ENTITY)
        today = dt_util.as_local(now).date()
        self._workdays = {
            day: value for day, value in self._workdays.items() if day >= today
        }
        if not entity_id:
            return {}
        if today_workday is not None:
            self._workdays[today] = today_workday
        for offset in (1, 2):
            day = today + timedelta(days=offset)
            if (
                day not in self._workdays
                and (value := await self._async_check_workday(entity_id, day))
                is not None
            ):
                self._workdays[day] = value
        return dict(self._workdays)

    async def _async_check_workday(self, entity_id: str, day: date) -> bool | None:
        """Ask the workday integration whether a date is a workday."""
        if not self.hass.services.has_service("workday", "check_date"):
            return None
        try:
            response = await self.hass.services.async_call(
                "workday",
                "check_date",
                {"check_date": day},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.debug("Workday check of %s failed: %s", day, err)
            return None
        value = (response or {}).get(entity_id, {}).get("workday")
        return value if isinstance(value, bool) else None

    def _observed_today(self, today: date) -> tuple[float, float | None]:
        """Return the energy and litres of the draws that started today."""
        draws = [
            event
            for event in self.model.consumptions
            if dt_util.as_local(event.start).date() == today
        ]
        energy = sum(event.energy_kwh for event in draws)
        if not self.meter_configured:
            return energy, None
        return energy, sum(event.liters or 0.0 for event in draws)

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
            temperature_updated=self._last_reported(data.get(CONF_TEMPERATURE_SENSOR)),
            power=self._read_measurement(data.get(CONF_POWER_SENSOR), "power"),
            energy=self._read_measurement(options.get(CONF_ENERGY_SENSOR), "energy"),
            regulator=self._read_float(options.get(CONF_REGULATOR_SENSOR)),
            manual_mode=self._read_bool(data.get(CONF_MANUAL_MODE_SWITCH)),
            heater_switch=self._read_bool(data.get(CONF_HEATER_SWITCH)),
            regulator_output=self._read_bool(options.get(CONF_REGULATOR_OUTPUT_SWITCH)),
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
            ambient_temperature=self._read_measurement(
                options.get(CONF_AMBIENT_SENSOR), "temperature"
            ),
            workday=self._read_bool(options.get(CONF_WORKDAY_ENTITY)),
            hot_water_volume=self._read_measurement(
                options.get(CONF_HOT_WATER_VOLUME_SENSOR), "volume"
            ),
            hot_water_flow=self._read_measurement(
                options.get(CONF_HOT_WATER_FLOW_SENSOR), "flow"
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

    def _last_reported(self, entity_id: str | None) -> datetime | None:
        """Return the last time an entity reported its state."""
        if not entity_id or (state := self.hass.states.get(entity_id)) is None:
            return None
        return state.last_reported

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


# Inputs whose devices are watched for maintenance (restarts, short outages).
MAINTENANCE_INPUT_KEYS = (
    CONF_TEMPERATURE_SENSOR,
    CONF_POWER_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_MANUAL_MODE_SWITCH,
    CONF_REGULATOR_SENSOR,
    CONF_REGULATOR_MODE_ENTITY,
    CONF_REGULATOR_OUTPUT_SWITCH,
    CONF_REGULATOR_UPTIME_SENSOR,
    CONF_HOT_WATER_VOLUME_SENSOR,
    CONF_HOT_WATER_FLOW_SENSOR,
)
UPTIME_NAMES = frozenset({"uptime", "tiempo de actividad", "tiempo encendido"})
SECONDS_PER = {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


def _is_uptime(entry: er.RegistryEntry) -> bool:
    """Whether a registry entry is a device's uptime sensor."""
    if entry.domain != "sensor":
        return False
    names = {
        (entry.translation_key or "").lower(),
        (entry.original_name or "").lower(),
        (entry.name or "").lower(),
    }
    return entry.entity_id.endswith("_uptime") or bool(names & UPTIME_NAMES)


def _uptime(state: State | None) -> float | datetime | None:
    """Seconds since boot, or the boot time, of an uptime sensor."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        moment = dt_util.parse_datetime(state.state)
        return dt_util.as_utc(moment) if moment is not None else None
    unit = state.attributes.get("unit_of_measurement", "s")
    return value * SECONDS_PER.get(unit, 1.0)


def _is_on(state: State | None) -> bool | None:
    """On (a script while it runs), off, or None if unknown."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state.state == "on"


def _numeric(state: State | None) -> float | None:
    """Return the numeric value of a state, or None."""
    if state is None:
        return None
    try:
        return parse_numeric(state.state)
    except InputError:
        return None


def _text(state: State | None) -> str | None:
    """Return a text state (mode, reason), or None if unavailable."""
    if state is None:
        return None
    try:
        return parse_text(state.state)
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
