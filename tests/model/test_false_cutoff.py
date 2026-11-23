"""Power drops caused by the regulator itself are not thermostat cut-offs.

The diverter heats through its own output: when its power supply fails, the
heating element stops drawing power although the thermostat is still closed,
and its entities are only reported unavailable about two minutes later. A
drop from one minute before to two minutes after the regulator was
unavailable, restarted, was in a mode that does not heat or asked for 0 % is
not a cut-off.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.replay import (
    ROLE_MANUAL_MODE,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_REGULATOR_MODE,
    ROLE_REGULATOR_OUTPUT,
    ROLE_REGULATOR_RESTART_REASON,
    ROLE_REGULATOR_UPTIME,
    ROLE_TEMPERATURE,
    StateChange,
    changes,
    replay,
)
from tankwise_model.tank import (
    CUTOFF_PLAUSIBLE_NEAR,
    PowerDropDetector,
    TankModel,
    TankParams,
    TankSample,
)

TZ = ZoneInfo("Europe/Madrid")
PARAMS = TankParams(mains_temperature=13.5, cutoff_temperature=68.0)
UNAVAILABLE = None

# (local time "HH:MM:SS[.ffffff]", kind, value). Kinds: power (W), regulator
# (%), manual (bool), mode, uptime (s), restart (reason), output (bool).
Event = tuple[str, str, Any]

REGULATOR_KINDS = ("regulator", "manual", "mode", "uptime", "restart", "output")


def _at(day: int, clock: str) -> datetime:
    return datetime.combine(
        datetime(2026, 11, day).date(),
        datetime.strptime(clock, "%H:%M:%S.%f" if "." in clock else "%H:%M:%S").time(),
        TZ,
    )


def _all_unavailable(clock: str) -> list[Event]:
    """The regulator's entities reported unavailable together."""
    return [(clock, kind, UNAVAILABLE) for kind in REGULATOR_KINDS]


def _replay(
    day: int,
    events: Iterable[Event],
    first_poll: str,
    last_poll: str,
    *,
    top: float,
    model: TankModel | None = None,
) -> tuple[TankModel, list[tuple[datetime, float]]]:
    """Replay the events and 60-second polls; return the model and the samples.

    The probe rises while the element draws power, up to ``top`` °C.
    """
    timeline = sorted((_at(day, t), kind, value) for t, kind, value in events)
    detector = PowerDropDetector(PARAMS.power_threshold_w)
    model = model if model is not None else TankModel()
    calibrations: list[tuple[datetime, float]] = []
    original = model._calibrate

    def record(now: datetime, *, regulator_confirmed: bool, params: TankParams):
        original(now, regulator_confirmed=regulator_confirmed, params=params)
        calibrations.append((now, model.cutoff_samples[-1]))

    model._calibrate = record  # type: ignore[method-assign]

    power = 0.0
    regulator: float | None = None
    manual: bool | None = None
    energy = 3390.0
    temperature = top - 6.0
    index = 0
    poll = _at(day, first_poll)
    end = _at(day, last_poll)
    while poll <= end:
        while index < len(timeline) and timeline[index][0] <= poll:
            moment, kind, value = timeline[index]
            if kind == "power":
                power = value
                detector.update_power(moment, value)
            elif kind == "regulator":
                regulator = value
                detector.update_regulator(moment, value)
            elif kind == "manual":
                manual = value
                detector.update_manual(moment, value)
            elif kind == "mode":
                detector.update_mode(moment, value)
            elif kind == "uptime":
                detector.update_uptime(moment, value)
            elif kind == "restart":
                detector.update_restart_reason(moment, value)
            else:
                detector.update_output(moment, value)
            index += 1
        energy += power / 1000 / 60
        if power > PARAMS.power_threshold_w:
            temperature = min(top, temperature + power / 1000 / 60 / 0.1163)
        model.update(
            TankSample(
                timestamp=poll,
                temperature=temperature,
                power=power,
                energy=energy,
                regulator=regulator,
                manual_mode=manual,
                power_drop=detector.drop,
                regulator_configured=True,
            ),
            PARAMS,
        )
        poll += timedelta(minutes=1)
    return model, calibrations


def _running(start: str, uptime: float) -> list[Event]:
    """The diverter heating in Auto at 100 %, its uptime reported every minute."""
    base = _at(23, start)
    events: list[Event] = [
        (start, "mode", "Auto"),
        (start, "manual", False),
        (start, "output", True),
        (start, "restart", "Reboot request from esphome.ota"),
        (start, "regulator", 100.0),
        (start, "power", 1500.0),
    ]
    for minute in range(0, 180):
        moment = base + timedelta(minutes=minute, seconds=23)
        events.append((moment.strftime("%H:%M:%S"), "uptime", uptime + 60 * minute))
    return events


def _until(events: list[Event], clock: str) -> list[Event]:
    """Drop the events from ``clock`` on (the diverter lost its power supply)."""
    return [event for event in events if event[0] < clock]


