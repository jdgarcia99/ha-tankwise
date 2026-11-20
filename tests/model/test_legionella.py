"""Anti-legionella cycle: tracking, deadline, target and chosen time."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tankwise_model.legionella import (
    LegionellaTracker,
    choose_time,
    target_temperature,
)

TZ = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)


def _feed(tracker: LegionellaTracker, temperatures: list[float]) -> list[bool]:
    return [
        tracker.update(T0 + timedelta(minutes=minute), value)
        for minute, value in enumerate(temperatures)
    ]


def test_an_hour_above_60_is_a_cycle() -> None:
    tracker = LegionellaTracker()
    done = _feed(tracker, [61.0] * 30 + [59.6] + [61.0] * 30)  # noise tolerated
    assert done.count(True) == 1
    assert tracker.last_cycle == T0 + timedelta(minutes=60)
    assert tracker.deadline(T0) == tracker.last_cycle + timedelta(days=7)


def test_a_real_drop_restarts_the_hour() -> None:
    tracker = LegionellaTracker()
    assert not any(_feed(tracker, [61.0] * 40 + [58.0] + [61.0] * 40))
    assert tracker.last_cycle is None


def test_deadline_without_cycles_and_due() -> None:
    tracker = LegionellaTracker()
    tracker.update(T0, 45.0)
    assert tracker.deadline(T0) == T0 + timedelta(days=3)
    assert not tracker.due(T0)
    assert tracker.due(T0 + timedelta(days=1, hours=1))
    restored = LegionellaTracker.from_dict(tracker.as_dict())
    assert restored.first_seen == T0


def test_target_temperature() -> None:
    assert target_temperature(68.5, 72.0) == 61.5
    assert target_temperature(61.5, 72.0) == 61.0
    assert target_temperature(60.2, 72.0) is None


def test_choose_time_prefers_the_sun_then_the_cheapest_hour() -> None:
    start = T0.replace(hour=0)
    slots = 36 * 4
    buy = [0.20] * slots
    buy[3 * 4 : 4 * 4] = [0.08] * 4  # 03:00-04:00 cheap
    surplus = [0.05] * slots
    solar = [0.0] * slots
    common = {
        "slot_minutes": 15,
        "buy": buy,
        "surplus_value": surplus,
        "heater_slot_kwh": 0.375,
    }
    moment = choose_time(start, start + timedelta(days=2), solar_kwh=solar, **common)
    assert moment == start + timedelta(hours=4)
    solar[13 * 4 : 14 * 4] = [0.35] * 4  # sunny 13:00-14:00
    moment = choose_time(start, start + timedelta(days=2), solar_kwh=solar, **common)
    assert moment == start + timedelta(hours=14)
    # The deadline comes first.
    moment = choose_time(start, start + timedelta(hours=3), solar_kwh=solar, **common)
    assert moment is not None and moment <= start + timedelta(hours=3)
