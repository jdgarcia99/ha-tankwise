"""Demand profile and forecast (DESIGN.md §5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
import json

import pytest
from tankwise_model.demand import (
    SLOTS_PER_DAY,
    DayTypeMode,
    DemandModel,
    DemandParams,
    DemandStatus,
    ShortageMark,
    day_type,
    spread_draws,
    weighted_quantile,
)

from .household import (
    EVENING_KWH,
    MORNING_KWH,
    TZ,
    feed,
    household,
    metered,
    shower,
)

FIRST = date(2026, 10, 5)  # a Monday
PARAMS = DemandParams(mode=DayTypeMode.ALL)


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), TZ)


def _trained(days: int = 28, params_seed: int = 1) -> tuple[DemandModel, datetime]:
    model = DemandModel(TZ)
    now = feed(model, FIRST, days, household(FIRST, days, params_seed))
    return model, now


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def test_weighted_quantile() -> None:
    assert weighted_quantile([], [], 0.5) == 0.0
    assert weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.5) == 2
    assert weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.9) == 4
    assert weighted_quantile([1, 10], [9, 1], 0.5) == 1
    assert weighted_quantile([1, 10], [1, 9], 0.5) == 10


def test_day_types() -> None:
    monday, saturday = date(2026, 11, 16), date(2026, 11, 21)
    assert day_type(monday, DayTypeMode.WEEKDAY) == "mon"
    assert day_type(monday, DayTypeMode.WORKDAY_WEEKEND) == "workday"
    assert day_type(saturday, DayTypeMode.WORKDAY_WEEKEND) == "weekend"
    assert day_type(saturday, DayTypeMode.ALL) == "all"
    # A holiday on a Monday (workday sensor off) behaves like a Sunday.
    assert day_type(monday, DayTypeMode.WEEKDAY, workday=False) == "sun"
    assert day_type(monday, DayTypeMode.WORKDAY_WEEKEND, workday=False) == "weekend"
    assert day_type(saturday, DayTypeMode.WORKDAY_WEEKEND, workday=True) == "workday"


def test_spread_draws_over_slots() -> None:
    day = date(2026, 11, 16)
    slots = spread_draws(day, [shower(_at(day, 6, 25), 1.0)], TZ)  # 06:25-06:35
    assert len(slots) == SLOTS_PER_DAY
    assert slots[25] == pytest.approx(0.5)  # 06:15-06:30
    assert slots[26] == pytest.approx(0.5)  # 06:30-06:45
    assert sum(slots) == pytest.approx(1.0)
    # A draw of another day is ignored.
    assert (
        sum(spread_draws(day, [shower(_at(day, 6) + timedelta(days=1), 1.0)], TZ)) == 0
    )


# ---------------------------------------------------------------------------
# Observed days
# ---------------------------------------------------------------------------


def test_complete_days_are_observed() -> None:
    model, _ = _trained(days=3)
    observations = model.observations
    assert [obs.day for obs in observations] == [
        FIRST + timedelta(days=i) for i in range(3)
    ]
    assert observations[0].total == pytest.approx(
        MORNING_KWH + EVENING_KWH * 2, abs=1.4
    )
    assert model.status() is DemandStatus.LEARNING


def test_partial_and_unoccupied_days_are_not_observed() -> None:
    model = DemandModel(TZ)
    second = FIRST + timedelta(days=1)
    third = FIRST + timedelta(days=2)
    feed(
        model,
        FIRST,
        3,
        household(FIRST, 3),
        available=lambda moment: moment.date() != FIRST or moment.hour >= 12,
        occupied=lambda moment: moment.date() != second,
    )
    assert [obs.day for obs in model.observations] == [third]


def test_unknown_occupancy_counts_as_occupied() -> None:
    model = DemandModel(TZ)
    feed(model, FIRST, 2, household(FIRST, 2), occupied=lambda moment: None)
    assert len(model.observations) == 2


# ---------------------------------------------------------------------------
# Forecast: the reference household
# ---------------------------------------------------------------------------


def test_morning_shower_is_forecast_at_0630() -> None:
    model, now = _trained()
    day = (now + timedelta(days=1)).date()
    forecast = model.forecast(_at(day, 0), PARAMS, hours=24)
    morning = forecast.window(_at(day, 6), _at(day, 7), 0.5)
    assert morning == pytest.approx(MORNING_KWH, rel=0.05)
    # Nothing between 08:00 and 16:00.
    assert forecast.window(_at(day, 8), _at(day, 16), 0.9) == 0
    # The slots share the mean shape (no jitter loss), scaled to the daily
    # quantiles: the P90 of the slots covers the shower, the P50 is below.
    slots = [s for s in forecast.slots if _at(day, 6) <= s.start < _at(day, 7)]
    assert sum(s.p90 for s in slots) >= MORNING_KWH
    assert 0 < sum(s.p50 for s in slots) <= sum(s.p90 for s in slots)


def test_evening_block_p50_is_a_normal_day_and_p90_a_strong_day() -> None:
    model, now = _trained()
    day = (now + timedelta(days=1)).date()
    forecast = model.forecast(_at(day, 0), PARAMS, hours=24)
    evening_p50 = forecast.window(_at(day, 17), _at(day, 19, 30), 0.5)
    evening_p90 = forecast.window(_at(day, 17), _at(day, 19, 30), 0.9)
    # Normal days have one shower, strong days two or three.
    assert EVENING_KWH * 0.9 <= evening_p50 <= EVENING_KWH * 2.1
    assert evening_p90 == pytest.approx(EVENING_KWH * 3, rel=0.1)


def test_daily_p90_is_not_inflated_by_per_slot_quantiles() -> None:
    """The P90 of the day is a strong day, not the sum of per-slot P90."""
    model, now = _trained()
    day = (now + timedelta(days=1)).date()
    p50, p90 = model.day_totals(day, now, PARAMS)
    strong_day = MORNING_KWH + EVENING_KWH * 3
    assert p90 == pytest.approx(strong_day, rel=0.1)
    assert p50 < p90

    observations = model.similar_days(day, PARAMS)
    weights = [model.weight(obs, now, PARAMS) for obs in observations]
    naive = sum(
        weighted_quantile([obs.slots[i] for obs in observations], weights, 0.9)
        for i in range(SLOTS_PER_DAY)
    )
    assert naive > p90 * 1.5  # what a per-slot P90 would have planned

    forecast = model.forecast(_at(day, 0), PARAMS, hours=24)
    assert sum(slot.p90 for slot in forecast.slots) == pytest.approx(p90, rel=1e-6)


def test_more_history_stabilises_the_p90() -> None:
    """With 8 days the P90 is the single highest day; with 16 it is steadier."""
    totals = {}
    for observations in (8, 16):
        params = replace(PARAMS, observations=observations)
        values = []
        for seed in range(1, 9):
            model, now = _trained(days=30, params_seed=seed)
            values.append(
                model.day_totals((now + timedelta(days=1)).date(), now, params)[1]
            )
        mean = sum(values) / len(values)
        totals[observations] = (max(values) - min(values)) / mean
    assert totals[16] <= totals[8]


def test_forecast_horizon_is_36_hours_of_15_minute_slots() -> None:
    model, now = _trained(days=10)
    forecast = model.forecast(now, PARAMS)
    assert len(forecast.slots) == 144
    assert forecast.slots[0].start.minute % 15 == 0
    assert forecast.end - forecast.start == timedelta(hours=36)
    assert forecast.total(0.9) >= forecast.total(0.5) > 0


def test_unoccupied_home_has_no_demand() -> None:
    model, now = _trained(days=10)
    forecast = model.forecast(now, PARAMS, occupied=False)
    assert all(slot.p90 == 0 for slot in forecast.slots)
    assert forecast.total() == 0


def test_change_of_routine_is_followed() -> None:
    """The morning shower moves from 06:30 to 07:30; recent days dominate."""
    model = DemandModel(TZ)
    old = household(FIRST, 21)
    moved_first = FIRST + timedelta(days=21)
    moved = [
        replace(d, start=d.start + timedelta(hours=1), end=d.end + timedelta(hours=1))
        if d.start.hour < 12
        else d
        for d in household(moved_first, 14, seed=2)
    ]
    now = feed(model, FIRST, 35, [*old, *moved])
    day = (now + timedelta(days=1)).date()
    forecast = model.forecast(_at(day, 0), replace(PARAMS, observations=16), hours=24)
    assert forecast.window(_at(day, 7), _at(day, 8), 0.5) == pytest.approx(
        MORNING_KWH, rel=0.1
    )
    assert forecast.window(_at(day, 6), _at(day, 7), 0.5) == 0


def test_shortage_raises_the_forecast_and_fades_out() -> None:
    model, now = _trained(days=14)
    day = (now + timedelta(days=1)).date()
    before = model.forecast(_at(day, 0), PARAMS, hours=24).window(
        _at(day, 6), _at(day, 7), 0.5
    )

    # A shortage at 06:30 yesterday.
    yesterday = now.date()
    model.update(
        now,
        available=True,
        occupied=True,
        draws=[shower(_at(yesterday, 6, 30) + timedelta(days=0), 1.4, shortage=True)],
    )
    assert len(model.marks) == 1
    after = model.forecast(_at(day, 0), PARAMS, hours=24).window(
        _at(day, 6), _at(day, 7), 0.5
    )
    assert after == pytest.approx(before * 1.2, rel=0.03)

    later = now + timedelta(days=15)
    faded = model.forecast(later, PARAMS, hours=24)
    assert model.slot_factors(later) == [1.0] * SLOTS_PER_DAY
    assert faded.total() >= 0


def test_shortages_at_the_same_time_do_not_compound() -> None:
    """0.10.0: 12 shortages around 18:00 raised the evening block by 1.5x."""
    model, now = _trained(days=14)
    day = (now + timedelta(days=1)).date()
    before = model.forecast(_at(day, 0), PARAMS, hours=24).window(
        _at(day, 6), _at(day, 7), 0.5
    )
    for days_ago in range(4):
        moment = _at(now.date() - timedelta(days=days_ago), 6, 30 + 5 * days_ago)
        model.add_mark(ShortageMark(moment, moment + timedelta(minutes=15)))
    factors = model.slot_factors(now)
    assert max(factors) <= 1.2
    after = model.forecast(_at(day, 0), PARAMS, hours=24).window(
        _at(day, 6), _at(day, 7), 0.5
    )
    assert after <= before * 1.2 + 1e-9


def test_day_types_with_workday_sensor() -> None:
    """Workdays shower at 06:30, holidays (and weekends) at 09:00."""
    model = DemandModel(TZ)
    holiday = date(2026, 10, 12)  # a Monday holiday
    draws = []
    for offset in range(28):
        day = FIRST + timedelta(days=offset)
        working = day.weekday() < 5 and day != holiday
        start = _at(day, 6, 30) if working else _at(day, 9)
        draws.append(shower(start, MORNING_KWH))
    now = feed(
        model,
        FIRST,
        28,
        draws,
        workday=lambda day: day.weekday() < 5 and day != holiday,
    )
    params = DemandParams(mode=DayTypeMode.WORKDAY_WEEKEND)
    tomorrow = (now + timedelta(days=1)).date()
    # Tell the forecast that tomorrow is a holiday, whatever weekday it is.
    forecast = model.forecast(
        _at(tomorrow, 0), params, hours=24, workdays={tomorrow: False}
    )
    assert forecast.window(_at(tomorrow, 9), _at(tomorrow, 10), 0.5) == pytest.approx(
        MORNING_KWH, rel=0.05
    )
    assert forecast.window(_at(tomorrow, 6), _at(tomorrow, 7), 0.9) == 0
    assert model.observations[7].workday is False  # the holiday was recorded


def test_few_days_of_a_type_use_all_days() -> None:
    model, _ = _trained(days=4)  # Monday-Thursday only
    params = DemandParams(mode=DayTypeMode.WORKDAY_WEEKEND)
    saturday = date(2026, 10, 10)
    assert len(model.similar_days(saturday, params)) == 4


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_round_trip() -> None:
    model, now = _trained(days=9)
    model.update(
        now,
        available=True,
        occupied=True,
        draws=[shower(now - timedelta(hours=1), 1.0, shortage=True)],
    )
    data = json.loads(json.dumps(model.as_dict()))
    restored = DemandModel.from_dict(data, TZ)
    assert restored.as_dict() == model.as_dict()
    assert restored.status() is DemandStatus.READY


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"format": 2},
        {
            "format": 1,
            "observations": [{"day": "2026-11-01", "slots": [0.0]}],
            "marks": [],
            "coverage": {},
            "last_update": None,
            "last_draw_start": None,
        },
    ],
)
def test_invalid_data(data: dict) -> None:
    with pytest.raises(ValueError):
        DemandModel.from_dict(data, TZ)


def test_reset() -> None:
    model, _ = _trained(days=3)
    model.reset()
    assert model.observations == []


# ---------------------------------------------------------------------------
# 0.10.2: slot quantiles from the mean shape (P90 never below P50)
# ---------------------------------------------------------------------------


def _profile_heavy_days_shower_earlier() -> tuple[DemandModel, datetime]:
    """Light days shower at 19:30, heavy days at 18:00 and 18:30 (and more)."""
    days = []
    for offset in range(12):
        day = FIRST + timedelta(days=offset)
        slots = [0.0] * SLOTS_PER_DAY
        slots[_index(6, 30)] = 0.6
        if offset % 3 == 0:  # heavy: two early showers
            slots[_index(18)] = 1.4
            slots[_index(18, 30)] = 1.4
        else:  # light: one late shower
            slots[_index(19, 30)] = 1.2
        days.append({"day": day.isoformat(), "workday": None, "slots": slots})
    model = DemandModel.from_dict(
        {
            "format": 1,
            "observations": days,
            "marks": [],
            "coverage": {},
            "last_update": None,
            "last_draw_start": None,
        },
        TZ,
    )
    return model, _at(FIRST + timedelta(days=12), 0)


def _index(hour: int, minute: int = 0) -> int:
    return hour * 4 + minute // 15


def _old_daily_totals(model: DemandModel, now: datetime) -> tuple[float, float]:
    """0.10.1: the end point of the quantile of the cumulative curves."""
    day = now.date()
    rows = [list(obs.slots) for obs in model.similar_days(day, PARAMS)]
    weights = [
        model.weight(obs, now, PARAMS) for obs in model.similar_days(day, PARAMS)
    ]
    totals = [sum(row) for row in rows]
    return (
        weighted_quantile(totals, weights, 0.5),
        weighted_quantile(totals, weights, 0.9),
    )


def test_p90_is_never_below_p50() -> None:
    model, now = _profile_heavy_days_shower_earlier()
    forecast = model.forecast(now, PARAMS, hours=24)
    for slot in forecast.slots:
        assert slot.p90 >= slot.p50 - 1e-12, slot.start
    p50 = [slot.p50 for slot in forecast.slots]
    p90 = [slot.p90 for slot in forecast.slots]
    for first in range(0, SLOTS_PER_DAY, 4):
        for last in range(first + 1, SLOTS_PER_DAY + 1, 3):
            assert sum(p90[first:last]) >= sum(p50[first:last]) - 1e-9
    # The case of the report: 19:30 had P50 > 0 and P90 = 0.
    assert p90[_index(19, 30)] > 0
    evening = slice(_index(17), SLOTS_PER_DAY)
    assert sum(p90[evening]) >= sum(p50[evening])


def test_daily_totals_are_unchanged() -> None:
    model, now = _profile_heavy_days_shower_earlier()
    p50, p90 = model.day_totals(now.date(), now, PARAMS)
    old = _old_daily_totals(model, now)
    assert (p50, p90) == pytest.approx(old)
    forecast = model.forecast(now, PARAMS, hours=24)
    assert sum(slot.p50 for slot in forecast.slots) == pytest.approx(p50)
    assert sum(slot.p90 for slot in forecast.slots) == pytest.approx(p90)
    # A window of the whole day gives the same totals.
    end = now + timedelta(days=1)
    assert forecast.window(now, end, 0.5) == pytest.approx(p50)
    assert forecast.window(now, end, 0.9) == pytest.approx(p90)


def test_p90_peaks_when_p50_peaks() -> None:
    model, now = _profile_heavy_days_shower_earlier()
    forecast = model.forecast(now, PARAMS, hours=24)
    p50 = [slot.p50 for slot in forecast.slots]
    p90 = [slot.p90 for slot in forecast.slots]
    assert p50.index(max(p50)) == p90.index(max(p90))


def test_windows_keep_p90_above_p50() -> None:
    """Demand left today and appointment blocks: quantiles of window totals."""
    model, now = _profile_heavy_days_shower_earlier()
    forecast = model.forecast(now, PARAMS, hours=24)
    for hour in range(0, 24, 2):
        start = now + timedelta(hours=hour)
        end = now + timedelta(days=1)
        assert forecast.window(start, end, 0.9) >= forecast.window(start, end, 0.5)


# ---------------------------------------------------------------------------
# Litres (hot water meter)
# ---------------------------------------------------------------------------


def _litres_of(kwh: float) -> float:
    return round(kwh / (0.001163 * (60.0 - 14.0)), 1)


def test_metered_days_keep_the_litres_of_each_slot() -> None:
    model = DemandModel(TZ)
    draws = metered(household(FIRST, 10))
    now = feed(model, FIRST, 10, draws, metered=lambda moment: True)
    [first, *_] = model.observations
    assert first.liters is not None
    day_draws = [d for d in draws if d.start.astimezone(TZ).date() == first.day]
    assert first.total_liters == pytest.approx(sum(d.liters or 0 for d in day_draws))
    # The morning shower: litres in the slots around 06:30, like the energy.
    morning = first.window_liters(24, 30)
    assert morning == pytest.approx(_litres_of(MORNING_KWH))
    p50, p90 = model.day_liters(FIRST + timedelta(days=10), now, PARAMS) or (0, 0)
    assert 0 < p50 <= p90
    totals = [obs.total_liters for obs in model.observations]
    assert min(totals) <= p50 <= max(totals)  # type: ignore[type-var]


def test_days_without_a_meter_have_no_litres() -> None:
    model = DemandModel(TZ)
    now = feed(model, FIRST, 5, household(FIRST, 5))
    assert all(obs.liters is None for obs in model.observations)
    assert model.day_liters(FIRST + timedelta(days=5), now, PARAMS) is None
    assert model.observations[0].window_liters(0, 96) is None


def test_a_day_partly_metered_has_no_litres() -> None:
    """The meter was added at noon: the litres of that day are incomplete."""
    model = DemandModel(TZ)
    added = datetime.combine(FIRST, time(12), TZ)
    feed(
        model,
        FIRST,
        2,
        metered(household(FIRST, 2)),
        metered=lambda moment: moment >= added,
    )
    first, second = model.observations
    assert first.liters is None
    assert second.liters is not None


def test_litres_round_trip() -> None:
    model = DemandModel(TZ)
    feed(model, FIRST, 3, metered(household(FIRST, 3)), metered=lambda moment: True)
    restored = DemandModel.from_dict(json.loads(json.dumps(model.as_dict())), TZ)
    assert [obs.liters for obs in restored.observations] == [
        tuple(round(value, 2) for value in obs.liters or ())
        for obs in model.observations
    ]
    # Profiles saved by 0.11 have no litres.
    data = model.as_dict()
    for item in data["observations"]:
        del item["liters"]
    for item in data["coverage"].values():
        del item["metered"]
    old = DemandModel.from_dict(data, TZ)
    assert all(obs.liters is None for obs in old.observations)
