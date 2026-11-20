"""Thermal model features of DESIGN.md §4: energy, litres, k_cap, power, draws."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest
from tankwise_model.tank import (
    DEFAULT_CAPACITY_FACTOR,
    MAX_CUTOFF_SAMPLES,
    TankModel,
    TankParams,
    TankSample,
)

from .simulator import PARAMS, START, TankSimulator, drive, error_kwh, feed

SHOWER_KW = 9.0


def _fill(model: TankModel, sim: TankSimulator, params: TankParams = PARAMS) -> None:
    before = model.as_dict()["calibration_count"]
    for _ in range(300):
        drive(model, sim, 1, regulator=100, params=params)
        if model.as_dict()["calibration_count"] > before:
            return
    raise AssertionError("the tank was never confirmed full")


# ---------------------------------------------------------------------------
# 4.1 Energy and useful litres
# ---------------------------------------------------------------------------


def test_heat_capacity_of_100_litres() -> None:
    assert TankParams(volume_l=100).heat_capacity == pytest.approx(0.1163)


def test_stored_energy_and_litres_at_40c() -> None:
    """60 °C, mains 17 °C: E = 0.1163 · 43 kWh, L = 100 · 43 / 23 litres."""
    params = TankParams(mains_temperature=17.0, cutoff_temperature=68.0)
    model = TankModel()
    state = model.update(
        TankSample(timestamp=START, temperature=60.0, power=0.0, energy=10.0), params
    )
    assert state.stored_energy_kwh == pytest.approx(0.1163 * 43, rel=1e-3)
    assert state.liters_available == pytest.approx(100 * 43 / 23, rel=1e-3)
    assert state.useful_energy_kwh == pytest.approx(0.1163 * 22, rel=1e-3)
    assert state.charge_percent == pytest.approx(100 * 22 / 30, rel=1e-3)


def test_litres_are_zero_below_mains_and_with_bad_settings() -> None:
    params = TankParams(mains_temperature=17.0, ambient_temperature=10.0)
    model = TankModel()
    state = model.update(
        TankSample(timestamp=START, temperature=15.0, power=0.0), params
    )
    assert state.liters_available == 0.0
    assert state.stored_energy_kwh == 0.0

    model = TankModel()
    state = model.update(
        TankSample(timestamp=START, temperature=50.0, power=0.0),
        replace(params, shower_temperature=15.0),
    )
    assert state.liters_available == 0.0


# ---------------------------------------------------------------------------
# 4.1 Effective capacity factor and 4.3 heater power
# ---------------------------------------------------------------------------


def test_capacity_factor_is_learned_from_heatings_without_draws() -> None:
    """The probe sees 90 % of the heat: k_cap moves from 1.0 to ~0.9."""
    sim = TankSimulator(temperature=30.0, heating_efficiency=0.9, noise=0.5)
    model = TankModel()
    drive(model, sim, 10)
    for _ in range(3):
        _fill(model, sim)
        drive(model, sim, 30)
        drive(model, sim, 12, draw_kw=SHOWER_KW)
        drive(model, sim, 30)
    state = drive(model, sim, 1)
    assert state.capacity_samples >= 3
    assert state.capacity_factor == pytest.approx(0.9, abs=0.04)


def test_capacity_factor_default_is_the_design_value() -> None:
    assert TankParams().capacity_factor == DEFAULT_CAPACITY_FACTOR == 0.9


def test_draw_energy_uses_the_capacity_factor() -> None:
    """With k_cap 0.9, a probe drop is worth C · ΔT / 0.9."""
    sim = TankSimulator(temperature=46.0, noise=0.5)
    model = TankModel()
    params = replace(PARAMS, capacity_factor=0.9)
    drive(model, sim, 60, params=params)
    drive(model, sim, 12, draw_kw=SHOWER_KW, params=params)
    drive(model, sim, 60, params=params)
    assert len(model.consumptions) == 1
    assert model.consumptions[0].energy_kwh == pytest.approx(1.8 / 0.9, rel=0.12)


def test_heater_power_is_learned_at_full_output() -> None:
    sim = TankSimulator(temperature=30.0, rated_power_w=1490.0)
    model = TankModel()
    drive(model, sim, 5)
    state = drive(model, sim, 60, regulator=100)
    assert state.heater_power_w == pytest.approx(1490.0, abs=1)


def test_heater_power_ignores_partial_regulator_output() -> None:
    """A PWM at 80 % gives 1.2 kW: not the full heater power."""
    sim = TankSimulator(temperature=30.0, rated_power_w=1500.0)
    model = TankModel()
    drive(model, sim, 5)
    state = drive(model, sim, 30, regulator=80)
    assert state.heater_power_w == PARAMS.rated_power_w  # not learned yet
    state = drive(model, sim, 30, regulator=100)
    assert state.heater_power_w == pytest.approx(1500.0, abs=1)


# ---------------------------------------------------------------------------
# 4.3 Thermostat cut-off with the FreeDS manual mode
# ---------------------------------------------------------------------------


def test_cutoff_detected_with_manual_mode_switch() -> None:
    """Manual mode on, power below 50 W for 3 min: full, cut-off recorded."""
    sim = TankSimulator(temperature=40.0, cutoff=63.0, report_manual=True)
    model = TankModel()
    params = replace(PARAMS, cutoff_temperature=68.0)
    drive(model, sim, 5, params=params)
    _fill(model, sim, params)
    state = drive(model, sim, 1, regulator=100, params=params)
    assert state.calibration_count == 1
    assert state.cutoff_samples[-1] == pytest.approx(63.0, abs=0.8)
    assert state.equivalent_temperature == pytest.approx(63.0, abs=0.8)


def test_manual_mode_off_is_not_a_cutoff() -> None:
    """The manual mode is switched off before the power falls: not full."""
    sim = TankSimulator(temperature=40.0, cutoff=63.0, report_manual=True)
    model = TankModel()
    drive(model, sim, 5)
    drive(model, sim, 30, regulator=100)
    state = drive(model, sim, 30, regulator=0)
    assert state.calibration_count == 0


def test_cutoff_history_keeps_the_last_20() -> None:
    sim = TankSimulator(temperature=40.0, cutoff=47.0, report_manual=True)
    model = TankModel()
    drive(model, sim, 5)
    for _ in range(MAX_CUTOFF_SAMPLES + 3):
        _fill(model, sim)
        drive(model, sim, 6, draw_kw=SHOWER_KW)
        drive(model, sim, 20)
    assert len(model.as_dict()["cutoff_samples"]) == MAX_CUTOFF_SAMPLES


# ---------------------------------------------------------------------------
# 4.4 Draw detection
# ---------------------------------------------------------------------------


def test_short_tap_draw_is_detected() -> None:
    """A short tap (~0.15 kWh in 3 min) drops the probe ~1.3 °C: detected."""
    sim = TankSimulator(temperature=50.0, noise=0.5)
    model = TankModel()
    drive(model, sim, 60)
    drive(model, sim, 3, draw_kw=3.0)
    drive(model, sim, 30)
    assert len(model.consumptions) == 1
    event = model.consumptions[0]
    assert event.energy_kwh == pytest.approx(0.15, abs=0.06)
    assert not event.shortage


def test_noise_is_not_a_draw() -> None:
    """±0.5 °C noise (the measured sensor noise) for a day: no draws."""
    sim = TankSimulator(temperature=60.0, noise=0.5, seed=7)
    model = TankModel()
    drive(model, sim, 24 * 60, params=replace(PARAMS, cutoff_temperature=68))
    assert model.consumptions == []


def test_shower_records_minimum_temperature_and_shortage() -> None:
    sim = TankSimulator(temperature=60.0, noise=0.5)
    model = TankModel()
    params = replace(PARAMS, cutoff_temperature=68.0, comfort_min_temperature=38.0)
    drive(model, sim, 60, params=params)
    drive(model, sim, 15, draw_kw=SHOWER_KW, params=params)
    state = drive(model, sim, 30, params=params)

    event = model.consumptions[0]
    assert event.min_temperature == pytest.approx(sim.temperature, abs=1.0)
    assert event.start_temperature == pytest.approx(60.0, abs=1.0)
    assert event.shortage is (event.min_temperature < 38.0)
    assert event.end > event.start
    assert state.last_consumption == event
    assert not state.drawing


def test_drawing_flag_while_a_draw_is_in_progress() -> None:
    sim = TankSimulator(temperature=60.0, noise=0.5)
    model = TankModel()
    params = replace(PARAMS, cutoff_temperature=68.0)
    drive(model, sim, 60, params=params)
    state = drive(model, sim, 6, draw_kw=SHOWER_KW, params=params)
    assert state.drawing


# ---------------------------------------------------------------------------
# Inputs: no energy meter, ambient sensor
# ---------------------------------------------------------------------------


def test_heating_without_energy_meter_integrates_the_power() -> None:
    sim = TankSimulator(temperature=30.0, report_energy=False)
    model = TankModel()
    drive(model, sim, 10)
    state = drive(model, sim, 60, regulator=100)
    assert state.available
    assert error_kwh(state, sim) < 0.15


def test_ambient_sensor_changes_the_losses() -> None:
    """A warmer room means smaller losses."""
    params = replace(PARAMS, cutoff_temperature=68.0)
    cold, warm = TankModel(), TankModel()
    for minute in range(120):
        moment = START + timedelta(minutes=minute)
        base = TankSample(timestamp=moment, temperature=60.0, power=0.0, energy=1.0)
        cold_state = cold.update(replace(base, ambient_temperature=10.0), params)
        warm_state = warm.update(replace(base, ambient_temperature=30.0), params)
    assert cold_state.loss_power_w(10.0) > warm_state.loss_power_w(30.0)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_format_1_data_is_converted() -> None:
    """Version 0.2 stored the losses as a 1/h coefficient."""
    data = {
        "format": 1,
        "equivalent_temperature": 45.0,
        "loss_coefficient": 0.0086,
        "loss_windows": 4,
        "cutoff_samples": [47.0, 47.2, 46.9],
        "uncertain": False,
        "calibration_count": 3,
        "last_calibration": START.isoformat(),
        "last_valid_time": START.isoformat(),
        "last_energy": 12.5,
        "consumptions": [
            {
                "start": START.isoformat(),
                "end": (START + timedelta(minutes=10)).isoformat(),
                "energy_kwh": 1.2,
                "start_temperature": 47.0,
                "end_temperature": 36.0,
                "while_heating": False,
            }
        ],
    }
    model = TankModel.from_dict(data, volume_l=100)
    assert model.ua == pytest.approx(0.0086 * 116.3, rel=1e-3)
    assert model.consumptions[0].min_temperature == 36.0
    assert not model.consumptions[0].shortage
    restored = TankModel.from_dict(json.loads(json.dumps(model.as_dict())))
    assert restored.as_dict() == model.as_dict()


def test_feed_helper_accepts_custom_params() -> None:
    sim = TankSimulator(temperature=46.0)
    state = feed(TankModel(), sim.run(3), replace(PARAMS, mains_temperature=10.0))
    assert state.mains_temperature == 10.0
