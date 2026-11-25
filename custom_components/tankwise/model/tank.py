"""Thermal model of an electric storage water heater.

The tank is modelled as an equivalent fully mixed volume whose temperature
(``equivalent_temperature``, on the scale of the upper probe) is tracked by an
energy balance, one sample per minute:

* heat in: increments of the heating element energy meter, or the integral of
  the power when there is no meter. Only a fraction ``k_cap`` (the effective
  capacity factor, learned) shows up at the probe;
* losses: ``P = UA · (T - T_amb)``, with ``UA`` (W/K) learned by recursive
  least squares from the slope of the probe over rest windows of at least
  two hours;
* draws: the residual between the observed and the predicted change of the
  filtered probe. A draw starts when the residual accumulated over ten
  minutes falls below a threshold and ends when it stays close to zero for
  five minutes.

The state is recalibrated to "full" when the thermostat cuts off: the
regulator asks for power (FreeDS manual mode on, or PWM output above 10 %)
and the element draws none for three minutes. Without regulator information,
heating that stops close to the learned cut-off temperature is used instead.
Because the regulator may be switched off a few seconds after the cut-off
(e.g. by an automation), its state is captured at the very instant the power
falls (:class:`PowerDropDetector`, fed with state change events), not only at
the 60-second samples.

Units: °C, W, kWh. ``C = V · 0.001163`` kWh/K.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import math
from statistics import median
from typing import Any

from .signal import ProbeFilter, theil_sen

STORAGE_FORMAT = 2

# Physics
WATER_HEAT_CAPACITY_KWH_PER_L_K = 0.001163  # kWh per litre per kelvin
DEFAULT_AMBIENT_TEMPERATURE = 20.0  # °C, when there is no ambient sensor
DEFAULT_UA = 1.0  # W/K
MIN_UA = 0.3
MAX_UA = 3.0
DEFAULT_CAPACITY_FACTOR = 0.9  # k_cap
MIN_CAPACITY_FACTOR = 0.6
MAX_CAPACITY_FACTOR = 1.0  # the probe cannot see more heat than was added

# Heater energy sanity margin over the rated power
HEATER_ENERGY_MARGIN = 1.25

# Learned heater power
HEATER_POWER_MIN_W = 1000.0  # samples above this are "full power"
HEATER_POWER_MIN_ALPHA = 0.02
REGULATOR_FULL_PERCENT = 95.0

# Thermostat cut-off ("full") detection
FULL_CONFIRM = timedelta(minutes=3)
FULL_CHECK_WINDOW = timedelta(minutes=30)
REGULATOR_DEMAND_PERCENT = 10.0
# The regulator stopping (its output down to REGULATOR_DEMAND_PERCENT or by
# REGULATOR_FALL_STEP points at once, or the manual mode switched off) this
# shortly before the power fell means the regulator stopped the heating, not
# the thermostat. A regulator modulating in automatic mode (77 % → 72 %)
# cannot stop the element: not a stop.
REGULATOR_FALLING_WINDOW = timedelta(seconds=30)
REGULATOR_FALL_STEP = 30.0
# The manual mode reported off (or the PWM output at rest) this shortly
# after the power fell also means an external order stopped the heating: the
# regulator state often reaches Home Assistant a few seconds after the power.
# Unless the regulator asked for more power first: with no consumption it
# raises its output (a thermostat cut-off), and whatever it does after that
# (an automation or SolarSplit taking the surplus elsewhere) is a consequence.
REGULATOR_STOP_AFTER = timedelta(seconds=60)
# Asking for more: the output rose this much above its value at the drop, or
# reached REGULATOR_FULL_PERCENT, or the manual mode was switched on. Or, at
# full output already (it cannot rise), it stayed there this long after the
# drop: its own stop would have been reported within a couple of seconds.
REGULATOR_INSIST_RISE = 10.0
REGULATOR_HOLD = timedelta(seconds=5)
# A cut-off this far below the configured cut-off temperature is not a
# thermostat cut-off (e.g. an automation stopping the heating at 45 °C).
# Used while no cut-off has been learned yet.
CUTOFF_PLAUSIBLE_BELOW = 12.0
# Once a cut-off is known, a new one must be this close to it.
CUTOFF_PLAUSIBLE_NEAR = 4.0
# A power drop is not a thermostat cut-off when the regulator (or its own
# power supply) stopped the heating:
# - from this long before the drop to the drop itself, the regulator was
#   unavailable, restarted, was in a mode that does not heat or asked for no
#   power;
# - after the drop, only if it became unavailable or restarted, up to
#   REGULATOR_UNAVAILABLE_AFTER: its entities take up to about two minutes to
#   be reported unavailable after a power cut (still within the confirmation
#   time). A mode or setpoint change after the drop is a consequence of the
#   cut-off (an automation, or SolarSplit taking the surplus elsewhere), not
#   its cause.
REGULATOR_DISTURBED_BEFORE = timedelta(minutes=1)
REGULATOR_UNAVAILABLE_AFTER = timedelta(minutes=3)
# Regulator modes that heat (``Auto``: surplus, ``Manual``: full power).
DEFAULT_REGULATOR_ACTIVE_MODES = ("Auto", "Manual")
# A sample to discard given by value matches within this margin.
CUTOFF_MATCH_MARGIN = 0.5
# Two cut-off samples are the same one within these margins (a history import
# finds again the samples learned live, at the same poll).
CUTOFF_SAME_MARGIN = 0.05  # °C
CUTOFF_SAME_SECONDS = 120.0
MIN_EPISODE_ENERGY_KWH = 0.02
BACKUP_FULL_MARGIN = 2.5  # °C below the cut-off temperature
CUTOFF_LEARN_MARGIN = 1.0  # °C, backup cut-offs closer than this are learned
MAX_CUTOFF_SAMPLES = 20
MIN_CUTOFF_SAMPLES = 3
CUTOFF_CLAMP_MARGIN = 2.0  # °C above the highest known cut-off

# Draw detection (residual of the filtered probe)
POST_HEATING = timedelta(minutes=15)
RESIDUAL_WINDOW = timedelta(minutes=10)
IDLE_DRAW_THRESHOLD = -1.0  # °C accumulated over RESIDUAL_WINDOW
HEATING_DRAW_THRESHOLD = -1.5  # while heating the probe response is uneven
END_WINDOW = timedelta(minutes=5)
END_TOLERANCE = 0.2  # °C accumulated over END_WINDOW counts as "about zero"
MIN_TRACK_DROP = 0.1  # °C, a new minimum must be this much lower
# Slow draws: a tap left running, or a shower with a hot, full tank where the
# cold water never reaches the upper probe quickly.
SLOW_WINDOW = timedelta(minutes=60)
SLOW_DRAW_THRESHOLD = -1.0  # °C accumulated over SLOW_WINDOW at rest
SLOW_END_WINDOW = timedelta(minutes=15)
SLOW_END_TOLERANCE = 0.15  # °C over SLOW_END_WINDOW counts as "about zero"
SLOW_SETTLE = timedelta(minutes=45)  # after heating or another draw
HISTORY_SPAN = SLOW_WINDOW

# Energy reconciliation: after each heating the probe sees the mixed tank, so
# the balance between two settled heatings tells how much energy was really
# drawn; the draws detected in between are scaled to match.
MIN_RECONCILE_HEAT_KWH = 0.3
MIN_RECONCILE_DRAWS_KWH = 0.1
MIN_RECONCILE_FACTOR = 0.5
MAX_RECONCILE_FACTOR = 5.0
MAX_RECONCILIATIONS = 20

# Useful litres: water at the shower temperature that can be drawn before the
# outlet falls below the comfort temperature. The incoming cold water mixes
# with the hot water, so only the heat above the comfort temperature is
# usable: L = k_use * V * (T - T_comfort) / (T_shower - T_mains). k_use is
# learned from real shortages (litres drawn since the last settled heating
# until the probe crossed the comfort temperature, over the litres predicted
# then); it was 1.00 (median) on the reference installation.
DEFAULT_USABLE_FACTOR = 1.0
MIN_USABLE_FACTOR = 0.5
MAX_USABLE_FACTOR = 1.5
MAX_USABLE_SAMPLES = 10
MIN_USABLE_PREDICTED_L = 5.0
MAX_CONSUMPTIONS = 200
# A draw is a shortage only if the probe went this far below the comfort
# temperature: water kept being drawn below it (about 6 litres or more), not
# just the end of a shower touching it. Measured on real showers: the
# shortages went 3-9.5 °C below 38 °C, the false ones 0.5-1.25 °C.
SHORTAGE_MARGIN = 1.5

# Probe filter time constants: smooth at rest, fast while following a draw
FILTER_TAU_S = 240.0
FILTER_FAST_TAU_S = 60.0

# Slow correction towards the probe at rest
SLOW_CORRECTION_TAU = timedelta(minutes=50)

# Gaps (restarts, unavailable sensors)
FILTER_RESET_GAP = timedelta(minutes=5)
LONG_GAP = timedelta(minutes=30)
# Maintenance windows kept to flag the draws that overlap them; samples of one
# window further apart than this start a new span.
MAX_MAINTENANCE_SPANS = 20
MAINTENANCE_SPAN_JOIN = timedelta(minutes=5)
# A drop of the probe across a gap of readings up to this long is recorded as
# a draw spread over the gap; beyond it the time is too vague for the profile.
MAX_GAP_DRAW = timedelta(hours=12)
# The prediction across a gap drifts (losses, metered heat): a drop is a draw
# only beyond 1 °C plus this much per hour of gap and share of the heat.
GAP_DRAW_DRIFT_PER_HOUR = 0.25  # °C
GAP_DRAW_HEAT_SHARE = 0.1
# Heater energy across a gap below this is meter noise, not a heating.
GAP_HEATING_MIN_KWH = MIN_EPISODE_ENERGY_KWH

# Hot water meter (litres leaving the tank)
# A draw ends after this long without a new litre (single pulses are grouped).
METER_DRAW_END = timedelta(minutes=3)
# A volume jump beyond this flow (plus one litre) is a glitch, not a draw.
METER_MAX_FLOW_L_PER_MIN = 30.0
# The last probe reading may be this old to give the outlet temperature.
METER_PROBE_MAX_AGE = timedelta(minutes=60)
# Stratification: probe drop per litre against the drop of a mixed tank.
STRATIFICATION_SETTLE = timedelta(minutes=10)  # after the last litre
MIN_STRATIFICATION_DROP = 4.0  # °C expected in a mixed tank (MAX6675: ±1 °C)
MIN_STRATIFICATION_RATIO = 0.2
MAX_STRATIFICATION_RATIO = 1.5
MAX_STRATIFICATION_SAMPLES = 20
MIN_STRATIFICATION_SAMPLES = 3
STRATIFICATION_BAND = 5.0  # °C, samples this close to a temperature are used
# The tank is treated as stratified (useful energy down to the mains water,
# times k_use) once the ratio is learned and clearly below a mixed tank's.
STRATIFIED_MAX_RATIO = 0.8
# With a meter the energy balance is trusted: the probe only corrects the
# model beyond this difference (resolution and noise of the probe).
METERED_PROBE_DEADBAND = 1.0  # °C

# Loss learning (rest windows)
LOSS_SETTLE = timedelta(minutes=30)  # after heating, draws or meter activity
LOSS_MIN_WINDOW = timedelta(hours=2)
LOSS_MAX_WINDOW = timedelta(hours=4)
MIN_LOSS_EXCESS = 10.0  # °C above ambient
MAX_LOSS_SCATTER = 1.0  # °C, median absolute residual
# Once UA is learned, a rest window that implies a UA outside this range of
# the current value is a slow draw (or a sensor glitch), not losses: a slow
# draw cannot be told apart from losses inside one window.
LOSS_PLAUSIBLE_RATIO = (0.5, 1.8)
# Merging an imported history with a running model: beyond these relative
# differences an estimated parameter of the running model is not coherent
# with the history and the history wins. Lists of measurements (cut-offs,
# capacity and usable factors) keep the source with more of them: each one
# is a real measurement, and the cut-off really varies from day to day.
MERGE_TOLERANCE = {"ua": 0.30, "power": 0.10}
MIN_LOSS_WINDOWS = 3
RLS_FORGETTING = 0.95
RLS_INITIAL_COVARIANCE = 0.002
METER_IDLE_KWH = 0.001  # per sample

# Effective capacity learning (heating episodes without draws)
CAPACITY_SETTLE = POST_HEATING
MIN_CAPACITY_EPISODE_KWH = 0.3
MIN_CAPACITY_EPISODE_RISE = 2.0  # °C
MAX_CAPACITY_SAMPLES = 10


class ModelStatus(StrEnum):
    """Confidence in the tank state."""

    CALIBRATED = "calibrated"
    UNCERTAIN = "uncertain"
    LEARNING = "learning"


# Why the model is not calibrated (``TankState.status_reason``).
REASON_CALIBRATED = "calibrated"
# Uncertain: the stored energy is not anchored to a heating to the cut-off.
REASON_NEVER_CALIBRATED = "never_calibrated"
REASON_DATA_GAP = "data_gap"  # readings missing for more than LONG_GAP
# Learning: not enough rest windows to trust the heat losses.
REASON_FEW_LOSS_WINDOWS = "few_loss_windows"


class HeaterState(StrEnum):
    """Heating element state machine."""

    IDLE = "idle"
    HEATING = "heating"
    CHECKING = "checking"


@dataclass(frozen=True, slots=True)
class TankParams:
    """Physical parameters and comfort settings of the tank."""

    volume_l: float = 100.0
    rated_power_w: float = 1500.0
    mains_temperature: float = 17.0
    ambient_temperature: float = DEFAULT_AMBIENT_TEMPERATURE
    comfort_min_temperature: float = 38.0
    shower_temperature: float = 40.0
    cutoff_temperature: float = 68.0
    power_threshold_w: float = 50.0
    # Initial effective capacity factor k_cap (then learned).
    capacity_factor: float = DEFAULT_CAPACITY_FACTOR
    # The probe is at the top and sees the water leaving. False: it is in the
    # thermostat's sheath, low in the tank, where the cold water arrives; it
    # stays hot during the first litres of a draw and then falls quickly when
    # the cold front reaches it, long before the hot water at the top runs out.
    probe_at_top: bool = True
    # k_use while none is learned with the probe in the sheath (it cannot be
    # learned from that probe): a prudent share of the energy above mains.
    usable_factor_default: float = 0.85
    # Share of the volume below a probe in the sheath: the cold water fills it
    # before reaching the probe (24/11: about 30 L of 100 L).
    probe_volume_below: float = 0.3

    @property
    def heat_capacity(self) -> float:
        """Return the heat capacity of the tank in kWh/K."""
        return self.volume_l * WATER_HEAT_CAPACITY_KWH_PER_L_K


@dataclass(frozen=True, slots=True)
class PowerDrop:
    """The heating element power fell below the threshold (event time)."""

    timestamp: datetime
    # The regulator asked for power at that instant (manual mode on or output
    # above 10 %) and had not just been lowered: the thermostat, not the
    # regulator, stopped the heating.
    demand: bool
    # The regulator was unavailable, restarted, did not heat in its mode or
    # asked for no power before the drop, or became unavailable or restarted
    # after it: never a thermostat cut-off.
    disturbed: bool = False
    # Regulator output at the drop (%), and whether it asked for more power
    # after it (then a later stop does not withdraw the demand).
    regulator: float | None = None
    insisted: bool = False
    # At full output (or in manual mode) at the drop and since: held there
    # for REGULATOR_HOLD, it counts as asking for more.
    full: bool = False


class PowerDropDetector:
    """Capture the regulator state at the instant the power falls.

    Fed with every state change of the power, regulator output and manual
    mode, in the order they happen, and of the regulator's health entities
    (mode, uptime, restart reason, output switch) when configured. The last
    drop is kept until the power rises again.
    """

    def __init__(
        self,
        threshold_w: float,
        active_modes: Iterable[str] = DEFAULT_REGULATOR_ACTIVE_MODES,
    ) -> None:
        """Initialise the detector."""
        self._threshold_w = threshold_w
        self._active_modes = frozenset(active_modes)
        self._regulator: float | None = None
        self._manual: bool | None = None
        self._falls: list[datetime] = []
        self._heating: bool | None = None
        self.drop: PowerDrop | None = None
        # Regulator health: sources in an abnormal state now (True when
        # unavailable), the recent moments it was disturbed, and the last
        # known uptime and restart reason (kept while unavailable, to see a
        # restart when it returns).
        self._abnormal: dict[str, bool] = {}
        self._disturbances: list[datetime] = []
        self._uptime: float | None = None
        self._restart_reason: str | None = None

    def _disturb(self, timestamp: datetime, unavailable: bool) -> None:
        """Record a moment the regulator was disturbed.

        ``unavailable``: it was lost or restarted, which is reported late and
        is the only disturbance that counts after the drop.
        """
        self._disturbances = [
            moment
            for moment in self._disturbances
            if timestamp - moment <= REGULATOR_DISTURBED_BEFORE
        ]
        self._disturbances.append(timestamp)
        drop = self.drop
        if (
            unavailable
            and drop is not None
            and not drop.disturbed
            and timedelta(0)
            <= timestamp - drop.timestamp
            <= REGULATOR_UNAVAILABLE_AFTER
        ):
            self.drop = replace(drop, disturbed=True)

    def _set_abnormal(
        self, timestamp: datetime, source: str, abnormal: bool, unavailable: bool
    ) -> None:
        """Track a source entering, staying in or leaving an abnormal state."""
        was = self._abnormal.pop(source, None)
        if abnormal:
            self._abnormal[source] = unavailable
            self._disturb(timestamp, unavailable)
        elif was is not None:
            self._disturb(timestamp, was)

    def _disturbed_at(self, timestamp: datetime) -> bool:
        """Return True if the regulator was disturbed shortly before ``timestamp``."""
        return bool(self._abnormal) or any(
            timedelta(0) <= timestamp - moment <= REGULATOR_DISTURBED_BEFORE
            for moment in self._disturbances
        )

    def update_mode(self, timestamp: datetime, value: str | None) -> None:
        """Record a regulator mode change (None: unavailable)."""
        self._set_abnormal(
            timestamp,
            "mode",
            value is None or value not in self._active_modes,
            value is None,
        )

    def update_uptime(self, timestamp: datetime, value: float | None) -> None:
        """Record the regulator uptime (s); a lower value is a restart."""
        self._set_abnormal(timestamp, "uptime", value is None, True)
        if value is None:
            return
        if self._uptime is not None and value < self._uptime:
            self._disturb(timestamp, True)
        self._uptime = value

    def update_restart_reason(self, timestamp: datetime, value: str | None) -> None:
        """Record the regulator's last restart reason; a new one is a restart."""
        self._set_abnormal(timestamp, "restart_reason", value is None, True)
        if value is None:
            return
        if self._restart_reason is not None and value != self._restart_reason:
            self._disturb(timestamp, True)
        self._restart_reason = value

    def update_output(self, timestamp: datetime, value: bool | None) -> None:
        """Record the regulator output switch (only its availability matters)."""
        self._set_abnormal(timestamp, "output", value is None, True)

    def _record_fall(self, timestamp: datetime) -> None:
        self._falls.append(timestamp)
        self._falls = [
            moment
            for moment in self._falls
            if timestamp - moment <= REGULATOR_FALLING_WINDOW
        ]

    def _after_drop(self, timestamp: datetime) -> PowerDrop | None:
        """The current drop, if ``timestamp`` is shortly after it."""
        drop = self.drop
        if (
            drop is not None
            and timedelta(0) <= timestamp - drop.timestamp <= REGULATOR_STOP_AFTER
        ):
            return drop
        return None

    def _withdraw(self, timestamp: datetime) -> None:
        """The regulator stopped right after the power fell: not a cut-off.

        Its state is reported a few seconds after the power, so a stop just
        after the drop may be its cause. Not once it asked for more power
        with the element drawing none: the thermostat had cut off.
        """
        drop = self._after_drop(timestamp)
        if drop is not None and drop.demand and not drop.insisted:
            self.drop = replace(drop, demand=False)

    def _held(self, timestamp: datetime) -> None:
        """Called before a regulator change: was it held at full since the drop?"""
        drop = self._after_drop(timestamp)
        if (
            drop is not None
            and drop.full
            and timestamp - drop.timestamp >= REGULATOR_HOLD
        ):
            self._insist(timestamp)

    def _insist(self, timestamp: datetime) -> None:
        """The regulator asked for more power right after the drop."""
        drop = self._after_drop(timestamp)
        if drop is not None and drop.demand and not drop.insisted:
            self.drop = replace(drop, insisted=True)

    def update_regulator(self, timestamp: datetime, value: float | None) -> None:
        """Record a regulator output change (%; None: unavailable)."""
        self._held(timestamp)
        if (
            (drop := self.drop) is not None
            and drop.full
            and not self._manual
            and (value is None or value < REGULATOR_FULL_PERCENT)
        ):
            self.drop = replace(drop, full=False)
        if (
            value is not None
            and self._regulator is not None
            and value < self._regulator
        ):
            if value <= REGULATOR_DEMAND_PERCENT:
                self._record_fall(timestamp)
                self._withdraw(timestamp)
            elif self._regulator - value >= REGULATOR_FALL_STEP:
                self._record_fall(timestamp)
        drop = self.drop
        if (
            value is not None
            and drop is not None
            and (
                value >= REGULATOR_FULL_PERCENT
                or (
                    drop.regulator is not None
                    and value >= drop.regulator + REGULATOR_INSIST_RISE
                )
            )
        ):
            self._insist(timestamp)
        self._regulator = value
        self._set_abnormal(
            timestamp, "regulator", value is None or value <= 0, value is None
        )

    def update_manual(self, timestamp: datetime, value: bool | None) -> None:
        """Record a manual mode change (full power requested when on)."""
        self._held(timestamp)
        if (
            (drop := self.drop) is not None
            and drop.full
            and value is not True
            and (self._regulator is None or self._regulator < REGULATOR_FULL_PERCENT)
        ):
            self.drop = replace(drop, full=False)
        if value is False and self._manual:
            self._record_fall(timestamp)
            self._withdraw(timestamp)
        elif value is True and not self._manual:
            self._insist(timestamp)
        self._manual = value
        self._set_abnormal(timestamp, "manual", value is None, True)

    @property
    def demand(self) -> bool:
        """Return True if the regulator currently asks for power."""
        return bool(self._manual) or (
            self._regulator is not None and self._regulator > REGULATOR_DEMAND_PERCENT
        )

    def update_power(self, timestamp: datetime, value: float | None) -> None:
        """Record a heating element power change (W)."""
        if value is None:
            return
        heating = value > self._threshold_w
        if heating:
            self.drop = None
        elif self._heating:
            falling = any(
                timedelta(0) <= timestamp - moment <= REGULATOR_FALLING_WINDOW
                for moment in self._falls
            )
            self.drop = PowerDrop(
                timestamp=timestamp,
                demand=self.demand and not falling,
                disturbed=self._disturbed_at(timestamp),
                regulator=self._regulator,
                full=bool(self._manual)
                or (
                    self._regulator is not None
                    and self._regulator >= REGULATOR_FULL_PERCENT
                ),
            )
        self._heating = heating


