"""Compensation cap: compensation, energy term, projection and free surplus."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.compensation import (
    CompensationTracker,
    billing_period,
    free_surplus_values,
)

TZ = ZoneInfo("Europe/Madrid")
UTC = ZoneInfo("UTC")
BUY = 0.20  # €/kWh
INJECTION = 0.10  # €/kWh


def _run(
    tracker: CompensationTracker,
    start: datetime,
    days: int,
    *,
    import_kwh: float,
    export_kwh: float,
    sun_hours: range = range(10, 16),
    totals: list[float] | None = None,
) -> list[float]:
    """Feed the meters minute by minute: import every hour, export in the sun."""
    moment = start
    if totals is None:
        totals = [1000.0, 500.0]
        tracker.add_reading(moment.astimezone(UTC), *totals, BUY, INJECTION)
    for _ in range(days * 24 * 4):
        local = moment.astimezone(TZ)
        totals[0] += import_kwh / 4
        if local.hour in sun_hours:
            totals[1] += export_kwh / 4
        moment += timedelta(minutes=15)
        tracker.add_reading(moment.astimezone(UTC), *totals, BUY, INJECTION)
    return totals


def test_billing_period_is_the_calendar_month_or_from_a_day() -> None:
    now = datetime(2026, 11, 27, 10, tzinfo=TZ)
    assert billing_period(now, TZ, 1) == (
        datetime(2026, 11, 1, tzinfo=TZ),
        datetime(2026, 12, 1, tzinfo=TZ),
    )
    assert billing_period(now, TZ, 28) == (
        datetime(2026, 10, 28, tzinfo=TZ),
        datetime(2026, 11, 28, tzinfo=TZ),
    )
    december = datetime(2026, 12, 20, tzinfo=TZ)
    assert billing_period(december, TZ, 15)[1] == datetime(2027, 1, 15, tzinfo=TZ)


def test_month_below_the_cap() -> None:
    """Little sun: the compensation never reaches the energy term."""
    tracker = CompensationTracker()
    start = datetime(2026, 11, 1, tzinfo=TZ)
    _run(tracker, start, 15, import_kwh=0.5, export_kwh=1.0)
    now = (start + timedelta(days=15)).astimezone(UTC)
    estimate = tracker.estimate(now, billing_period(now, TZ, 1))
    # 15 days: import 0.5 kWh/h at 0.20 and export 6 kWh/day at 0.10.
    assert estimate.energy_term == pytest.approx(15 * 24 * 0.5 * BUY, rel=1e-3)
    assert estimate.compensation == pytest.approx(15 * 6 * INJECTION, rel=1e-3)
    # Projected to 30 days at the same rate.
    assert estimate.projected_energy_term == pytest.approx(72.0, rel=1e-2)
    assert estimate.projected_compensation == pytest.approx(18.0, rel=1e-2)
    assert estimate.margin == pytest.approx(54.0, rel=1e-2)
    assert estimate.excess == 0.0
    values = [0.10, 0.10]
    assert free_surplus_values(values, [0.4, 0.4], estimate.excess) == values


def test_month_over_the_cap_from_the_middle() -> None:
    """Cloudy first half, sunny second half: the cap is reached mid-month."""
    tracker = CompensationTracker()
    start = datetime(2026, 11, 1, tzinfo=TZ)
    totals = _run(tracker, start, 10, import_kwh=0.3, export_kwh=0.5)
    day10 = (start + timedelta(days=10)).astimezone(UTC)
    early = tracker.estimate(day10, billing_period(day10, TZ, 1))
    # Day 10: energy 14.4 €, compensation 3 €, and cloudy days projected.
    assert early.margin_now == pytest.approx(14.4 - 3.0, rel=1e-3)
    assert early.excess == 0.0

    # Sunny from day 11: little import, a lot of export.
    _run(
        tracker,
        start + timedelta(days=10),
        10,
        import_kwh=0.1,
        export_kwh=5.0,
        sun_hours=range(9, 17),
        totals=totals,
    )
    day20 = (start + timedelta(days=20)).astimezone(UTC)
    late = tracker.estimate(day20, billing_period(day20, TZ, 1))
    # Days 11-20: energy +4.8 €, compensation +40 €: already over the term.
    assert late.energy_term == pytest.approx(14.4 + 4.8, rel=1e-3)
    assert late.compensation == pytest.approx(3.0 + 40.0, rel=1e-3)
    assert late.margin_now < 0
    # The projection uses the last 14 days (4 cloudy, 10 sunny).
    assert late.projected_compensation > late.compensation
    assert late.excess == pytest.approx(-late.margin)
    assert late.excess > 30

    # Only the part over the cap is free: a small excess frees the first slots.
    values = [0.10, 0.10, 0.10, 0.10]
    solar = [0.375, 0.375, 0.375, 0.0]
    assert free_surplus_values(values, solar, late.excess) == [0.0, 0.0, 0.0, 0.10]
    assert free_surplus_values(values, solar, 0.05) == pytest.approx(
        [0.0, 0.10 * (1 - 0.0125 / 0.0375), 0.10, 0.10]
    )


def test_gaps_and_meter_resets_are_not_accounted() -> None:
    tracker = CompensationTracker()
    t0 = datetime(2026, 11, 2, 9, tzinfo=UTC)
    tracker.add_reading(t0, 100.0, 50.0, BUY, INJECTION)
    tracker.add_reading(t0 + timedelta(minutes=30), 101.0, 51.0, BUY, INJECTION)
    # Three hours without readings: the energy in between is not accounted.
    tracker.add_reading(t0 + timedelta(hours=4), 105.0, 55.0, BUY, INJECTION)
    # Meter reset.
    tracker.add_reading(t0 + timedelta(hours=4, minutes=5), 0.5, 0.0, BUY, INJECTION)
    # Unknown prices: the last known ones are used.
    tracker.add_reading(t0 + timedelta(hours=4, minutes=10), 1.5, 2.0, None, None)
    total_import = sum(t.import_kwh for t in tracker.hours.values())
    total_value = sum(t.compensation_eur for t in tracker.hours.values())
    assert total_import == pytest.approx(2.0)
    assert total_value == pytest.approx(3.0 * INJECTION)


def test_history_import_and_persistence() -> None:
    tracker = CompensationTracker()
    hour = datetime(2026, 11, 3, 12, tzinfo=UTC)
    assert tracker.add_hour(hour, 0.2, 1.5, BUY, INJECTION)
    assert not tracker.add_hour(hour, 9.0, 9.0, BUY, INJECTION)  # already known
    tracker.add_reading(hour + timedelta(hours=1), 10.0, 20.0, BUY, INJECTION)
    copy = CompensationTracker.from_dict(tracker.as_dict())
    assert copy.as_dict() == tracker.as_dict()
    assert copy.hours[hour].compensation_eur == pytest.approx(0.15)
    # Hours older than the kept window are forgotten.
    copy.prune(hour + timedelta(days=60))
    assert copy.hours == {}


def test_no_projection_without_a_day_of_data() -> None:
    tracker = CompensationTracker()
    start = datetime(2026, 11, 1, tzinfo=TZ)
    _run(tracker, start, 0, import_kwh=0.5, export_kwh=1.0)
    _run(
        tracker,
        start,
        1,
        import_kwh=0.5,
        export_kwh=1.0,
    )
    now = (start + timedelta(hours=12)).astimezone(UTC)
    estimate = tracker.estimate(now, billing_period(now, TZ, 1))
    assert estimate.hours_learned < 24
    assert estimate.projected_energy_term == estimate.energy_term


def test_free_surplus_stops_at_the_end_of_the_period() -> None:
    """Slots of the next billing period keep their value."""
    values = [0.10, 0.10, 0.10]
    solar = [0.3, 0.3, 0.3]
    assert free_surplus_values(values, solar, 100.0, slots=2) == [0.0, 0.0, 0.10]
    assert free_surplus_values(values, solar, 100.0, slots=0) == values


def test_energy_term_factor_scales_the_energy_term_only() -> None:
    """Calibrated with a bill: the cap counts only part of the purchase price."""
    tracker = CompensationTracker()
    start = datetime(2026, 11, 1, tzinfo=TZ)
    _run(tracker, start, 15, import_kwh=0.5, export_kwh=1.0)
    now = (start + timedelta(days=15)).astimezone(UTC)
    period = billing_period(now, TZ, 1)
    full = tracker.estimate(now, period)
    half = tracker.estimate(now, period, energy_term_factor=0.5)
    assert half.energy_term == pytest.approx(full.energy_term * 0.5)
    assert half.projected_energy_term == pytest.approx(full.projected_energy_term * 0.5)
    assert half.compensation == full.compensation
    assert half.energy_term_factor == 0.5
    # With a factor of 0.2 the same month goes over the cap.
    low = tracker.estimate(now, period, energy_term_factor=0.2)
    assert low.excess > 0 and full.excess == 0
