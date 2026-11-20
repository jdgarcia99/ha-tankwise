"""Synthetic water heater used to generate test data for the tank model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import math
import random

from tankwise_model.tank import (
    DEFAULT_AMBIENT_TEMPERATURE as AMBIENT_TEMPERATURE,
    DEFAULT_UA,
    WATER_HEAT_CAPACITY_KWH_PER_L_K,
    TankModel,
    TankParams,
    TankSample,
    TankState,
)

START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
PARAMS = TankParams(
    mains_temperature=15.0,
    rated_power_w=1450.0,
    cutoff_temperature=47.0,
    comfort_min_temperature=32.0,
    capacity_factor=1.0,
)


@dataclass
class TankSimulator:
    """Fully mixed tank with a thermostat, a PWM regulator and a noisy probe.

    ``heating_efficiency`` is the fraction of the electric energy that shows
    up at the probe (the apparent efficiency the model learns as k_cap).
    ``report_manual`` reports the regulator as a manual mode switch (on at
    100 %) instead of a PWM output.
    """

    temperature: float = 46.0
    volume_l: float = 100.0
    rated_power_w: float = 1450.0
    cutoff: float = 47.0
    hysteresis: float = 5.0
    ua: float = DEFAULT_UA
    heating_efficiency: float = 1.0
    noise: float = 1.0
    seed: int = 1
    report_regulator: bool = True
    report_manual: bool = False
    report_energy: bool = True
    time: datetime = START
    energy_kwh: float = 250.0
    thermostat_closed: bool = field(init=False)
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self.thermostat_closed = self.temperature < self.cutoff

    @property
    def capacity(self) -> float:
        return self.volume_l * WATER_HEAT_CAPACITY_KWH_PER_L_K

    def step(self, *, regulator: float = 0.0, draw_kw: float = 0.0) -> TankSample:
        """Advance one minute and return the sensor readings."""
        hours = 1 / 60
        if self.temperature >= self.cutoff:
            self.thermostat_closed = False
        elif self.temperature <= self.cutoff - self.hysteresis:
            self.thermostat_closed = True
        power = self.rated_power_w * regulator / 100 if self.thermostat_closed else 0.0
        heat = power / 1000 * hours
        self.energy_kwh += heat
        self.temperature += (
            heat * self.heating_efficiency - draw_kw * hours
        ) / self.capacity
        rate = self.ua / (self.capacity * 1000)
        self.temperature = AMBIENT_TEMPERATURE + (
            self.temperature - AMBIENT_TEMPERATURE
        ) * math.exp(-rate * hours)
        self.time += timedelta(minutes=1)
        probe = self.temperature + self._rng.uniform(-self.noise, self.noise)
        return TankSample(
            timestamp=self.time,
            temperature=round(probe, 1),
            power=round(power, 1),
            energy=round(self.energy_kwh, 4) if self.report_energy else None,
            regulator=(
                regulator if self.report_regulator and not self.report_manual else None
            ),
            manual_mode=regulator >= 100 if self.report_manual else None,
        )

    def run(
        self, minutes: int, *, regulator: float = 0.0, draw_kw: float = 0.0
    ) -> list[TankSample]:
        """Advance several minutes."""
        return [self.step(regulator=regulator, draw_kw=draw_kw) for _ in range(minutes)]

    def skip(self, minutes: int, *, regulator: float = 0.0) -> None:
        """Advance without producing readings (e.g. Home Assistant is down)."""
        self.run(minutes, regulator=regulator)


def feed(
    model: TankModel, samples: list[TankSample], params: TankParams = PARAMS
) -> TankState:
    """Feed samples to the model and return the last state."""
    state = None
    for sample in samples:
        state = model.update(sample, params)
    assert state is not None
    return state


def drive(
    model: TankModel,
    sim: TankSimulator,
    minutes: int,
    *,
    regulator: float = 0.0,
    draw_kw: float = 0.0,
    params: TankParams = PARAMS,
) -> TankState:
    """Advance the simulator and the model together, minute by minute."""
    return feed(model, sim.run(minutes, regulator=regulator, draw_kw=draw_kw), params)


def error_kwh(state: TankState, sim: TankSimulator) -> float:
    """Absolute error of the model's stored energy versus the simulator."""
    assert state.equivalent_temperature is not None
    return abs(state.equivalent_temperature - sim.temperature) * sim.capacity
