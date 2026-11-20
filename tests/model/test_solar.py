"""Expected solar surplus: forecasts, learned base load and plan slots."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.solar import (
    DEFAULT_BASE_LOAD_KWH,
    SolarModel,
    extend_forecast,
    slot_surplus,
    sum_forecasts,
)

TZ = ZoneInfo("Europe/Madrid")


def _at(hour: int, minute: int = 0, day: int = 18) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_period_end_forecasts_are_summed_per_hour() -> None:
    """Forecast.Solar: 08:00 holds 07:00-08:00; the sunrise value is 0."""
    plane_1 = {
        "2026-11-18T07:44:00+01:00": 0,
        "2026-11-18T08:00:00+01:00": 120,
        "2026-11-18T09:00:00+01:00": 500,
    }
    plane_2 = {"2026-11-18T08:00:00+01:00": 80, "2026-11-18T09:00:00+01:00": 300}
    starts = {"2026-11-18T07:00:00+01:00": 50, "2026-11-18T08:00:00+01:00": 100}
    total = sum_forecasts([(plane_1, True), (plane_2, True), (starts, False)], TZ)
    assert total[_at(7)] == pytest.approx(0.25)  # 0.12 + 0.08 + 0.05
    assert total[_at(8)] == pytest.approx(0.9)  # 0.5 + 0.3 + 0.1


def _hour(
    model: SolarModel,
    hour: int,
    *,
    forecast: float,
    export: float,
    heater: float,
    day: int = 18,
    total: float = 100.0,
) -> float:
    """Feed a whole hour, one sample a minute; return the export meter."""
    for minute in range(60):
        model.observe(
            _at(hour, minute, day),
            forecast_kwh=forecast,
            export_total_kwh=total + export * minute / 60,
            heater_solar_kwh=heater / 60,
        )
    return total + export


def test_base_load_is_learned_from_export_and_heater() -> None:
    model = SolarModel(TZ)
    assert model.base_load(12) == DEFAULT_BASE_LOAD_KWH
    total = _hour(model, 12, forecast=1.5, export=0.6, heater=0.5)
    model.observe(_at(13), forecast_kwh=1.0, export_total_kwh=total, heater_solar_kwh=0)
    assert model.base_load(12) == pytest.approx(0.4, abs=1e-6)
    surplus = model.surplus({_at(12, day=19): 2.0, _at(13, day=19): 0.1})
    assert surplus[_at(12, day=19)] == pytest.approx(1.6)
    assert surplus[_at(13, day=19)] == 0.0  # default base load, never negative
    assert model.learned_hours() == 1


def test_incomplete_hours_are_not_learned() -> None:
    model = SolarModel(TZ)
    # Started at 12:30 (restart): not a whole hour.
    for minute in range(30, 60):
        model.observe(
            _at(12, minute),
            forecast_kwh=1.0,
            export_total_kwh=100.0,
            heater_solar_kwh=0,
        )
    # A 20-minute gap inside 13:00.
    for minute in [*range(10), *range(30, 60)]:
        model.observe(
            _at(13, minute),
            forecast_kwh=1.0,
            export_total_kwh=100.0,
            heater_solar_kwh=0,
        )
    # Night: nothing to learn.
    _hour(model, 22, forecast=0.0, export=0.0, heater=0.0)
    model.observe(_at(23), forecast_kwh=0.0, export_total_kwh=100.0, heater_solar_kwh=0)
    assert model.learned_hours() == 0


def test_round_trip_and_history_limit() -> None:
    model = SolarModel(TZ)
    total = 100.0
    for day in range(1, 20):
        total = _hour(
            model, 12, forecast=1.0, export=0.5, heater=0.0, day=day, total=total
        )
        model.observe(
            _at(13, day=day),
            forecast_kwh=1.0,
            export_total_kwh=total,
            heater_solar_kwh=0,
        )
    restored = SolarModel.from_dict(model.as_dict(), TZ)
    assert restored.base_load(12) == pytest.approx(0.5, abs=1e-3)
    assert len(restored.as_dict()["shortfalls"]["12"]) <= 14
    with pytest.raises(ValueError):
        SolarModel.from_dict({"format": 99}, TZ)


def test_slot_surplus_spreads_and_caps() -> None:
    start = _at(11, 30)
    slots = slot_surplus(
        {_at(11): 0.4, _at(12): 2.0}, start, 4, slot_minutes=15, max_kwh=0.375
    )
    assert slots == pytest.approx([0.1, 0.1, 0.375, 0.375])
    assert start + timedelta(minutes=30) == _at(12)


def test_missing_days_repeat_the_last_forecast_day() -> None:
    """Forecast.Solar covers today and tomorrow; the plan reaches further."""
    friday = datetime(2026, 11, 20, 0, 0, tzinfo=TZ)
    forecast = {
        friday + timedelta(days=day, hours=hour): 0.5 + day
        for day in (0, 1)
        for hour in range(8, 17)
    }
    until = datetime(2026, 11, 22, 11, 0, tzinfo=TZ)
    extended = extend_forecast(forecast, until)
    sunday = friday + timedelta(days=2)
    assert extended[sunday + timedelta(hours=10)] == 1.5  # Saturday repeated
    assert extended[sunday + timedelta(hours=16)] == 1.5  # whole day, not cut
    assert friday + timedelta(days=3, hours=10) not in extended
    assert extend_forecast({}, until) == {}
