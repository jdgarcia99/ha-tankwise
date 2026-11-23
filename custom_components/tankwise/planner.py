"""Heating plan in Home Assistant: prices, replanning, accounting and logbook.

The plan is computed again at every 15-minute slot and when something that
matters changes (tomorrow's prices arrive, a draw ends, the mode, occupancy,
an appointment or a setting changes, or the tank drifts from the plan). The
optimiser runs in the executor.

The decisions of the plan are accounted as they would have been executed
(planned cost, rescue recharges) and external orders on the regulator manual
mode that contradict the plan are logged. The controller (:mod:`.controller`)
executes the plan in the modes that act.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INJECTION_PRICE_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RESCUE,
    CONF_SOLAR_FORECAST_ENTRIES,
    DEFAULT_MODE,
    REPLAN_DEVIATION,
    SETTING_MAX_TEMPERATURE,
    SETTING_RESERVE_LITERS,
    SETTING_SURPLUS_VALUE,
    SURPLUS_EXPORT_W,
)
from .messages import log
from .model.ledger import Ledger
from .model.legionella import LegionellaTracker, choose_time, target_temperature
from .model.optimizer import Plan, PlanInput, PlanSlot, optimise
from .model.planning import HORIZON_SLOTS, Mode, build_plan_input
from .model.prices import DayPrices, SlotPrices, parse_hourly_prices, slot_prices
from .model.solar import SolarModel, extend_forecast, slot_surplus
from .solar_forecast import async_hourly_forecast

if TYPE_CHECKING:
    from .coordinator import TankReadings, TankWiseCoordinator
    from .model.demand import DemandForecast
    from .model.tank import TankState

_LOGGER = logging.getLogger(__name__)

SLOT = timedelta(minutes=15)
# Grid energy (kWh) the surplus must save for the request to be high priority.
SOLAR_NEEDED_KWH = 0.1
PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"
PRIORITY_NONE = "none"


def slot_start(moment: datetime) -> datetime:
    """Return the local start of the 15-minute slot containing ``moment``."""
    local = dt_util.as_local(moment)
    return local.replace(
        minute=local.minute - local.minute % 15, second=0, microsecond=0
    )


class Planner:
    """Plan, accounting and logbook of one TankWise entry."""

    def __init__(self, coordinator: TankWiseCoordinator) -> None:
        """Initialise the planner."""
        self.coordinator = coordinator
        self.hass: HomeAssistant = coordinator.hass
        self.mode = Mode(DEFAULT_MODE)
        self.compensation_cap = False
        self.plan: Plan | None = None
        self.plan_computed: datetime | None = None
        self.plan_reason: str | None = None
        self.prices: SlotPrices | None = None
        self.ledger = Ledger(dt_util.get_default_time_zone())
        self.solar = SolarModel(dt_util.get_default_time_zone())
        # Summed solar production forecast, kWh per local hour start.
        self.solar_forecast: dict[datetime, float] | None = None
        self.anti_legionella = True
        self.legionella = LegionellaTracker()
        self._legionella_impossible = False
        # The plan needs the surplus for comfort (SolarSplit priority).
        self.solar_needed = False
        self._requested: str | None = "start"
        self._tomorrow_known: bool | None = None
        self._draws = 0
        self._last_energy: float | None = None
        self._last_power: float | None = None
        self._last_time: datetime | None = None
        self._logged_rescues: set[str] = set()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def restore(self, data: dict[str, Any]) -> None:
        """Restore the control state and the ledger."""
        try:
            self.mode = Mode(data.get("mode", DEFAULT_MODE))
        except ValueError:
            self.mode = Mode(DEFAULT_MODE)
        self.compensation_cap = bool(data.get("compensation_cap", False))
        if ledger := data.get("ledger"):
            try:
                self.ledger = Ledger.from_dict(ledger, self.ledger.tz)
            except ValueError as err:
                _LOGGER.warning("Discarding stored cost accounting: %s", err)
        self.anti_legionella = bool(data.get("anti_legionella", True))
        if legionella := data.get("legionella"):
            try:
                self.legionella = LegionellaTracker.from_dict(legionella)
            except ValueError as err:
                _LOGGER.warning("Discarding stored anti-legionella state: %s", err)
        if solar := data.get("solar"):
            try:
                self.solar = SolarModel.from_dict(solar, self.ledger.tz)
            except ValueError as err:
                _LOGGER.warning("Discarding stored solar model: %s", err)

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent control state."""
        return {
            "mode": self.mode.value,
            "compensation_cap": self.compensation_cap,
            "ledger": self.ledger.as_dict(),
            "solar": self.solar.as_dict(),
            "anti_legionella": self.anti_legionella,
            "legionella": self.legionella.as_dict(),
        }

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def request(self, reason: str) -> None:
        """Ask for a new plan at the next update."""
        self._requested = reason

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def _day_prices(
        self, entity_id: str | None, today
    ) -> tuple[DayPrices | None, float | None]:
        if not entity_id:
            return None, None
        state: State | None = self.hass.states.get(entity_id)
        if state is None:
            return None, None
        try:
            current = float(state.state)
        except ValueError:
            current = None
        return parse_hourly_prices(state.attributes, today), current

    def _slot_prices(self, start: datetime) -> SlotPrices | None:
        coordinator = self.coordinator
        today = start.date()
        buy, current = self._day_prices(
            coordinator.config_entry.data.get(CONF_PRICE_SENSOR), today
        )
        if buy is None:
            return None
        injection, _ = self._day_prices(
            coordinator.config_entry.options.get(CONF_INJECTION_PRICE_SENSOR), today
        )
        return slot_prices(
            start,
            HORIZON_SLOTS,
            start.tzinfo,
            buy=buy,
            injection=injection,
            fixed_surplus_value=coordinator.settings[SETTING_SURPLUS_VALUE],
            compensation_cap_reached=self.compensation_cap,
            current_buy=current,
        )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def current_slot(self, now: datetime) -> PlanSlot | None:
        """Return the slot of the plan containing ``now``."""
        if self.plan is None:
            return None
        start = slot_start(now)
        for slot in self.plan.slots:
            if slot.start == start:
                return slot
        return None

    async def async_update(
        self,
        now: datetime,
        readings: TankReadings,
        tank: TankState,
        forecast: DemandForecast,
        workdays: dict,
    ) -> None:
        """Account the real cost and replan when needed."""
        start = slot_start(now)
        self.prices = self._slot_prices(start)
        energy, from_surplus = self._account_real(now, readings)
        await self._async_update_solar(now, readings, energy if from_surplus else 0.0)
        if self.legionella.update(now, readings.temperature):
            self.log("anti_legionella_done")
            self.request("legionella")

        reason = self._replan_reason(start, tank)
        if self.mode is Mode.OFF:
            self.plan = None
        elif reason is not None and tank.available and self.prices is not None:
            await self._async_replan(start, reason, readings, tank, forecast, workdays)
        elif self.prices is None:
            self.plan = None

        if self.plan is not None and (slot := self.current_slot(now)) is not None:
            self.ledger.commit_slot(
                slot.start, grid_kwh=slot.grid_kwh, cost=slot.cost, rescue=slot.rescue
            )

    def _replan_reason(self, start: datetime, tank: TankState) -> str | None:
        reason = self._requested
        draws = len(self.coordinator.model.consumptions)
        if reason is None and draws != self._draws:
            reason = "draw"
        if (
            reason is None
            and self.prices is not None
            and (self.prices.tomorrow_known != self._tomorrow_known)
        ):
            reason = "prices"
        if reason is None and (self.plan is None or self.plan.slots[0].start != start):
            reason = "slot"
        if (
            reason is None
            and self.plan is not None
            and tank.equivalent_temperature is not None
            and abs(self.plan.slots[0].temperature - tank.equivalent_temperature)
            > REPLAN_DEVIATION
        ):
            reason = "deviation"
        self._draws = draws
        return reason

    async def _async_replan(
        self,
        start: datetime,
        reason: str,
        readings: TankReadings,
        tank: TankState,
        forecast: DemandForecast,
        workdays: dict,
    ) -> None:
        coordinator = self.coordinator
        assert self.prices is not None and tank.equivalent_temperature is not None
        params = coordinator.tank_params_at(start)
        ambient = (
            readings.ambient_temperature
            if readings.ambient_temperature is not None
            else params.ambient_temperature
        )
        data = build_plan_input(
            start=start,
            tz=start.tzinfo,
            mode=self.mode,
            temperature=tank.equivalent_temperature,
            volume_l=params.volume_l,
            mains_temperature=tank.mains_temperature,
            ambient_temperature=ambient,
            # Stratified tank: the energy is useful down to the mains water.
            comfort_temperature=tank.useful_floor_temperature,
            shower_temperature=params.shower_temperature,
            max_temperature=min(
                tank.cutoff_temperature,
                coordinator.settings[SETTING_MAX_TEMPERATURE],
            ),
            heater_power_w=tank.heater_power_w,
            capacity_factor=tank.capacity_factor,
            usable_factor=tank.usable_factor,
            ua_w_per_k=tank.ua_w_per_k,
            prices=self.prices,
            forecast=forecast,
            appointments=coordinator.appointments,
            workdays=workdays,
            occupied=readings.occupancy,
            reserve_liters=coordinator.settings[SETTING_RESERVE_LITERS],
            rescue=bool(coordinator.parameters[CONF_RESCUE]),
            solar_kwh=self.expected_surplus(start, tank.heater_power_w),
        )
        if data is None:
            self.plan = None
            return
        if (legionella := self._legionella_target(start, tank, data)) is not None:
            data = replace(data, legionella=legionella)
        manual_since = coordinator.controller.state.manual_since
        if manual_since is not None and readings.manual_mode:
            data = replace(
                data,
                initial_run_slots=math.ceil((dt_util.utcnow() - manual_since) / SLOT),
            )
        self.plan = await self.hass.async_add_executor_job(optimise, data)
        self.solar_needed = await self._async_solar_needed(data)
        self.plan_computed = dt_util.utcnow()
        self.plan_reason = reason
        self._requested = None
        self._tomorrow_known = self.prices.tomorrow_known
        self._log_rescues()

    async def _async_update_solar(
        self, now: datetime, readings: TankReadings, heater_solar_kwh: float
    ) -> None:
        """Read the solar forecast and learn the base load of each hour."""
        entries = self.coordinator.config_entry.options.get(
            CONF_SOLAR_FORECAST_ENTRIES, []
        )
        tz = self.ledger.tz
        previous = self.solar_forecast
        self.solar_forecast = (
            await async_hourly_forecast(self.hass, entries, tz) if entries else None
        )
        if self.solar_forecast is None:
            if previous is not None:
                self.request("solar")
            return
        if previous is None:
            self.request("solar")
        hour = dt_util.as_local(now).replace(minute=0, second=0, microsecond=0)
        self.solar.observe(
            now,
            forecast_kwh=self.solar_forecast.get(hour, 0.0),
            export_total_kwh=readings.grid_export,
            heater_solar_kwh=heater_solar_kwh,
        )

    def expected_surplus(self, start: datetime, heater_power_w: float) -> list[float]:
        """Expected solar surplus (kWh) per plan slot from ``start``."""
        if self.solar_forecast is None:
            return [0.0] * HORIZON_SLOTS
        forecast = extend_forecast(
            self.solar_forecast, start + timedelta(minutes=15 * HORIZON_SLOTS)
        )
        return slot_surplus(
            self.solar.surplus(forecast),
            start,
            HORIZON_SLOTS,
            slot_minutes=15,
            max_kwh=heater_power_w / 1000 * 0.25,
        )

    async def _async_solar_needed(self, data: PlanInput) -> bool:
        """Return True if, without the surplus, the plan heats more from the grid.

        Compared until the next appointment block starts (or the whole
        horizon without blocks): the surplus then replaces grid energy that
        comfort needs, not only fills the tank.
        """
        plan = self.plan
        if plan is None or not any(data.solar_kwh or []):
            return False
        without = await self.hass.async_add_executor_job(
            optimise, replace(data, solar_kwh=None)
        )
        starts = [block.start for block in plan.blocks if block.start > data.start]
        until = min(starts) if starts else plan.slots[-1].start
        grid = sum(slot.grid_kwh for slot in plan.slots if slot.start < until)
        grid_without = sum(
            slot.grid_kwh for slot in without.slots if slot.start < until
        )
        return grid_without > grid + SOLAR_NEEDED_KWH

    def power_request(self, now: datetime, tank: TankState) -> tuple[float, str]:
        """Return the surplus power TankWise wants now (W) and its priority."""
        slot = self.current_slot(now)
        if (
            self.mode is Mode.OFF
            or slot is None
            or not slot.solar_allowed
            or slot.solar_kwh <= 0
        ):
            return 0.0, PRIORITY_NONE
        power = min(tank.heater_power_w, slot.solar_kwh / 0.25 * 1000)
        return round(power), PRIORITY_HIGH if self.solar_needed else PRIORITY_LOW

    def _legionella_target(
        self, start: datetime, tank: TankState, data: PlanInput
    ) -> tuple[int, float] | None:
        """Return the anti-legionella (slot, temperature) of the plan, if due."""
        if not self.anti_legionella or not self.legionella.due(start):
            return None
        target = target_temperature(
            tank.cutoff_temperature,
            self.coordinator.settings[SETTING_MAX_TEMPERATURE],
        )
        if target is None:
            if not self._legionella_impossible:
                self._legionella_impossible = True
                self.log(
                    "anti_legionella_impossible",
                    cutoff=f"{tank.cutoff_temperature:.1f}",
                )
            return None
        self._legionella_impossible = False
        moment = choose_time(
            start,
            self.legionella.deadline(start),
            slot_minutes=data.slot_minutes,
            buy=data.buy,
            surplus_value=data.surplus_value,
            solar_kwh=data.solar_kwh or [0.0] * data.slots,
            heater_slot_kwh=tank.heater_power_w / 1000 * data.slot_minutes / 60,
        )
        if moment is None:
            # Deadline already past: as soon as possible (in an hour).
            moment = start + timedelta(hours=1)
        index = int((moment - start) / SLOT)
        return (index, target) if 0 <= index < data.slots else None

    def _account_real(
        self, now: datetime, readings: TankReadings
    ) -> tuple[float, bool]:
        """Value the energy the water heater took since the last update."""
        energy = 0.0
        if readings.energy is not None:
            if self._last_energy is not None and readings.energy > self._last_energy:
                energy = readings.energy - self._last_energy
            self._last_energy = readings.energy
        elif (
            readings.power is not None
            and self._last_power is not None
            and self._last_time is not None
        ):
            hours = min((now - self._last_time).total_seconds(), 300) / 3600
            energy = max(0.0, (readings.power + self._last_power) / 2) * hours / 1000
        self._last_power = readings.power
        self._last_time = now
        prices = self.prices
        # The diverter in automatic mode heats only with surplus, and it
        # keeps the export close to zero while it does: exporting is not a
        # requirement then. In manual mode it is surplus only if still
        # exporting.
        from_surplus = (
            readings.manual_mode is False
            or (readings.export_power or 0.0) > SURPLUS_EXPORT_W
        )
        self.ledger.add_real(
            now,
            energy,
            from_surplus=from_surplus,
            buy_price=readings.price
            if readings.price is not None
            else (prices.buy[0] if prices else None),
            surplus_value=prices.surplus[0] if prices else 0.0,
        )
        return energy, from_surplus

    # ------------------------------------------------------------------
    # Logbook
    # ------------------------------------------------------------------

    def log(self, key: str, **values: Any) -> None:
        """Write a logbook entry."""
        log(self.hass, self.coordinator.config_entry.title, key, **values)

    def _log_rescues(self) -> None:
        assert self.plan is not None
        for block in self.plan.blocks:
            if block.rescue_kwh <= 0 or block.id in self._logged_rescues:
                continue
            self._logged_rescues.add(block.id)
            self.log(
                "rescue",
                name=block.name,
                time=dt_util.as_local(block.start).strftime("%H:%M"),
                kwh=f"{block.rescue_kwh:.2f}",
                cost=f"{block.rescue_cost:.2f}",
            )

    def on_manual_mode(self, now: datetime, on: bool) -> None:
        """Log an external order on the manual mode that contradicts the plan."""
        slot = self.current_slot(now)
        if slot is None or self.mode is Mode.OFF:
            return
        if on and slot.grid_kwh <= 0:
            self.ledger.add_deviation(now)
            self.log("manual_on")
        elif not on and slot.grid_kwh > 0:
            self.ledger.add_deviation(now)
            self.log("manual_off")
