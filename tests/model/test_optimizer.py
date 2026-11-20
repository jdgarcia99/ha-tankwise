"""Heating plan (DESIGN.md §7 and §13): closed cases, appointments and rescue."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import time as clock
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.optimizer import Block, PlanInput, optimise

TZ = ZoneInfo("Europe/Madrid")
START = datetime(2026, 11, 23, 0, 0, tzinfo=TZ)  # a Monday, 00:00
SLOTS = 96  # 24 hours


def _slot(hour: int, minute: int = 0) -> int:
    return hour * 4 + minute // 15


def _demand() -> list[float]:
    """1.2 kWh shower at 06:30 and 3 kWh of showers 18:00-19:30."""
    demand = [0.0] * SLOTS
    demand[_slot(6, 30)] = demand[_slot(6, 45)] = 0.6
    for index in range(_slot(18), _slot(19, 30)):
        demand[index] = 0.5
    return demand


def _input(**changes) -> PlanInput:
    data = PlanInput(
        start=START,
        initial_temperature=45.0,
        volume_l=100,
        mains_temperature=14.0,
        ambient_temperature=20.0,
        comfort_temperature=38.0,
        shower_temperature=40.0,
        max_temperature=68.0,
        heater_power_w=1500,
        capacity_factor=1.0,
        usable_factor=1.0,
        ua_w_per_k=1.0,
        buy=[0.20] * SLOTS,
        surplus_value=[0.10] * SLOTS,
        demand_kwh=_demand(),
    )
    return replace(data, **changes)


def _grid_between(plan, first: int, last: int) -> float:
    return sum(slot.grid_kwh for slot in plan.slots[first:last])


def _prices(base: float, **hours: float) -> list[float]:
    """Hourly prices; keyword h18=0.35 sets 18:00-18:59."""
    prices = [base] * SLOTS
    for key, value in hours.items():
        hour = int(key[1:])
        for index in range(hour * 4, hour * 4 + 4):
            prices[index] = value
    return prices


# ---------------------------------------------------------------------------
# §13 closed cases
# ---------------------------------------------------------------------------


def test_sunny_day_with_free_surplus_needs_no_grid() -> None:
    solar = [0.0] * SLOTS
    for index in range(_slot(10), _slot(16)):
        solar[index] = 0.375
    plan = optimise(
        _input(
            initial_temperature=56.0,
            solar_kwh=solar,
            surplus_value=[0.0] * SLOTS,
        )
    )
    assert plan.grid_kwh == pytest.approx(0.0, abs=1e-9)
    assert plan.slots[_slot(18)].temperature > 50
    assert not plan.at_risk


def test_cloudy_day_heats_at_the_cheapest_night_hours() -> None:
    prices = _prices(0.20, h1=0.09, h2=0.08, h3=0.08, h4=0.09)
    plan = optimise(_input(buy=prices))
    night = _grid_between(plan, _slot(1), _slot(5))
    morning = _grid_between(plan, 0, _slot(6, 30))
    assert morning > 0
    assert night == pytest.approx(morning)  # everything before 06:30 at night
    assert not plan.at_risk


def test_price_peak_at_18_preheats_before_17() -> None:
    prices = _prices(0.15, h17=0.35, h18=0.35, h19=0.35, h20=0.35)
    plan = optimise(_input(buy=prices, initial_temperature=60.0))
    assert _grid_between(plan, _slot(17), _slot(21)) == pytest.approx(0.0)
    assert _grid_between(plan, _slot(8), _slot(17)) > 0
    assert not plan.at_risk


def test_high_injection_price_sells_and_heats_at_night() -> None:
    """Surplus worth 0.20 €/kWh at noon, grid at 0.09: sell, heat with the grid."""
    solar = [0.0] * SLOTS
    for index in range(_slot(11), _slot(15)):
        solar[index] = 0.375
    prices = _prices(0.20, h1=0.09, h2=0.09, h3=0.09, h4=0.09, h15=0.09, h16=0.09)
    plan = optimise(_input(buy=prices, surplus_value=[0.20] * SLOTS, solar_kwh=solar))
    assert sum(slot.solar_kwh for slot in plan.slots) == pytest.approx(0.0)
    assert all(not slot.solar_allowed for slot in plan.slots[_slot(11) : _slot(15)])
    assert _grid_between(plan, _slot(1), _slot(5)) > 0
    assert _grid_between(plan, _slot(15), _slot(17)) > 0

    # Selling must pay more than the risk margin: at the same price as the
    # grid later, the plan takes the sun.
    even = _prices(0.20, h1=0.09, h2=0.09, h3=0.09, h4=0.09)
    plan = optimise(_input(buy=even, surplus_value=[0.20] * SLOTS, solar_kwh=solar))
    assert sum(slot.solar_kwh for slot in plan.slots) > 0

    # With cheap surplus (0.05 €/kWh) the plan takes the sun instead.
    cheap = optimise(_input(buy=prices, surplus_value=[0.05] * SLOTS, solar_kwh=solar))
    assert sum(slot.solar_kwh for slot in cheap.slots) > 0


# ---------------------------------------------------------------------------
# Appointments, rescue and risk
# ---------------------------------------------------------------------------


def _evening_block(demand_kwh: float, minimum_kwh: float = 0.0) -> Block:
    return Block(
        id="tarde",
        name="Tarde",
        first=_slot(17),
        last=_slot(20),
        minimum_kwh=minimum_kwh,
        demand_kwh=demand_kwh,
    )


def test_block_reached_with_the_required_temperature() -> None:
    """3 kWh block: 38 + 3 / 0.1163 = 63.8 °C at 17:00, no rescue."""
    plan = optimise(_input(blocks=[_evening_block(3.0)]))
    [block] = plan.blocks
    assert block.required_temperature == pytest.approx(38 + 3 / 0.1163, abs=0.01)
    assert block.predicted_temperature >= block.required_temperature - 0.5
    assert block.rescue_kwh == 0
    assert not plan.at_risk
    assert _grid_between(plan, _slot(17), _slot(20)) == 0


def test_block_beyond_a_full_tank_fills_up_and_rescues() -> None:
    """4.2 kWh block needs 74 °C > 68 °C: arrive full, rescue the deficit."""
    plan = optimise(_input(blocks=[_evening_block(4.2)]))
    [block] = plan.blocks
    assert block.required_temperature > 68
    assert block.predicted_temperature == pytest.approx(68.0, abs=0.6)
    assert 0 < block.rescue_kwh <= 4.2 - 0.1163 * 30 + 0.2
    assert not block.at_risk and not plan.at_risk
    rescue_slots = [index for index, slot in enumerate(plan.slots) if slot.rescue]
    assert rescue_slots
    assert all(_slot(17) <= index < _slot(20) for index in rescue_slots)
    # Never after the block.
    assert all(not slot.rescue for slot in plan.slots[_slot(20) :])
    assert plan.rescue_cost == pytest.approx(
        sum(slot.cost for slot in plan.slots if slot.rescue)
    )


def test_strict_mode_without_rescue_is_at_risk() -> None:
    plan = optimise(_input(blocks=[_evening_block(4.2)], rescue=False))
    [block] = plan.blocks
    assert block.rescue_kwh == 0
    assert block.at_risk and plan.at_risk
    assert _grid_between(plan, _slot(17), _slot(20)) == 0


def test_rescue_that_cannot_cover_the_block_is_at_risk() -> None:
    """9 kWh in 3 hours: a full tank plus 1.5 kW cannot make it."""
    plan = optimise(_input(blocks=[_evening_block(9.0)]))
    assert plan.blocks[0].at_risk
    assert plan.at_risk


def test_appointment_minimum_rules_when_above_the_usage() -> None:
    block = _evening_block(demand_kwh=1.0, minimum_kwh=3.0)
    plan = optimise(_input(blocks=[block]))
    assert plan.blocks[0].required_temperature == pytest.approx(
        38 + 3 / 0.1163, abs=0.01
    )


def test_not_enough_time_before_a_block_is_at_risk() -> None:
    """At 16:30 with 40 °C there is no time to reach 63.8 °C by 17:00."""
    start = START.replace(hour=16, minute=30)
    block = Block("tarde", "Tarde", first=2, last=14, minimum_kwh=0, demand_kwh=3.0)
    plan = optimise(
        _input(
            start=start,
            initial_temperature=40.0,
            buy=[0.2] * 32,
            surplus_value=[0.1] * 32,
            demand_kwh=[0.0] * 32,
            blocks=[block],
            rescue=False,
        )
    )
    assert plan.at_risk


def test_holiday_mode_uses_no_grid() -> None:
    plan = optimise(_input(allow_grid=False, demand_kwh=[0.0] * SLOTS))
    assert plan.grid_kwh == 0
    assert not plan.at_risk


def test_next_heating_and_runs() -> None:
    prices = _prices(0.20, h2=0.08, h3=0.08)
    plan = optimise(_input(buy=prices))
    heating = plan.next_heating()
    assert heating is not None
    assert heating.start.hour in (2, 3)
    assert heating.energy_kwh == pytest.approx(
        sum(run.energy_kwh for run in plan.heatings()[:1])
    )
    assert heating.cost == pytest.approx(heating.energy_kwh * 0.08)
    assert not heating.rescue


def test_terminal_value_keeps_energy_at_the_end() -> None:
    """Without a terminal value the plan would end with an empty tank."""
    plan = optimise(_input(demand_kwh=[0.0] * SLOTS, initial_temperature=60.0))
    assert plan.slots[-1].temperature > 50


def test_36_hour_plan_is_fast_enough() -> None:
    slots = 144
    data = _input(
        buy=[0.2] * slots,
        surplus_value=[0.1] * slots,
        demand_kwh=(_demand() + _demand())[:slots],
        solar_kwh=[0.2 if 40 <= index % 96 < 64 else 0.0 for index in range(slots)],
        blocks=[_evening_block(4.2)],
    )
    began = clock.perf_counter()
    plan = optimise(data)
    assert len(plan.slots) == slots
    assert clock.perf_counter() - began < 5


def test_grid_heating_runs_are_at_most_two_hours() -> None:
    """A cold tank and one cheap window: the run is split with pauses."""
    prices = [0.30] * SLOTS
    for index in range(_slot(1), _slot(6)):
        prices[index] = 0.08
    plan = optimise(_input(buy=prices, initial_temperature=26.0))
    run = longest = 0
    for slot in plan.slots:
        run = run + 1 if slot.grid_kwh > 0 else 0
        longest = max(longest, run)
    assert plan.grid_kwh > 0.375 * 8  # more than two hours of heating
    assert longest <= 8


def test_anti_legionella_target_is_reached_cheaply() -> None:
    """61.5 °C at 15:00: heated in the cheap 13:00-15:00 hours, not at risk."""
    # No demand and no value for the energy left at the end: without the
    # cycle nothing is heated.
    quiet = {"demand_kwh": [0.0] * SLOTS, "terminal_price": 0.0}
    data = _input(
        buy=_prices(0.20, h13=0.08, h14=0.08),
        legionella=(_slot(15), 61.5),
        **quiet,
    )
    plan = optimise(data)
    assert plan.legionella is not None
    moment, target, reached = plan.legionella
    assert moment.hour == 15 and target == 61.5
    assert reached >= 61.0
    assert not plan.at_risk
    assert _grid_between(plan, _slot(13), _slot(15)) > 1.5
    without = optimise(_input(buy=_prices(0.20, h13=0.08, h14=0.08), **quiet))
    assert without.legionella is None
    assert without.grid_kwh == 0
    assert _grid_between(plan, 0, _slot(13)) == 0  # not earlier than needed


def test_heating_already_done_counts_for_the_two_hour_limit() -> None:
    """After 90 minutes of heating, at most 30 more before a pause."""
    cold = _input(
        initial_temperature=30.0,
        buy=[0.05] * 8 + [0.30] * (SLOTS - 8),
        demand_kwh=[0.0] * 8 + [1.0] + [0.0] * (SLOTS - 9),
    )
    fresh = optimise(cold)
    assert all(slot.grid_kwh > 0 for slot in fresh.slots[:8])
    late = optimise(replace(cold, initial_run_slots=6))
    run = 0
    for slot in late.slots[:8]:
        if slot.grid_kwh <= 0:
            break
        run += 1
    assert run <= 2


# ---------------------------------------------------------------------------
# 0.10.1: stray small draws and the reasons of a risk
# ---------------------------------------------------------------------------

RESERVE_KWH = 0.3  # about 10 litres


def test_stray_small_draw_does_not_heat_at_an_expensive_hour() -> None:
    """23:15 at 37 °C, 5 L of P90 demand at 23:30, cheaper from 00:00."""
    start = datetime(2026, 11, 20, 23, 15, tzinfo=TZ)
    demand = [0.0] * SLOTS
    demand[1], demand[2] = 0.136, 0.022  # 23:30, 23:45
    buy = [0.26] * 3 + [0.12] * (SLOTS - 3)
    plan = optimise(
        _input(
            start=start,
            initial_temperature=37.3,
            buy=buy,
            demand_kwh=demand,
            reserve_kwh=RESERVE_KWH,
        )
    )
    assert _grid_between(plan, 0, 3) == 0  # nothing at 0.26 €/kWh
    assert not plan.at_risk
    assert plan.risks() == []


def test_real_draw_before_the_cheap_hours_keeps_the_reserve() -> None:
    """The same expensive hour, but a shower (1.2 kWh) before it gets cheaper."""
    start = datetime(2026, 11, 20, 23, 15, tzinfo=TZ)
    demand = [0.0] * SLOTS
    demand[2], demand[3] = 0.6, 0.6  # 23:45, 00:00
    buy = [0.26] * 6 + [0.12] * (SLOTS - 6)
    plan = optimise(
        _input(
            start=start,
            initial_temperature=37.3,
            buy=buy,
            demand_kwh=demand,
            reserve_kwh=RESERVE_KWH,
        )
    )
    assert _grid_between(plan, 0, 2) > 0


def test_risk_outside_a_block_is_explained() -> None:
    """Plan at risk with its blocks fine: the reason says where and why."""
    demand = [0.0] * SLOTS
    demand[_slot(0, 15)] = 1.5  # a big draw right away: impossible to cover
    plan = optimise(
        _input(
            initial_temperature=38.0,
            demand_kwh=demand,
            blocks=[Block("b", "Tarde", _slot(17), _slot(20), 0.5, 1.0)],
        )
    )
    assert plan.at_risk
    assert not any(block.at_risk for block in plan.blocks)
    risks = plan.risks()
    assert [risk.kind for risk in risks] == ["demand"]
    assert risks[0].start == plan.slots[_slot(0, 15)].start
    assert risks[0].block is None
    assert risks[0].shortfall_kwh > 0.05


def test_risk_inside_a_block_names_the_appointment() -> None:
    plan = optimise(
        _input(
            demand_kwh=[0.0] * SLOTS,
            blocks=[Block("b", "Tarde", _slot(18), _slot(21), 9.0, 9.0)],
            rescue=False,
        )
    )
    risks = plan.risks()
    assert plan.at_risk and risks
    assert {risk.kind for risk in risks} == {"appointment"}
    assert risks[0].block == "Tarde"