@dataclass(frozen=True, slots=True)
class TankSample:
    """One reading of the inputs (°C, W, kWh, %)."""

    timestamp: datetime
    temperature: float | None
    power: float | None
    energy: float | None = None
    regulator: float | None = None
    manual_mode: bool | None = None
    ambient_temperature: float | None = None
    # Last power drop seen by a PowerDropDetector, if any.
    power_drop: PowerDrop | None = None
    # A regulator (manual mode or PWM output) is configured. When it is but
    # its state is unavailable, a stop of the heating is left undecided: the
    # temperature backup criterion is only for installations without one.
    regulator_configured: bool = False
    # Hot water meter: cumulative litres and flow (L/min); ``meter_configured``
    # switches the draw detection from the probe to the litres.
    hot_water_volume: float | None = None
    hot_water_flow: float | None = None
    meter_configured: bool = False
    # A maintenance window is open (devices updated or restarting): the model
    # keeps its energy balance but learns nothing, and the draws it detects
    # meanwhile are neither shortages nor part of the demand profile.
    maintenance: bool = False

    @property
    def valid(self) -> bool:
        """Return True when the required inputs (probe and power) are present."""
        return self.temperature is not None and self.power is not None

    @property
    def regulator_known(self) -> bool:
        """Return True if the regulator state is known."""
        return self.regulator is not None or self.manual_mode is not None

    @property
    def regulator_demand(self) -> bool:
        """Return True if the regulator asks for power."""
        return bool(self.manual_mode) or (
            self.regulator is not None and self.regulator > REGULATOR_DEMAND_PERCENT
        )