# 23/11, the real power cut of the diverter. It heats in Auto at 100 % until
# 11:43:21, when its power supply fails: the element stops, but Home Assistant
# keeps the last states until 11:45:21.5. It starts again at 11:46:02
# ("power-on event", uptime 2.8 s), heats from 11:46:03 and loses its power
# supply again at 11:47:56.8; its entities are unavailable from 11:49:51.7.
# TankWise 0.10.2 took the second drop as a 60.9 °C cut-off at 11:51:51.
POWER_CUT_1147: list[Event] = [
    *_until(_running("11:00:00", 11_281.1), "11:43:21"),
    ("11:43:21", "power", 0.0),
    *_all_unavailable("11:45:21.5"),
    ("11:46:02.4", "restart", "power-on event"),
    ("11:46:02.4", "uptime", 2.8),
    ("11:46:02.4", "mode", "Auto"),
    ("11:46:02.4", "manual", False),
    ("11:46:02.4", "output", True),
    ("11:46:02.4", "regulator", 0.0),
    ("11:46:03", "power", 72.5),
    ("11:46:06", "regulator", 40.0),
    ("11:46:11", "regulator", 80.0),
    ("11:46:12", "power", 1541.9),
    ("11:46:16", "regulator", 100.0),
    ("11:47:01", "uptime", 62.8),
    ("11:47:56.8", "power", 0.0),
    *_all_unavailable("11:49:51.7"),
]

# 23/11, 13:13: a real thermostat cut-off at about 68 °C with the diverter
# heating in Auto at 100 %, available and not restarting.
CUTOFF_1313: list[Event] = [
    *_running("12:30:00", 20_000.0),
    ("13:13:00", "power", 0.0),
]


def test_power_cut_of_the_diverter_at_61_is_not_a_cutoff() -> None:
    model, calibrations = _replay(23, POWER_CUT_1147, "11:00:30", "12:10:30", top=60.9)
    assert calibrations == []
    assert model.cutoff_samples == ()


def test_thermostat_cutoff_at_68_with_the_diverter_running_is_a_cutoff() -> None:
    model, calibrations = _replay(23, CUTOFF_1313, "12:30:30", "13:30:30", top=67.9)
    assert len(calibrations) == 1
    assert calibrations[0][0] == _at(23, "13:16:30")  # first poll >= 3 min
    assert model.cutoff_samples == (pytest.approx(67.9, abs=0.1),)


# One disturbance at a time around a drop at 13:13:00, relative to it.
@pytest.mark.parametrize(
    ("events", "cutoff"),
    [
        # Unavailable 1:55 after the drop (the real delay): not a cut-off.
        (_all_unavailable("13:14:55"), False),
        # ... but 2:01 after it, too late to say anything about the drop.
        (_all_unavailable("13:15:01"), True),
        # Unavailable 40 s before, back 20 s before the drop.
        (
            [
                *_all_unavailable("13:12:20"),
                ("13:12:40", "regulator", 100.0),
                ("13:12:40", "manual", False),
                ("13:12:40", "mode", "Auto"),
                ("13:12:40", "output", True),
                ("13:12:40", "restart", "x"),
                ("13:12:40", "uptime", 1.0),
            ],
            False,
        ),
        # A restart (uptime back to a few seconds) 50 s before the drop.
        ([("13:12:10", "uptime", 3.0)], False),
        # ... or 70 s before: outside the minute before.
        ([("13:11:50", "uptime", 3.0)], True),
        # A new restart reason one minute after the drop.
        ([("13:14:00", "restart", "power-on event")], False),
        # A mode that does not heat, 30 s after the drop.
        ([("13:13:30", "mode", "Apagado")], False),
        # The regulator at 0 % in the minute before the drop.
        ([("13:12:30", "regulator", 0.0), ("13:12:35", "regulator", 100.0)], False),
        # Only the output switch unavailable, a minute after.
        ([("13:14:00", "output", UNAVAILABLE)], False),
    ],
)
def test_regulator_disturbance_around_the_drop(
    events: list[Event], cutoff: bool
) -> None:
    timeline = [*CUTOFF_1313, *events]
    _, calibrations = _replay(23, timeline, "12:30:30", "13:30:30", top=67.9)
    assert bool(calibrations) is cutoff


def test_a_mode_listed_as_heating_is_not_a_disturbance() -> None:
    timeline = [*CUTOFF_1313, ("13:12:00", "mode", "Manual")]
    _, calibrations = _replay(23, timeline, "12:30:30", "13:30:30", top=67.9)
    assert len(calibrations) == 1


# ---------------------------------------------------------------------------
# Plausibility: near a known cut-off; far below the configured one otherwise
# ---------------------------------------------------------------------------


def _model_with(samples: list[float]) -> TankModel:
    model = TankModel()
    model._cutoff_samples = list(samples)
    return model


