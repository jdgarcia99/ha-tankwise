"""Energy reconciliation and slow draws (real-tank behaviour of the upper probe)."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest
from tankwise_model.tank import TankModel, TankParams

from .layered import LayeredTank
from .simulator import PARAMS, TankSimulator, drive, feed

LAYERED = TankParams(
    mains_temperature=14.0,
    cutoff_temperature=68.0,
    rated_power_w=1500.0,
    capacity_factor=1.0,
)


def _morning(
    tank: LayeredTank, model: TankModel, liters_per_minute: float, minutes: int
) -> None:
    """Night heating, a shower, rest, then the afternoon heating."""
    feed(model, tank.run(60), LAYERED)
    feed(model, tank.run(90, heat=True), LAYERED)  # to ~60 °C
    feed(model, tank.run(60), LAYERED)
    tank.drawn_kwh = 0.0
    feed(model, tank.run(minutes, liters=liters_per_minute), LAYERED)
    feed(model, tank.run(240), LAYERED)
    feed(model, tank.run(60, heat=True), LAYERED)  # afternoon heating
    feed(model, tank.run(40), LAYERED)  # settles: reconciliation


def test_probe_alone_underestimates_a_shower_in_a_stratified_tank() -> None:
    tank = LayeredTank(temperature=40.0)
    model = TankModel()
    feed(model, tank.run(60), LAYERED)
    feed(model, tank.run(90, heat=True), LAYERED)
    feed(model, tank.run(60), LAYERED)
    tank.drawn_kwh = 0.0
    feed(model, tank.run(6, liters=6), LAYERED)  # 36 litres
    feed(model, tank.run(60), LAYERED)
    [shower] = model.consumptions
    assert shower.reconciled is None
    assert shower.energy_kwh < tank.drawn_kwh * 0.8


def test_next_heating_reconciles_the_shower_energy() -> None:
    tank = LayeredTank(temperature=40.0)
    model = TankModel()
    _morning(tank, model, 6, 6)
    [shower] = model.consumptions
    assert shower.reconciled is not None and shower.reconciled > 1
    assert shower.energy_kwh == pytest.approx(tank.drawn_kwh, rel=0.2)
    [balance, *_] = [r for r in model.reconciliations if r.measured_kwh > 0]
    assert balance.actual_kwh == pytest.approx(tank.drawn_kwh, rel=0.2)


def test_reconciliation_also_lowers_overestimated_draws() -> None:
    """A fully mixed tank: measured draws are right, factor stays close to 1."""
    sim = TankSimulator(temperature=40.0, noise=0.5, report_manual=True)
    model = TankModel()
    drive(model, sim, 30)
    drive(model, sim, 120, regulator=100)  # heat to the cut-off
    drive(model, sim, 30)
    drive(model, sim, 12, draw_kw=9.0)
    drive(model, sim, 60)
    drive(model, sim, 120, regulator=100)
    drive(model, sim, 40)
    [shower] = model.consumptions
    assert shower.reconciled == pytest.approx(1.0, abs=0.15)
    assert shower.energy_kwh == pytest.approx(1.8, rel=0.15)


def test_no_reconciliation_without_heating() -> None:
    sim = TankSimulator(temperature=46.0, noise=0.5)
    model = TankModel()
    drive(model, sim, 60)
    drive(model, sim, 12, draw_kw=9.0)
    drive(model, sim, 240)
    assert model.reconciliations == []
    assert model.consumptions[0].reconciled is None


def test_slow_draw_is_detected() -> None:
    """0.4 kW for an hour: too slow for the 10-minute detector."""
    sim = TankSimulator(temperature=60.0, noise=0.5, seed=4)
    model = TankModel()
    params = replace(PARAMS, cutoff_temperature=68.0)
    drive(model, sim, 120, params=params)
    drive(model, sim, 60, draw_kw=0.4, params=params)
    drive(model, sim, 60, params=params)
    [draw] = model.consumptions
    assert draw.slow
    assert draw.energy_kwh == pytest.approx(0.4, rel=0.35)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_no_slow_draw_from_noise(seed: int) -> None:
    """A day at rest with ±1 °C noise and a slightly wrong UA: no draws."""
    sim = TankSimulator(temperature=62.0, seed=seed, ua=1.3)
    model = TankModel()
    drive(model, sim, 24 * 60, params=replace(PARAMS, cutoff_temperature=68.0))
    assert model.consumptions == []


def test_reconciled_draws_survive_a_restart() -> None:
    tank = LayeredTank(temperature=40.0)
    model = TankModel()
    _morning(tank, model, 6, 6)
    data = json.loads(json.dumps(model.as_dict()))
    restored = TankModel.from_dict(data)
    assert restored.as_dict()["consumptions"] == model.as_dict()["consumptions"]
    assert restored.consumptions[0].reconciled is not None