@dataclass(frozen=True, slots=True)
class ConsumptionEvent:
    """A detected hot water draw."""

    start: datetime
    end: datetime
    energy_kwh: float
    start_temperature: float
    min_temperature: float
    while_heating: bool
    shortage: bool
    # Detected by the slow draw detector (60-minute window).
    slow: bool = False
    # Factor applied by the energy reconciliation (None: not reconciled).
    reconciled: float | None = None
    # Not observed: inferred from the probe drop across a gap of readings
    # (start and end are the bounds of the gap).
    estimated: bool = False
    # Litres measured by the hot water meter (None: no meter).
    liters: float | None = None
    # Overlaps a maintenance window: not a shortage, not learned as demand.
    maintenance: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "energy_kwh": round(self.energy_kwh, 4),
            "start_temperature": round(self.start_temperature, 2),
            "min_temperature": round(self.min_temperature, 2),
            "while_heating": self.while_heating,
            "shortage": self.shortage,
            "slow": self.slow,
            "reconciled": (
                round(self.reconciled, 3) if self.reconciled is not None else None
            ),
            "estimated": self.estimated,
            "liters": round(self.liters, 1) if self.liters is not None else None,
            **({"maintenance": True} if self.maintenance else {}),
        }

    @property
    def metered(self) -> bool:
        """Return True if the draw was measured by the hot water meter."""
        return self.liters is not None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionEvent:
        """Rebuild an event from :meth:`as_dict` output (or format 1)."""
        minimum = data.get("min_temperature", data.get("end_temperature"))
        if minimum is None:
            raise KeyError("min_temperature")
        return cls(
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            energy_kwh=float(data["energy_kwh"]),
            start_temperature=float(data["start_temperature"]),
            min_temperature=float(minimum),
            while_heating=bool(data["while_heating"]),
            shortage=bool(data.get("shortage", False)),
            slow=bool(data.get("slow", False)),
            estimated=bool(data.get("estimated", False)),
            liters=None if data.get("liters") is None else float(data["liters"]),
            maintenance=bool(data.get("maintenance", False)),
            reconciled=(
                None if data.get("reconciled") is None else float(data["reconciled"])
            ),
        )


@dataclass(frozen=True, slots=True)
class TankState:
    """Published state of the tank model."""

    timestamp: datetime
    available: bool
    status: ModelStatus
    heater_state: HeaterState
    drawing: bool
    probe_temperature: float | None
    equivalent_temperature: float | None
    mains_temperature: float
    stored_energy_kwh: float | None
    useful_energy_kwh: float | None
    charge_percent: float | None
    liters_available: float | None  # useful litres (see DEFAULT_USABLE_FACTOR)
    liters_theoretical: float | None  # all the heat above mains, stratified
    cutoff_temperature: float
    cutoff_samples: tuple[float, ...]
    ua_w_per_k: float
    loss_windows: int
    capacity_factor: float
    capacity_samples: int
    usable_factor: float
    usable_samples: int
    heater_power_w: float
    calibration_count: int
    last_calibration: datetime | None
    last_consumption: ConsumptionEvent | None
    # Why the status is not "calibrated", and the evidence of each parameter.
    status_reason: str = REASON_CALIBRATED
    uncertain_since: datetime | None = None
    heater_power_samples: int = 0
    # Hot water meter: probe drop per litre against a mixed tank (1: mixed).
    stratification_ratio: float = 1.0
    stratification_samples: int = 0
    # Stratification learned: the useful energy is k_use times the energy
    # above the mains water, not the energy above the comfort temperature of
    # a mixed tank. ``useful_floor_temperature`` is that lower limit (the
    # comfort temperature, or the mains water when stratified).
    stratified: bool = False
    useful_floor_temperature: float = 38.0
    # Temperature of the hot layer at the outlet (what the probe sees), as
    # opposed to the equivalent (mixed) temperature.
    outlet_temperature: float | None = None

    def loss_power_w(self, ambient_temperature: float) -> float | None:
        """Return the current standing loss in W."""
        if self.equivalent_temperature is None:
            return None
        return self.ua_w_per_k * (self.equivalent_temperature - ambient_temperature)


@dataclass(slots=True)
class _HistoryPoint:
    timestamp: datetime
    probe: float


@dataclass(slots=True)
class _ActiveEvent:
    start: datetime
    start_temperature: float
    energy_kwh: float
    min_probe: float
    min_time: datetime
    detected: datetime
    while_heating: bool
    slow: bool = False
    # Draw energy accumulated when the probe crossed the comfort temperature.
    energy_at_comfort: float | None = None


@dataclass(slots=True)
class _MeterDraw:
    """A draw measured by the hot water meter, still running."""

    start: datetime
    last_flow: datetime
    outlet: float  # tank temperature when it started (°C)
    liters: float = 0.0
    energy_kwh: float = 0.0
    min_probe: float | None = None
    heating: bool = False
    estimated: bool = False  # litres counted across a gap of the meter
    energy_at_comfort: float | None = None


@dataclass(slots=True)
class _StratificationProbe:
    """Probe drop against the metered draws since a reading before them."""

    start_probe: float
    start_time: datetime
    expected: float = 0.0  # °C the model drops for the metered energy
    heating: bool = False


@dataclass(slots=True)
class _HeaterTracker:
    state: HeaterState = HeaterState.IDLE
    episode_energy: float = 0.0
    below_since: datetime | None = None
    demand_since: datetime | None = None
    last_heating_end: datetime | None = None
    last_heating_sample: datetime | None = None
    # Regulator demand captured when the power fell (None: not known).
    drop_demand: bool | None = None


@dataclass(slots=True)
class _CapacityEpisode:
    """A heating episode used to learn the effective capacity factor."""

    start_probe: float
    energy_kwh: float = 0.0
    draw_seen: bool = False
    probe_sum: float = 0.0
    ambient_sum: float = 0.0
    samples: int = 0
    hours: float = 0.0


