"""Synthetic-data tests for the tank thermal model."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest
from tankwise_model.tank import (
    DEFAULT_UA,
    HeaterState,
    ModelStatus,
    TankModel,
    TankSample,
)

from .simulator import PARAMS, TankSimulator, drive, error_kwh, feed

SHOWER_KW = 9.0  # 1.8 kWh in 12 minutes


def _fill(model: TankModel, sim: TankSimulator) -> None:
    """Heat until the thermostat cuts off and the model confirms it."""
    before = model.as_dict()["calibration_count"]
    for _ in range(240):
        drive(model, sim, 1, regulator=100)
        if model.as_dict()["calibration_count"] > before:
            return
    raise AssertionError("the tank was never confirmed full")


@pytest.mark.parametrize("report_regulator", [True, False])
def test_full_heat_up(report_regulator: bool) -> None:
    """30 → 47 °C at 1.45 kW: balance tracks the tank, one recalibration."""
    sim = TankSimulator(temperature=30.0, report_regulator=report_regulator)
    model = TankModel()
    state = drive(model, sim, 10)
    assert state.status is ModelStatus.UNCERTAIN

    state = drive(model, sim, 60, regulator=100)
    assert state.heater_state is HeaterState.HEATING
    assert error_kwh(state, sim) < 0.15
    assert state.calibration_count == 0

    minutes = 0
    while sim.thermostat_closed:
        drive(model, sim, 1, regulator=100)
        minutes += 1
    assert 75 <= 60 + minutes <= 90  # ~82 min to heat 17 K

    state = drive(model, sim, 30, regulator=100)
    assert state.calibration_count == 1
    assert state.equivalent_temperature == pytest.approx(47.0, abs=0.2)
    assert error_kwh(state, sim) < 0.1
    assert state.status is ModelStatus.LEARNING
    assert state.charge_percent == pytest.approx(100.0, abs=2)
    assert state.useful_energy_kwh == pytest.approx(0.1163 * 15, rel=0.02)
    assert model.consumptions == []


@pytest.mark.parametrize("report_regulator", [True, False])
def test_heating_stopped_by_regulator_is_not_full(report_regulator: bool) -> None:
    """The diverter stops at 38 °C for lack of surplus: no false full."""
    sim = TankSimulator(temperature=30.0, report_regulator=report_regulator)
    model = TankModel()
    drive(model, sim, 10)
    drive(model, sim, 40, regulator=100)
    state = drive(model, sim, 60, regulator=0)

    assert sim.temperature < 40
    assert state.calibration_count == 0
    assert state.status is ModelStatus.UNCERTAIN
    assert error_kwh(state, sim) < 0.15


def test_turning_on_with_full_tank_does_not_recalibrate() -> None:
    """Regulator demand with the thermostat already open recalibrates nothing."""
    sim = TankSimulator(temperature=30.0)
    model = TankModel()
    drive(model, sim, 10)
    _fill(model, sim)
    drive(model, sim, 120)
    full_equivalent = model.as_dict()["equivalent_temperature"]

    # A short power spike when the diverter switches on.
    sample = sim.step(regulator=100)
    sim.energy_kwh += 0.005
    feed(model, [replace(sample, power=300.0, energy=sim.energy_kwh)])
    state = drive(model, sim, 120, regulator=100)

    assert not sim.thermostat_closed
    assert state.calibration_count == 1
    assert state.equivalent_temperature is not None
    assert state.equivalent_temperature < full_equivalent
    assert error_kwh(state, sim) < 0.1
    assert model.consumptions == []


def test_strong_shower() -> None:
    """A 1.8 kWh shower takes the probe from ~46 to ~30 °C."""
    sim = TankSimulator(temperature=46.0)
    model = TankModel()
    drive(model, sim, 60)
    drive(model, sim, 12, draw_kw=SHOWER_KW)
    state = drive(model, sim, 60)

    assert 29.5 < sim.temperature < 31.5
    assert len(model.consumptions) == 1
    event = model.consumptions[0]
    assert event.energy_kwh == pytest.approx(1.8, rel=0.15)
    assert not event.while_heating
    assert event.min_temperature < PARAMS.comfort_min_temperature
    assert event.shortage
    assert state.calibration_count == 0
    assert error_kwh(state, sim) < 0.12
    assert state.useful_energy_kwh == pytest.approx(0.0, abs=0.12)


def test_two_consecutive_showers() -> None:
    """Two 1.2 kWh showers 20 minutes apart are two separate draws."""
    sim = TankSimulator(temperature=47.0)
    model = TankModel()
    drive(model, sim, 30)
    drive(model, sim, 10, draw_kw=7.2)
    drive(model, sim, 20)
    drive(model, sim, 10, draw_kw=7.2)
    state = drive(model, sim, 60)

    assert len(model.consumptions) == 2
    for event in model.consumptions:
        assert event.energy_kwh == pytest.approx(1.2, rel=0.2)
    assert error_kwh(state, sim) < 0.12


def test_shower_while_heating() -> None:
    """A strong shower started while heating at 1.45 kW is still detected."""
    sim = TankSimulator(temperature=36.0)
    model = TankModel()
    drive(model, sim, 10)
    drive(model, sim, 20, regulator=100)
    drive(model, sim, 12, regulator=100, draw_kw=SHOWER_KW)
    state = drive(model, sim, 30, regulator=100)

    assert len(model.consumptions) == 1
    event = model.consumptions[0]
    assert event.while_heating
    assert event.energy_kwh == pytest.approx(1.8, rel=0.2)
    assert state.heater_state is HeaterState.HEATING
    assert state.calibration_count == 0
    assert error_kwh(state, sim) < 0.17


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_noisy_probe_at_rest(seed: int) -> None:
    """±1 °C probe noise for 12 h looks neither like draws nor like heating."""
    sim = TankSimulator(temperature=46.0, seed=seed)
    model = TankModel()
    for _ in range(12 * 60):
        state = drive(model, sim, 1)
        assert state.heater_state is HeaterState.IDLE

    assert model.consumptions == []
    assert state.calibration_count == 0
    assert error_kwh(state, sim) < 0.1


def _calibrated_model_after_shower() -> tuple[TankModel, TankSimulator]:
    sim = TankSimulator(temperature=30.0)
    model = TankModel()
    drive(model, sim, 10)
    _fill(model, sim)
    drive(model, sim, 60)
    drive(model, sim, 12, draw_kw=SHOWER_KW)
    drive(model, sim, 30)
    assert len(model.consumptions) == 1
    return model, sim


def _restart(model: TankModel) -> TankModel:
    """Simulate saving to and restoring from storage."""
    return TankModel.from_dict(json.loads(json.dumps(model.as_dict())))


def test_restart_with_six_hour_gap() -> None:
    """Heating during a 6 h outage comes from the meter; state is uncertain."""
    model, sim = _calibrated_model_after_shower()
    assert model.status() is ModelStatus.LEARNING

    restored = _restart(model)
    sim.skip(40, regulator=100)  # heating while Home Assistant is down
    sim.skip(320)

    state = drive(restored, sim, 1)
    assert state.status is ModelStatus.UNCERTAIN
    assert error_kwh(state, sim) < 0.15
    assert len(restored.consumptions) == 1  # nothing invented
    assert state.calibration_count == 1

    _fill(restored, sim)
    assert restored.status() is ModelStatus.LEARNING


def test_restart_gap_with_unseen_draw() -> None:
    """A draw during the outage is followed but not recorded."""
    sim = TankSimulator(temperature=30.0)
    model = TankModel()
    drive(model, sim, 10)
    _fill(model, sim)
    drive(model, sim, 30)

    restored = _restart(model)
    sim.skip(60)
    sim.run(12, draw_kw=SHOWER_KW)
    sim.skip(288)

    state = drive(restored, sim, 1)
    assert restored.consumptions == []
    assert state.status is ModelStatus.UNCERTAIN
    assert error_kwh(state, sim) < 0.15


@pytest.mark.parametrize(
    ("gap_minutes", "expected"),
    [
        (3, ModelStatus.LEARNING),
        (10, ModelStatus.LEARNING),
        (45, ModelStatus.UNCERTAIN),
    ],
)
def test_short_and_long_restarts(gap_minutes: int, expected: ModelStatus) -> None:
    """Only gaps longer than 30 minutes make the state uncertain."""
    sim = TankSimulator(temperature=30.0)
    model = TankModel()
    drive(model, sim, 10)
    _fill(model, sim)
    drive(model, sim, 30)

    restored = _restart(model)
    sim.skip(gap_minutes)
    state = drive(restored, sim, 5)
    assert state.status is expected
    assert error_kwh(state, sim) < 0.15


def test_safe_mode_freezes_the_model() -> None:
    """Missing required sensors: unavailable, frozen, nothing invented."""
    sim = TankSimulator(temperature=46.0)
    model = TankModel()
    drive(model, sim, 30)
    frozen = model.as_dict()

    for _ in range(20):
        sample = sim.step()
        state = model.update(replace(sample, temperature=None), PARAMS)
        assert not state.available
        assert state.useful_energy_kwh is None
    assert model.as_dict() == frozen

    state = drive(model, sim, 5)
    assert state.available
    assert model.consumptions == []
    assert state.status is ModelStatus.UNCERTAIN
    assert error_kwh(state, sim) < 0.15


def test_ua_is_learned_from_rest_windows() -> None:
    """Rest windows move UA towards the real value despite ±1 °C noise."""
    true_ua = 1.6
    sim = TankSimulator(temperature=66.0, ua=true_ua)
    model = TankModel()
    state = drive(model, sim, 48 * 60, params=replace(PARAMS, cutoff_temperature=68))

    assert state.loss_windows >= 8
    assert state.ua_w_per_k == pytest.approx(true_ua, rel=0.15)
    assert state.ua_w_per_k > DEFAULT_UA


def test_no_loss_learning_while_heating_or_drawing() -> None:
    """Windows with heating or draws are not used to learn losses."""
    sim = TankSimulator(temperature=40.0)
    model = TankModel()
    for _ in range(6):
        drive(model, sim, 60, regulator=100)
        drive(model, sim, 10, draw_kw=SHOWER_KW)
        drive(model, sim, 60)
    assert model.as_dict()["loss_windows"] == 0


@pytest.mark.parametrize("report_regulator", [True, False])
def test_cutoff_temperature_is_learned(report_regulator: bool) -> None:
    """The cut-off temperature becomes the median of confirmed cut-offs."""
    sim = TankSimulator(
        temperature=35.0, cutoff=48.5, report_regulator=report_regulator
    )
    model = TankModel()
    drive(model, sim, 10)
    for _ in range(4):
        _fill(model, sim)
        drive(model, sim, 30)
        drive(model, sim, 10, draw_kw=SHOWER_KW)
        drive(model, sim, 30)

    assert model.cutoff_temperature(PARAMS) == pytest.approx(48.5, abs=0.7)
    _fill(model, sim)
    state = drive(model, sim, 1)
    assert error_kwh(state, sim) < 0.1


def test_serialisation_round_trip() -> None:
    """The persistent state survives JSON serialisation unchanged."""
    model, _ = _calibrated_model_after_shower()
    data = model.as_dict()
    assert _restart(model).as_dict() == data
    assert data["consumptions"][0]["energy_kwh"] > 1


@pytest.mark.parametrize(
    "data",
    [{}, {"format": 99}, {"format": 1, "equivalent_temperature": "hot"}],
)
def test_invalid_stored_data(data: dict) -> None:
    """Corrupt stored data raises ValueError."""
    with pytest.raises(ValueError):
        TankModel.from_dict(data)


def test_duplicate_timestamp_is_ignored() -> None:
    """A repeated sample does not advance the model."""
    sim = TankSimulator(temperature=46.0)
    model = TankModel()
    drive(model, sim, 10)
    sample = sim.step()
    model.update(sample, PARAMS)
    before = model.as_dict()
    model.update(replace(sample, temperature=10.0), PARAMS)
    assert model.as_dict() == before


def test_meter_reset_adds_no_energy() -> None:
    """A heater energy meter reset is not counted as heat."""
    sim = TankSimulator(temperature=40.0)
    model = TankModel()
    drive(model, sim, 10)
    before = model.as_dict()["equivalent_temperature"]
    sample = sim.step()
    model.update(replace(sample, energy=0.0), PARAMS)
    after = model.as_dict()["equivalent_temperature"]
    assert after <= before + 0.05
    assert isinstance(sample, TankSample)
    assert sample.timestamp - sim.time == timedelta(0)
