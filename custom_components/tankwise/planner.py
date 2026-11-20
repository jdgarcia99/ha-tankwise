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

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INJECTION_PRICE_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RESCUE,
    DEFAULT_MODE,
    REPLAN_DEVIATION,
    SETTING_MAX_TEMPERATURE,
    SETTING_RESERVE_LITERS,
    SETTING_SURPLUS_VALUE,
    SURPLUS_EXPORT_W,
)
from .messages import log
from .model.ledger import Ledger
from .model.optimizer import Plan, PlanSlot, optimise
from .model.planning import HORIZON_SLOTS, Mode, build_plan_input
from .model.prices import DayPrices, SlotPrices, parse_hourly_prices, slot_prices

if TYPE_CHECKING:
    from .coordinator import TankReadings, TankWiseCoordinator
    from .model.demand import DemandForecast
    from .model.tank import TankState

_LOGGER = logging.getLogger(__name__)

SLOT = timedelta(minutes=15)


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

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent control state."""
        return {
            "mode": self.mode.value,
            "compensation_cap": self.compensation_cap,
            "ledger": self.ledger.as_dict(),
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
        self._account_real(now, readings)

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
            comfort_temperature=params.comfort_min_temperature,
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
        )
        if data is None:
            self.plan = None
            return
        self.plan = await self.hass.async_add_executor_job(optimise, data)
        self.plan_computed = dt_util.utcnow()
        self.plan_reason = reason
        self._requested = None
        self._tomorrow_known = self.prices.tomorrow_known
        self._log_rescues()

    def _account_real(self, now: datetime, readings: TankReadings) -> None:
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
        self.ledger.add_real(
            now,
            energy,
            from_surplus=(readings.export_power or 0.0) > SURPLUS_EXPORT_W,
            buy_price=readings.price
            if readings.price is not None
            else (prices.buy[0] if prices else None),
            surplus_value=prices.surplus[0] if prices else 0.0,
        )

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
