"""Optimiser input from the models: modes and appointment blocks."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from tankwise_model.appointments import Appointment
from tankwise_model.demand import DayTypeMode, DemandModel, DemandParams
from tankwise_model.optimizer import optimise
from tankwise_model.planning import Mode, appointment_blocks, build_plan_input
from tankwise_model.prices import SlotPrices

from .household import TZ, feed, household

FIRST = date(2026, 10, 5)
DAYS7 = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
MORNING = Appointment("m", "Mañana", time(6, 30), frozenset({"workday"}), 40)
EVENING = Appointment("t", "Tarde", time(17), DAYS7, 0, duration=timedelta(hours=3))


@pytest.fixture(scope="module")
def trained() -> tuple[DemandModel, datetime]:
    model = DemandModel(TZ)
    now = feed(model, FIRST, 28, household(FIRST, 28))
    return model, now


def _prices(start: datetime, slots: int = 144) -> SlotPrices:
    return SlotPrices(start, tuple([0.2] * slots), tuple([0.1] * slots), True)


def _input(
    trained,
    mode: Mode,
    *,
    start: datetime | None = None,
    occupied=True,
    appointments=(MORNING, EVENING),
    rescue=True,
):
    model, now = trained
    start = start or datetime.combine(now.date() + timedelta(days=1), time(0), TZ)
    forecast = model.forecast(start, DemandParams(mode=DayTypeMode.ALL), hours=36)
    return build_plan_input(
        start=start,
        tz=TZ,
        mode=mode,
        temperature=45.0,
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
        prices=_prices(start),
        forecast=forecast,
        appointments=appointments,
        workdays={},
        occupied=occupied,
        reserve_liters=10,
        rescue=rescue,
    )


def test_modes_change_the_demand(trained) -> None:
    auto = _input(trained, Mode.AUTO)
    eco = _input(trained, Mode.ECO)
    comfort = _input(trained, Mode.COMFORT)
    assert sum(eco.demand_kwh) < sum(auto.demand_kwh)
    assert sum(comfort.demand_kwh) == pytest.approx(sum(auto.demand_kwh) * 1.2)
    assert _input(trained, Mode.OFF) is None
    holiday = _input(trained, Mode.HOLIDAY)
    assert not holiday.allow_grid
    assert sum(holiday.demand_kwh) == 0
    assert list(holiday.blocks) == []


def test_blocks_of_the_horizon(trained) -> None:
    data = _input(trained, Mode.AUTO)
    names = [(block.name, block.first, block.last) for block in data.blocks]
    # Starting at 00:00 of a day: 06:30 (if workday), 17:00, then next day's.
    assert ("Tarde", 68, 80) in names
    evening = next(block for block in data.blocks if block.first == 68)
    assert evening.demand_kwh > 2.0  # strong-day quantile of the 3-hour block
    assert evening.minimum_kwh == 0


def test_block_in_progress_keeps_only_the_demand_left(trained) -> None:
    _, now = trained
    start = datetime.combine(now.date() + timedelta(days=1), time(18), TZ)
    data = _input(trained, Mode.AUTO, start=start)
    current = [block for block in data.blocks if block.first == -1]
    assert len(current) == 1 and current[0].last == 8
    assert current[0].minimum_kwh == 0


def test_nobody_home_suspends_appointments(trained) -> None:
    data = _input(trained, Mode.AUTO, occupied=False)
    assert data.blocks == [] and sum(data.demand_kwh) == 0 and data.reserve_kwh == 0


def test_workday_appointment_skips_weekends(trained) -> None:
    saturday = datetime(2026, 11, 21, 0, 0, tzinfo=TZ)
    model, _ = trained
    blocks = appointment_blocks(
        [MORNING],
        start=saturday,
        slots=48 * 4,
        tz=TZ,
        forecast=model.forecast(saturday, DemandParams(), hours=48),
        workdays={},
        mode=Mode.AUTO,
        shower_temperature=40,
        mains_temperature=14,
    )
    assert [block.id.split("@")[1][:10] for block in blocks] == []  # Sat and Sun


def test_plan_from_the_household(trained) -> None:
    plan = optimise(_input(trained, Mode.AUTO))
    assert plan.grid_kwh > 0
    evening = [block for block in plan.blocks if block.name == "Tarde"]
    assert evening and not plan.at_risk
