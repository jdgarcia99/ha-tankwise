"""v0.14.0: a new water heater (reset_tank) and the outlet probe."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.replay import (
    ROLE_ENERGY,
    ROLE_HOT_WATER_VOLUME,
    ROLE_OUTLET_TEMPERATURE,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_TEMPERATURE,
    StateChange,
    replay,
)
from tankwise_model.tank import (
    REASON_TANK_RESET,
    WATER_HEAT_CAPACITY_KWH_PER_L_K,
    ModelStatus,
    TankModel,
    TankParams,
    TankSample,
)
from tankwise_model.water import PLACEMENT_CISTERN, PLACEMENT_PIPE, MainsSensor

from .simulator import PARAMS as SIM_PARAMS, TankSimulator

TZ = ZoneInfo("Europe/Madrid")
MAINS = 13.4
C = WATER_HEAT_CAPACITY_KWH_PER_L_K
PARAMS = TankParams(mains_temperature=MAINS, cutoff_temperature=68.0)
SHEATH = TankParams(
    mains_temperature=MAINS, cutoff_temperature=68.0, probe_at_top=False
)
START = datetime(2026, 11, 28, 7, 0, tzinfo=TZ)


class _Feed:
    """Minute samples with a meter and an outlet probe read every 5 s.

    ``outlet(seconds)`` gives the outlet reading that many seconds after the
    water started to flow (None: no reading); at rest the pipe reads 22 °C.
    """

    def __init__(
        self,
        *,
        probe: float = 55.0,
        params: TankParams = PARAMS,
        outlet_configured: bool = True,
    ) -> None:
        self.model = TankModel()
        self.params = params
        self.outlet_configured = outlet_configured
        self.time = START
        self.volume = 1000.0
        self.probe = probe
        self.flow_start: datetime | None = None
        self.state = self._sample((), ())

    def _sample(self, outlet, meter):
        return self.model.update(
            TankSample(
                timestamp=self.time,
                temperature=self.probe,
                power=0.0,
                energy=10.0,
                hot_water_volume=self.volume,
                meter_configured=True,
                outlet_configured=self.outlet_configured,
                outlet_readings=tuple(outlet),
                meter_readings=tuple(meter),
            ),
            self.params,
        )

    def minute(self, liters_per_min: int = 0, outlet=None) -> None:
        """One minute: a litre every 60/flow seconds, a reading every 5 s."""
        start = self.time
        meter, readings = [], []
        if liters_per_min and self.flow_start is None:
            self.flow_start = start
        if not liters_per_min:
            self.flow_start = None
        for second in range(5, 61, 5):
            moment = start + timedelta(seconds=second)
            if self.flow_start is not None and outlet is not None:
                value = outlet((moment - self.flow_start).total_seconds())
            else:
                value = 22.0
            if value is not None:
                readings.append((moment, value))
        if liters_per_min:
            every = 60 / liters_per_min
            for index in range(1, liters_per_min + 1):
                self.volume += 1.0
                meter.append((start + timedelta(seconds=every * index), self.volume))
        self.time = start + timedelta(minutes=1)
        self.state = self._sample(readings, meter)

    def rest(self, minutes: int) -> None:
        for _ in range(minutes):
            self.minute()


def _pipe_then(hot: float):
    """15 s of water standing in the pipe (25 °C), then the tank's water."""
    return lambda seconds: 25.0 if seconds < 15 else hot


# ---------------------------------------------------------------------------
# Outlet probe
# ---------------------------------------------------------------------------


def test_energy_is_litre_by_litre_at_the_outlet_without_the_pipe_water() -> None:
    """The probe says 55 °C, the outlet 60 °C: the litres left at 60 °C."""
    feed = _Feed(probe=55.0)
    for _ in range(5):
        feed.minute(6, _pipe_then(60.0))
    feed.rest(5)
    [draw] = feed.model.consumptions
    assert draw.liters == 30
    # Not at the 25 °C of the pipe nor at the 55 °C of the probe.
    assert draw.energy_kwh == pytest.approx(30 * C * (60.0 - MAINS), rel=1e-6)
    assert not draw.shortage


def test_without_the_outlet_probe_the_energy_is_as_before() -> None:
    feed = _Feed(probe=55.0, outlet_configured=False)
    for _ in range(5):
        feed.minute(6, _pipe_then(60.0))
    feed.rest(5)
    [draw] = feed.model.consumptions
    assert draw.energy_kwh == pytest.approx(30 * C * (55.0 - MAINS), rel=1e-3)


