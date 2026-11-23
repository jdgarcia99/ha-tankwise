"""Controller in Home Assistant: executes the plan on the regulator.

Reads what :class:`.model.control.Controller` needs, sends its orders to the
regulator manual mode and the heater switch, writes them to the logbook and
keeps the repair issues of the controller up to date. It acts only in the
modes that act (auto, eco, comfort and holiday); in shadow mode it only
gives the regulator back if TankWise had it in manual mode. A boost (the
user wants hot water now) acts in every mode except off.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOOST_AS_SHORTAGE,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_HEATER_SWITCH,
    CONF_MANUAL_MODE_SWITCH,
    CONF_REGULATOR_MARGIN,
    CONF_REGULATOR_OUTPUT_SWITCH,
    DOMAIN,
    EVENT_BOOST_FINISHED,
    EVENT_BOOST_STARTED,
    ISSUE_CONFLICTING_AUTOMATIONS,
    ISSUE_HEATER_SWITCH_OFF,
    ISSUE_REGULATOR_LOST,
    SETTING_BOOST_DURATION,
    SETTING_BOOST_TARGET,
    SETTING_MAX_TEMPERATURE,
)
from .messages import log, message
from .model.appointments import required_temperature
from .model.boost import Boost, BoostEnd
from .model.control import (
    DEGRADED_LOOKAHEAD,
    PROBE_TIMEOUT,
    SAFETY_REASONS,
    Action,
    ControlInput,
    Controller,
    DegradedTarget,
    Due,
    Order,
    Reason,
    degraded_target,
    heating_time,
)
from .model.demand import ShortageMark
from .model.planning import Mode

if TYPE_CHECKING:
    from .coordinator import TankReadings, TankWiseCoordinator
    from .model.appointments import AppointmentRequirement
    from .model.tank import TankState

_LOGGER = logging.getLogger(__name__)

AUTOMATION_DOMAIN = "automation"
CONFLICT_CHECK = timedelta(minutes=5)
ACTIVE_MODES = frozenset({Mode.AUTO, Mode.ECO, Mode.COMFORT, Mode.HOLIDAY})
SLOT = timedelta(minutes=15)
# A slot planned at (almost) full power is heated whole.
WHOLE_SLOT_MINUTES = 14.0
# After a (re)start the prices and the tank model may take a moment: no
# degraded mode before this.
STARTUP_GRACE = timedelta(minutes=5)

DEGRADED_PROBE = "probe"
DEGRADED_NO_PLAN = "no_plan"
RISK_REGULATOR = "regulator_unavailable"
RISK_PROBE = "probe_unavailable"


def issue_id(kind: str, entry_id: str) -> str:
    """Return the repair issue ID of an entry."""
    return f"{kind}_{entry_id}"


class TankWiseController:
    """Execute the plan of one TankWise entry."""

    def __init__(self, coordinator: TankWiseCoordinator) -> None:
        """Initialise the controller."""
        self.coordinator = coordinator
        self.hass: HomeAssistant = coordinator.hass
        self.state = Controller()
        self.degraded_reason: str | None = None
        self.risk_reasons: list[str] = []
        # Reasons already announced with the plan at risk event (persisted,
        # so a restart does not announce them again).
        self.announced_risks: list[str] = []
        self.boost: Boost | None = None
        self._conflicts: tuple[list[str], datetime | None] = ([], None)
        self._started: datetime | None = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def restore(self, data: dict[str, Any]) -> None:
        """Restore what TankWise had switched before a restart."""
        try:
            self.state = Controller.from_dict(data)
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Discarding stored controller state: %s", err)
        self.announced_risks = [str(item) for item in data.get("announced_risks", [])]
        if boost := data.get("boost"):
            try:
                self.boost = Boost.from_dict(boost)
            except ValueError as err:
                _LOGGER.warning("Discarding stored boost: %s", err)

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent state."""
        return {
            **self.state.as_dict(),
            "announced_risks": list(self.announced_risks),
            "boost": self.boost.as_dict() if self.boost else None,
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Return True if the current mode acts on the regulator."""
        return self.coordinator.planner.mode in ACTIVE_MODES

    @property
    def degraded(self) -> DegradedTarget | None:
        """Return what the degraded rule wants, while in degraded mode."""
        return self.state.degraded if self.degraded_reason else None

    @property
    def regulator_margin(self) -> timedelta:
        """Return how long the regulator may be unavailable."""
        return timedelta(
            minutes=float(self.coordinator.parameters[CONF_REGULATOR_MARGIN])
        )

    def on_manual_change(self, now: datetime, value: bool) -> bool:
        """Record a manual mode change; return True if TankWise caused it."""
        return self.state.on_manual_change(now, value)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def async_update(
        self,
        now: datetime,
        readings: TankReadings,
        tank: TankState,
        requirements: dict[str, AppointmentRequirement],
        workdays: dict,
    ) -> None:
        """Decide and send the orders of this minute."""
        coordinator = self.coordinator
        active = self.active
        temperature = self._fresh_temperature(now, readings)
        max_temperature = coordinator.settings[SETTING_MAX_TEMPERATURE]
        plan_heat = self._plan_heat(now, tank)
        self._started = self._started or now
        if plan_heat is None and now - self._started < STARTUP_GRACE:
            plan_heat = False  # wait for the prices and the model
        dues, no_appointments = self._dues(now, requirements, readings, workdays)

        if (boost := self.boost) is not None:
            if coordinator.planner.mode is Mode.OFF:
                self._finish_boost(now, BoostEnd.CANCELLED, temperature)
            else:
                self.boost = boost = boost.account(now, readings.power, readings.price)
                if (end := boost.end_reason(now, temperature)) is not None:
                    self._finish_boost(now, end, temperature)

        degraded: DegradedTarget | None = None
        if plan_heat is None and temperature is not None:
            state = self.state
            degraded = degraded_target(
                dt_util.as_local(now),
                temperature,
                dues,
                use_fallback=no_appointments,
                max_temperature=min(tank.cutoff_temperature, max_temperature),
                heat_capacity=coordinator.tank_params.heat_capacity,
                heater_power_w=tank.heater_power_w,
                capacity_factor=tank.capacity_factor,
                heating=bool(state.desired and state.degraded is not None),
            )
        self._update_degraded_reason(
            None
            if not active
            else DEGRADED_PROBE
            if temperature is None
            else DEGRADED_NO_PLAN
            if plan_heat is None
            else None
        )

        decision = self.state.step(
            ControlInput(
                now=now,
                active=active,
                temperature=temperature,
                power=readings.power,
                manual=readings.manual_mode,
                heater_switch=readings.heater_switch,
                plan_heat=plan_heat,
                degraded=degraded,
                max_temperature=max_temperature,
                power_threshold=float(
                    coordinator.parameters[CONF_CUTOFF_POWER_THRESHOLD]
                ),
                regulator_margin=self.regulator_margin,
                boost=self.boost is not None,
                sell_surplus=self._sell_surplus(now, tank),
                output=readings.regulator_output,
            )
        )
        for order in decision.orders:
            await self._async_execute(order)
            self._log_order(order, now, readings, tank, degraded)
            if self.boost is not None and order.reason in SAFETY_REASONS:
                end = (
                    BoostEnd.THERMOSTAT
                    if order.reason is Reason.CUTOFF
                    else BoostEnd.SAFETY
                )
                self._finish_boost(now, end, temperature)

        self._update_issues(now, active and decision.heater_switch_off)
        self.risk_reasons = self._risk_reasons(now, temperature, dues, degraded)

    def _fresh_temperature(self, now: datetime, readings: TankReadings) -> float | None:
        """Return the probe temperature, or None if missing or not updated."""
        updated = readings.temperature_updated
        if updated is not None and now - updated > PROBE_TIMEOUT:
            return None
        return readings.temperature

    def _sell_surplus(self, now: datetime, tank: TankState) -> bool:
        """Return True if the plan lets the surplus go to the grid now."""
        planner = self.coordinator.planner
        if planner.plan is None or not tank.available:
            return False
        slot = planner.current_slot(now)
        return slot is not None and not slot.solar_allowed

    def _plan_heat(self, now: datetime, tank: TankState) -> bool | None:
        """Return whether the plan heats from the grid now (None: no plan)."""
        planner = self.coordinator.planner
        if planner.plan is None or not tank.available:
            return None
        slot = planner.current_slot(now)
        if slot is None:
            return None
        if slot.grid_kwh <= 0 or tank.heater_power_w <= 0:
            return False
        minutes = slot.grid_kwh / (tank.heater_power_w / 1000) * 60
        if minutes >= WHOLE_SLOT_MINUTES:
            return True
        return now - slot.start < timedelta(minutes=minutes)

    def _dues(
        self,
        now: datetime,
        requirements: dict[str, AppointmentRequirement],
        readings: TankReadings,
        workdays: dict,
    ) -> tuple[list[Due], bool]:
        """Return the next appointments and whether there are none at all."""
        if readings.occupancy is False or self.coordinator.planner.mode is Mode.HOLIDAY:
            return [], False  # appointments suspended, no fixed windows either
        local = dt_util.as_local(now)
        enabled = [
            appointment
            for appointment in self.coordinator.appointments
            if appointment.enabled and appointment.days
        ]
        dues = [
            Due(appointment.name, due, requirements[appointment.id].temperature)
            for appointment in enabled
            if appointment.id in requirements
            and (due := appointment.next_due(local, workdays)) is not None
        ]
        return dues, not enabled

    # ------------------------------------------------------------------
    # Boost
    # ------------------------------------------------------------------

    def boost_target(
        self, temperature: float | None = None, liters: float | None = None
    ) -> float:
        """Return the boost target (°C), limited to the safety maximum."""
        coordinator = self.coordinator
        if temperature is not None:
            target = float(temperature)
        elif liters is not None:
            params = coordinator.tank_params
            data = coordinator.data
            target = required_temperature(
                float(liters),
                volume_l=params.volume_l,
                comfort_temperature=(
                    data.tank.useful_floor_temperature
                    if data
                    else params.comfort_min_temperature
                ),
                shower_temperature=params.shower_temperature,
                mains_temperature=params.mains_temperature,
                usable_factor=data.tank.usable_factor if data else 1.0,
            )
        else:
            target = coordinator.settings[SETTING_BOOST_TARGET]
        return min(target, coordinator.settings[SETTING_MAX_TEMPERATURE])

    def expected_boost_end(self, now: datetime) -> datetime | None:
        """Return when the boost should reach its target (or its deadline)."""
        boost = self.boost
        data = self.coordinator.data
        if boost is None or data is None:
            return None
        temperature = self._fresh_temperature(now, data.readings)
        if temperature is None:
            return boost.deadline
        end = now + heating_time(
            temperature,
            boost.target,
            heat_capacity=self.coordinator.tank_params.heat_capacity,
            heater_power_w=data.tank.heater_power_w,
            capacity_factor=data.tank.capacity_factor,
        )
        return min(end, boost.deadline) if boost.deadline else end

    async def async_start_boost(
        self,
        *,
        temperature: float | None = None,
        liters: float | None = None,
        duration: float | None = None,
    ) -> bool:
        """Heat now up to the boost target; False if not needed."""
        coordinator = self.coordinator
        if coordinator.planner.mode is Mode.OFF:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="boost_mode_off"
            )
        now = dt_util.utcnow()
        target = self.boost_target(temperature, liters)
        minutes = (
            float(duration)
            if duration is not None
            else coordinator.settings[SETTING_BOOST_DURATION]
        )
        deadline = now + timedelta(minutes=minutes) if minutes > 0 else None
        data = coordinator.data
        current = self._fresh_temperature(now, data.readings) if data else None
        title = coordinator.config_entry.title
        if current is not None and current >= target:
            log(
                self.hass,
                title,
                "boost_not_needed",
                temperature=f"{current:.1f}",
                target=f"{target:.1f}",
            )
            return False
        previous = self.boost
        self.boost = Boost(
            start=previous.start if previous else now,
            target=target,
            deadline=deadline,
            start_temperature=previous.start_temperature if previous else current,
            energy_kwh=previous.energy_kwh if previous else 0.0,
            cost=previous.cost if previous else 0.0,
            last_update=now,
        )
        self.state.start_boost()
        limit = (
            message(
                self.hass,
                "boost_limit",
                time=dt_util.as_local(deadline).strftime("%H:%M"),
            )
            if deadline
            else ""
        )
        log(self.hass, title, "boost_started", target=f"{target:.1f}", limit=limit)
        self.hass.bus.async_fire(
            EVENT_BOOST_STARTED,
            {
                "entry_id": coordinator.config_entry.entry_id,
                "name": title,
                "target": round(target, 1),
                "temperature": current,
                "deadline": deadline.isoformat() if deadline else None,
            },
        )
        if previous is None and coordinator.parameters[CONF_BOOST_AS_SHORTAGE]:
            coordinator.demand.add_mark(
                ShortageMark(now, now + timedelta(minutes=15), reason="boost")
            )
        await coordinator.async_refresh()
        return True

    async def async_cancel_boost(self) -> bool:
        """Cancel the boost in progress; False if there was none."""
        if self.boost is None:
            return False
        now = dt_util.utcnow()
        data = self.coordinator.data
        temperature = self._fresh_temperature(now, data.readings) if data else None
        self._finish_boost(now, BoostEnd.CANCELLED, temperature)
        await self.coordinator.async_refresh()
        return True

    async def async_toggle_boost(self) -> None:
        """Start a boost, or cancel the one in progress (one push button)."""
        if self.boost is not None:
            await self.async_cancel_boost()
        else:
            await self.async_start_boost()

    def _finish_boost(
        self, now: datetime, end: BoostEnd, temperature: float | None
    ) -> None:
        boost = self.boost
        if boost is None:
            return
        self.boost = None
        coordinator = self.coordinator
        title = coordinator.config_entry.title
        log(
            self.hass,
            title,
            "boost_finished",
            reason=message(self.hass, f"boost_reason_{end.value}"),
            temperature=f"{temperature:.1f}" if temperature is not None else "?",
            kwh=f"{boost.energy_kwh:.2f}",
            cost=f"{boost.cost:.2f}",
        )
        self.hass.bus.async_fire(
            EVENT_BOOST_FINISHED,
            {
                "entry_id": coordinator.config_entry.entry_id,
                "name": title,
                "reason": end.value,
                "target": round(boost.target, 1),
                "temperature": temperature,
                "energy_kwh": round(boost.energy_kwh, 3),
                "cost": round(boost.cost, 3),
                "duration_min": round((now - boost.start).total_seconds() / 60),
            },
        )
        # Replan with the energy already stored.
        coordinator.planner.request("boost")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def _async_execute(self, order: Order) -> None:
        data = self.coordinator.config_entry.data
        if order.action in (Action.MANUAL_ON, Action.MANUAL_OFF):
            entity_id = data.get(CONF_MANUAL_MODE_SWITCH)
        elif order.action in (Action.OUTPUT_ON, Action.OUTPUT_OFF):
            entity_id = self.coordinator.config_entry.options.get(
                CONF_REGULATOR_OUTPUT_SWITCH
            )
        else:
            entity_id = data.get(CONF_HEATER_SWITCH)
        if not entity_id:
            return
        on = order.action in (Action.MANUAL_ON, Action.HEATER_ON, Action.OUTPUT_ON)
        try:
            await self.hass.services.async_call(
                entity_id.split(".", 1)[0],
                SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
                {"entity_id": entity_id},
                blocking=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Could not switch %s: %s", entity_id, err)

    def _log_order(
        self,
        order: Order,
        now: datetime,
        readings: TankReadings,
        tank: TankState,
        degraded: DegradedTarget | None,
    ) -> None:
        reason = order.reason
        if reason is Reason.REGULATOR_BACK and order.action is Action.MANUAL_OFF:
            return  # one entry for both orders
        if reason in (Reason.BOOST, Reason.BOOST_DONE, Reason.HEAT):
            return  # the boost has its own entries; output on before heating
        if order.action is Action.OUTPUT_ON and reason is Reason.MODE:
            return  # one entry for the mode change
        values: dict[str, Any] = {}
        key = reason.value
        temperature = readings.temperature
        if reason is Reason.PLAN:
            values = self._plan_values(now)
        elif reason is Reason.DEGRADED and degraded is not None:
            due = dt_util.as_local(degraded.due) if degraded.due else None
            values = {
                "target": f"{degraded.target or 0:.1f}",
                "time": due.strftime("%H:%M") if due else "",
            }
            if degraded.name is None:
                key = "degraded_fixed"
            else:
                values["name"] = degraded.name
        elif reason in (Reason.DEGRADED_DONE, Reason.CUTOFF):
            values = {"temperature": f"{temperature or 0:.1f}"}
        elif reason is Reason.MAX_TEMPERATURE:
            values = {
                "temperature": f"{temperature or 0:.1f}",
                "max": f"{self.coordinator.settings[SETTING_MAX_TEMPERATURE]:.1f}",
            }
        elif reason is Reason.MODE:
            values = {"mode": self.coordinator.planner.mode.value}
        elif reason is Reason.SELL:
            slot = self.coordinator.planner.current_slot(now)
            prices = self.coordinator.planner.prices
            values = {"price": f"{prices.surplus[0]:.3f}" if prices else "?"}
            del slot
        elif reason is Reason.REGULATOR_LOST:
            lost = self.state.lost_since or now
            values = {"minutes": round((now - lost).total_seconds() / 60)}
        log(self.hass, self.coordinator.config_entry.title, key, **values)

    def _plan_values(self, now: datetime) -> dict[str, Any]:
        planner = self.coordinator.planner
        plan = planner.plan
        heating = next(
            (
                run
                for run in (plan.heatings() if plan else [])
                if run.start <= now < run.end
            ),
            None,
        )
        slot = planner.current_slot(now)
        price = (slot.cost / slot.grid_kwh) if slot and slot.grid_kwh > 0 else 0.0
        return {
            "end": dt_util.as_local(heating.end).strftime("%H:%M") if heating else "",
            "kwh": f"{heating.energy_kwh:.2f}" if heating else "0",
            "price": f"{price:.3f}",
        }

    def _update_degraded_reason(self, reason: str | None) -> None:
        if reason == self.degraded_reason:
            return
        previous, self.degraded_reason = self.degraded_reason, reason
        title = self.coordinator.config_entry.title
        if reason is not None:
            log(self.hass, title, f"degraded_on_{reason}")
        elif previous is not None and self.active:
            log(self.hass, title, "degraded_off")

    # ------------------------------------------------------------------
    # Repair issues and plan at risk
    # ------------------------------------------------------------------

    def conflicting_automations(self) -> list[str]:
        """Enabled automations that switch what TankWise controls.

        They would fight the controller: the regulator manual mode, its
        output switch or the heater switch. Automations that only start a
        boost (e.g. from the blueprint) do not refer to them.
        """
        if AUTOMATION_DOMAIN not in self.hass.config.components:
            return []
        from homeassistant.components.automation import (
            automations_with_entity,
        )

        entry = self.coordinator.config_entry
        controlled = [
            entry.data.get(CONF_MANUAL_MODE_SWITCH),
            entry.data.get(CONF_HEATER_SWITCH),
            entry.options.get(CONF_REGULATOR_OUTPUT_SWITCH),
        ]
        found: set[str] = set()
        for entity_id in controlled:
            if entity_id:
                found.update(automations_with_entity(self.hass, entity_id))
        return sorted(
            automation
            for automation in found
            if (state := self.hass.states.get(automation)) is not None
            and state.state == "on"
        )

    def _update_issues(self, now: datetime, heater_switch_off: bool) -> None:
        entry = self.coordinator.config_entry
        placeholders = {"name": entry.title}
        if not self.active:
            self._conflicts = ([], None)  # checked at once when it acts
        elif self._conflicts[1] is None or now - self._conflicts[1] >= CONFLICT_CHECK:
            self._conflicts = (self.conflicting_automations(), now)
        conflicts = self._conflicts[0]
        identifier = issue_id(ISSUE_CONFLICTING_AUTOMATIONS, entry.entry_id)
        if conflicts:
            names = [
                f"- {state.name if (state := self.hass.states.get(item)) else item}"
                f" (`{item}`)"
                for item in conflicts
            ]
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                identifier,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_CONFLICTING_AUTOMATIONS,
                translation_placeholders={
                    **placeholders,
                    "automations": "\n".join(names),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, identifier)
        for kind, present in (
            (ISSUE_HEATER_SWITCH_OFF, heater_switch_off),
            (ISSUE_REGULATOR_LOST, self.state.repeated_cuts(now)),
        ):
            identifier = issue_id(kind, entry.entry_id)
            if present:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    identifier,
                    is_fixable=False,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=kind,
                    translation_placeholders=placeholders,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, identifier)

    def _risk_reasons(
        self,
        now: datetime,
        temperature: float | None,
        dues: list[Due],
        degraded: DegradedTarget | None,
    ) -> list[str]:
        """Reasons, besides the plan itself, why an appointment is at risk."""
        reasons: list[str] = []
        if self.coordinator.planner.mode is Mode.OFF:
            return reasons
        if self.state.regulator_lost(
            now, self.regulator_margin
        ) and self._heating_before_appointment(now, temperature, degraded):
            reasons.append(RISK_REGULATOR)
        if (
            self.active
            and temperature is None
            and any(item.due - now <= DEGRADED_LOOKAHEAD for item in dues)
        ):
            reasons.append(RISK_PROBE)
        return reasons

    def _heating_before_appointment(
        self, now: datetime, temperature: float | None, degraded: DegradedTarget | None
    ) -> bool:
        """Return True if an appointment needs grid heating before it starts."""
        plan = self.coordinator.planner.plan
        if plan is not None:
            upcoming = [block for block in plan.blocks if block.start > now]
            if not upcoming:
                return False
            first = min(block.start for block in upcoming)
            return any(
                slot.grid_kwh > 0 and now - SLOT < slot.start < first
                for slot in plan.slots
            )
        return (
            degraded is not None
            and degraded.name is not None
            and degraded.target is not None
            and temperature is not None
            and temperature < degraded.target
        )
