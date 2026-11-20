"""Replay of recorded state changes (bootstrap and simulation bench)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.replay import (
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_POWER,
    ROLE_TEMPERATURE,
    StateChange,
    changes,
    replay,
)
from tankwise_model.tank import TankModel, TankParams

from .simulator import PARAMS, TankSimulator, drive

TZ = ZoneInfo("Europe/Madrid")


def _at(hhmmss: str, day: int = 15) -> datetime:
    hour, minute, second = (int(part) for part in hhmmss.split(":"))
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def _only_changes(
    points: list[tuple[datetime, object]],
) -> list[tuple[datetime, object]]:
    """Keep only the points where the value changes (like the recorder)."""
    result: list[tuple[datetime, object]] = []
    for moment, value in points:
        if not result or result[-1][1] != value:
            result.append((moment, value))
    return result


def test_replay_of_simulated_history_matches_live_model() -> None:
    """Replaying state changes detects the same draws as feeding samples."""
    sim = TankSimulator(temperature=30.0, report_manual=True, noise=0.5)
    live = TankModel()
    samples = []
    for minutes, regulator, draw in (
        (10, 0, 0),
        (120, 100, 0),
        (60, 0, 0),
        (12, 0, 9.0),
        (60, 0, 0),
    ):
        batch = sim.run(minutes, regulator=regulator, draw_kw=draw)
        samples.extend(batch)
        for sample in batch:
            live.update(sample, PARAMS)

    series = {
        ROLE_TEMPERATURE: changes(
            _only_changes([(s.timestamp, s.temperature) for s in samples])
        ),
        ROLE_POWER: changes(_only_changes([(s.timestamp, s.power) for s in samples])),
        ROLE_ENERGY: changes(_only_changes([(s.timestamp, s.energy) for s in samples])),
        ROLE_MANUAL_MODE: changes(
            _only_changes([(s.timestamp, s.manual_mode) for s in samples])
        ),
    }
    result = replay(series, PARAMS)

    assert result.samples == len(samples)
    assert result.model.as_dict()["calibration_count"] == 1
    assert len(result.model.consumptions) == len(live.consumptions) == 1
    assert result.model.consumptions[0].energy_kwh == pytest.approx(
        live.consumptions[0].energy_kwh, rel=0.05
    )


def test_manual_mode_switched_off_after_cutoff_is_full() -> None:
    """15/11 23:08: cut-off with the manual mode on; an automation turns it
    off three minutes later. The drop is captured at the event instant."""
    params = TankParams(mains_temperature=14.0, cutoff_temperature=60.0)
    series = {
        ROLE_TEMPERATURE: changes(
            [(_at("22:00:00"), 55.0), (_at("23:00:00"), 58.0), (_at("23:08:00"), 59.5)]
        ),
        ROLE_POWER: changes([(_at("22:00:05"), 1480.0), (_at("23:08:57"), 1.8)]),
        ROLE_MANUAL_MODE: changes([(_at("22:00:00"), True), (_at("23:11:58"), False)]),
    }
    result = replay(series, params, end=_at("23:30:00"))
    assert result.model.as_dict()["calibration_count"] == 1
    assert result.model.as_dict()["cutoff_samples"]


def test_manual_mode_switched_off_before_drop_is_not_full() -> None:
    params = TankParams(mains_temperature=14.0, cutoff_temperature=60.0)
    series = {
        ROLE_TEMPERATURE: changes([(_at("15:00:00"), 45.0), (_at("16:00:00"), 52.0)]),
        ROLE_POWER: changes([(_at("15:00:02"), 1480.0), (_at("16:00:04"), 0.5)]),
        ROLE_MANUAL_MODE: changes([(_at("15:00:00"), True), (_at("16:00:01"), False)]),
    }
    result = replay(series, params, end=_at("16:45:00"))
    assert result.model.as_dict()["calibration_count"] == 0


def test_unavailable_values_freeze_the_model() -> None:
    params = TankParams()
    start = _at("10:00:00")
    series = {
        ROLE_TEMPERATURE: [
            StateChange(start, 50.0),
            StateChange(start + timedelta(minutes=10), None),
            StateChange(start + timedelta(minutes=20), 50.0),
        ],
        ROLE_POWER: [StateChange(start, 0.0)],
    }
    seen = []
    replay(series, params, on_sample=lambda sample, state: seen.append(state.available))
    assert seen[:10] == [True] * 10
    assert not any(seen[10:20])
    assert seen[20]


def test_params_can_depend_on_time() -> None:
    start = _at("23:58:00", day=30)
    series = {
        ROLE_TEMPERATURE: [StateChange(start, 50.0)],
        ROLE_POWER: [StateChange(start, 0.0)],
    }
    mains: list[float] = []

    def params_at(moment: datetime) -> TankParams:
        return TankParams(mains_temperature=12.0 if moment.day == 30 else 11.0)

    replay(
        series,
        params_at,
        end=start + timedelta(minutes=4),
        on_sample=lambda sample, state: mains.append(state.mains_temperature),
    )
    assert mains == [12.0, 12.0, 11.0, 11.0, 11.0]


def test_empty_or_invalid_series() -> None:
    assert replay({}, TankParams()).samples == 0
    assert (
        replay({ROLE_POWER: [StateChange(_at("10:00:00"), 0.0)]}, TankParams()).samples
        == 0
    )
    with pytest.raises(ValueError):
        replay({"humidity": []}, TankParams())


def test_existing_model_can_be_continued() -> None:
    sim = TankSimulator(temperature=46.0)
    model = TankModel()
    drive(model, sim, 10)
    samples = sim.run(5)
    series = {
        ROLE_TEMPERATURE: changes([(s.timestamp, s.temperature) for s in samples]),
        ROLE_POWER: changes([(s.timestamp, s.power) for s in samples]),
    }
    result = replay(series, PARAMS, model=model)
    assert result.model is model
    assert result.samples == 5