def test_each_litre_at_its_own_temperature() -> None:
    """The outlet falls during the draw: each litre at the reading of its time."""

    def outlet(seconds: float) -> float:
        if seconds < 15:
            return 25.0
        return 60.0 if seconds < 120 else 45.0

    feed = _Feed(probe=60.0)
    for _ in range(4):
        feed.minute(6, outlet)
    feed.rest(5)
    [draw] = feed.model.consumptions
    # 12 litres up to 120 s at 60 °C (the one at 120 s reads 45 °C), 12 at 45 °C.
    expected = C * (11 * (60.0 - MAINS) + 13 * (45.0 - MAINS))
    assert draw.energy_kwh == pytest.approx(expected, rel=1e-6)
    assert draw.min_temperature == 45.0


def test_k_use_is_learned_from_the_outlet() -> None:
    """Useful litres until the outlet fell below the shower temperature."""
    feed = _Feed(probe=50.0)
    start = feed.model.as_dict()["equivalent_temperature"]

    def outlet(seconds: float) -> float:
        if seconds < 15:
            return 25.0
        return 50.0 if seconds < 300 else 36.0  # 30 litres, then cold

    for _ in range(6):
        feed.minute(6, outlet)
    feed.rest(5)
    span = PARAMS.shower_temperature - MAINS
    predicted = PARAMS.volume_l * (start - PARAMS.comfort_min_temperature) / span
    assert feed.model.outlet_usable_samples == [pytest.approx(29 / predicted)]
    assert feed.state.usable_samples == 1
    assert feed.state.usable_factor == pytest.approx(29 / predicted)
    # The outlet went below the comfort temperature while flowing: a shortage.
    [draw] = feed.model.consumptions
    assert draw.shortage


def test_cold_pipe_after_the_draw_is_not_a_shortage() -> None:
    """After the flow stops the pipe cools: not the tank's water."""
    feed = _Feed(probe=55.0)
    for _ in range(3):
        feed.minute(6, _pipe_then(55.0))
    feed.rest(5)  # the pipe reads 22 °C at rest
    [draw] = feed.model.consumptions
    assert not draw.shortage
    assert feed.model.outlet_usable_samples == []


def test_measured_outlet_is_published_and_corrects_the_hot_layer() -> None:
    """Probe in the sheath: the hot layer is what the outlet measures."""
    feed = _Feed(probe=58.0, params=SHEATH)
    feed.model._hot = 65.0  # the last full charge
    for _ in range(3):
        feed.minute(6, _pipe_then(52.0))
    assert feed.state.outlet_temperature == 52.0
    feed.rest(5)
    # No longer the 65 °C of the last charge.
    assert feed.state.outlet_temperature == pytest.approx(58.0, abs=0.5)
    assert feed.model.as_dict()["hot_temperature"] < 60.0


def test_outlet_samples_are_saved() -> None:
    model = TankModel()
    model._outlet_usable_samples = [0.9]
    copy = TankModel.from_dict(model.as_dict())
    assert copy.outlet_usable_samples == [0.9]
    assert copy.usable_factor == pytest.approx(0.9)
    assert copy.reset_usable_factor() == 1


def test_replay_feeds_the_outlet_readings() -> None:
    """History import: the outlet readings between samples reach the model."""
    t0 = START
    temperature = [StateChange(t0, 55.0)]
    volume = [StateChange(t0, 1000.0)]
    outlet = [StateChange(t0, 22.0)]
    for second in range(10, 181, 10):
        volume.append(
            StateChange(t0 + timedelta(minutes=1, seconds=second), 1000.0 + second / 10)
        )
    for second in range(5, 181, 5):
        outlet.append(
            StateChange(
                t0 + timedelta(minutes=1, seconds=second), 25.0 if second < 25 else 60.0
            )
        )
    series = {
        ROLE_TEMPERATURE: temperature,
        ROLE_POWER: [StateChange(t0, 0.0)],
        ROLE_ENERGY: [StateChange(t0, 10.0)],
        ROLE_HOT_WATER_VOLUME: volume,
        ROLE_OUTLET_TEMPERATURE: outlet,
    }
    result = replay(series, PARAMS, end=t0 + timedelta(minutes=12))
    [draw] = result.model.consumptions
    assert draw.liters == 18
    assert draw.energy_kwh == pytest.approx(18 * C * (60.0 - MAINS), rel=1e-6)


# ---------------------------------------------------------------------------
# New water heater
# ---------------------------------------------------------------------------


