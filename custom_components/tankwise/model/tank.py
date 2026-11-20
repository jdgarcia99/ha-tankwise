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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
# A regulator decrease (or the manual mode switched off) this shortly before
# the power fell means the regulator stopped the heating, not the thermostat.
REGULATOR_FALLING_WINDOW = timedelta(seconds=30)
# The manual mode reported off (or the PWM output at rest) this shortly
# after the power fell also means an external order stopped the heating: the
# regulator state often reaches Home Assistant a few seconds after the power.
REGULATOR_STOP_AFTER = timedelta(seconds=60)
# A cut-off this far below the configured cut-off temperature is not a
# thermostat cut-off (e.g. an automation stopping the heating at 45 °C).
CUTOFF_PLAUSIBLE_BELOW = 12.0
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
HISTORY_SPAN = timedelta(minutes=10)
MAX_CONSUMPTIONS = 200

# Probe filter time constants: smooth at rest, fast while following a draw
FILTER_TAU_S = 240.0
FILTER_FAST_TAU_S = 60.0

# Slow correction towards the probe at rest
SLOW_CORRECTION_TAU = timedelta(minutes=50)

# Gaps (restarts, unavailable sensors)
FILTER_RESET_GAP = timedelta(minutes=5)
LONG_GAP = timedelta(minutes=30)

# Loss learning (rest windows)
LOSS_SETTLE = timedelta(minutes=30)  # after heating, draws or meter activity
LOSS_MIN_WINDOW = timedelta(hours=2)
LOSS_MAX_WINDOW = timedelta(hours=4)
MIN_LOSS_EXCESS = 10.0  # °C above ambient
MAX_LOSS_SCATTER = 1.0  # °C, median absolute residual
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


class PowerDropDetector:
    """Capture the regulator state at the instant the power falls.

    Fed with every state change of the power, regulator output and manual
    mode, in the order they happen. The last drop is kept until the power
    rises again.
    """

    def __init__(self, threshold_w: float) -> None:
        """Initialise the detector."""
        self._threshold_w = threshold_w
        self._regulator: float | None = None
        self._manual: bool | None = None
        self._falls: list[datetime] = []
        self._heating: bool | None = None
        self.drop: PowerDrop | None = None

    def _record_fall(self, timestamp: datetime) -> None:
        self._falls.append(timestamp)
        self._falls = [
            moment
            for moment in self._falls
            if timestamp - moment <= REGULATOR_FALLING_WINDOW
        ]

    def _withdraw(self, timestamp: datetime) -> None:
        """The regulator stopped right after the power fell: not a cut-off."""
        drop = self.drop
        if (
            drop is not None
            and drop.demand
            and timedelta(0) <= timestamp - drop.timestamp <= REGULATOR_STOP_AFTER
        ):
            self.drop = PowerDrop(timestamp=drop.timestamp, demand=False)

    def update_regulator(self, timestamp: datetime, value: float | None) -> None:
        """Record a regulator output change (%)."""
        if (
            value is not None
            and self._regulator is not None
            and value < self._regulator
        ):
            self._record_fall(timestamp)
            if value <= REGULATOR_DEMAND_PERCENT:
                self._withdraw(timestamp)
        self._regulator = value

    def update_manual(self, timestamp: datetime, value: bool | None) -> None:
        """Record a manual mode change (full power requested when on)."""
        if value is False and self._manual:
            self._record_fall(timestamp)
            self._withdraw(timestamp)
        self._manual = value

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
                timestamp=timestamp, demand=self.demand and not falling
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
        }

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
    liters_available: float | None
    cutoff_temperature: float
    cutoff_samples: tuple[float, ...]
    ua_w_per_k: float
    loss_windows: int
    capacity_factor: float
    capacity_samples: int
    heater_power_w: float
    calibration_count: int
    last_calibration: datetime | None
    last_consumption: ConsumptionEvent | None

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
class _LossWindow:
    points: list[tuple[datetime, float]] = field(default_factory=list)
    ambient_sum: float = 0.0


