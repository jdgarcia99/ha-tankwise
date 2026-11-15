"""Thermal model of an electric storage water heater.

The tank is modelled as an equivalent fully mixed volume whose temperature
(``equivalent_temperature``) is tracked by an energy balance:

* heat in: increments of the heating element energy meter;
* losses: Newton cooling towards a fixed ambient temperature, with a
  coefficient learned slowly from quiet periods;
* draws: detected from sustained drops of the filtered probe temperature.

The probe is only trusted for slow corrections while the tank is at rest.
Heating is never inferred from the probe, so probe noise can neither look
like heating nor (thanks to the filter, a drop threshold and a confirmation
time) like a draw.

The state is recalibrated to "full" when the thermostat cuts off: either the
regulator (e.g. a PWM diverter) asks for power and the element draws none, or,
without a regulator sensor, heating stops close to the cut-off temperature.
Because the regulator may be switched off a few seconds after the cut-off
(e.g. by an automation), the regulator value is captured at the very instant
the power falls (:class:`PowerDropDetector`, fed with state change events),
not only at the 60-second samples.

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

STORAGE_FORMAT = 1

# Physics
WATER_HEAT_CAPACITY_KWH_PER_L_K = 4.186 / 3600  # kWh per litre per kelvin
AMBIENT_TEMPERATURE = 20.0  # °C, fixed until an ambient sensor is supported
LOSS_REFERENCE_TEMPERATURE = 46.0  # °C, reference for the °C/h loss figure
DEFAULT_LOSS_RATE = 0.25  # °C/h at the reference temperature
DEFAULT_LOSS_COEFFICIENT = DEFAULT_LOSS_RATE / (
    LOSS_REFERENCE_TEMPERATURE - AMBIENT_TEMPERATURE
)  # 1/h
MIN_LOSS_COEFFICIENT = DEFAULT_LOSS_COEFFICIENT / 4
MAX_LOSS_COEFFICIENT = DEFAULT_LOSS_COEFFICIENT * 4

# Heater energy sanity margin over the rated power
HEATER_ENERGY_MARGIN = 1.2

# Thermostat cut-off ("full") detection
FULL_CONFIRM = timedelta(minutes=3)
FULL_CHECK_WINDOW = timedelta(minutes=30)
REGULATOR_DEMAND_PERCENT = 10.0
# A regulator decrease this shortly before the power fell means the regulator
# stopped the heating, not the thermostat.
REGULATOR_FALLING_WINDOW = timedelta(seconds=10)
MIN_EPISODE_ENERGY_KWH = 0.02
BACKUP_FULL_MARGIN = 2.5  # °C below the cut-off temperature
CUTOFF_LEARN_MARGIN = 1.0  # °C, backup cut-offs closer than this are learned
MAX_CUTOFF_SAMPLES = 15
MIN_CUTOFF_SAMPLES = 3

# Draw detection
POST_HEATING = timedelta(minutes=15)
IDLE_DROP = 1.5  # °C below the recent maximum ...
IDLE_WINDOW = timedelta(minutes=10)  # ... within this window ...
IDLE_CONFIRM_SAMPLES = 3  # ... for this many consecutive samples
HEATING_DROP = 2.0
HEATING_WINDOW = timedelta(minutes=5)
HEATING_CONFIRM_SAMPLES = 2
EVENT_END = timedelta(minutes=3)  # no new minimum for this long ends a draw
EVENT_MIN_TOLERANCE = 0.3  # °C, above the residual noise of the filtered probe
HISTORY_SPAN = timedelta(minutes=10)

# Probe filter time constants: smooth at rest, fast while following a draw
FILTER_TAU_S = 240.0
FILTER_FAST_TAU_S = 60.0
MAX_CONSUMPTIONS = 50

# Slow correction towards the probe at rest
SLOW_CORRECTION_TAU = timedelta(minutes=50)

# Gaps (restarts, unavailable sensors)
FILTER_RESET_GAP = timedelta(minutes=5)
LONG_GAP = timedelta(minutes=30)

# Loss learning
LOSS_QUIET = timedelta(hours=2)
LOSS_WINDOW = timedelta(hours=3)
LOSS_LEARNING_RATE = 0.05
MIN_LOSS_WINDOWS = 5
MIN_LOSS_EXCESS = 10.0  # °C above ambient
MAX_LOSS_SCATTER = 1.5  # °C, median absolute residual
METER_IDLE_KWH = 0.001  # per sample


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
    """Physical parameters of the tank."""

    volume_l: float = 100.0
    rated_power_w: float = 1450.0
    mains_temperature: float = 15.0
    min_useful_temperature: float = 32.0
    cutoff_temperature: float = 47.0
    power_threshold_w: float = 50.0
    shower_energy_kwh: float = 1.8

    @property
    def heat_capacity(self) -> float:
        """Return the heat capacity of the tank in kWh/K."""
        return self.volume_l * WATER_HEAT_CAPACITY_KWH_PER_L_K


@dataclass(frozen=True, slots=True)
class PowerDrop:
    """The heating element power fell below the threshold (event time)."""

    timestamp: datetime
    # The regulator asked for power (> 10 %) at that instant and had not just
    # been lowered: the thermostat, not the regulator, stopped the heating.
    regulator_demand: bool


class PowerDropDetector:
    """Capture the regulator value at the instant the power falls.

    Fed with every state change of the power and regulator sensors, in the
    order they happen. The last drop is kept until the power rises again.
    """

    def __init__(self, threshold_w: float) -> None:
        """Initialise the detector."""
        self._threshold_w = threshold_w
        self._regulator: float | None = None
        self._regulator_falls: list[datetime] = []
        self._heating: bool | None = None
        self.drop: PowerDrop | None = None

    def update_regulator(self, timestamp: datetime, value: float | None) -> None:
        """Record a regulator output change (%)."""
        if (
            value is not None
            and self._regulator is not None
            and value < self._regulator
        ):
            self._regulator_falls.append(timestamp)
            self._regulator_falls = [
                moment
                for moment in self._regulator_falls
                if timestamp - moment <= REGULATOR_FALLING_WINDOW
            ]
        self._regulator = value

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
                for moment in self._regulator_falls
            )
            self.drop = PowerDrop(
                timestamp=timestamp,
                regulator_demand=(
                    self._regulator is not None
                    and self._regulator > REGULATOR_DEMAND_PERCENT
                    and not falling
                ),
            )
        self._heating = heating


@dataclass(frozen=True, slots=True)
class TankSample:
    """One reading of the inputs (°C, W, kWh, %)."""

    timestamp: datetime
    temperature: float | None
    power: float | None
    energy: float | None
    regulator: float | None = None
    # Last power drop seen by a PowerDropDetector, if any.
    power_drop: PowerDrop | None = None

    @property
    def valid(self) -> bool:
        """Return True when all required inputs are present."""
        return (
            self.temperature is not None
            and self.power is not None
            and self.energy is not None
        )


@dataclass(frozen=True, slots=True)
class ConsumptionEvent:
    """A detected hot water draw."""

    start: datetime
    end: datetime
    energy_kwh: float
    start_temperature: float
    end_temperature: float
    while_heating: bool

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "energy_kwh": round(self.energy_kwh, 4),
            "start_temperature": round(self.start_temperature, 2),
            "end_temperature": round(self.end_temperature, 2),
            "while_heating": self.while_heating,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionEvent:
        """Rebuild an event from :meth:`as_dict` output."""
        return cls(
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            energy_kwh=float(data["energy_kwh"]),
            start_temperature=float(data["start_temperature"]),
            end_temperature=float(data["end_temperature"]),
            while_heating=bool(data["while_heating"]),
        )


@dataclass(frozen=True, slots=True)
class TankState:
    """Published state of the tank model."""

    timestamp: datetime
    available: bool
    status: ModelStatus
    heater_state: HeaterState
    probe_temperature: float | None
    equivalent_temperature: float | None
    stored_energy_kwh: float | None
    useful_energy_kwh: float | None
    charge_percent: float | None
    showers_available: float | None
    cutoff_temperature: float
    loss_coefficient: float
    loss_windows: int
    calibration_count: int
    last_calibration: datetime | None

    @property
    def loss_rate(self) -> float:
        """Return the loss rate in °C/h at the reference temperature."""
        return self.loss_coefficient * (
            LOSS_REFERENCE_TEMPERATURE - AMBIENT_TEMPERATURE
        )


@dataclass(slots=True)
class _HistoryPoint:
    timestamp: datetime
    probe: float
    equivalent: float


@dataclass(slots=True)
class _ActiveEvent:
    start: datetime
    start_temperature: float
    energy_kwh: float
    min_probe: float
    min_time: datetime
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
class _LossLearner:
    points: list[tuple[datetime, float]] = field(default_factory=list)


class TankModel:
    """Energy-balance model of the tank, updated once per sample."""

    def __init__(self) -> None:
        """Create an uninitialised model (state taken from the first sample)."""
        self._equivalent: float | None = None
        self._loss_coefficient = DEFAULT_LOSS_COEFFICIENT
        self._loss_windows = 0
        self._cutoff_samples: list[float] = []
        self._uncertain = True
        self._calibration_count = 0
        self._last_calibration: datetime | None = None
        self._last_valid_time: datetime | None = None
        self._last_energy: float | None = None
        self._consumptions: deque[ConsumptionEvent] = deque(maxlen=MAX_CONSUMPTIONS)

        # Runtime only (not persisted)
        self._filter = ProbeFilter(tau_s=FILTER_TAU_S)
        self._probe: float | None = None
        self._heater = _HeaterTracker()
        self._history: deque[_HistoryPoint] = deque()
        self._drop_count = 0
        self._event: _ActiveEvent | None = None
        self._last_consumption_end: datetime | None = None
        self._last_meter_activity: datetime | None = None
        self._quiet_since: datetime | None = None
        self._loss = _LossLearner()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def consumptions(self) -> list[ConsumptionEvent]:
        """Return the most recent detected draws, oldest first."""
        return list(self._consumptions)

    @property
    def last_consumption(self) -> ConsumptionEvent | None:
        """Return the most recent detected draw, if any."""
        return self._consumptions[-1] if self._consumptions else None

    @property
    def loss_coefficient(self) -> float:
        """Return the learned loss coefficient (1/h)."""
        return self._loss_coefficient

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
        assert sample.energy is not None

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

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent part of the model."""
        return {
            "format": STORAGE_FORMAT,
            "equivalent_temperature": self._equivalent,
            "loss_coefficient": self._loss_coefficient,
            "loss_windows": self._loss_windows,
            "cutoff_samples": list(self._cutoff_samples),
            "uncertain": self._uncertain,
            "calibration_count": self._calibration_count,
            "last_calibration": _iso(self._last_calibration),
            "last_valid_time": _iso(self._last_valid_time),
            "last_energy": self._last_energy,
            "consumptions": [event.as_dict() for event in self._consumptions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TankModel:
        """Restore a model saved with :meth:`as_dict`.

        Raises ValueError if the data is not usable.
        """
        try:
            if data.get("format") != STORAGE_FORMAT:
                raise ValueError(f"unsupported format {data.get('format')!r}")
            model = cls()
            equivalent = data["equivalent_temperature"]
            model._equivalent = None if equivalent is None else float(equivalent)
            model._loss_coefficient = _clamp(
                float(data["loss_coefficient"]),
                MIN_LOSS_COEFFICIENT,
                MAX_LOSS_COEFFICIENT,
            )
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

    def _recover_from_gap(
        self, sample: TankSample, params: TankParams, elapsed: timedelta
    ) -> None:
        """Resume after a restart or a period without valid readings."""
        assert sample.temperature is not None
        assert self._equivalent is not None
        now = sample.timestamp
        heat = self._heater_energy(sample.energy, params, elapsed)
        predicted = self._predict(self._equivalent, heat, elapsed, params)

        self._filter.reset()
        self._probe = self._filter.update(now, sample.temperature)
        if self._probe < predicted - IDLE_DROP:
            # Water was drawn during the gap: follow the probe, but do not
            # record a draw we did not observe.
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
        heat = self._heater_energy(sample.energy, params, elapsed)
        if heat > METER_IDLE_KWH:
            self._last_meter_activity = now

        self._probe = self._filter.update(now, sample.temperature)
        predicted = self._predict(self._equivalent, heat, elapsed, params)

        full = self._update_heater(sample, heat, params)
        heating_window = self._in_heating_window(sample, params, now)

        equivalent = self._track_draws(now, predicted, heating_window, params)
        if self._event is None and not heating_window:
            alpha = 1.0 - math.exp(-elapsed / SLOW_CORRECTION_TAU)
            equivalent += alpha * (self._probe - equivalent)
        self._equivalent = self._clamp_temperature(equivalent, params)

        if full is not None:
            self._calibrate(now, regulator_confirmed=full, params=params)

        self._history.append(_HistoryPoint(now, self._probe, self._equivalent))
        while self._history and now - self._history[0].timestamp > HISTORY_SPAN:
            self._history.popleft()

        self._learn_losses(sample, now, heating_window)
        self._last_valid_time = now

    # ------------------------------------------------------------------
    # Energy balance
    # ------------------------------------------------------------------

    def _heater_energy(
        self, energy: float | None, params: TankParams, elapsed: timedelta
    ) -> float:
        """Return the heater energy (kWh) since the previous valid sample."""
        assert energy is not None
        previous, self._last_energy = self._last_energy, energy
        if previous is None:
            return 0.0
        delta = energy - previous
        if delta <= 0:
            return 0.0  # meter reset or no heating
        limit = (
            params.rated_power_w
            / 1000
            * elapsed.total_seconds()
            / 3600
            * HEATER_ENERGY_MARGIN
        )
        return min(delta, limit)

    def _predict(
        self,
        equivalent: float,
        heat_kwh: float,
        elapsed: timedelta,
        params: TankParams,
    ) -> float:
        hours = elapsed.total_seconds() / 3600
        cooled = AMBIENT_TEMPERATURE + (equivalent - AMBIENT_TEMPERATURE) * math.exp(
            -self._loss_coefficient * hours
        )
        return self._clamp_temperature(cooled + heat_kwh / params.heat_capacity, params)

    def _clamp_temperature(self, value: float, params: TankParams) -> float:
        return _clamp(
            value,
            min(params.mains_temperature, AMBIENT_TEMPERATURE),
            self.cutoff_temperature(params),
        )

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
            and (tracker.drop_demand is None or drop.timestamp > tracker.below_since)
        ):
            # Exact instant of the cut-off and the regulator value then.
            tracker.below_since = drop.timestamp
            tracker.last_heating_end = drop.timestamp
            tracker.drop_demand = drop.regulator_demand

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

        if sample.regulator is not None:
            if sample.regulator > REGULATOR_DEMAND_PERCENT:
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

        # Backup criterion: no regulator sensor (or it is unavailable).
        if now - tracker.below_since >= FULL_CONFIRM:
            tracker.state = HeaterState.IDLE
            assert self._probe is not None
            if self._probe >= self.cutoff_temperature(params) - BACKUP_FULL_MARGIN:
                return False
        return None

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
        self._equivalent = self.cutoff_temperature(params)
        self._uncertain = False
        self._calibration_count += 1
        self._last_calibration = now
        self._heater.episode_energy = 0.0
        self._history.clear()
        self._drop_count = 0

    def _in_heating_window(
        self, sample: TankSample, params: TankParams, now: datetime
    ) -> bool:
        assert sample.power is not None
        tracker = self._heater
        return (
            sample.power > params.power_threshold_w
            or tracker.state is not HeaterState.IDLE
            or (
                tracker.last_heating_end is not None
                and now - tracker.last_heating_end <= POST_HEATING
            )
        )

    # ------------------------------------------------------------------
    # Draw detection
    # ------------------------------------------------------------------

    def _track_draws(
        self,
        now: datetime,
        predicted: float,
        heating_window: bool,
        params: TankParams,
    ) -> float:
        """Detect and follow draws; return the corrected equivalent temperature."""
        assert self._probe is not None
        assert self._equivalent is not None
        capacity = params.heat_capacity
        event = self._event

        if event is not None:
            # While water is drawn the probe is the best estimate of the
            # mixed temperature; the draw energy is what the balance cannot
            # explain. The model never rises faster than the heat input.
            corrected = min(predicted, self._probe)
            event.energy_kwh += (predicted - corrected) * capacity
            if self._probe < event.min_probe - EVENT_MIN_TOLERANCE:
                event.min_probe = self._probe
                event.min_time = now
            elif now - event.min_time >= EVENT_END:
                self._close_event(now)
            return corrected

        drop, window, confirm = (
            (HEATING_DROP, HEATING_WINDOW, HEATING_CONFIRM_SAMPLES)
            if heating_window
            else (IDLE_DROP, IDLE_WINDOW, IDLE_CONFIRM_SAMPLES)
        )
        recent = [point for point in self._history if now - point.timestamp <= window]
        if not recent:
            self._drop_count = 0
            return predicted
        reference = max(recent, key=lambda point: point.probe)
        if reference.probe - self._probe > drop:
            self._drop_count += 1
        else:
            self._drop_count = 0
        if self._drop_count < confirm:
            return predicted

        self._drop_count = 0
        corrected = min(predicted, self._probe)
        self._filter.tau_s = FILTER_FAST_TAU_S
        self._event = _ActiveEvent(
            start=reference.timestamp,
            start_temperature=reference.probe,
            energy_kwh=(predicted - corrected) * capacity,
            min_probe=self._probe,
            min_time=now,
            while_heating=heating_window,
        )
        return corrected

    def _close_event(self, now: datetime) -> None:
        event = self._event
        assert event is not None
        self._consumptions.append(
            ConsumptionEvent(
                start=event.start,
                end=event.min_time,
                energy_kwh=event.energy_kwh,
                start_temperature=event.start_temperature,
                end_temperature=event.min_probe,
                while_heating=event.while_heating,
            )
        )
        self._event = None
        self._filter.tau_s = FILTER_TAU_S
        self._last_consumption_end = now
        self._history.clear()
        self._drop_count = 0

    # ------------------------------------------------------------------
    # Loss learning
    # ------------------------------------------------------------------

    def _learn_losses(
        self, sample: TankSample, now: datetime, heating_window: bool
    ) -> None:
        assert sample.temperature is not None
        points = self._loss.points
        quiet_refs = [
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
            and self._drop_count == 0
            and all(now - ref >= LOSS_QUIET for ref in quiet_refs)
        )
        if not quiet:
            points.clear()
            return

        points.append((now, sample.temperature))
        if now - points[0][0] < LOSS_WINDOW:
            return

        start = points[0][0]
        xy = [((t - start).total_seconds() / 3600, value) for t, value in points]
        points.clear()
        slope, intercept = theil_sen(xy)
        mean_temperature = sum(value for _, value in xy) / len(xy)
        excess = mean_temperature - AMBIENT_TEMPERATURE
        scatter = median(abs(y - (intercept + slope * x)) for x, y in xy)
        if slope >= 0 or excess < MIN_LOSS_EXCESS or scatter > MAX_LOSS_SCATTER:
            return
        observed = -slope / excess
        self._loss_coefficient = _clamp(
            self._loss_coefficient
            + LOSS_LEARNING_RATE * (observed - self._loss_coefficient),
            MIN_LOSS_COEFFICIENT,
            MAX_LOSS_COEFFICIENT,
        )
        self._loss_windows += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_runtime(self, now: datetime) -> None:
        self._heater = _HeaterTracker()
        self._history.clear()
        self._drop_count = 0
        self._event = None
        self._filter.tau_s = FILTER_TAU_S
        self._loss = _LossLearner()
        self._quiet_since = now

    def _state(
        self, now: datetime, params: TankParams, *, available: bool
    ) -> TankState:
        cutoff = self.cutoff_temperature(params)
        equivalent = self._equivalent if available else None
        stored = useful = charge = showers = None
        if equivalent is not None:
            capacity = params.heat_capacity
            stored = max(0.0, capacity * (equivalent - params.mains_temperature))
            useful = max(0.0, capacity * (equivalent - params.min_useful_temperature))
            full_useful = capacity * (cutoff - params.min_useful_temperature)
            charge = (
                _clamp(100 * useful / full_useful, 0.0, 100.0)
                if full_useful > 0
                else 0.0
            )
            showers = (
                useful / params.shower_energy_kwh
                if params.shower_energy_kwh > 0
                else 0.0
            )
        return TankState(
            timestamp=now,
            available=available and equivalent is not None,
            status=self.status(),
            heater_state=self._heater.state,
            probe_temperature=self._probe if available else None,
            equivalent_temperature=equivalent,
            stored_energy_kwh=stored,
            useful_energy_kwh=useful,
            charge_percent=charge,
            showers_available=showers,
            cutoff_temperature=cutoff,
            loss_coefficient=self._loss_coefficient,
            loss_windows=self._loss_windows,
            calibration_count=self._calibration_count,
            last_calibration=self._last_calibration,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