@pytest.mark.parametrize(
    ("samples", "top", "learned"),
    [
        # No cut-off learned yet: anything above 68 - 12 = 56 °C.
        ([], 57.0, True),
        ([], 55.0, False),
        # Known cut-off 68.5 °C (median of three samples): within 4 °C only.
        ([69.1, 68.5, 67.9], 64.6, True),
        ([69.1, 68.5, 67.9], 64.4, False),
        ([69.1, 68.5, 67.9], 60.9, False),
        # One or two samples: the configured cut-off (68 °C) is the known one.
        ([68.4], 64.5, True),
        ([68.4], 63.5, False),
    ],
)
def test_plausible_cutoff(samples: list[float], top: float, learned: bool) -> None:
    model = _model_with(samples)
    _, calibrations = _replay(
        23, CUTOFF_1313, "12:30:30", "13:30:30", top=top, model=model
    )
    assert bool(calibrations) is learned
    assert len(model.cutoff_samples) == len(samples) + learned


def test_plausible_near_is_four_degrees() -> None:
    assert CUTOFF_PLAUSIBLE_NEAR == 4.0


# ---------------------------------------------------------------------------
# Discarding samples
# ---------------------------------------------------------------------------

REAL_SAMPLES = [
    69.10578359006531,
    68.51358522993043,
    60.91136935455522,
    67.93412395900349,
]


def test_discard_the_60_9_sample_by_value() -> None:
    model = _model_with(REAL_SAMPLES)
    assert model.cutoff_temperature(PARAMS) == pytest.approx(68.22, abs=0.01)
    assert model.discard_cutoff_sample(value=60.9) == REAL_SAMPLES[2]
    assert model.cutoff_samples == (REAL_SAMPLES[0], REAL_SAMPLES[1], REAL_SAMPLES[3])
    assert model.cutoff_temperature(PARAMS) == pytest.approx(
        median([REAL_SAMPLES[0], REAL_SAMPLES[1], REAL_SAMPLES[3]])
    )


def test_discard_by_index_and_last() -> None:
    model = _model_with(REAL_SAMPLES)
    assert model.discard_cutoff_sample(index=2) == REAL_SAMPLES[2]
    assert model.discard_cutoff_sample(index=-1) == REAL_SAMPLES[3]
    assert model.cutoff_samples == (REAL_SAMPLES[0], REAL_SAMPLES[1])
    # Below three samples the configured cut-off applies again.
    assert model.cutoff_temperature(PARAMS) == PARAMS.cutoff_temperature


@pytest.mark.parametrize(
    "kwargs",
    [{"value": 62.0}, {"index": 4}, {"index": -5}, {}],
)
def test_discard_nothing_matches(kwargs: dict[str, Any]) -> None:
    model = _model_with(REAL_SAMPLES)
    with pytest.raises(ValueError):
        model.discard_cutoff_sample(**kwargs)
    assert model.cutoff_samples == tuple(REAL_SAMPLES)


def test_discard_without_samples() -> None:
    with pytest.raises(ValueError):
        TankModel().discard_cutoff_sample(index=-1)


def test_discarded_sample_stays_discarded_after_a_restore() -> None:
    model = _model_with(REAL_SAMPLES)
    model.discard_cutoff_sample(value=60.9)
    restored = TankModel.from_dict(model.as_dict())
    assert restored.cutoff_samples == model.cutoff_samples


# ---------------------------------------------------------------------------
# Importing the history replays the regulator health too
# ---------------------------------------------------------------------------

ROLE_OF = {
    "power": ROLE_POWER,
    "regulator": ROLE_REGULATOR,
    "manual": ROLE_MANUAL_MODE,
    "mode": ROLE_REGULATOR_MODE,
    "uptime": ROLE_REGULATOR_UPTIME,
    "restart": ROLE_REGULATOR_RESTART_REASON,
    "output": ROLE_REGULATOR_OUTPUT,
}


def _series(
    events: Iterable[Event], temperature: float
) -> dict[str, list[StateChange]]:
    series: dict[str, list[tuple[datetime, Any]]] = {}
    for clock, kind, value in events:
        series.setdefault(ROLE_OF[kind], []).append((_at(23, clock), value))
    result = {role: changes(points) for role, points in series.items()}
    first = min(change.timestamp for changes_ in result.values() for change in changes_)
    result[ROLE_TEMPERATURE] = changes(
        (first + timedelta(minutes=minute), temperature) for minute in range(80)
    )
    return result


@pytest.mark.parametrize(
    ("events", "temperature", "samples"),
    [
        (POWER_CUT_1147, 60.9, 0),
        (CUTOFF_1313, 67.9, 1),
        # Only the mode role tells: the diverter was switched off.
        ([*CUTOFF_1313, ("13:13:30", "mode", "Apagado")], 67.9, 0),
    ],
)
def test_history_import_ignores_the_power_cut(
    events: list[Event], temperature: float, samples: int
) -> None:
    result = replay(_series(events, temperature), PARAMS)
    assert len(result.model.cutoff_samples) == samples
