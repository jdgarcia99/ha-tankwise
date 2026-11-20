"""Stratified water heater for tests: the upper probe sees draws late.

The tank is a column of layers. A draw removes hot water from the top and
cold mains water enters at the bottom (plug flow), so the upper probe only
drops when the cold front reaches it. The heating element warms the bottom
layer and convection mixes any layer warmer than the one above it. This is
what makes a single upper probe underestimate morning showers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import math
import random

from tankwise_model.tank import WATER_HEAT_CAPACITY_KWH_PER_L_K, TankSample

from .simulator import AMBIENT_TEMPERATURE, START


@dataclass
class LayeredTank:
    """Column of ``layers`` layers; the probe reads the top one."""

    temperature: float = 46.0
    volume_l: float = 100.0
    layers: int = 10
    mains: float = 14.0
    rated_power_w: float = 1500.0
    cutoff: float = 68.0
    ua: float = 1.0
    # Share of the incoming cold water that mixes into the column (jet
    # mixing); the rest stays at the bottom. 1.0 makes a 30 L shower lower a
    # 46 °C probe by about 4 °C, as in the reference installation.
    mixing: float = 1.0
    noise: float = 0.25
    seed: int = 1
    time: object = START
    energy_kwh: float = 100.0
    drawn_kwh: float = 0.0  # energy drawn above mains (what the model should find)
    profile: list[float] = field(init=False)
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.profile = [self.temperature] * self.layers
        self._rng = random.Random(self.seed)

    @property
    def layer_l(self) -> float:
        return self.volume_l / self.layers

    def _draw(self, liters: float) -> None:
        """Plug flow: shift the column up by ``liters``; mains enters below."""
        shift = liters / self.layer_l
        old = self.profile
        drawn = 0.0
        # Energy leaving through the top.
        remaining = shift
        index = 0
        while remaining > 0 and index < self.layers:
            portion = min(1.0, remaining)
            drawn += portion * self.layer_l * (old[index] - self.mains)
            remaining -= portion
            index += 1
        self.drawn_kwh += drawn * WATER_HEAT_CAPACITY_KWH_PER_L_K

        def at(position: float) -> float:  # position in layers from the top
            if position >= self.layers:
                return self.mains
            return old[int(position)]

        new = []
        for index in range(self.layers):
            samples = [at(index + shift + k / 10) for k in range(10)]
            new.append(sum(samples) / len(samples))
        blend = min(1.0, self.mixing * liters / self.volume_l)
        mean = sum(new) / len(new)
        self.profile = [(1 - blend) * value + blend * mean for value in new]

    def _heat(self, kwh: float) -> None:
        self.profile[-1] += kwh / (self.layer_l * WATER_HEAT_CAPACITY_KWH_PER_L_K)
        # Convection: a layer warmer than the one above mixes with it.
        changed = True
        while changed:
            changed = False
            for index in range(self.layers - 1, 0, -1):
                if self.profile[index] > self.profile[index - 1] + 1e-9:
                    mean = (self.profile[index] + self.profile[index - 1]) / 2
                    self.profile[index] = self.profile[index - 1] = mean
                    changed = True

    def step(self, *, heat: bool = False, liters: float = 0.0) -> TankSample:
        """Advance one minute (``liters`` drawn during it)."""
        hours = 1 / 60
        power = 0.0
        if heat and self.profile[-3] < self.cutoff:
            power = self.rated_power_w
            self._heat(power / 1000 * hours)
            self.energy_kwh += power / 1000 * hours
        if liters:
            self._draw(liters)
        rate = self.ua / (self.volume_l * WATER_HEAT_CAPACITY_KWH_PER_L_K * 1000)
        self.profile = [
            AMBIENT_TEMPERATURE + (t - AMBIENT_TEMPERATURE) * math.exp(-rate * hours)
            for t in self.profile
        ]
        self.time += timedelta(minutes=1)
        probe = self.profile[0] + self._rng.uniform(-self.noise, self.noise)
        return TankSample(
            timestamp=self.time,
            temperature=round(probe * 4) / 4,  # 0.25 °C resolution, like the real probe
            power=power,
            energy=round(self.energy_kwh, 4),
            manual_mode=heat,
            regulator_configured=True,
        )

    def run(
        self, minutes: int, *, heat: bool = False, liters: float = 0.0
    ) -> list[TankSample]:
        return [self.step(heat=heat, liters=liters) for _ in range(minutes)]
