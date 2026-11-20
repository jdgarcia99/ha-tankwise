"""Controller: turns the plan into orders for the regulator, with safeties.

Every minute the controller decides whether the regulator (e.g. FreeDS)
must be in manual mode (full power from the grid) or in automatic mode
(surplus only):

* With a plan, it heats during the planned minutes of the current slot
  (a whole slot, or its first half).
* Without a plan (no prices, tank model not available) it falls back to the
  degraded rule: before each active appointment, heat up to the temperature
  the appointment needs (limited to the learned thermostat cut-off), starting
  as early as the learned power requires. Only when there are no
  appointments: fixed last-resort windows (50 °C at dawn, full in the
  afternoon).
* Safeties, whoever switched the manual mode on: temperature above the
  safety maximum for one minute, thermostat cut-off (no power for three
  minutes), more than two hours in manual mode, probe unavailable or not
  updated. They are never skipped.
* Boost: the user asked for hot water now. It heats in every mode the
  caller allows (also shadow and holiday), with the same safeties.
* Regulator lost: if the regulator becomes unavailable while TankWise had it
  in manual mode (or the element heats outside the plan) and is still
  unavailable after a margin, the heater switch is opened as a last resort.
  When the regulator comes back it is set to automatic and the heater
  switch is closed again.

Orders are sent on changes of the desired state (and retried if they did
not take effect), so an external order is respected until the next change;
the safeties still apply to it.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any

SAFETY_CONFIRM = timedelta(minutes=1)
CUTOFF_CONFIRM = timedelta(minutes=3)
MAX_MANUAL = timedelta(hours=2)
LONG_RUN_PAUSE = timedelta(minutes=15)
PROBE_TIMEOUT = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=2)
# Our own order is recognised in the state change it causes this long after.
OWN_ORDER_WINDOW = timedelta(seconds=30)
# After a thermostat cut-off or the safety maximum, no grid heating until
# the tank cools this much (°C).
FULL_HYSTERESIS = 3.0
# Degraded rule: a heating starts only this far below the target (°C) and
# the time it needs is stretched by this factor plus a fixed margin.
DEGRADED_RESTART = 1.0
DEGRADED_TIME_FACTOR = 1.25
DEGRADED_TIME_MARGIN = timedelta(minutes=15)
DEGRADED_LOOKAHEAD = timedelta(hours=24)
# Last resort without appointments: (from, to, target °C or None = cut-off).
FALLBACK_WINDOWS: tuple[tuple[time, time, float | None], ...] = (
    (time(4, 0), time(6, 15), 50.0),
    (time(15, 0), time(16, 0), None),
)
# Heater switch cuts that make the regulator losses a repair issue.
REPEATED_CUTS = 3
REPEATED_CUTS_WINDOW = timedelta(days=7)


class Action(StrEnum):
    """Order for an actuator."""

    MANUAL_ON = "manual_on"
    MANUAL_OFF = "manual_off"
    HEATER_OFF = "heater_off"
    HEATER_ON = "heater_on"


class Reason(StrEnum):
    """Why an order was given."""

    PLAN = "plan"
    PLAN_DONE = "plan_done"
    DEGRADED = "degraded"
    DEGRADED_DONE = "degraded_done"
    MODE = "mode"
    MAX_TEMPERATURE = "max_temperature"
    CUTOFF = "cutoff"
    TOO_LONG = "too_long"
    PROBE_LOST = "probe_lost"
    REGULATOR_LOST = "regulator_lost"
    REGULATOR_BACK = "regulator_back"
    BOOST = "boost"
    BOOST_DONE = "boost_done"


# Safeties that end the manual mode (and a boost).
SAFETY_REASONS = frozenset(
    {
        Reason.MAX_TEMPERATURE,
        Reason.CUTOFF,
        Reason.TOO_LONG,
        Reason.PROBE_LOST,
        Reason.REGULATOR_LOST,
    }
)


@dataclass(frozen=True, slots=True)
class Order:
    """An order for an actuator and its reason."""

    action: Action
    reason: Reason


@dataclass(frozen=True, slots=True)
class Due:
    """An appointment the degraded rule heats for."""

    name: str
    due: datetime
    temperature: float  # needed at ``due``


@dataclass(frozen=True, slots=True)
class DegradedTarget:
    """What the degraded rule wants now."""

    heat: bool
    target: float | None = None
    name: str | None = None  # appointment, or None for a fixed window
    due: datetime | None = None
    start: datetime | None = None


def heating_time(
    temperature: float,
    target: float,
    *,
    heat_capacity: float,
    heater_power_w: float,
    capacity_factor: float,
) -> timedelta:
    """Time the element needs to take the tank from one temperature to another."""
    if target <= temperature or heater_power_w <= 0:
        return timedelta(0)
    hours = (
        heat_capacity
        * (target - temperature)
        / (capacity_factor * heater_power_w / 1000)
    )
    return timedelta(hours=hours)


def degraded_target(
    now: datetime,
    temperature: float,
    dues: Sequence[Due],
    *,
    use_fallback: bool,
    max_temperature: float,
    heat_capacity: float,
    heater_power_w: float,
    capacity_factor: float,
    heating: bool,
) -> DegradedTarget:
    """Return what the degraded rule wants (``now`` in local time).

    ``dues`` are the next times of the active appointments; ``use_fallback``
    is True only when there are no active appointments at all.
    ``max_temperature`` is the learned cut-off (limited to the safety
    maximum). ``heating`` tells whether the rule is already heating, for the
    hysteresis.
    """

    def wants(target: float) -> bool:
        threshold = target if heating else target - DEGRADED_RESTART
        return temperature < threshold

    candidates: list[DegradedTarget] = []
    for item in sorted(dues, key=lambda due: due.due):
        if not now < item.due <= now + DEGRADED_LOOKAHEAD:
            continue
        target = min(item.temperature, max_temperature)
        needed = heating_time(
            temperature,
            target,
            heat_capacity=heat_capacity,
            heater_power_w=heater_power_w,
            capacity_factor=capacity_factor,
        )
        start = item.due - needed * DEGRADED_TIME_FACTOR - DEGRADED_TIME_MARGIN
        if start <= now and wants(target):
            return DegradedTarget(
                heat=True, target=target, name=item.name, due=item.due, start=start
            )
        candidates.append(
            DegradedTarget(
                heat=False, target=target, name=item.name, due=item.due, start=start
            )
        )
    if use_fallback:
        for begin, end, value in FALLBACK_WINDOWS:
            start = now.replace(
                hour=begin.hour, minute=begin.minute, second=0, microsecond=0
            )
            finish = now.replace(
                hour=end.hour, minute=end.minute, second=0, microsecond=0
            )
            target = max_temperature if value is None else min(value, max_temperature)
            if start <= now < finish:
                return DegradedTarget(
                    heat=wants(target), target=target, due=finish, start=start
                )
    return candidates[0] if candidates else DegradedTarget(heat=False)


@dataclass(frozen=True, slots=True)
class ControlInput:
    """What the controller sees at one step."""

    now: datetime
    # The mode acts on the regulator (auto, eco, comfort, holiday).
    active: bool
    # Probe temperature; None when unavailable or not updated.
    temperature: float | None
    power: float | None
    # Regulator manual mode; None when the regulator is unavailable.
    manual: bool | None
    heater_switch: bool | None
    # The plan heats from the grid now; None when there is no plan.
    plan_heat: bool | None
    # Degraded rule, used when there is no plan.
    degraded: DegradedTarget | None
    max_temperature: float  # safety maximum
    power_threshold: float  # W, below it the element is not heating
    regulator_margin: timedelta
    # A boost is in progress: heat now, in any mode (even shadow).
    boost: bool = False


@dataclass(slots=True)
class Decision:
    """Orders of one step and the problems found."""

    orders: list[Order] = field(default_factory=list)
    # The heater switch is open and TankWise did not open it.
    heater_switch_off: bool = False

    def add(self, action: Action, reason: Reason) -> None:
        """Add an order."""
        self.orders.append(Order(action, reason))


class Controller:
    """State machine of the orders sent to the regulator and heater switch."""

    def __init__(self) -> None:
        """Initialise the controller."""
        # Persistent: TankWise switched the manual mode on / the switch off.
        self.owns_manual = False
        self.heater_cut = False
        self.cuts: list[datetime] = []
        # Runtime state.
        self.desired: bool | None = None
        self.degraded: DegradedTarget | None = None
        self.lost_since: datetime | None = None
        self.manual_since: datetime | None = None
        self.full_latch: float | None = None
        self.pause_until: datetime | None = None
        self._low_since: datetime | None = None
        self._hot_since: datetime | None = None
        self._command: tuple[bool, datetime] | None = None
        self._overridden = False
        self._boosting = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the state that must survive a restart."""
        return {
            "owns_manual": self.owns_manual,
            "heater_cut": self.heater_cut,
            "cuts": [moment.isoformat() for moment in self.cuts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Controller:
        """Rebuild the controller from :meth:`as_dict` output."""
        controller = cls()
        controller.owns_manual = bool(data.get("owns_manual", False))
        controller.heater_cut = bool(data.get("heater_cut", False))
        controller.cuts = [
            datetime.fromisoformat(value) for value in data.get("cuts", [])
        ]
        return controller

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def start_boost(self) -> None:
        """A boost starts: an earlier cut-off no longer blocks the heating.

        If the tank is still full the thermostat cuts off again in a few
        minutes and the boost ends.
        """
        self.full_latch = None

    def regulator_lost(self, now: datetime, margin: timedelta) -> bool:
        """Return True if the regulator has been unavailable beyond the margin."""
        return self.lost_since is not None and now - self.lost_since >= margin

    def repeated_cuts(self, now: datetime) -> bool:
        """Return True if the heater switch had to be opened too often lately."""
        self.cuts = [
            moment for moment in self.cuts if now - moment <= REPEATED_CUTS_WINDOW
        ]
        return len(self.cuts) >= REPEATED_CUTS

    def on_manual_change(self, now: datetime, value: bool) -> bool:
        """Record a manual mode change; return True if TankWise caused it."""
        command = self._command
        if (
            command is not None
            and command[0] == value
            and timedelta(0) <= now - command[1] <= OWN_ORDER_WINDOW
        ):
            return True
        # External order: respected until the desired state changes.
        self._overridden = True
        if not value:
            self.owns_manual = False
        return False

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------

    def step(self, data: ControlInput) -> Decision:
        """Decide the orders for this minute."""
        decision = Decision()
        now = data.now
        boost_ended = self._boosting and not data.boost
        self._boosting = data.boost
        active = data.active or data.boost

        if data.manual is None:
            if self.lost_since is None:
                self.lost_since = now
        else:
            self.lost_since = None
        if data.manual:
            self.manual_since = self.manual_since or now
        else:
            self.manual_since = None
            self._low_since = None
            self._hot_since = None

        # The regulator is back after the heater switch was opened.
        if self.heater_cut and data.manual is not None:
            if data.manual:
                self._send(decision, Action.MANUAL_OFF, Reason.REGULATOR_BACK, now)
            decision.add(Action.HEATER_ON, Reason.REGULATOR_BACK)
            self.heater_cut = False
            self.owns_manual = False
            self.desired = None
            return decision

        if not active:
            self.degraded = None
            if self.owns_manual and data.manual:
                reason = Reason.BOOST_DONE if boost_ended else Reason.MODE
                self._send(decision, Action.MANUAL_OFF, reason, now)
            self.owns_manual = False
            self.desired = None
            return decision

        if data.manual is None:
            heating = data.power is not None and data.power > data.power_threshold
            outside_plan = heating and not self.desired
            if (
                not self.heater_cut
                and self.regulator_lost(now, data.regulator_margin)
                and (self.owns_manual or outside_plan)
            ):
                decision.add(Action.HEATER_OFF, Reason.REGULATOR_LOST)
                self.heater_cut = True
                self.cuts.append(now)
            return decision

        if data.heater_switch is False and not self.heater_cut:
            decision.heater_switch_off = True

        desired, reason_on, reason_off = self._desired(data)
        if boost_ended:
            reason_off = Reason.BOOST_DONE

        # Safeties on the manual mode, whoever switched it on.
        if data.manual and (safety := self._safety(data)) is not None:
            self._send(decision, Action.MANUAL_OFF, safety, now)
            self.owns_manual = False
            self.desired = False
            return decision

        changed = desired != self.desired
        if changed:
            self._overridden = False
        retry = (
            not self._overridden
            and self._command is not None
            and self._command[0] == desired
            and now - self._command[1] >= RETRY_AFTER
        )
        if desired and not data.manual:
            if changed or retry:
                self._send(decision, Action.MANUAL_ON, reason_on, now)
        elif desired and data.manual:
            self.owns_manual = True  # taken over, switched off at the end
        elif not desired and data.manual and self.owns_manual and (changed or retry):
            self._send(decision, Action.MANUAL_OFF, reason_off, now)
        self.desired = desired
        return decision

    def _desired(self, data: ControlInput) -> tuple[bool, Reason, Reason]:
        """Return whether to heat from the grid now, and the order reasons."""
        now = data.now
        if data.boost:
            desired = True
            reasons = Reason.BOOST, Reason.BOOST_DONE
        elif data.plan_heat is not None:
            self.degraded = None
            desired = data.plan_heat
            reasons = Reason.PLAN, Reason.PLAN_DONE
        else:
            self.degraded = data.degraded
            desired = bool(data.degraded and data.degraded.heat)
            reasons = Reason.DEGRADED, Reason.DEGRADED_DONE
        if data.temperature is None:
            desired = False
        elif (
            self.full_latch is not None
            and data.temperature <= self.full_latch - FULL_HYSTERESIS
        ):
            self.full_latch = None
        if self.full_latch is not None:
            desired = False
        if self.pause_until is not None:
            if now < self.pause_until:
                desired = False
            else:
                self.pause_until = None
        return desired, *reasons

    def _safety(self, data: ControlInput) -> Reason | None:
        """Return the safety that ends the manual mode now, if any."""
        now = data.now
        temperature = data.temperature
        if temperature is None:
            return Reason.PROBE_LOST
        if temperature > data.max_temperature:
            self._hot_since = self._hot_since or now
            if now - self._hot_since >= SAFETY_CONFIRM:
                self.full_latch = temperature
                return Reason.MAX_TEMPERATURE
        else:
            self._hot_since = None
        if data.power is not None and data.power <= data.power_threshold:
            self._low_since = self._low_since or now
            if now - self._low_since >= CUTOFF_CONFIRM:
                self.full_latch = temperature
                return Reason.CUTOFF
        else:
            self._low_since = None
        if self.manual_since is not None and now - self.manual_since >= MAX_MANUAL:
            self.pause_until = now + LONG_RUN_PAUSE
            return Reason.TOO_LONG
        return None

    def _send(
        self, decision: Decision, action: Action, reason: Reason, now: datetime
    ) -> None:
        decision.add(action, reason)
        on = action is Action.MANUAL_ON
        self._command = (on, now)
        self.owns_manual = on