class TankModel:
    """Energy-balance model of the tank, updated once per sample."""

    def __init__(self) -> None:
        """Create an uninitialised model (state taken from the first sample)."""
        self._equivalent: float | None = None
        self._ua = DEFAULT_UA
        self._ua_covariance = RLS_INITIAL_COVARIANCE
        self._loss_windows = 0
        self._capacity_samples: list[float] = []
        self._heater_power_w: float | None = None
        self._heater_power_samples = 0
        self._cutoff_samples: list[float] = []
        self._uncertain = True
        self._calibration_count = 0
        self._last_calibration: datetime | None = None
        self._last_valid_time: datetime | None = None
        self._last_energy: float | None = None
        self._last_power: float | None = None
        self._consumptions: deque[ConsumptionEvent] = deque(maxlen=MAX_CONSUMPTIONS)

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def consumptions(self) -> list[ConsumptionEvent]:
        """Return the most recent detected draws, oldest first."""
        return list(self._consumptions)

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

    def update(self, sample: TankSample, params: TankParams) -> TankState:
        """Process one sample and return the new state."""
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
            return self._state(now, params, available=True)

        self._step(sample, params, elapsed)
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
        self._heater_power_w = None
        self._heater_power_samples = 0
        self._cutoff_samples.clear()
        self._consumptions.clear()
        self._episode = None

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent part of the model."""
        return {
            "format": STORAGE_FORMAT,
            "equivalent_temperature": self._equivalent,
            "ua": self._ua,
            "ua_covariance": self._ua_covariance,
            "loss_windows": self._loss_windows,
            "capacity_samples": list(self._capacity_samples),
            "heater_power_w": self._heater_power_w,
            "heater_power_samples": self._heater_power_samples,
            "cutoff_samples": list(self._cutoff_samples),
            "uncertain": self._uncertain,
            "calibration_count": self._calibration_count,
            "last_calibration": _iso(self._last_calibration),
            "last_valid_time": _iso(self._last_valid_time),
            "last_energy": self._last_energy,
            "consumptions": [event.as_dict() for event in self._consumptions],
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
                power = data["heater_power_w"]
                model._heater_power_w = None if power is None else float(power)
                model._heater_power_samples = int(data["heater_power_samples"])
            model._ua = _clamp(ua, MIN_UA, MAX_UA)
            model._loss_windows = int(data["loss_windows"])
            model._cutoff_samples = [float(value) for value in data["cutoff_samples"]][
                -MAX_CUTOFF_SAMPLES:
            ]
            model._uncertain = bool(data["uncertain"])
            model._calibration_count = int(data["calibration_count"])
            model._last_calibration = _from_iso(data["last_calibration"])
            model._last_valid_time = _from_iso(data["last_valid_time"])
            last_energy = data["last_energy"]
            model._last_energy = None if last_energy is None else float(last_energy)
            for event in data["consumptions"]:
                model._consumptions.append(ConsumptionEvent.from_dict(event))
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
        self._uncertain = True
        self._last_valid_time = sample.timestamp
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

        self._filter.reset()
        self._probe = self._filter.update(now, sample.temperature)
        if abs(self._probe - predicted) > -IDLE_DRAW_THRESHOLD:
            # Water was drawn (or heat added without a meter) during the gap:
            # follow the probe, but do not record a draw we did not observe.
            predicted = self._probe
        self._equivalent = self._clamp_temperature(predicted, params)

        if elapsed > LONG_GAP:
            self._uncertain = True
        self._reset_runtime(now)
        self._last_valid_time = now

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
        while self._residuals and now - self._residuals[0][0] > RESIDUAL_WINDOW:
            self._residuals.popleft()

        equivalent = self._track_draws(
            now, predicted, full_residual, heating_window, params
        )
        if self._event is None and not heating_window:
            alpha = 1.0 - math.exp(-elapsed / SLOW_CORRECTION_TAU)
            equivalent += alpha * (self._probe - equivalent)
        self._equivalent = self._clamp_temperature(equivalent, params)

        self._track_capacity_episode(sample, heat, elapsed, ambient, params)

        if full is not None and self._plausible_cutoff(params):
            self._calibrate(now, regulator_confirmed=full, params=params)

        self._history.append(_HistoryPoint(now, self._probe))
        while self._history and now - self._history[0].timestamp > HISTORY_SPAN:
            self._history.popleft()

        self._learn_losses(sample, now, heating_window, ambient, params)
        self._last_valid_time = now

    # ------------------------------------------------------------------
    # Energy balance
    # ------------------------------------------------------------------

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
        """Return True if the probe is high enough for a thermostat cut-off."""
        assert self._probe is not None
        return self._probe >= params.cutoff_temperature - CUTOFF_PLAUSIBLE_BELOW

    def purge_cutoffs(self, params: TankParams) -> int:
        """Forget learned cut-offs that cannot be thermostat cut-offs.

        Returns how many were removed.
        """
        minimum = params.cutoff_temperature - CUTOFF_PLAUSIBLE_BELOW
        kept = [value for value in self._cutoff_samples if value >= minimum]
        removed = len(self._cutoff_samples) - len(kept)
        self._cutoff_samples = kept
        return removed

    def _calibrate(
        self, now: datetime, *, regulator_confirmed: bool, params: TankParams
    ) -> None:
        assert self._probe is not None
        if (
            regulator_confirmed
            or self._probe >= self.cutoff_temperature(params) - CUTOFF_LEARN_MARGIN
        ):
            self._cutoff_samples.append(self._probe)
            del self._cutoff_samples[:-MAX_CUTOFF_SAMPLES]
        self._equivalent = (
            self._probe if regulator_confirmed else self.cutoff_temperature(params)
        )
        self._uncertain = False
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
            if self._probe < event.min_probe - MIN_TRACK_DROP:
                event.min_probe = self._probe
                event.min_time = now
            if (
                now - event.detected >= END_WINDOW
                and self._residual_sum(now, END_WINDOW) > -END_TOLERANCE
            ):
                self._close_event(now, params)
            return corrected

        threshold = HEATING_DRAW_THRESHOLD if heating_window else IDLE_DRAW_THRESHOLD
        if self._residual_sum(now, RESIDUAL_WINDOW) >= threshold:
            return predicted

        reference = max(self._history, key=lambda point: point.probe, default=None)
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
            energy_kwh=-self._residual_sum(now, RESIDUAL_WINDOW) * capacity / k_cap,
            min_probe=self._probe,
            min_time=now,
            detected=now,
            while_heating=heating_window,
        )
        if self._episode is not None:
            self._episode.draw_seen = True
        return corrected

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
                shortage=event.min_probe < params.comfort_min_temperature,
            )
        )
        self._event = None
        self._filter.tau_s = FILTER_TAU_S
        self._last_consumption_end = now
        self._history.clear()
        self._residuals.clear()

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
            if heating and self._event is None:
                self._episode = _CapacityEpisode(start_probe=self._probe)
                self._episode.energy_kwh += heat
            return

        episode.energy_kwh += heat
        episode.probe_sum += self._probe
        episode.ambient_sum += ambient
        episode.samples += 1
        episode.hours += elapsed.total_seconds() / 3600
        if self._event is not None:
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
            and all(now - ref >= LOSS_SETTLE for ref in settle_refs)
        )
        if quiet:
            window.points.append((now, sample.temperature))
            window.ambient_sum += ambient
            if now - window.points[0][0] < LOSS_MAX_WINDOW:
                return
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
        self._filter.tau_s = FILTER_TAU_S
        self._loss = _LossWindow()
        self._quiet_since = now

    def _state(
        self, now: datetime, params: TankParams, *, available: bool
    ) -> TankState:
        cutoff = self.cutoff_temperature(params)
        equivalent = self._equivalent if available else None
        stored = useful = charge = liters = None
        if equivalent is not None:
            capacity = params.heat_capacity
            mains = params.mains_temperature
            stored = max(0.0, capacity * (equivalent - mains))
            comfort = params.comfort_min_temperature
            useful = max(0.0, capacity * (equivalent - comfort))
            full_useful = capacity * (cutoff - comfort)
            charge = (
                _clamp(100 * useful / full_useful, 0.0, 100.0)
                if full_useful > 0
                else 0.0
            )
            span = params.shower_temperature - mains
            liters = (
                max(0.0, params.volume_l * (equivalent - mains) / span)
                if span > 0
                else 0.0
            )
        return TankState(
            timestamp=now,
            available=available and equivalent is not None,
            status=self.status(),
            heater_state=self._heater.state,
            drawing=self._event is not None,
            probe_temperature=self._probe if available else None,
            equivalent_temperature=equivalent,
            mains_temperature=params.mains_temperature,
            stored_energy_kwh=stored,
            useful_energy_kwh=useful,
            charge_percent=charge,
            liters_available=liters,
            cutoff_temperature=cutoff,
            cutoff_samples=tuple(self._cutoff_samples),
            ua_w_per_k=self._ua,
            loss_windows=self._loss_windows,
            capacity_factor=self.capacity_factor(params),
            capacity_samples=len(self._capacity_samples),
            heater_power_w=self.heater_power(params),
            calibration_count=self._calibration_count,
            last_calibration=self._last_calibration,
            last_consumption=self._consumptions[-1] if self._consumptions else None,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
