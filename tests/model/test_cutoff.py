"""Full-tank detection from real event sequences (regulator vs thermostat).

The coordinator samples every 60 s, but the regulator can be switched off a
few seconds after the thermostat cuts off. The regulator value is therefore
captured at the instant the power falls, from state change events.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tankwise_model.tank import (
    PowerDropDetector,
    TankModel,
    TankParams,
    TankSample,
)

TZ = ZoneInfo("Europe/Madrid")
PARAMS = TankParams(mains_temperature=14.0)

# (local time "HH:MM:SS", "power" | "regulator", value)
Event = tuple[str, str, float]


def _at(day: int, hhmmss: str) -> datetime:
    hour, minute, second = (int(part) for part in hhmmss.split(":"))
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def _replay(
    day: int,
    events: Iterable[Event],
    first_poll: str,
    last_poll: str,
    *,
    use_events: bool = True,
) -> tuple[TankModel, list[tuple[datetime, bool]]]:
    """Replay events and 60-second polls; return the model and calibrations."""
    timeline = sorted((_at(day, t), kind, value) for t, kind, value in events)
    detector = PowerDropDetector(PARAMS.power_threshold_w)
    model = TankModel()
    calibrations: list[tuple[datetime, bool]] = []
    original = model._calibrate

    def record(now: datetime, *, regulator_confirmed: bool, params: TankParams):
        calibrations.append((now, regulator_confirmed))
        original(now, regulator_confirmed=regulator_confirmed, params=params)

    model._calibrate = record  # type: ignore[method-assign]

    power = regulator = 0.0
    energy = 500.0
    temperature = 40.0
    index = 0
    poll = _at(day, first_poll)
    end = _at(day, last_poll)
    while poll <= end:
        while index < len(timeline) and timeline[index][0] <= poll:
            moment, kind, value = timeline[index]
            if kind == "power":
                power = value
                detector.update_power(moment, value)
            else:
                regulator = value
                detector.update_regulator(moment, value)
            index += 1
        energy += power / 1000 / 60
        if power > PARAMS.power_threshold_w:
            temperature = min(46.7, temperature + power / 1000 / 60 / 0.1163)
        model.update(
            TankSample(
                timestamp=poll,
                temperature=temperature,
                power=power,
                energy=energy,
                regulator=regulator,
                power_drop=detector.drop if use_events else None,
            ),
            PARAMS,
        )
        poll += timedelta(minutes=1)
    return model, calibrations


# 15/11: thermostat cut-off at 23:08:57 with the PWM at 100 %; an automation
# switches the manual mode off at 23:11:58 and the PWM falls to 0.
CUTOFF_2308: list[Event] = [
    ("22:00:00", "regulator", 100),
    ("22:00:05", "power", 1441),
    ("23:08:57", "power", 1.8),
    ("23:11:58", "regulator", 0),
]

# 15/11: an automation switches the manual mode off at 16:00:01, the PWM falls
# to 0 at 16:00:03 and the power falls at 16:00:04. Not a full tank.
REGULATOR_STOP_1600: list[Event] = [
    ("15:00:00", "regulator", 100),
    ("15:00:02", "power", 1441),
    ("16:00:03", "regulator", 0),
    ("16:00:04", "power", 0.5),
]

# Thermostat cut-off, then the regulator falls one second later.
CUTOFF_THEN_REGULATOR: list[Event] = [
    ("15:00:00", "regulator", 100),
    ("15:00:02", "power", 1441),
    ("16:00:03", "power", 1.8),
    ("16:00:04", "regulator", 0),
]


def test_cutoff_at_2308_is_full_although_regulator_falls_later() -> None:
    model, calibrations = _replay(15, CUTOFF_2308, "22:00:30", "23:30:30")
    assert calibrations == [(_at(15, "23:12:30"), True)]  # first poll >= 3 min
    assert model.as_dict()["calibration_count"] == 1


def test_cutoff_at_2308_was_missed_with_polling_only() -> None:
    """The v0.2.1 behaviour: only ~2 minutes of regulator demand were seen."""
    _, calibrations = _replay(15, CUTOFF_2308, "22:00:30", "23:30:30", use_events=False)
    assert calibrations == []


def test_regulator_stop_at_1600_is_not_full() -> None:
    model, calibrations = _replay(15, REGULATOR_STOP_1600, "15:00:30", "16:45:30")
    assert calibrations == []
    assert model.as_dict()["calibration_count"] == 0


def test_regulator_falling_one_second_after_cutoff_is_full() -> None:
    _, calibrations = _replay(15, CUTOFF_THEN_REGULATOR, "15:00:30", "16:30:30")
    assert calibrations == [(_at(15, "16:03:30"), True)]


def test_power_rising_again_cancels_the_drop() -> None:
    """The PWM modulates: power dips below the threshold and comes back."""
    events: list[Event] = [
        ("15:00:00", "regulator", 100),
        ("15:00:02", "power", 1441),
        ("15:30:10", "power", 20),
        ("15:30:40", "power", 900),
    ]
    _, calibrations = _replay(15, events, "15:00:30", "16:00:30")
    assert calibrations == []


def test_detector_regulator_value_at_drop() -> None:
    detector = PowerDropDetector(50)
    detector.update_regulator(_at(15, "22:00:00"), 100)
    detector.update_power(_at(15, "22:00:05"), 1441)
    assert detector.drop is None
    detector.update_power(_at(15, "23:08:57"), 1.8)
    assert detector.drop is not None
    assert detector.drop.timestamp == _at(15, "23:08:57")
    assert detector.drop.regulator_demand
    detector.update_regulator(_at(15, "23:11:58"), 0)
    assert detector.drop.regulator_demand  # kept: captured at the drop


def test_detector_regulator_lowered_just_before_the_drop() -> None:
    """A regulator decrease right before the drop means it stopped the heating."""
    detector = PowerDropDetector(50)
    detector.update_regulator(_at(15, "15:00:00"), 100)
    detector.update_power(_at(15, "15:00:02"), 1441)
    detector.update_regulator(_at(15, "16:00:03"), 30)  # still > 10 %
    detector.update_power(_at(15, "16:00:04"), 0.5)
    assert detector.drop is not None
    assert not detector.drop.regulator_demand


def test_detector_ignores_first_reading_and_unknown_values() -> None:
    detector = PowerDropDetector(50)
    detector.update_regulator(_at(15, "10:00:00"), 100)
    detector.update_power(_at(15, "10:00:01"), 0)  # no previous heating
    assert detector.drop is None
    detector.update_power(_at(15, "10:00:02"), 1441)
    detector.update_power(_at(15, "10:00:03"), None)  # unavailable: ignored
    assert detector.drop is None