@dataclass(slots=True)
class _Anchor:
    """A settled state after a heating, start of an energy balance."""

    time: datetime
    probe: float
    heat_kwh: float = 0.0
    loss_kwh: float = 0.0


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Result of an energy balance between two settled heatings."""

    start: datetime
    end: datetime
    heat_kwh: float
    loss_kwh: float
    measured_kwh: float
    actual_kwh: float
    factor: float | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "heat_kwh": round(self.heat_kwh, 3),
            "loss_kwh": round(self.loss_kwh, 3),
            "measured_kwh": round(self.measured_kwh, 3),
            "actual_kwh": round(self.actual_kwh, 3),
            "factor": round(self.factor, 3) if self.factor is not None else None,
        }


@dataclass(frozen=True, slots=True)
class _UsableObservation:
    """A real shortage, waiting for the reconciliation of its interval."""

    anchor_time: datetime
    anchor_probe: float
    energy_before_kwh: float  # draws since the anchor, before this one
    energy_until_comfort_kwh: float  # this draw until the comfort crossing
    mains: float
    shower: float
    comfort: float
    volume_l: float


@dataclass(slots=True)
class _LossWindow:
    points: list[tuple[datetime, float]] = field(default_factory=list)
    ambient_sum: float = 0.0
    ambients: list[float] = field(default_factory=list)


class TankModel:
    """Energy-balance model of the tank, updated once per sample."""

    def __init__(self) -> None:
        """Create an uninitialised model (state taken from the first sample)."""
        self._equivalent: float | None = None
        self._ua = DEFAULT_UA
        self._ua_covariance = RLS_INITIAL_COVARIANCE
        self._loss_windows = 0
        self._loss_rejected = 0  # runtime: windows rejected as implausible
        self._capacity_samples: list[float] = []
        self._heater_power_w: float | None = None
        self._heater_power_samples = 0
        # Learned cut-offs (time, °C), oldest first; the time is None for
        # samples saved before 0.12.3. Samples discarded by hand are kept
        # apart, so a history import does not bring them back.
        self._cutoffs: list[tuple[datetime | None, float]] = []
        self._discarded_cutoffs: list[tuple[datetime | None, float]] = []
        self._uncertain = True
        self._uncertain_reason = REASON_NEVER_CALIBRATED
        self._uncertain_since: datetime | None = None
        self._calibration_count = 0
        self._last_calibration: datetime | None = None
        self._last_valid_time: datetime | None = None
        self._last_energy: float | None = None
        self._last_power: float | None = None
        self._consumptions: deque[ConsumptionEvent] = deque(maxlen=MAX_CONSUMPTIONS)
        # Maintenance windows seen lately (start, end), and whether this sample
        # is in one: nothing is learned from it.
        self._maintenance_spans: deque[tuple[datetime, datetime]] = deque(
            maxlen=MAX_MAINTENANCE_SPANS
        )
        self._maintenance = False
        # Hot water meter: last reading, (temperature, ratio) samples of the
        # stratification, and how far the probe is expected above the model.
        self._meter_volume: float | None = None
        self._meter_time: datetime | None = None
        self._stratification: list[tuple[float, float]] = []
        self._stratification_offset = 0.0
        # Probe in the thermostat's sheath: temperature of the hot layer at
        # the top (the last full charge, less the losses; draws do not lower
        # it while hot water remains).
        self._hot: float | None = None

        # Runtime only (not persisted)
        self._filter = ProbeFilter(tau_s=FILTER_TAU_S)
        self._probe: float | None = None
        self._heater = _HeaterTracker()
        self._history: deque[_HistoryPoint] = deque()
        self._residuals: deque[tuple[datetime, float]] = deque()
        self._event: _ActiveEvent | None = None
        self._last_consumption_end: datetime | None = None
        self._last_meter_activity: datetime | None = None
        self._quiet_since: datetime | None = None
        self._loss = _LossWindow()
        self._episode: _CapacityEpisode | None = None
        self._anchor: _Anchor | None = None
        self._reconciliations: deque[Reconciliation] = deque(maxlen=MAX_RECONCILIATIONS)
        self._usable_samples: list[float] = []
        self._pending_usable: list[_UsableObservation] = []
        self._meter_draw: _MeterDraw | None = None
        self._strat_probe: _StratificationProbe | None = None
        self._last_probe_time: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def _cutoff_samples(self) -> list[float]:
        """Learned cut-off temperatures, oldest first."""
        return [value for _, value in self._cutoffs]

    @_cutoff_samples.setter
    def _cutoff_samples(self, values: list[float]) -> None:
        """Replace the samples by temperatures without a time (tests, old data)."""
        self._cutoffs = [(None, float(value)) for value in values]

    @property
    def consumptions(self) -> list[ConsumptionEvent]:
        """Return the most recent detected draws, oldest first."""
        return list(self._consumptions)

    @property
    def reconciliations(self) -> list[Reconciliation]:
        """Return the recent energy balances, oldest first."""
        return list(self._reconciliations)

    @property
    def ua(self) -> float:
        """Return the learned loss coefficient UA (W/K)."""
        return self._ua

    def capacity_factor(self, params: TankParams) -> float:
        """Return the learned effective capacity factor k_cap."""
        if not self._capacity_samples:
            return params.capacity_factor
        return _clamp(
            median(self._capacity_samples), MIN_CAPACITY_FACTOR, MAX_CAPACITY_FACTOR
        )

    @property
    def usable_factor(self) -> float:
        """Return the learned share of the useful litres really available."""
        if not self._usable_samples:
            return DEFAULT_USABLE_FACTOR
        return _clamp(
            median(self._usable_samples), MIN_USABLE_FACTOR, MAX_USABLE_FACTOR
        )

    def usable_factor_for(self, params: TankParams) -> float:
        """Return k_use: learned, or the default while none is learned.

        The default is 1.0 with the probe at the top (as before 0.12.3) and
        ``params.usable_factor_default`` with the probe in the sheath, which
        cannot teach it.
        """
        if self._usable_samples or params.probe_at_top:
            return self.usable_factor
        return params.usable_factor_default

    def reset_usable_factor(self) -> int:
        """Forget the learned k_use samples; return how many there were."""
        count = len(self._usable_samples)
        self._usable_samples.clear()
        self._pending_usable = []
        return count

    def stratification_ratio(self, temperature: float | None = None) -> float:
        """Return the probe drop per metered energy against a mixed tank.

        1 means the probe falls as if the tank were fully mixed; less, that
        the water at the probe stays hot while cold water enters below it.
        Samples taken near ``temperature`` are preferred.
        """
        samples = self._stratification
        if temperature is not None:
            near = [
                ratio
                for start, ratio in samples
                if abs(start - temperature) <= STRATIFICATION_BAND
            ]
            if len(near) >= MIN_STRATIFICATION_SAMPLES:
                return median(near)
        if len(samples) >= MIN_STRATIFICATION_SAMPLES:
            return median(ratio for _, ratio in samples)
        return 1.0

    @property
    def stratified(self) -> bool:
        """Return True when the tank is known to be stratified.

        Enough metered draws showed the probe falling clearly less than a
        mixed tank would.
        """
        return (
            len(self._stratification) >= MIN_STRATIFICATION_SAMPLES
            and self.stratification_ratio() <= STRATIFIED_MAX_RATIO
        )

    def useful_floor_temperature(self, params: TankParams) -> float:
        """Return the temperature down to which the stored energy is useful.

        In a mixed tank only the heat above the comfort temperature is
        usable. In a stratified tank the water leaves hot while cold water
        fills the bottom: the energy above the mains water is usable, times
        the learned ``k_use`` (the mixing zone is lost).
        """
        if self.stratified:
            return params.mains_temperature
        return params.comfort_min_temperature

    @property
    def stratification_samples(self) -> list[tuple[float, float]]:
        """Return the (start temperature, ratio) samples, oldest first."""
        return list(self._stratification)

    def heater_power(self, params: TankParams) -> float:
        """Return the learned heater power (W), or the rated one."""
        if self._heater_power_w is None:
            return params.rated_power_w
        return self._heater_power_w

    def cutoff_temperature(self, params: TankParams) -> float:
        """Return the learned thermostat cut-off temperature."""
        if len(self._cutoff_samples) >= MIN_CUTOFF_SAMPLES:
            return median(self._cutoff_samples)
        return params.cutoff_temperature

    def status(self) -> ModelStatus:
        """Return the confidence in the current state."""
        if self._uncertain:
            return ModelStatus.UNCERTAIN
        if self._loss_windows < MIN_LOSS_WINDOWS:
            return ModelStatus.LEARNING
        return ModelStatus.CALIBRATED

    def status_reason(self) -> str:
        """Return why the status is what it is (``REASON_*``).

        Uncertain until a heating reaches the thermostat cut-off (never
        calibrated, or readings missing for a while); then learning until
        ``MIN_LOSS_WINDOWS`` rest windows have measured the heat losses.
        """
        if self._uncertain:
            return self._uncertain_reason
        if self._loss_windows < MIN_LOSS_WINDOWS:
            return REASON_FEW_LOSS_WINDOWS
        return REASON_CALIBRATED

    def _set_uncertain(self, reason: str, now: datetime) -> None:
        if not self._uncertain or self._uncertain_since is None:
            self._uncertain_since = now
        self._uncertain = True
        self._uncertain_reason = reason

    def update(self, sample: TankSample, params: TankParams) -> TankState:
        """Process one sample and return the new state."""
        self._track_maintenance(sample)
        try:
            return self._update(sample, params)
        finally:
            self._mark_maintenance_draws()

    def _track_maintenance(self, sample: TankSample) -> None:
        """Extend the current maintenance span, or open one."""
        self._maintenance = sample.maintenance
        if not sample.maintenance:
            return
        now = sample.timestamp
        spans = self._maintenance_spans
        if spans and now - spans[-1][1] <= MAINTENANCE_SPAN_JOIN:
            spans[-1] = (spans[-1][0], now)
        else:
            # It started at the previous sample at the latest.
            start = min(now, self._last_valid_time or now)
            spans.append((start, now))

    def _mark_maintenance_draws(self) -> None:
        """Flag the draws that overlap a maintenance window."""
        if not self._maintenance_spans:
            return
        oldest = self._maintenance_spans[0][0]
        for index, event in enumerate(self._consumptions):
            if event.maintenance or event.end < oldest:
                continue
            if any(
                event.start <= end and start <= event.end
                for start, end in self._maintenance_spans
            ):
                self._consumptions[index] = replace(
                    event, maintenance=True, shortage=False
                )

    def _update(self, sample: TankSample, params: TankParams) -> TankState:
        if sample.meter_configured:
            # Litres are counted even while the probe or the power is missing.
            self._track_meter(sample, params)
        if not sample.valid:
            # Safe mode: freeze everything, invent nothing.
            return self._state(sample.timestamp, params, available=False)
        assert sample.temperature is not None

        now = sample.timestamp
        if self._quiet_since is None:
            self._quiet_since = now

        if self._equivalent is None or self._last_valid_time is None:
            self._initialise(sample, params)
            return self._state(now, params, available=True)

        elapsed = now - self._last_valid_time
        if elapsed <= timedelta(0):
            return self._state(now, params, available=True)
        if elapsed > FILTER_RESET_GAP:
            self._recover_from_gap(sample, params, elapsed)
        else:
            self._step(sample, params, elapsed)
        self._last_probe_time = now
        if sample.meter_configured and not self._maintenance:
            self._learn_stratification(now, self._ambient(sample, params), params)
        return self._state(now, params, available=True)

    def reset_losses(self) -> None:
        """Forget the learned loss coefficient."""
        self._ua = DEFAULT_UA
        self._ua_covariance = RLS_INITIAL_COVARIANCE
        self._loss_windows = 0
        self._loss = _LossWindow()

    def reset_learning(self) -> None:
        """Forget every learned parameter (losses, k_cap, power, cut-offs, draws)."""
        self.reset_losses()
        self._capacity_samples.clear()
        self._usable_samples.clear()
        self._heater_power_w = None
        self._heater_power_samples = 0
        self._cutoffs.clear()
        self._consumptions.clear()
        self._stratification.clear()
        self._stratification_offset = 0.0
        self._episode = None

    def learned_samples(self) -> int:
        """Return how much the model has learned (0: nothing at all)."""
        return (
            self._loss_windows
            + len(self._capacity_samples)
            + len(self._usable_samples)
            + self._heater_power_samples
            + len(self._cutoff_samples)
            + len(self._consumptions)
            + len(self._stratification)
        )

    def merge_learning(self, other: TankModel) -> None:
        """Keep, parameter by parameter, what is better learned in ``other``.

        Used after a history import (``self`` is the imported model, ``other``
        the model that was running): every learned parameter with more
        evidence in the running model is taken from it, so an import never
        loses learning. But when both learned a parameter and the running
        value is not coherent with the history (beyond a tolerance), the
        history wins whatever the evidence: e.g. a heat loss coefficient
        inflated by slow draws. Draws are joined (the imported ones win where
        both overlap).
        """

        def keep_running(
            own_evidence: int,
            running_evidence: int,
            own: float,
            running: float,
            tolerance: float,
        ) -> bool:
            if running_evidence <= own_evidence:
                return False
            return own_evidence == 0 or abs(running - own) <= tolerance

        if keep_running(
            self._loss_windows,
            other._loss_windows,
            self._ua,
            other._ua,
            MERGE_TOLERANCE["ua"] * self._ua,
        ):
            self._ua = other._ua
            self._ua_covariance = other._ua_covariance
            self._loss_windows = other._loss_windows
        if keep_running(
            len(self._capacity_samples),
            len(other._capacity_samples),
            median(self._capacity_samples or [0.0]),
            median(other._capacity_samples or [0.0]),
            math.inf,
        ):
            self._capacity_samples = list(other._capacity_samples)
        if keep_running(
            len(self._usable_samples),
            len(other._usable_samples),
            median(self._usable_samples or [0.0]),
            median(other._usable_samples or [0.0]),
            math.inf,
        ):
            self._usable_samples = list(other._usable_samples)
        if keep_running(
            self._heater_power_samples,
            other._heater_power_samples,
            self._heater_power_w or 0.0,
            other._heater_power_w or 0.0,
            MERGE_TOLERANCE["power"] * (self._heater_power_w or 0.0),
        ):
            self._heater_power_w = other._heater_power_w
            self._heater_power_samples = other._heater_power_samples
        # Cut-offs: the union of both (an import of the last days may lack
        # older samples the running model has), without the discarded ones.
        discarded = _union_cutoffs(self._discarded_cutoffs, other._discarded_cutoffs)
        self._discarded_cutoffs = discarded[-MAX_CUTOFF_SAMPLES:]
        self._cutoffs = [
            sample
            # The imported samples first: they carry their time.
            for sample in _union_cutoffs(self._cutoffs, other._cutoffs)
            if not any(_same_cutoff(sample, gone) for gone in discarded)
        ][-MAX_CUTOFF_SAMPLES:]
        if len(other._stratification) > len(self._stratification):
            self._stratification = list(other._stratification)
        # Draws of the running model that the import did not see (or that
        # are older than the imported period) are kept; overlapping ones are
        # the same draw, recomputed by the import with the current models.
        imported = list(self._consumptions)
        kept = [
            event
            for event in other._consumptions
            if not any(
                event.start < item.end and item.start < event.end for item in imported
            )
        ]
        result = sorted([*imported, *kept], key=lambda event: event.start)
        self._consumptions = deque(result, maxlen=MAX_CONSUMPTIONS)

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent part of the model."""
        return {
            "format": STORAGE_FORMAT,
            "equivalent_temperature": self._equivalent,
            "ua": self._ua,
            "ua_covariance": self._ua_covariance,
            "loss_windows": self._loss_windows,
            "capacity_samples": list(self._capacity_samples),
            "usable_samples": list(self._usable_samples),
            "heater_power_w": self._heater_power_w,
            "heater_power_samples": self._heater_power_samples,
            "cutoff_samples": list(self._cutoff_samples),
            "cutoff_times": [_iso(moment) for moment, _ in self._cutoffs],
            "discarded_cutoffs": [
                [_iso(moment), value] for moment, value in self._discarded_cutoffs
            ],
            "uncertain": self._uncertain,
            "uncertain_reason": self._uncertain_reason,
            "uncertain_since": _iso(self._uncertain_since),
            "calibration_count": self._calibration_count,
            "last_calibration": _iso(self._last_calibration),
            "last_valid_time": _iso(self._last_valid_time),
            "last_energy": self._last_energy,
            "consumptions": [event.as_dict() for event in self._consumptions],
            "meter_volume": self._meter_volume,
            "meter_time": _iso(self._meter_time),
            "stratification": [list(item) for item in self._stratification],
            "stratification_offset": self._stratification_offset,
            "hot_temperature": self._hot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, volume_l: float = 100.0) -> TankModel:
        """Restore a model saved with :meth:`as_dict`.

        Data saved by version 0.2 (format 1, losses as a 1/h coefficient) is
        converted; ``volume_l`` is needed for that conversion only.
        Raises ValueError if the data is not usable.
        """
        try:
            fmt = data.get("format")
            if fmt not in (1, STORAGE_FORMAT):
                raise ValueError(f"unsupported format {fmt!r}")
            model = cls()
            equivalent = data["equivalent_temperature"]
            model._equivalent = None if equivalent is None else float(equivalent)
            if fmt == 1:
                ua = (
                    float(data["loss_coefficient"])
                    * volume_l
                    * WATER_HEAT_CAPACITY_KWH_PER_L_K
                    * 1000
                )
            else:
                ua = float(data["ua"])
                model._ua_covariance = float(data["ua_covariance"])
                model._capacity_samples = [
                    float(value) for value in data["capacity_samples"]
                ][-MAX_CAPACITY_SAMPLES:]
                model._usable_samples = [
                    float(value) for value in data.get("usable_samples", [])
                ][-MAX_USABLE_SAMPLES:]
                power = data["heater_power_w"]
                model._heater_power_w = None if power is None else float(power)
                model._heater_power_samples = int(data["heater_power_samples"])
            model._ua = _clamp(ua, MIN_UA, MAX_UA)
            model._loss_windows = int(data["loss_windows"])
            values = [float(value) for value in data["cutoff_samples"]]
            times = data.get("cutoff_times") or []
            if len(times) != len(values):
                times = [None] * len(values)  # saved before 0.12.3
            model._cutoffs = [
                (_from_iso(moment), value)
                for moment, value in zip(times, values, strict=True)
            ][-MAX_CUTOFF_SAMPLES:]
            model._discarded_cutoffs = [
                (_from_iso(moment), float(value))
                for moment, value in data.get("discarded_cutoffs", [])
            ]
            model._uncertain = bool(data["uncertain"])
            model._calibration_count = int(data["calibration_count"])
            model._uncertain_reason = str(
                data.get("uncertain_reason")
                or (
                    REASON_NEVER_CALIBRATED
                    if model._calibration_count == 0
                    else REASON_DATA_GAP
                )
            )
            model._uncertain_since = _from_iso(data.get("uncertain_since"))
            model._last_calibration = _from_iso(data["last_calibration"])
            model._last_valid_time = _from_iso(data["last_valid_time"])
            last_energy = data["last_energy"]
            model._last_energy = None if last_energy is None else float(last_energy)
            for event in data["consumptions"]:
                model._consumptions.append(ConsumptionEvent.from_dict(event))
            volume = data.get("meter_volume")
            model._meter_volume = None if volume is None else float(volume)
            model._meter_time = _from_iso(data.get("meter_time"))
            model._stratification = [
                (float(start), float(ratio))
                for start, ratio in data.get("stratification", [])
            ][-MAX_STRATIFICATION_SAMPLES:]
            model._stratification_offset = float(data.get("stratification_offset", 0.0))
            hot = data.get("hot_temperature")
            model._hot = None if hot is None else float(hot)
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"invalid tank model data: {err}") from err
        return model

    # ------------------------------------------------------------------
    # Update steps
    # ------------------------------------------------------------------

    def _initialise(self, sample: TankSample, params: TankParams) -> None:
        assert sample.temperature is not None
        self._filter.reset()
        self._probe = self._filter.update(sample.timestamp, sample.temperature)
        self._equivalent = self._clamp_temperature(self._probe, params)
        self._set_uncertain(
            REASON_NEVER_CALIBRATED
            if self._calibration_count == 0
            else REASON_DATA_GAP,
            sample.timestamp,
        )
        self._last_valid_time = sample.timestamp
        self._last_probe_time = sample.timestamp
        self._last_energy = sample.energy
        self._last_power = sample.power

    def _recover_from_gap(
        self, sample: TankSample, params: TankParams, elapsed: timedelta
    ) -> None:
        """Resume after a restart or a period without valid readings."""
        assert sample.temperature is not None
        assert self._equivalent is not None
        now = sample.timestamp
        # Without a meter, heating during the gap cannot be known.
        heat = self._heater_energy(sample, params, elapsed, integrate_power=False)
        ambient = self._ambient(sample, params)
        predicted = self._predict(self._equivalent, heat, elapsed, params, ambient)

        before = self._probe
        gap_start = self._last_valid_time
        self._filter.reset()
        self._probe = self._filter.update(now, sample.temperature)
        hours = elapsed.total_seconds() / 3600
        tolerance = (
            -IDLE_DRAW_THRESHOLD
            + GAP_DRAW_DRIFT_PER_HOUR * hours
            + GAP_DRAW_HEAT_SHARE * heat / params.heat_capacity
        )
        if sample.meter_configured:
            # The litres drawn during the gap were counted (the model already
            # lost their energy): the probe only corrects what they do not
            # explain, e.g. heat added without an energy meter.
            self._stratification_offset = max(
                0.0,
                self._stratification_offset
                - self.capacity_factor(params) * heat / params.heat_capacity,
            )
            target = self._probe - self._stratification_offset
            # A probe in the thermostat's sheath says where the cold front
            # is, not how much energy is left: the metered balance stands.
            if params.probe_at_top and abs(predicted - target) > tolerance:
                predicted = target
        elif abs(predicted - self._probe) > -IDLE_DRAW_THRESHOLD:
            # Water was drawn (or heat added without a meter) during the gap:
            # follow the probe.
            drop = predicted - self._probe
            if drop > tolerance and gap_start is not None and elapsed <= MAX_GAP_DRAW:
                self._record_gap_draw(
                    gap_start, now, drop, before, predicted, heat, params
                )
            predicted = self._probe
        self._equivalent = self._clamp_temperature(predicted, params)
        self._apply_probe_floor(params)
        self._update_hot(elapsed, ambient, params)

        if elapsed > LONG_GAP and not self._maintenance:
            self._set_uncertain(REASON_DATA_GAP, now)
        self._reset_runtime(now)
        self._last_valid_time = now

    def _record_gap_draw(
        self,
        start: datetime,
        end: datetime,
        drop: float,
        before: float | None,
        predicted: float,
        heat: float,
        params: TankParams,
    ) -> None:
        """Record the draw that explains a probe drop across a gap.

        The drop is below what the losses (and the metered heat) explain;
        unknown heat could only hide part of the draw, so this is a lower
        bound. The energy is spread over the gap for the demand profile.
        """
        assert self._probe is not None
        self._consumptions.append(
            ConsumptionEvent(
                start=start,
                end=end,
                energy_kwh=drop * params.heat_capacity / self.capacity_factor(params),
                start_temperature=before if before is not None else predicted,
                min_temperature=self._probe,
                # Meter noise (a few mWh) is not a heating.
                while_heating=heat > GAP_HEATING_MIN_KWH,
                shortage=self._is_shortage(self._probe, params),
                estimated=True,
            )
        )

    def _step(self, sample: TankSample, params: TankParams, elapsed: timedelta) -> None:
        assert sample.temperature is not None
        assert sample.power is not None
        assert self._equivalent is not None
        now = sample.timestamp
        capacity = params.heat_capacity
        k_cap = self.capacity_factor(params)
        ambient = self._ambient(sample, params)

        heat = self._heater_energy(sample, params, elapsed)
        if heat > METER_IDLE_KWH:
            self._last_meter_activity = now
        if not self._maintenance:
            self._learn_heater_power(sample)

        previous_probe = self._probe
        self._probe = self._filter.update(now, sample.temperature)
        if previous_probe is None:  # first sample after a restore
            previous_probe = self._probe
        cooled = self._predict(self._equivalent, 0.0, elapsed, params, ambient)
        predicted = self._clamp_temperature(cooled + k_cap * heat / capacity, params)

        full = self._update_heater(sample, heat, params)
        heating_window = self._in_heating_window(now)

        # Residual of the probe change against the prediction. While heating
        # the probe responds unevenly (stratification), so only drops count.
        observed = self._probe - previous_probe
        loss_change = cooled - self._equivalent
        full_residual = observed - loss_change - k_cap * heat / capacity
        residual = min(0.0, observed - loss_change) if heating_window else full_residual
        self._residuals.append((now, residual))
        while self._residuals and now - self._residuals[0][0] > SLOW_WINDOW:
            self._residuals.popleft()

        alpha = 1.0 - math.exp(-elapsed / SLOW_CORRECTION_TAU)
        if sample.meter_configured:
            # Draws are measured by the meter (their energy is already out of
            # the model): the probe only corrects the drift, beyond its
            # resolution, and is expected above the model while the water
            # at the probe is hotter than the mean (stratification).
            equivalent = predicted
            self._stratification_offset = max(
                0.0, self._stratification_offset - k_cap * heat / capacity
            )
            if params.probe_at_top and self._meter_draw is None and not heating_window:
                # In the thermostat's sheath the probe falls when the cold
                # front reaches it, not with the energy left: no correction
                # between full charges, which set the model at the probe.
                error = self._probe - self._stratification_offset - equivalent
                excess = max(0.0, abs(error) - METERED_PROBE_DEADBAND)
                equivalent += alpha * math.copysign(excess, error)
        else:
            equivalent = self._track_draws(
                now, predicted, full_residual, heating_window, params
            )
            if self._event is None and not heating_window:
                equivalent += alpha * (self._probe - equivalent)
        self._equivalent = self._clamp_temperature(equivalent, params)
        self._apply_probe_floor(params)
        self._update_hot(elapsed, ambient, params)

        if self._maintenance:
            # Episodes that include the window would learn from it: dropped.
            self._episode = None
            self._anchor = None
            self._pending_usable = []
        else:
            self._track_capacity_episode(sample, heat, elapsed, ambient, params)
            self._track_anchor(sample, heat, elapsed, ambient, params)
            # A power drop while devices restart is not a thermostat cut-off.
            if full is not None and self._plausible_cutoff(params):
                self._calibrate(now, regulator_confirmed=full, params=params)

        self._history.append(_HistoryPoint(now, self._probe))
        while self._history and now - self._history[0].timestamp > HISTORY_SPAN:
            self._history.popleft()

        if self._maintenance:
            self._loss = _LossWindow()  # a loss window across it is not learned
        else:
            self._learn_losses(sample, now, heating_window, ambient, params)
        self._last_valid_time = now

    # ------------------------------------------------------------------
    # Energy balance
    # ------------------------------------------------------------------

    def probe_floor(self, params: TankParams) -> float | None:
        """Lowest mean a probe in the sheath allows (None: no bound).

        In a stably stratified tank the water gets warmer upwards: the water
        above the probe is at least at its temperature and the water below
        it at least at the mains temperature. So the mean is at least
        ``p · T_mains + (1 - p) · T_probe``, with ``p`` the share of the
        volume below the probe. Not the probe itself: the cold water fills
        the bottom below it first (24/11: the probe stayed at 68 °C for the
        first 30 L while the mean fell to 60 °C).
        """
        if params.probe_at_top or self._probe is None:
            return None
        share = _clamp(params.probe_volume_below, 0.0, 0.95)
        return share * params.mains_temperature + (1 - share) * self._probe

    def _apply_probe_floor(self, params: TankParams) -> None:
        """Raise a model that fell below what the probe in the sheath allows.

        E.g. draws counted at the temperature of a hot layer that was running
        out: the model lost energy the tank did not. The hot layer, above
        the probe, is never colder than it.
        """
        floor = self.probe_floor(params)
        if floor is None or self._equivalent is None:
            return
        if self._equivalent < floor:
            self._equivalent = self._clamp_temperature(floor, params)
        if (
            self._hot is not None
            and self._probe is not None
            and self._hot < self._probe
        ):
            self._hot = self._clamp_temperature(self._probe, params)

    def _update_hot(
        self, elapsed: timedelta, ambient: float, params: TankParams
    ) -> None:
        """Follow the hot layer at the top: losses lower it, heating raises it.

        Draws do not: cold water enters at the bottom and the water at the
        top leaves at its temperature until it runs out (plug flow).
        """
        if self._equivalent is None:
            return
        hot = self._equivalent if self._hot is None else self._hot
        rate = self._ua / (params.heat_capacity * 1000)  # 1/h
        hot = ambient + (hot - ambient) * math.exp(
            -rate * elapsed.total_seconds() / 3600
        )
        if not params.probe_at_top and self._probe is not None:
            hot = max(hot, self._probe)  # above the probe: never colder
        self._hot = self._clamp_temperature(max(hot, self._equivalent), params)

    def _ambient(self, sample: TankSample, params: TankParams) -> float:
        if sample.ambient_temperature is not None:
            return sample.ambient_temperature
        return params.ambient_temperature

    def _heater_energy(
        self,
        sample: TankSample,
        params: TankParams,
        elapsed: timedelta,
        *,
        integrate_power: bool = True,
    ) -> float:
        """Return the heater energy (kWh) since the previous valid sample."""
        hours = elapsed.total_seconds() / 3600
        limit = params.rated_power_w / 1000 * hours * HEATER_ENERGY_MARGIN
        previous_power, self._last_power = self._last_power, sample.power
        previous, self._last_energy = self._last_energy, sample.energy
        if sample.energy is not None:
            if previous is None:
                return 0.0
            delta = sample.energy - previous
            if delta <= 0:
                return 0.0  # meter reset or no heating
            return min(delta, limit)
        # No energy meter: integrate the power (trapezoid).
        if not integrate_power or previous_power is None or sample.power is None:
            return 0.0
        average_w = max(0.0, (previous_power + sample.power) / 2)
        return min(average_w / 1000 * hours, limit)

    def _predict(
        self,
        equivalent: float,
        heat_kwh: float,
        elapsed: timedelta,
        params: TankParams,
        ambient: float,
    ) -> float:
        hours = elapsed.total_seconds() / 3600
        capacity = params.heat_capacity
        rate = self._ua / (capacity * 1000)  # 1/h
        cooled = ambient + (equivalent - ambient) * math.exp(-rate * hours)
        return self._clamp_temperature(
            cooled + self.capacity_factor(params) * heat_kwh / capacity, params
        )

    def _clamp_temperature(self, value: float, params: TankParams) -> float:
        high = max([self.cutoff_temperature(params), *self._cutoff_samples])
        return _clamp(
            value,
            min(params.mains_temperature, params.ambient_temperature),
            high + CUTOFF_CLAMP_MARGIN,
        )

    def _learn_heater_power(self, sample: TankSample) -> None:
        """Learn the full heater power from samples at full regulator output."""
        assert sample.power is not None
        if sample.power <= HEATER_POWER_MIN_W:
            return
        if sample.regulator_known and not (
            sample.manual_mode
            or (
                sample.regulator is not None
                and sample.regulator >= REGULATOR_FULL_PERCENT
            )
        ):
            return
        self._heater_power_samples += 1
        if self._heater_power_w is None:
            self._heater_power_w = sample.power
            return
        alpha = max(1.0 / self._heater_power_samples, HEATER_POWER_MIN_ALPHA)
        self._heater_power_w += alpha * (sample.power - self._heater_power_w)

    # ------------------------------------------------------------------
    # Heater state machine and recalibration
    # ------------------------------------------------------------------

    def _update_heater(
        self, sample: TankSample, heat: float, params: TankParams
    ) -> bool | None:
        """Advance the heater state machine.

        Returns None when the tank is not confirmed full, True when the
        thermostat cut-off was confirmed by the regulator and False when it
        was confirmed by the temperature backup criterion.
        """
        assert sample.power is not None
        tracker = self._heater
        now = sample.timestamp

        if sample.power > params.power_threshold_w:
            if tracker.state is HeaterState.IDLE:
                tracker.episode_energy = 0.0
            tracker.state = HeaterState.HEATING
            tracker.episode_energy += heat
            tracker.below_since = None
            tracker.demand_since = None
            tracker.drop_demand = None
            tracker.last_heating_sample = now
            return None

        if tracker.state is HeaterState.HEATING:
            tracker.episode_energy += heat
            tracker.state = HeaterState.CHECKING
            tracker.below_since = now
            tracker.last_heating_end = now
            tracker.drop_demand = None

        drop = sample.power_drop
        if (
            tracker.state is HeaterState.CHECKING
            and drop is not None
            and drop.timestamp <= now
            and (
                tracker.last_heating_sample is None
                or drop.timestamp >= tracker.last_heating_sample
            )
            and (tracker.drop_demand is None or drop.timestamp >= tracker.below_since)
        ):
            # Exact instant of the cut-off and the regulator state then.
            tracker.below_since = drop.timestamp
            tracker.last_heating_end = drop.timestamp
            tracker.drop_demand = drop.demand
            if drop.disturbed:
                # The regulator (or its power supply) stopped the heating:
                # the episode ends without a cut-off.
                tracker.state = HeaterState.IDLE
                tracker.drop_demand = None
                return None

        if tracker.state is not HeaterState.CHECKING:
            return None
        assert tracker.below_since is not None

        if tracker.episode_energy < MIN_EPISODE_ENERGY_KWH:
            # A short spike, not a heating episode.
            tracker.state = HeaterState.IDLE
            return None

        if tracker.drop_demand:
            # The regulator was asking for power when the element stopped
            # drawing it: the thermostat is open, even if the regulator is
            # switched off afterwards.
            if now - tracker.below_since >= FULL_CONFIRM:
                tracker.state = HeaterState.IDLE
                return True
            return None

        if sample.regulator_known:
            if sample.regulator_demand:
                if tracker.demand_since is None:
                    tracker.demand_since = now
                if now - tracker.demand_since >= FULL_CONFIRM:
                    tracker.state = HeaterState.IDLE
                    return True
            else:
                # Regulator at rest: heating stopped for lack of surplus.
                tracker.demand_since = None
            if now - tracker.below_since > FULL_CHECK_WINDOW:
                tracker.state = HeaterState.IDLE
            return None

        if sample.regulator_configured:
            # Regulator configured but unavailable: nothing can be concluded.
            if now - tracker.below_since > FULL_CHECK_WINDOW:
                tracker.state = HeaterState.IDLE
            return None

        # Backup criterion: no regulator in the installation.
        if now - tracker.below_since >= FULL_CONFIRM:
            tracker.state = HeaterState.IDLE
            assert self._probe is not None
            if self._probe >= self.cutoff_temperature(params) - BACKUP_FULL_MARGIN:
                return False
        return None

    def _plausible_cutoff(self, params: TankParams) -> bool:
        """Return True if the probe is plausible for a thermostat cut-off.

        Until a cut-off has been learned, high enough compared with the
        configured cut-off; afterwards, close to the known cut-off.
        """
        assert self._probe is not None
        if self._cutoff_samples:
            known = self.cutoff_temperature(params)
            return abs(self._probe - known) <= CUTOFF_PLAUSIBLE_NEAR
        return self._probe >= params.cutoff_temperature - CUTOFF_PLAUSIBLE_BELOW

    @property
    def cutoff_samples(self) -> tuple[float, ...]:
        """Return the learned cut-off samples, oldest first."""
        return tuple(self._cutoff_samples)

    def discard_cutoff_sample(
        self, *, index: int | None = None, value: float | None = None
    ) -> float:
        """Forget one learned cut-off sample and return it.

        ``index`` counts from the oldest (0; negative values from the newest,
        -1 being the last); ``value`` removes the sample closest to it, within
        :data:`CUTOFF_MATCH_MARGIN`. Raises ValueError if nothing matches.
        """
        samples = self._cutoff_samples
        if not samples:
            raise ValueError("no cut-off samples")
        # Remembered, so that a history import does not learn it again.
        if value is not None:
            position = min(range(len(samples)), key=lambda i: abs(samples[i] - value))
            if abs(samples[position] - value) > CUTOFF_MATCH_MARGIN:
                raise ValueError(f"no cut-off sample near {value}")
        elif index is not None:
            if not -len(samples) <= index < len(samples):
                raise ValueError(f"index {index} out of range")
            position = index % len(samples)
        else:
            raise ValueError("an index or a value is required")
        removed = self._cutoffs.pop(position)
        self._discarded_cutoffs = [*self._discarded_cutoffs, removed][
            -MAX_CUTOFF_SAMPLES:
        ]
        return removed[1]

    def purge_cutoffs(self, params: TankParams) -> int:
        """Forget learned cut-offs that cannot be thermostat cut-offs.

        Returns how many were removed.
        """
        minimum = params.cutoff_temperature - CUTOFF_PLAUSIBLE_BELOW
        kept = [sample for sample in self._cutoffs if sample[1] >= minimum]
        removed = len(self._cutoffs) - len(kept)
        self._cutoffs = kept
        return removed

    def _calibrate(
        self, now: datetime, *, regulator_confirmed: bool, params: TankParams
    ) -> None:
        assert self._probe is not None
        if (
            regulator_confirmed
            or self._probe >= self.cutoff_temperature(params) - CUTOFF_LEARN_MARGIN
        ):
            self._cutoffs.append((now, self._probe))
            del self._cutoffs[:-MAX_CUTOFF_SAMPLES]
        self._equivalent = (
            self._probe if regulator_confirmed else self.cutoff_temperature(params)
        )
        self._uncertain = False
        self._uncertain_since = None
        self._stratification_offset = 0.0  # heated to the cut-off: mixed
        self._hot = self._equivalent
        self._calibration_count += 1
        self._last_calibration = now
        self._heater.episode_energy = 0.0
        self._history.clear()
        self._residuals.clear()

    def _in_heating_window(self, now: datetime) -> bool:
        """Return True while heating or shortly after (probe still settling)."""
        tracker = self._heater
        return tracker.state is not HeaterState.IDLE or (
            tracker.last_heating_end is not None
            and now - tracker.last_heating_end <= POST_HEATING
        )

    # ------------------------------------------------------------------
    # Draw detection
    # ------------------------------------------------------------------

    def _residual_sum(self, now: datetime, window: timedelta) -> float:
        return sum(value for moment, value in self._residuals if now - moment < window)

    def _track_draws(
        self,
        now: datetime,
        predicted: float,
        full_residual: float,
        heating_window: bool,
        params: TankParams,
    ) -> float:
        """Detect and follow draws; return the corrected equivalent temperature.

        The draw energy is the residual accumulated since the draw started
        (``C · ΔT_residual / k_cap``), including the heat added meanwhile.
        """
        assert self._probe is not None
        capacity = params.heat_capacity
        k_cap = self.capacity_factor(params)
        event = self._event

        if event is not None:
            # While water is drawn the probe is the best estimate of the
            # mixed temperature; the model never rises faster than the heat
            # input. The draw energy is what the balance cannot explain.
            corrected = min(predicted, self._probe)
            event.energy_kwh -= full_residual * capacity / k_cap
            if (
                event.energy_at_comfort is None
                and self._probe < params.comfort_min_temperature
            ):
                event.energy_at_comfort = event.energy_kwh
            if self._probe < event.min_probe - MIN_TRACK_DROP:
                event.min_probe = self._probe
                event.min_time = now
            end_window, tolerance = (
                (SLOW_END_WINDOW, SLOW_END_TOLERANCE)
                if event.slow
                else (END_WINDOW, END_TOLERANCE)
            )
            if (
                now - event.detected >= end_window
                and self._residual_sum(now, end_window) > -tolerance
            ):
                self._close_event(now, params)
            return corrected

        threshold = HEATING_DRAW_THRESHOLD if heating_window else IDLE_DRAW_THRESHOLD
        window = RESIDUAL_WINDOW
        slow = False
        if self._residual_sum(now, RESIDUAL_WINDOW) >= threshold:
            if not self._slow_draw(now, heating_window, params):
                return predicted
            window = SLOW_WINDOW
            slow = True
            self._loss = _LossWindow()  # the window contained the draw

        recent = [point for point in self._history if now - point.timestamp <= window]
        reference = max(recent, key=lambda point: point.probe, default=None)
        start, start_temperature = (
            (reference.timestamp, reference.probe)
            if reference is not None
            else (now, self._probe)
        )
        corrected = min(predicted, self._probe)
        self._filter.tau_s = FILTER_FAST_TAU_S
        self._event = _ActiveEvent(
            start=start,
            start_temperature=start_temperature,
            energy_kwh=-self._residual_sum(now, window) * capacity / k_cap,
            min_probe=self._probe,
            min_time=now,
            detected=now,
            while_heating=heating_window,
            slow=slow,
        )
        if self._episode is not None:
            self._episode.draw_seen = True
        return corrected

    def _slow_draw(
        self, now: datetime, heating_window: bool, params: TankParams
    ) -> bool:
        """Return True if the probe fell slowly but clearly over an hour.

        Both the residual accumulated over the hour and the robust slope of
        the whole hour (less sensitive to noise than its two ends) must show
        a drop beyond the standing losses.
        """
        if heating_window or not self._residuals:
            return False
        if now - self._residuals[0][0] < SLOW_WINDOW - timedelta(minutes=5):
            return False  # not enough history yet
        for reference in (self._heater.last_heating_end, self._last_consumption_end):
            if reference is not None and now - reference < SLOW_SETTLE:
                return False
        if self._residual_sum(now, SLOW_WINDOW) >= SLOW_DRAW_THRESHOLD:
            return False
        points = [
            ((point.timestamp - now).total_seconds() / 3600, point.probe)
            for point in self._history
        ]
        if len(points) < 2:
            return False
        slope, _ = theil_sen(points)  # °C/h
        assert self._equivalent is not None
        loss_rate = (
            self._ua
            * (self._equivalent - params.ambient_temperature)
            / (params.heat_capacity * 1000)
        )
        hours = SLOW_WINDOW.total_seconds() / 3600
        return (slope + loss_rate) * hours < SLOW_DRAW_THRESHOLD

    def _close_event(self, now: datetime, params: TankParams) -> None:
        event = self._event
        assert event is not None
        self._consumptions.append(
            ConsumptionEvent(
                start=event.start,
                end=event.min_time,
                energy_kwh=max(0.0, event.energy_kwh),
                start_temperature=event.start_temperature,
                min_temperature=event.min_probe,
                while_heating=event.while_heating,
                shortage=self._is_shortage(event.min_probe, params),
                slow=event.slow,
            )
        )
        if self._consumptions[-1].shortage:
            self._observe_shortage(
                event.start,
                event.energy_at_comfort
                if event.energy_at_comfort is not None
                else event.energy_kwh,
                params,
            )
        self._event = None
        self._filter.tau_s = FILTER_TAU_S
        self._last_consumption_end = now
        self._history.clear()
        self._residuals.clear()

    def _is_shortage(self, probe_minimum: float, params: TankParams) -> bool:
        """Whether a draw left the water below the comfort temperature.

        With the probe at the top, its minimum tells. In the thermostat's
        sheath it falls below comfort long before the hot water at the top
        runs out: then only the model can tell, when no useful energy is left.
        """
        if params.probe_at_top:
            return probe_minimum <= params.comfort_min_temperature - SHORTAGE_MARGIN
        return (
            self._equivalent is not None
            and self._equivalent <= self.useful_floor_temperature(params)
        )

    def _observe_shortage(
        self, start: datetime, energy_until_comfort: float, params: TankParams
    ) -> None:
        """Keep a shortage (the last draw) to learn k_use when reconciled.

        Only with the probe at the top: the litres drawn until it crossed the
        comfort temperature are the useful litres. In the sheath it crosses
        it much earlier, so nothing is learned.
        """
        anchor = self._anchor
        if anchor is None or not params.probe_at_top:
            return
        before = sum(
            draw.energy_kwh
            for draw in list(self._consumptions)[:-1]
            if anchor.time <= draw.start < start
        )
        self._pending_usable.append(
            _UsableObservation(
                anchor_time=anchor.time,
                anchor_probe=anchor.probe,
                energy_before_kwh=before,
                energy_until_comfort_kwh=energy_until_comfort,
                mains=params.mains_temperature,
                shower=params.shower_temperature,
                comfort=params.comfort_min_temperature,
                volume_l=params.volume_l,
            )
        )

    # ------------------------------------------------------------------
    # Hot water meter
    # ------------------------------------------------------------------

    def _outlet_temperature(self, now: datetime, params: TankParams) -> float | None:
        """Return the temperature of the water leaving the tank now.

        With the probe at the top, its last reading, or the model with the
        expected stratification when it is too old. With the probe in the
        thermostat's sheath, the hot layer: the probe there reads the cold
        water entering, not the water leaving.
        """
        if not params.probe_at_top:
            if self._equivalent is None:
                return None
            return max(self._equivalent, self._hot or self._equivalent)
        if (
            self._probe is not None
            and self._last_probe_time is not None
            and now - self._last_probe_time <= METER_PROBE_MAX_AGE
        ):
            return self._probe
        if self._equivalent is None:
            return None
        return self._equivalent + self._stratification_offset

    def _track_meter(self, sample: TankSample, params: TankParams) -> None:
        """Count the litres leaving the tank and group them into draws.

        A draw starts with the first new litre and ends after
        ``METER_DRAW_END`` without another one (and without flow, if the
        flow rate is known). Its energy is ``litres · c · (T_out - T_mains)``
        with the tank temperature when it started: the water leaves hot
        nearly the whole draw in a stratified tank.
        """
        now = sample.timestamp
        heating = sample.power is not None and sample.power > params.power_threshold_w
        draw = self._meter_draw
        if heating:
            if draw is not None:
                draw.heating = True
            if self._strat_probe is not None:
                self._strat_probe.heating = True
        volume = sample.hot_water_volume
        if volume is None:
            return  # meter unavailable: its litres are counted when it returns
        previous, since = self._meter_volume, self._meter_time
        self._meter_volume, self._meter_time = volume, now
        if previous is None or since is None or now <= since:
            return
        delta = volume - previous
        elapsed = now - since
        limit = METER_MAX_FLOW_L_PER_MIN * elapsed.total_seconds() / 60 + 1
        if delta < 0 or delta > limit or elapsed > MAX_GAP_DRAW:
            return  # meter reset, glitch or too long a gap: count from here
        if delta > 0:
            outlet = self._outlet_temperature(now, params)
            if draw is None:
                if outlet is None:
                    return  # nothing known about the tank yet
                draw = self._open_meter_draw(since, now, outlet, heating)
            if elapsed > METER_DRAW_END:
                draw.estimated = True  # litres counted across a gap
            self._add_meter_litres(draw, delta, params)
            draw.last_flow = now
        if draw is None:
            return
        if sample.temperature is not None:
            if draw.min_probe is None or sample.temperature < draw.min_probe:
                draw.min_probe = sample.temperature
            if (
                draw.energy_at_comfort is None
                and sample.temperature < params.comfort_min_temperature
            ):
                draw.energy_at_comfort = draw.energy_kwh
        flowing = sample.hot_water_flow is not None and sample.hot_water_flow > 0
        if delta <= 0 and not flowing and now - draw.last_flow >= METER_DRAW_END:
            self._close_meter_draw(now, params)

    def _open_meter_draw(
        self, since: datetime, now: datetime, outlet: float, heating: bool
    ) -> _MeterDraw:
        draw = _MeterDraw(start=since, last_flow=now, outlet=outlet, heating=heating)
        self._meter_draw = draw
        if self._episode is not None:
            self._episode.draw_seen = True
        if (
            self._strat_probe is None
            and not heating
            and not self._in_heating_window(now)
            and self._probe is not None
            and self._last_probe_time is not None
            and now - self._last_probe_time <= METER_PROBE_MAX_AGE
        ):
            self._strat_probe = _StratificationProbe(
                start_probe=self._probe, start_time=self._last_probe_time
            )
        return draw

    def _add_meter_litres(
        self, draw: _MeterDraw, liters: float, params: TankParams
    ) -> None:
        """Take the energy of some litres out of the model."""
        energy = (
            liters
            * WATER_HEAT_CAPACITY_KWH_PER_L_K
            * max(0.0, draw.outlet - params.mains_temperature)
        )
        draw.liters += liters
        draw.energy_kwh += energy
        if self._equivalent is None:
            return
        drop = self.capacity_factor(params) * energy / params.heat_capacity
        self._equivalent = self._clamp_temperature(self._equivalent - drop, params)
        # The probe is expected to fall only part of it (stratification).
        ratio = self.stratification_ratio(draw.outlet)
        self._stratification_offset = max(
            0.0, self._stratification_offset + (1 - ratio) * drop
        )
        if self._strat_probe is not None:
            self._strat_probe.expected += drop

    def _close_meter_draw(self, now: datetime, params: TankParams) -> None:
        draw = self._meter_draw
        assert draw is not None
        self._meter_draw = None
        minimum = draw.min_probe
        if minimum is None:
            minimum = (
                self._equivalent + self._stratification_offset
                if self._equivalent is not None
                else draw.outlet
            )
        event = ConsumptionEvent(
            start=draw.start,
            end=draw.last_flow,
            energy_kwh=draw.energy_kwh,
            start_temperature=draw.outlet,
            min_temperature=minimum,
            while_heating=draw.heating,
            shortage=self._is_shortage(minimum, params),
            estimated=draw.estimated,
            liters=draw.liters,
        )
        self._consumptions.append(event)
        self._last_consumption_end = now
        if event.shortage:
            self._observe_shortage(
                event.start,
                draw.energy_at_comfort
                if draw.energy_at_comfort is not None
                else draw.energy_kwh,
                params,
            )

    def _learn_stratification(
        self, now: datetime, ambient: float, params: TankParams
    ) -> None:
        """Compare the probe drop with the metered draws, once settled.

        The ratio of the observed drop (losses removed) to the drop of a
        mixed tank says how much of the cold water reaches the probe. Only
        drops the probe can resolve are used (``MIN_STRATIFICATION_DROP``).
        """
        probe = self._strat_probe
        if probe is None or self._meter_draw is not None or self._probe is None:
            return
        end = self._last_consumption_end
        if end is None or now - end < STRATIFICATION_SETTLE:
            return
        self._strat_probe = None
        if probe.heating or probe.expected < MIN_STRATIFICATION_DROP:
            return
        hours = (now - probe.start_time).total_seconds() / 3600
        mean = (probe.start_probe + self._probe) / 2
        loss = self._ua * (mean - ambient) * hours / (params.heat_capacity * 1000)
        observed = probe.start_probe - self._probe - loss
        ratio = _clamp(
            observed / probe.expected,
            MIN_STRATIFICATION_RATIO,
            MAX_STRATIFICATION_RATIO,
        )
        self._stratification.append((probe.start_probe, ratio))
        del self._stratification[:-MAX_STRATIFICATION_SAMPLES]

    # ------------------------------------------------------------------
    # Energy reconciliation
    # ------------------------------------------------------------------

    def _track_anchor(
        self,
        sample: TankSample,
        heat: float,
        elapsed: timedelta,
        ambient: float,
        params: TankParams,
    ) -> None:
        """Balance the energy between settled states after heatings."""
        assert sample.power is not None
        assert self._probe is not None
        assert self._equivalent is not None
        now = sample.timestamp
        anchor = self._anchor
        if anchor is not None:
            anchor.heat_kwh += heat
            hours = elapsed.total_seconds() / 3600
            anchor.loss_kwh += self._ua * (self._equivalent - ambient) * hours / 1000

        tracker = self._heater
        settled = (
            tracker.state is HeaterState.IDLE
            and sample.power <= params.power_threshold_w
            and tracker.last_heating_end is not None
            and now - tracker.last_heating_end >= POST_HEATING
            and self._event is None
            and self._meter_draw is None
        )
        if not settled:
            return
        if anchor is None:
            self._anchor = _Anchor(now, self._probe)
            return
        if anchor.heat_kwh < MIN_RECONCILE_HEAT_KWH:
            return  # no real heating since the anchor: keep balancing from it
        self._reconcile(anchor, now, params)
        self._anchor = _Anchor(now, self._probe)

    def _reconcile(self, anchor: _Anchor, now: datetime, params: TankParams) -> None:
        """Scale the draws since the anchor to the energy really drawn.

        With the probe at the top, a draw is seen late and too small when
        the cold water stays at the bottom; the next heating mixes the tank
        and reveals the energy that is really missing.
        """
        assert self._probe is not None
        capacity = params.heat_capacity
        k_cap = self.capacity_factor(params)
        interval = [
            index
            for index, event in enumerate(self._consumptions)
            if anchor.time <= event.start < now
        ]
        # Metered draws are measured, not estimated: they are never scaled,
        # and the rest of the balance is what the estimated ones drew.
        metered = sum(
            self._consumptions[index].energy_kwh
            for index in interval
            if self._consumptions[index].metered
        )
        indexes = [index for index in interval if not self._consumptions[index].metered]
        measured = sum(self._consumptions[index].energy_kwh for index in indexes)
        drawn_at_probe = (
            k_cap * anchor.heat_kwh
            - anchor.loss_kwh
            - capacity * (self._probe - anchor.probe)
        )
        actual = max(0.0, drawn_at_probe / k_cap - metered)
        factor = None
        if measured >= MIN_RECONCILE_DRAWS_KWH:
            factor = _clamp(
                actual / measured, MIN_RECONCILE_FACTOR, MAX_RECONCILE_FACTOR
            )
            for index in indexes:
                event = self._consumptions[index]
                self._consumptions[index] = replace(
                    event, energy_kwh=event.energy_kwh * factor, reconciled=factor
                )
        self._learn_usable(anchor, factor if factor is not None else 1.0)
        self._reconciliations.append(
            Reconciliation(
                start=anchor.time,
                end=now,
                heat_kwh=anchor.heat_kwh,
                loss_kwh=anchor.loss_kwh,
                measured_kwh=measured,
                actual_kwh=actual,
                factor=factor,
            )
        )

    def _learn_usable(self, anchor: _Anchor, factor: float) -> None:
        """Learn k_use from the shortages of a reconciled interval."""
        pending, self._pending_usable = self._pending_usable, []
        for item in pending:
            if item.anchor_time != anchor.time:
                continue  # older interval (e.g. before a restart): dropped
            span = item.shower - item.mains
            if span <= 0:
                continue
            predicted = item.volume_l * (item.anchor_probe - item.comfort) / span
            if predicted < MIN_USABLE_PREDICTED_L:
                continue
            drawn_kwh = (
                item.energy_before_kwh + item.energy_until_comfort_kwh
            ) * factor
            drawn_l = drawn_kwh / (WATER_HEAT_CAPACITY_KWH_PER_L_K * span)
            self._usable_samples.append(
                _clamp(drawn_l / predicted, MIN_USABLE_FACTOR, MAX_USABLE_FACTOR)
            )
            del self._usable_samples[:-MAX_USABLE_SAMPLES]

    # ------------------------------------------------------------------
    # Effective capacity learning
    # ------------------------------------------------------------------

    def _track_capacity_episode(
        self,
        sample: TankSample,
        heat: float,
        elapsed: timedelta,
        ambient: float,
        params: TankParams,
    ) -> None:
        """Compare heating energy with the probe rise (episodes without draws)."""
        assert sample.power is not None
        assert self._probe is not None
        heating = sample.power > params.power_threshold_w
        episode = self._episode
        if episode is None:
            if heating and self._event is None and self._meter_draw is None:
                self._episode = _CapacityEpisode(start_probe=self._probe)
                self._episode.energy_kwh += heat
            return

        episode.energy_kwh += heat
        episode.probe_sum += self._probe
        episode.ambient_sum += ambient
        episode.samples += 1
        episode.hours += elapsed.total_seconds() / 3600
        if self._event is not None or self._meter_draw is not None:
            episode.draw_seen = True

        tracker = self._heater
        if heating or tracker.last_heating_end is None:
            return
        if sample.timestamp - tracker.last_heating_end < CAPACITY_SETTLE:
            return

        self._episode = None
        rise = self._probe - episode.start_probe
        if (
            episode.draw_seen
            or episode.energy_kwh < MIN_CAPACITY_EPISODE_KWH
            or rise < MIN_CAPACITY_EPISODE_RISE
            or episode.samples == 0
        ):
            return
        capacity = params.heat_capacity
        mean_excess = (episode.probe_sum - episode.ambient_sum) / episode.samples
        loss_kwh = self._ua * mean_excess * episode.hours / 1000
        observed = (capacity * rise + loss_kwh) / episode.energy_kwh
        self._capacity_samples.append(
            _clamp(observed, MIN_CAPACITY_FACTOR, MAX_CAPACITY_FACTOR)
        )
        del self._capacity_samples[:-MAX_CAPACITY_SAMPLES]

    # ------------------------------------------------------------------
    # Loss learning
    # ------------------------------------------------------------------

    def _learn_losses(
        self,
        sample: TankSample,
        now: datetime,
        heating_window: bool,
        ambient: float,
        params: TankParams,
    ) -> None:
        assert sample.temperature is not None
        window = self._loss
        settle_refs = [
            ref
            for ref in (
                self._quiet_since,
                self._heater.last_heating_end,
                self._last_consumption_end,
                self._last_meter_activity,
            )
            if ref is not None
        ]
        quiet = (
            not heating_window
            and self._event is None
            and self._meter_draw is None
            and all(now - ref >= LOSS_SETTLE for ref in settle_refs)
        )
        if quiet:
            window.points.append((now, sample.temperature))
            window.ambient_sum += ambient
            window.ambients.append(ambient)
            if now - window.points[0][0] < LOSS_MAX_WINDOW:
                return
        if not quiet and self._event is not None and window.points:
            # A draw ended the window. It was detected late (the slow draw
            # detector looks an hour back): its beginning is still in the
            # window and would look like losses. Drop that part.
            limit = self._event.start - SLOW_WINDOW
            kept = [
                index for index, point in enumerate(window.points) if point[0] < limit
            ]
            window.points = window.points[: len(kept)]
            window.ambients = window.ambients[: len(kept)]
            window.ambient_sum = sum(window.ambients)
        if window.points and window.points[-1][0] - window.points[0][0] >= (
            LOSS_MIN_WINDOW
        ):
            self._fit_loss_window(window, params)
        self._loss = _LossWindow()

    def _fit_loss_window(self, window: _LossWindow, params: TankParams) -> None:
        """Update UA by recursive least squares with one rest window."""
        points = window.points
        start = points[0][0]
        xy = [((t - start).total_seconds() / 3600, value) for t, value in points]
        slope, intercept = theil_sen(xy)
        mean_temperature = sum(value for _, value in xy) / len(xy)
        excess = mean_temperature - window.ambient_sum / len(points)
        scatter = median(abs(y - (intercept + slope * x)) for x, y in xy)
        if slope >= 0 or excess < MIN_LOSS_EXCESS or scatter > MAX_LOSS_SCATTER:
            return
        observed_w = -slope * params.heat_capacity * 1000  # W
        if self._loss_windows >= MIN_LOSS_WINDOWS:
            low, high = LOSS_PLAUSIBLE_RATIO
            if not low * self._ua <= observed_w / excess <= high * self._ua:
                self._loss_rejected += 1
                return
        covariance = self._ua_covariance
        gain = covariance * excess / (RLS_FORGETTING + excess * covariance * excess)
        self._ua = _clamp(
            self._ua + gain * (observed_w - excess * self._ua), MIN_UA, MAX_UA
        )
        self._ua_covariance = min(
            (1 - gain * excess) * covariance / RLS_FORGETTING,
            RLS_INITIAL_COVARIANCE,
        )
        self._loss_windows += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_runtime(self, now: datetime) -> None:
        self._heater = _HeaterTracker()
        self._history.clear()
        self._residuals.clear()
        self._event = None
        self._episode = None
        self._anchor = None
        self._pending_usable = []
        self._filter.tau_s = FILTER_TAU_S
        self._loss = _LossWindow()
        self._quiet_since = now

    def _state(
        self, now: datetime, params: TankParams, *, available: bool
    ) -> TankState:
        cutoff = self.cutoff_temperature(params)
        equivalent = self._equivalent if available else None
        stored = useful = charge = liters = theoretical = outlet = None
        stratified = self.stratified
        floor = self.useful_floor_temperature(params)
        if equivalent is not None:
            capacity = params.heat_capacity
            mains = params.mains_temperature
            stored = max(0.0, capacity * (equivalent - mains))
            # Stratified: k_use times the energy above the mains water.
            share = self.usable_factor_for(params) if stratified else 1.0
            useful = max(0.0, share * capacity * (equivalent - floor))
            full_useful = share * capacity * (cutoff - floor)
            if params.probe_at_top:
                # The probe sees the hot layer; the model is the mean.
                outlet = self._clamp_temperature(
                    equivalent + self._stratification_offset, params
                )
            else:
                outlet = max(equivalent, self._hot or equivalent)
            charge = (
                _clamp(100 * useful / full_useful, 0.0, 100.0)
                if full_useful > 0
                else 0.0
            )
            span = params.shower_temperature - mains
            if span > 0:
                theoretical = max(0.0, params.volume_l * (equivalent - mains) / span)
                liters = max(
                    0.0,
                    self.usable_factor_for(params)
                    * params.volume_l
                    * (equivalent - floor)
                    / span,
                )
            else:
                theoretical = liters = 0.0
        return TankState(
            timestamp=now,
            available=available and equivalent is not None,
            status=self.status(),
            heater_state=self._heater.state,
            drawing=self._event is not None or self._meter_draw is not None,
            probe_temperature=self._probe if available else None,
            equivalent_temperature=equivalent,
            mains_temperature=params.mains_temperature,
            stored_energy_kwh=stored,
            useful_energy_kwh=useful,
            charge_percent=charge,
            liters_available=liters,
            liters_theoretical=theoretical,
            cutoff_temperature=cutoff,
            cutoff_samples=tuple(self._cutoff_samples),
            ua_w_per_k=self._ua,
            loss_windows=self._loss_windows,
            capacity_factor=self.capacity_factor(params),
            capacity_samples=len(self._capacity_samples),
            usable_factor=self.usable_factor_for(params),
            usable_samples=len(self._usable_samples),
            heater_power_w=self.heater_power(params),
            calibration_count=self._calibration_count,
            last_calibration=self._last_calibration,
            last_consumption=self._consumptions[-1] if self._consumptions else None,
            status_reason=self.status_reason(),
            uncertain_since=self._uncertain_since if self._uncertain else None,
            heater_power_samples=self._heater_power_samples,
            stratification_ratio=self.stratification_ratio(
                self._probe if available else None
            ),
            stratification_samples=len(self._stratification),
            stratified=stratified,
            useful_floor_temperature=floor,
            outlet_temperature=outlet,
        )


def _same_cutoff(
    first: tuple[datetime | None, float], second: tuple[datetime | None, float]
) -> bool:
    """Whether two cut-off samples are the same one.

    By time and value when both have a time; by value alone (within
    CUTOFF_SAME_MARGIN) when one was saved without its time.
    """
    if abs(first[1] - second[1]) > CUTOFF_SAME_MARGIN:
        return False
    if first[0] is None or second[0] is None:
        return True
    return abs((first[0] - second[0]).total_seconds()) <= CUTOFF_SAME_SECONDS


def _union_cutoffs(
    first: list[tuple[datetime | None, float]],
    second: list[tuple[datetime | None, float]],
) -> list[tuple[datetime | None, float]]:
    """Both lists of cut-off samples without duplicates, oldest first.

    Samples without a time are older than any with one (saved before 0.12.3).
    """
    result = list(first)
    for sample in second:
        if not any(_same_cutoff(sample, kept) for kept in result):
            result.append(sample)
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(
        result, key=lambda sample: (sample[0] is not None, sample[0] or oldest)
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
