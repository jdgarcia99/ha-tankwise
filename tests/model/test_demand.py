"""Synthetic-data tests for the demand model."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import random
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.demand import (
    DEFAULT_PROFILE,
    DayType,
    DemandModel,
    DemandStatus,
    FailureReason,
    _weighted_percentile,
    comfort_percentile,
    day_type_for,
    slot_of,
)

TZ = ZoneInfo("Europe/Madrid")
START = date(2026, 9, 7)  # a Monday
SHOWER = 1.8


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def day_type_of(day: date) -> DayType:
    return day_type_for(day)


def add_shower(
    model: DemandModel,
    moment: datetime,
    energy: float = SHOWER,
    useful_at_start: float = 1.7,
) -> None:
    model.add_draw(
        moment,
        energy,
        day_type_for(moment.date()),
        useful_at_start_kwh=useful_at_start,
        shower_energy_kwh=SHOWER,
    )


def household(
    model: DemandModel,
    first: date,
    days: int,
    *,
    seed: int = 1,
    evening: tuple[int, int] = (19, 0),
) -> date:
    """Simulate the reference household; return the day after the last one."""
    rng = random.Random(seed)
    day = first
    for _ in range(days):
        kind = day_type_for(day)
        model.observe(at(day, 12), kind)
        if kind is DayType.WORKDAY:
            add_shower(model, at(day, 6, rng.randint(0, 14)), 0.9)
        else:
            add_shower(model, at(day, 8, 30 + rng.randint(0, 14)))
        if rng.random() < 0.9:
            add_shower(model, at(day, evening[0], evening[1] + rng.randint(0, 14)))
        if rng.random() < 0.5:
            add_shower(model, at(day, 21, 30 + rng.randint(0, 14)), 1.5)
        day += timedelta(days=1)
    return day


def total(forecast) -> float:
    return sum(slot.energy_kwh for slot in forecast)


def test_day_types() -> None:
    monday, saturday, sunday = date(2026, 9, 7), date(2026, 9, 12), date(2026, 9, 13)
    assert day_type_for(monday) is DayType.WORKDAY
    assert day_type_for(monday, True) is DayType.WORKDAY
    assert day_type_for(monday, False) is DayType.SUNDAY_HOLIDAY
    assert day_type_for(saturday, False) is DayType.SATURDAY  # holiday Saturday
    assert day_type_for(sunday) is DayType.SUNDAY_HOLIDAY
    assert slot_of(at(monday, 6, 14)) == 24
    assert comfort_percentile("p95") == 0.95
    assert comfort_percentile("bogus") == 0.9


def test_profile_learns_the_household() -> None:
    model = DemandModel()
    today = household(model, START, 42)

    workday = model.slot_profile(DayType.WORKDAY, today)
    saturday = model.slot_profile(DayType.SATURDAY, today)
    assert workday[24].probability > 0.95  # 06:00
    assert workday[24].mean_energy_kwh == pytest.approx(0.9, rel=0.05)
    assert saturday[24].probability == 0.0
    assert saturday[34].probability > 0.95  # 08:30
    assert workday[76].probability > 0.7  # 19:00
    assert model.status(today) is DemandStatus.READY
    assert model.shower_energy(today) == pytest.approx(1.8, abs=0.31)


def test_learning_until_fourteen_days() -> None:
    model = DemandModel()
    today = household(model, START, 13)
    assert model.status(today) is DemandStatus.LEARNING
    assert model.days_with_data(today) == 13
    today = household(model, today, 1)
    assert model.status(today) is DemandStatus.READY


def test_comfort_level_raises_the_forecast() -> None:
    model = DemandModel()
    today = household(model, START, 42)
    now = at(today, 0)
    totals = [
        total(model.forecast(now, 24, comfort_percentile(level), day_type_of))
        for level in ("p80", "p90", "p95")
    ]
    assert totals[0] <= totals[1] <= totals[2]
    assert 2.5 < totals[1] < 5.5


def test_cumulative_percentile_matches_daily_demand() -> None:
    """With showers spread over the evening, per-slot percentiles miss the
    daily demand; the percentile of cumulative curves follows it."""
    rng = random.Random(3)
    model = DemandModel()
    day = START
    for _ in range(42):
        kind = day_type_for(day)
        model.observe(at(day, 12), kind)
        if kind is DayType.WORKDAY:
            minute = rng.randint(17 * 60, 22 * 60 + 30)
            add_shower(model, at(day, minute // 60, minute % 60))
        day += timedelta(days=1)
    today = day
    while today.weekday() != 0:
        today += timedelta(days=1)

    forecast_total = total(model.forecast(at(today, 0), 24, 0.9, day_type_of))
    days = model._day_vectors(DayType.WORKDAY, today)
    per_slot_sum = sum(
        _weighted_percentile([(vec[slot], w) for w, vec in days], 0.9)
        for slot in range(96)
    )
    daily_p90 = _weighted_percentile([(sum(vec), w) for w, vec in days], 0.9)
    assert daily_p90 == pytest.approx(SHOWER)
    assert forecast_total == pytest.approx(daily_p90, rel=0.05)
    assert abs(per_slot_sum - daily_p90) > 1.0


def test_recent_weeks_weigh_more() -> None:
    """After a change of habit, the profile follows the recent weeks."""
    model = DemandModel()
    today = household(model, START, 28, evening=(19, 0))
    today = household(model, today, 21, seed=2, evening=(22, 0))
    profile = model.slot_profile(DayType.WORKDAY, today)
    assert profile[88].probability > profile[76].probability  # 22:00 vs 19:00


def test_strong_shower_is_not_a_failure() -> None:
    """A normal strong shower starts with enough energy (46 → 30 °C)."""
    model = DemandModel()
    draw = model.add_draw(
        at(START, 20),
        1.8,
        DayType.WORKDAY,
        useful_at_start_kwh=1.63,  # probe at 46 °C, minimum useful 32 °C
        shower_energy_kwh=SHOWER,
    )
    assert not draw.failure


def test_draw_started_without_energy_is_a_failure() -> None:
    model = DemandModel()
    draw = model.add_draw(
        at(START, 20),
        0.6,
        DayType.WORKDAY,
        useful_at_start_kwh=0.2,  # probe already below ~34 °C
        shower_energy_kwh=SHOWER,
    )
    assert draw.failure


def test_failures_raise_the_forecast_of_their_day() -> None:
    def build(fail: bool) -> tuple[DemandModel, date]:
        model = DemandModel()
        today = household(model, START, 28)
        # Last week: late evening showers, started with an empty tank.
        for offset in range(1, 8):
            day = today - timedelta(days=offset)
            add_shower(model, at(day, 23), 1.2, useful_at_start=0.1 if fail else 1.5)
        return model, today

    normal, today = build(False)
    failing, _ = build(True)
    now = at(today, 22)
    assert total(failing.forecast(now, 2, 0.9, day_type_of)) > total(
        normal.forecast(now, 2, 0.9, day_type_of)
    )


def test_manual_mode_failure_links_to_next_draw() -> None:
    model = DemandModel()
    model.add_failure(at(START, 7), DayType.WORKDAY, FailureReason.MANUAL_MODE)
    linked = model.add_draw(
        at(START, 8, 30),
        1.5,
        DayType.WORKDAY,
        useful_at_start_kwh=1.7,
        shower_energy_kwh=SHOWER,
    )
    later = model.add_draw(
        at(START, 11),
        1.5,
        DayType.WORKDAY,
        useful_at_start_kwh=1.7,
        shower_energy_kwh=SHOWER,
    )
    assert linked.failure
    assert not later.failure
    assert model.failures[0].linked


def test_holiday_uses_sunday_profile() -> None:
    model = DemandModel()
    today = household(model, START, 42)
    while today.weekday() != 0:
        today += timedelta(days=1)

    def with_holiday(day: date) -> DayType:
        return day_type_for(day, False if day == today else None)

    workday = model.next_draw(at(today, 5), 0.9, day_type_of)
    holiday = model.next_draw(at(today, 5), 0.9, with_holiday)
    assert workday is not None
    assert holiday is not None
    assert workday.start.hour == 6
    assert workday.day_type is DayType.WORKDAY
    assert holiday.start.hour == 8
    assert holiday.day_type is DayType.SUNDAY_HOLIDAY


def test_cold_start_uses_default_profile() -> None:
    model = DemandModel()
    monday = START
    nxt = model.next_draw(at(monday, 5), 0.9, day_type_of)
    assert nxt is not None
    assert nxt.start == at(monday, 6)
    assert nxt.energy_kwh == pytest.approx(0.9)
    forecast = model.forecast(at(monday, 0), 24, 0.9, day_type_of)
    assert total(forecast) == pytest.approx(sum(DEFAULT_PROFILE[DayType.WORKDAY]))


def test_cold_start_blends_towards_learned_profile() -> None:
    """With 7 of 14 days, the forecast is halfway to the learned one."""
    model = DemandModel()
    today = household(model, START, 7)
    learned_only = DemandModel(min_days=1)
    household(learned_only, START, 7)
    now = at(today, 0)
    default = sum(DEFAULT_PROFILE[day_type_for(today)])
    learned = total(learned_only.forecast(now, 24, 0.9, day_type_of))
    blended = total(model.forecast(now, 24, 0.9, day_type_of))
    assert blended == pytest.approx((default + learned) / 2, rel=0.05)


def test_next_draw_none_without_demand() -> None:
    model = DemandModel(min_days=1)
    for offset in range(20):
        day = START + timedelta(days=offset)
        model.observe(at(day, 12), day_type_for(day))
    assert model.next_draw(at(START + timedelta(days=20), 0), 0.9, day_type_of) is None


@pytest.mark.parametrize(
    ("day", "slots"),
    [(date(2026, 10, 25), 100), (date(2026, 3, 29), 92), (date(2026, 9, 8), 96)],
)
def test_daylight_saving_days(day: date, slots: int) -> None:
    model = DemandModel()
    household(model, day - timedelta(days=30), 30)
    start = at(day, 0)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=TZ)
    hours = (end - start).total_seconds() / 3600
    forecast = model.forecast(start, hours, 0.9, day_type_of)
    assert len(forecast) == slots
    utc = [slot.start.timestamp() for slot in forecast]
    assert utc == sorted(utc)
    assert len(set(utc)) == len(utc)


def test_history_is_pruned_after_ninety_days() -> None:
    model = DemandModel()
    today = household(model, START, 120)
    model.observe(at(today, 12), day_type_for(today))
    oldest = min(draw.start.date() for draw in model.draws)
    assert (today - oldest).days <= 90
    assert model.days_with_data(today) <= 90


def test_round_trip() -> None:
    model = DemandModel()
    today = household(model, START, 20)
    model.add_failure(at(today, 7), DayType.WORKDAY, FailureReason.MANUAL_MODE)
    restored = DemandModel.from_dict(model.as_dict())
    assert restored.as_dict() == model.as_dict()
    now = at(today, 0)
    assert total(restored.forecast(now, 24, 0.9, day_type_of)) == pytest.approx(
        total(model.forecast(now, 24, 0.9, day_type_of))
    )


@pytest.mark.parametrize(
    "data",
    [{}, {"format": 2}, {"format": 1, "observed_days": {"x": "workday"}}],
)
def test_invalid_data(data: dict) -> None:
    with pytest.raises(ValueError):
        DemandModel.from_dict(data)
