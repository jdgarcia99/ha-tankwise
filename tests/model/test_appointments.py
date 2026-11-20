"""Hot water appointments and the litres actually used after them."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from tankwise_model.appointments import (
    MAX_APPOINTMENTS,
    Appointment,
    appointment_usage,
    energy_from_liters,
    liters_from_energy,
    load_appointments,
    slugify,
)
from tankwise_model.demand import DayTypeMode, DemandModel, DemandParams

from .household import EVENING_KWH, MORNING_KWH, TZ, feed, household

FIRST = date(2026, 10, 5)  # a Monday
MORNING = Appointment(
    "a1",
    "Ducha mañana",
    time(6, 30),
    frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"}),
    50,
)
EVENING = Appointment("a2", "Tarde", time(17, 0), frozenset({"workday"}), 120)


def test_slug_and_round_trip() -> None:
    assert MORNING.slug == "ducha_manana"
    assert slugify("  ¡Baño!  niños ") == "bano_ninos"
    assert Appointment("x9", "!!!", time(7), frozenset(), 10).slug == "x9"
    data = MORNING.as_dict()
    assert data == {
        "id": "a1",
        "name": "Ducha mañana",
        "time": "06:30",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "liters": 50,
        "enabled": True,
        "duration": 60,
    }
    assert Appointment.from_dict(data) == MORNING


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"id": "a", "name": "x", "time": "25:00", "days": [], "liters": 1},
        {"id": "a", "name": "x", "time": "07:00", "days": ["moon"], "liters": 1},
    ],
)
def test_invalid_appointment(data: dict) -> None:
    """Missing fields, bad time, unknown day."""
    with pytest.raises(ValueError):
        Appointment.from_dict(data)


def test_load_skips_invalid_and_limits_to_six() -> None:
    items = [MORNING.as_dict() | {"id": f"a{i}"} for i in range(8)]
    items.insert(1, {"broken": True})
    loaded = load_appointments(items)
    assert len(loaded) == MAX_APPOINTMENTS - 1  # first six items, one broken


def test_applies_by_weekday_and_workday() -> None:
    monday, saturday = date(2026, 11, 16), date(2026, 11, 21)
    weekend = Appointment("w", "Finde", time(10), frozenset({"sat", "sun"}), 40)
    assert weekend.applies(saturday) and not weekend.applies(monday)
    assert EVENING.applies(monday) and not EVENING.applies(saturday)
    # Workday sensor: Monday holiday, Saturday working.
    assert not EVENING.applies(monday, workday=False)
    assert EVENING.applies(saturday, workday=True)
    holiday = Appointment("h", "Festivo", time(10), frozenset({"holiday"}), 40)
    assert holiday.applies(monday, workday=False) and holiday.applies(saturday)


def test_next_due() -> None:
    friday_evening = datetime(2026, 11, 20, 18, 0, tzinfo=TZ)
    assert MORNING.next_due(friday_evening) == datetime(2026, 11, 21, 6, 30, tzinfo=TZ)
    assert EVENING.next_due(friday_evening) == datetime(2026, 11, 23, 17, 0, tzinfo=TZ)
    # Saturday 21 declared a workday by the sensor.
    assert EVENING.next_due(friday_evening, {date(2026, 11, 21): True}) == datetime(
        2026, 11, 21, 17, 0, tzinfo=TZ
    )
    disabled = Appointment("d", "Off", time(7), MORNING.days, 50, enabled=False)
    assert disabled.next_due(friday_evening) is None


def test_liters_and_energy() -> None:
    # 1 kWh with mains at 15 °C and showers at 40 °C: 1 / (0.001163 · 25) L.
    assert liters_from_energy(1.0, 40, 15) == pytest.approx(34.39, abs=0.01)
    assert energy_from_liters(34.39, 40, 15) == pytest.approx(1.0, abs=0.001)
    assert liters_from_energy(1.0, 15, 15) == 0


def test_usage_after_appointments_of_the_reference_household() -> None:
    model = DemandModel(TZ)
    now = feed(model, FIRST, 28, household(FIRST, 28))
    params = DemandParams(mode=DayTypeMode.ALL)
    morning = appointment_usage(
        MORNING, model, params, now, shower_temperature=40, mains_temperature=15
    )
    assert morning.days == 28
    expected = liters_from_energy(MORNING_KWH, 40, 15)
    # The shower starts 06:20-06:40: most of it falls in 06:30-07:30.
    assert morning.p90 == pytest.approx(expected, rel=0.05)
    assert morning.p50 > expected * 0.5

    evening = appointment_usage(
        EVENING, model, params, now, shower_temperature=40, mains_temperature=15
    )
    assert evening.days == 20  # workdays of the last four weeks
    # Only the hour after 17:00 counts: the showers of the 17:00-19:30 block
    # that start later are not part of it.
    assert 0 < evening.p90 < liters_from_energy(EVENING_KWH * 3, 40, 15)


def test_usage_without_observed_days() -> None:
    model = DemandModel(TZ)
    usage = appointment_usage(
        MORNING,
        model,
        DemandParams(),
        datetime(2026, 11, 20, tzinfo=TZ),
        shower_temperature=40,
        mains_temperature=15,
    )
    assert usage.p90 is None and usage.days == 0


def test_only_recent_weeks_count() -> None:
    model = DemandModel(TZ)
    now = feed(model, FIRST, 42, household(FIRST, 42))
    usage = appointment_usage(
        MORNING, model, DemandParams(), now, shower_temperature=40, mains_temperature=15
    )
    assert usage.days == 28
    assert now - timedelta(days=42) < now


def test_duration_defaults_and_limits() -> None:
    data = MORNING.as_dict()
    del data["duration"]
    assert Appointment.from_dict(data).duration == timedelta(hours=1)  # 0.4 beta
    with pytest.raises(ValueError):
        Appointment.from_dict({**MORNING.as_dict(), "duration": 5})
    with pytest.raises(ValueError):
        Appointment.from_dict({**MORNING.as_dict(), "duration": 13 * 60})


def test_usage_covers_the_whole_block() -> None:
    """A 3 h block from 17:00 sees every evening shower of the household."""
    model = DemandModel(TZ)
    now = feed(model, FIRST, 28, household(FIRST, 28))
    params = DemandParams(mode=DayTypeMode.ALL)
    block = Appointment(
        "b",
        "Tarde",
        time(17),
        frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"}),
        120,
        duration=timedelta(hours=3),
    )
    usage = appointment_usage(
        block, model, params, now, shower_temperature=40, mains_temperature=15
    )
    one_shower = liters_from_energy(EVENING_KWH, 40, 15)
    assert usage.p50 >= one_shower * 0.95  # at least the gym shower every day
    assert usage.p90 == pytest.approx(one_shower * 3, rel=0.05)  # strong days
