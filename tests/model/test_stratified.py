"""Useful energy of a stratified tank (learned with a hot water meter).

Reference case, 23/11 at 20:45 with TankWise 0.12.0: the tank was full at
68 °C at 13:13, then about 36 L were measured. The energy left, about
3.6 kWh above the mains water, gives a mixed temperature of 44.3 °C, but the
probe at the top reads 56.9 °C: some 60 L at about 57 °C sit above cold
water. Taking the useful energy above the comfort temperature of the mixed
tank gave 21 L available and a grid heating at 21:00; above the mains water
(times k_use) it is about 100 L at 40 °C.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.optimizer import Block, PlanInput, optimise
from tankwise_model.tank import (
    MIN_STRATIFICATION_SAMPLES,
    STRATIFIED_MAX_RATIO,
    TankModel,
    TankParams,
    TankSample,
)

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 23, 20, 45, tzinfo=TZ)
PARAMS = TankParams(
    volume_l=100,
    mains_temperature=13.5,
    comfort_min_temperature=38.0,
    shower_temperature=40.0,
    cutoff_temperature=68.0,
)
MIXED = 44.28  # °C, equivalent temperature of the energy left
PROBE = 56.875  # °C, the hot layer at the top
USABLE = 0.892


def _model(samples: list[tuple[float, float]]) -> TankModel:
    """The model of 20:45: calibrated, learned k_use and stratification."""
    data = TankModel().as_dict()
    data.update(
        equivalent_temperature=MIXED,
        usable_samples=[USABLE] * 10,
        cutoff_samples=[68.2, 68.5, 68.6],
        loss_windows=31,
        uncertain=False,
        calibration_count=1,
        last_valid_time=(NOW - timedelta(minutes=1)).isoformat(),
        stratification=[list(item) for item in samples],
        stratification_offset=PROBE - MIXED,
    )
    return TankModel.from_dict(data)


def _state(model: TankModel):
    return model.update(
        TankSample(
            timestamp=NOW,
            temperature=PROBE,
            power=0.0,
            energy=3395.3,
            hot_water_volume=351990.0,
            meter_configured=True,
        ),
        PARAMS,
    )


LEARNED = [(68.0 - index % 5, 0.45 + 0.01 * (index % 4)) for index in range(15)]


def test_stratified_tank_counts_the_energy_above_the_mains_water() -> None:
    state = _state(_model(LEARNED))
    assert state.stratified
    assert state.useful_floor_temperature == PARAMS.mains_temperature
    # Same energy as before: the mixed temperature is not changed.
    assert state.equivalent_temperature == pytest.approx(MIXED, abs=0.05)
    assert state.liters_theoretical == pytest.approx(116, abs=1)
    # k_use x energy above the mains water, in litres at 40 °C.
    assert 95 <= state.liters_available <= 105
    assert state.liters_available == pytest.approx(
        USABLE * state.liters_theoretical, rel=1e-3
    )
    assert state.useful_energy_kwh == pytest.approx(
        USABLE * state.stored_energy_kwh, rel=1e-3
    )
    # The hot layer, consistent with the probe.
    assert state.outlet_temperature == pytest.approx(PROBE, abs=1.0)


def test_without_learned_stratification_the_tank_is_mixed() -> None:
    """Fewer than three samples: the comfort limit of 0.12.0 (21 L)."""
    samples = LEARNED[: MIN_STRATIFICATION_SAMPLES - 1]
    state = _state(_model(samples))
    assert not state.stratified
    assert state.useful_floor_temperature == PARAMS.comfort_min_temperature
    assert state.liters_available == pytest.approx(21.1, abs=0.5)


def test_a_mixed_tank_measured_by_the_meter_stays_mixed() -> None:
    """Ratios near 1: the probe falls like a mixed tank, no stratification."""
    samples = [(65.0, 0.95)] * 10
    assert STRATIFIED_MAX_RATIO < 0.95
    state = _state(_model(samples))
    assert not state.stratified
    assert state.liters_available < 30


def test_a_mixed_tank_shows_its_temperature_as_outlet() -> None:
    model = _model([])
    data = model.as_dict()
    data["stratification_offset"] = 0.0
    state = _state(TankModel.from_dict(data))
    assert state.outlet_temperature == pytest.approx(state.equivalent_temperature)


# ---------------------------------------------------------------------------
# The plan of the evening
# ---------------------------------------------------------------------------

# P90 demand forecast published at 20:45 (local "HH:MM" of the 23rd and the
# 24th/25th as day offsets), kWh per slot.
FORECAST = {
    (0, "20:45"): 0.233, (0, "21:00"): 0.057, (0, "21:15"): 0.197,
    (0, "21:30"): 0.125, (0, "21:45"): 0.106, (0, "22:00"): 0.024,
    (0, "22:30"): 0.008, (0, "22:45"): 0.112, (0, "23:00"): 0.022,
    (0, "23:15"): 0.005, (0, "23:30"): 0.018, (0, "23:45"): 0.009,
    (1, "06:30"): 0.288, (1, "06:45"): 0.58, (1, "07:00"): 0.16,
    (1, "09:15"): 0.16, (1, "14:45"): 0.187, (1, "15:00"): 0.108,
    (1, "16:30"): 0.12, (1, "17:15"): 0.098, (1, "17:30"): 0.365,
    (1, "17:45"): 0.412, (1, "18:00"): 0.112, (1, "18:30"): 0.297,
    (1, "18:45"): 0.098, (1, "19:00"): 0.411, (1, "19:15"): 0.477,
    (1, "19:30"): 0.177, (1, "19:45"): 0.072, (1, "20:45"): 0.233,
    (1, "21:15"): 0.197, (1, "21:30"): 0.125, (1, "21:45"): 0.106,
    (1, "22:45"): 0.112, (2, "06:30"): 0.288, (2, "06:45"): 0.58,
    (2, "07:00"): 0.16,
}  # fmt: skip
SLOTS = 144


def _index(day: int, clock: str) -> int:
    hour, minute = (int(part) for part in clock.split(":"))
    moment = NOW.replace(hour=hour, minute=minute) + timedelta(days=day)
    return int((moment - NOW) / timedelta(minutes=15))


def _price(index: int) -> float:
    """Evening peak, cheap night, PVPC-like day (€/kWh)."""
    hour = (NOW + timedelta(minutes=15 * index)).hour
    if hour < 8:
        return 0.19
    if hour < 14:
        return 0.30
    if hour < 18:
        return 0.22
    return 0.375


def _liters_kwh(liters: float) -> float:
    return liters * 0.001163 * (PARAMS.shower_temperature - PARAMS.mains_temperature)


def _plan_input(floor: float) -> PlanInput:
    demand = [0.0] * SLOTS
    for (day, clock), kwh in FORECAST.items():
        demand[_index(day, clock)] = kwh
    morning, evening = _liters_kwh(40), _liters_kwh(60)
    blocks = [
        Block("m", "Mañana", _index(1, "06:30"), _index(1, "07:30"), morning, 1.03),
        Block(
            "t",
            "Tarde",
            _index(1, "17:00"),
            _index(1, "20:00"),
            evening,
            _liters_kwh(108.7),
        ),
        Block("m2", "Mañana", _index(2, "06:30"), _index(2, "07:30"), morning, 1.03),
    ]
    return PlanInput(
        start=NOW,
        initial_temperature=MIXED,
        volume_l=PARAMS.volume_l,
        mains_temperature=PARAMS.mains_temperature,
        ambient_temperature=20.0,
        comfort_temperature=floor,
        shower_temperature=PARAMS.shower_temperature,
        max_temperature=68.5,
        heater_power_w=1500,
        capacity_factor=0.9,
        usable_factor=USABLE,
        ua_w_per_k=1.0,
        buy=[_price(index) for index in range(SLOTS)],
        surplus_value=[0.05] * SLOTS,
        demand_kwh=demand,
        blocks=blocks,
        reserve_kwh=_liters_kwh(10),
    )


def _grid_tonight(floor: float) -> float:
    plan = optimise(_plan_input(floor))
    midnight = _index(1, "00:00")
    return sum(slot.grid_kwh for slot in plan.slots[:midnight])


def test_mixed_comfort_limit_heats_from_the_grid_tonight() -> None:
    """0.12.0: only 0.65 kWh useful above 38 °C for 0.92 kWh of P90 demand."""
    assert _grid_tonight(PARAMS.comfort_min_temperature) > 0.1


def test_stratified_tank_needs_no_grid_heating_tonight() -> None:
    state = _state(_model(LEARNED))
    plan = optimise(_plan_input(state.useful_floor_temperature))
    midnight = _index(1, "00:00")
    assert sum(slot.grid_kwh for slot in plan.slots[:midnight]) == 0
    assert not plan.at_risk
    # The night heating before the morning shower is still planned.
    assert sum(slot.grid_kwh for slot in plan.slots[midnight : _index(1, "06:30")]) > 0


def test_appointment_needs_the_mixed_temperature_of_its_useful_energy() -> None:
    """The evening block (108.7 L at 40 °C) is feasible below the cut-off."""
    from tankwise_model.appointments import required_temperature

    state = _state(_model(LEARNED))
    stratified = required_temperature(
        108.7,
        volume_l=100,
        comfort_temperature=state.useful_floor_temperature,
        shower_temperature=40,
        mains_temperature=13.5,
        usable_factor=USABLE,
    )
    assert stratified == pytest.approx(13.5 + 108.7 * 26.5 / (100 * USABLE))
    assert stratified < 46  # was 70.3 °C, above the cut-off, with 38 °C
    mixed = required_temperature(
        108.7,
        volume_l=100,
        comfort_temperature=38.0,
        shower_temperature=40,
        mains_temperature=13.5,
        usable_factor=USABLE,
    )
    assert mixed == pytest.approx(70.3, abs=0.1)