def test_reset_tank_forgets_the_tank_and_keeps_the_draws() -> None:
    feed = _Feed(probe=55.0, outlet_configured=False)
    feed.minute(6)
    feed.rest(5)
    model = feed.model
    model._ua = 2.0
    model._loss_windows = 5
    model._capacity_samples = [0.8]
    model._usable_samples = [0.9]
    model._outlet_usable_samples = [0.95]
    model._heater_power_w = 1450.0
    model._heater_power_samples = 30
    model._cutoff_samples = [66.0, 67.0, 68.0]
    model._stratification = [(60.0, 0.5)]
    model._hot = 64.0
    model._calibration_count = 4
    draws = list(model.consumptions)

    model.reset_tank(feed.time)
    data = model.as_dict()
    assert model.ua == 1.0
    assert data["loss_windows"] == 0
    assert data["capacity_samples"] == []
    assert data["usable_samples"] == [] and data["outlet_usable_samples"] == []
    assert data["heater_power_w"] is None
    assert data["cutoff_samples"] == []
    assert data["stratification"] == []
    assert data["hot_temperature"] is None
    assert model.tank_since == feed.time
    assert model.consumptions == draws  # the usage stays

    feed.rest(2)  # the stored energy is taken again from the probe
    assert feed.state.available
    assert feed.state.status is ModelStatus.LEARNING
    assert feed.state.status_reason == REASON_TANK_RESET
    assert TankModel.from_dict(model.as_dict()).tank_since == model.tank_since


def _series(samples) -> dict[str, list[StateChange]]:
    roles = {
        ROLE_TEMPERATURE: "temperature",
        ROLE_POWER: "power",
        ROLE_ENERGY: "energy",
        ROLE_REGULATOR: "regulator",
    }
    return {
        role: [
            StateChange(sample.timestamp, getattr(sample, key)) for sample in samples
        ]
        for role, key in roles.items()
    }


def test_history_import_learns_the_tank_only_after_the_swap() -> None:
    """Two heatings to the cut-off; the tank was replaced between them."""
    sim = TankSimulator(temperature=44.0, noise=0.0)
    samples = sim.run(30, regulator=100.0)  # first cut-off
    samples += sim.run(10, draw_kw=3.0)
    samples += sim.run(30)  # the draw is over before the swap
    swap = sim.time
    samples += sim.run(10, draw_kw=3.0)
    samples += sim.run(60, regulator=100.0)  # second cut-off
    samples += sim.run(20)
    series = _series(samples)

    whole = replay(series, SIM_PARAMS)
    swapped = replay(series, SIM_PARAMS, tank_since=swap)
    assert whole.model.as_dict()["calibration_count"] == 2
    assert swapped.model.as_dict()["calibration_count"] == 1
    assert swapped.model.tank_since == swap
    # The draws before the swap are still there (for the usage profile).
    assert any(draw.start < swap for draw in swapped.model.consumptions)


# ---------------------------------------------------------------------------
# Cold water sensor
# ---------------------------------------------------------------------------


def test_cistern_sensor_is_a_slow_mean() -> None:
    sensor = MainsSensor(PLACEMENT_CISTERN)
    assert sensor.temperature is None
    sensor.update(START, 14.0, drawing=False)
    assert sensor.temperature == 14.0
    sensor.update(START + timedelta(minutes=1), 20.0, drawing=False)
    assert 14.0 < sensor.temperature < 14.2  # one minute of a one-hour mean
    sensor.update(START + timedelta(minutes=2), 80.0, drawing=False)  # error
    assert sensor.temperature < 14.2


def test_pipe_sensor_uses_the_flowing_water() -> None:
    """At rest the pipe takes the room temperature; while drawing, the mains."""
    sensor = MainsSensor(PLACEMENT_PIPE)
    moment = START
    for _ in range(30):
        sensor.update(moment, 21.0, drawing=False)
        moment += timedelta(minutes=1)
    assert sensor.temperature == pytest.approx(21.0)  # slow mean until a draw
    assert not sensor.from_draw
    for value in (21.0, 18.0, 15.5, 14.8, 14.6):
        sensor.update(moment, value, drawing=True)
        moment += timedelta(minutes=1)
    sensor.update(moment, 15.0, drawing=False)
    assert sensor.from_draw
    assert sensor.temperature == 14.6
    for _ in range(60):
        moment += timedelta(minutes=1)
        sensor.update(moment, 21.0, drawing=False)
    assert sensor.temperature == 14.6  # kept until the next draw
    copy = MainsSensor.from_dict(sensor.as_dict(), PLACEMENT_PIPE)
    assert copy.temperature == 14.6
    assert (
        MainsSensor.from_dict(sensor.as_dict(), PLACEMENT_CISTERN).temperature is None
    )
