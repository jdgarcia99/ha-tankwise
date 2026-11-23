"""Replay recorded state changes through the tank model.

Home Assistant (and the CSV exports used by the simulation bench) store the
inputs as state changes, not as regular samples. The replay merges
the changes of every input, feeds the power, regulator and manual mode
changes to a :class:`PowerDropDetector` in the exact order they happened
(with the regulator health roles: mode, uptime, restart reason, output
switch), and samples the latest value of each input (including the hot water
meter) on a regular grid (one minute by default), just as the coordinator
does live.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from .tank import (
    DEFAULT_REGULATOR_ACTIVE_MODES,
    PowerDropDetector,
    TankModel,
    TankParams,
    TankSample,
    TankState,
)

ROLE_TEMPERATURE: Final = "temperature"
ROLE_POWER: Final = "power"
ROLE_ENERGY: Final = "energy"
ROLE_REGULATOR: Final = "regulator"
ROLE_MANUAL_MODE: Final = "manual_mode"
ROLE_AMBIENT: Final = "ambient"
# Not used by the tank model; passed to ``on_sample`` (demand profile).
ROLE_OCCUPANCY: Final = "occupancy"
ROLE_WORKDAY: Final = "workday"
# Regulator health, only fed to the power drop detector.
ROLE_REGULATOR_MODE: Final = "regulator_mode"
ROLE_REGULATOR_UPTIME: Final = "regulator_uptime"
ROLE_REGULATOR_RESTART_REASON: Final = "regulator_restart_reason"
ROLE_REGULATOR_OUTPUT: Final = "regulator_output"
# Hot water meter: cumulative litres and flow rate (L/min).
ROLE_HOT_WATER_VOLUME: Final = "hot_water_volume"
ROLE_HOT_WATER_FLOW: Final = "hot_water_flow"
ROLES: Final = (
    ROLE_TEMPERATURE,
    ROLE_POWER,
    ROLE_ENERGY,
    ROLE_REGULATOR,
    ROLE_MANUAL_MODE,
    ROLE_AMBIENT,
    ROLE_OCCUPANCY,
    ROLE_WORKDAY,
    ROLE_REGULATOR_MODE,
    ROLE_REGULATOR_UPTIME,
    ROLE_REGULATOR_RESTART_REASON,
    ROLE_REGULATOR_OUTPUT,
    ROLE_HOT_WATER_VOLUME,
    ROLE_HOT_WATER_FLOW,
)
DEFAULT_STEP: Final = timedelta(minutes=1)

type Value = float | bool | str | None


@dataclass(frozen=True, slots=True)
class StateChange:
    """An input took a new value (None: unavailable or unknown)."""

    timestamp: datetime
    value: Value


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Outcome of a replay."""

    model: TankModel
    samples: int
    start: datetime | None
    end: datetime | None
    last_state: TankState | None


def replay(
    series: Mapping[str, Sequence[StateChange]],
    params: TankParams | Callable[[datetime], TankParams],
    *,
    model: TankModel | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    step: timedelta = DEFAULT_STEP,
    on_sample: Callable[[TankSample, TankState, Mapping[str, Value]], None]
    | None = None,
    active_modes: Iterable[str] = DEFAULT_REGULATOR_ACTIVE_MODES,
) -> ReplayResult:
    """Replay the state changes of each role (see ``ROLES``) through a model.

    ``params`` may be a function of the sample time (e.g. the mains water
    temperature changes with the date). ``on_sample`` receives each sample,
    the resulting state and the current value of every role. Sampling
    starts at the first temperature change (or ``start``) and ends at the
    last change of any input (or ``end``).
    """
    unknown = set(series) - set(ROLES)
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")
    model = model if model is not None else TankModel()
    params_at = params if callable(params) else (lambda _moment: params)

    timeline = sorted(
        (
            (change.timestamp, role, change.value)
            for role, changes in series.items()
            for change in changes
        ),
        key=lambda item: item[0],
    )
    temperatures = series.get(ROLE_TEMPERATURE, ())
    if not timeline or not temperatures:
        return ReplayResult(model, 0, None, None, None)

    first = start if start is not None else min(c.timestamp for c in temperatures)
    last = end if end is not None else timeline[-1][0]
    detector = PowerDropDetector(params_at(first).power_threshold_w, active_modes)
    regulator_configured = bool(
        series.get(ROLE_MANUAL_MODE) or series.get(ROLE_REGULATOR)
    )
    meter_configured = bool(series.get(ROLE_HOT_WATER_VOLUME))
    current: dict[str, Value] = dict.fromkeys(ROLES)

    index = 0
    samples = 0
    moment = first
    state: TankState | None = None
    while moment <= last:
        while index < len(timeline) and timeline[index][0] <= moment:
            timestamp, role, value = timeline[index]
            current[role] = value
            if role == ROLE_POWER:
                detector.update_power(timestamp, _float(value))
            elif role == ROLE_REGULATOR:
                detector.update_regulator(timestamp, _float(value))
            elif role == ROLE_MANUAL_MODE:
                detector.update_manual(timestamp, _bool(value))
            elif role == ROLE_REGULATOR_MODE:
                detector.update_mode(timestamp, _text(value))
            elif role == ROLE_REGULATOR_UPTIME:
                detector.update_uptime(timestamp, _float(value))
            elif role == ROLE_REGULATOR_RESTART_REASON:
                detector.update_restart_reason(timestamp, _text(value))
            elif role == ROLE_REGULATOR_OUTPUT:
                detector.update_output(timestamp, _bool(value))
            index += 1
        sample = TankSample(
            timestamp=moment,
            temperature=_float(current[ROLE_TEMPERATURE]),
            power=_float(current[ROLE_POWER]),
            energy=_float(current[ROLE_ENERGY]),
            regulator=_float(current[ROLE_REGULATOR]),
            manual_mode=_bool(current[ROLE_MANUAL_MODE]),
            ambient_temperature=_float(current[ROLE_AMBIENT]),
            power_drop=detector.drop,
            regulator_configured=regulator_configured,
            hot_water_volume=_float(current[ROLE_HOT_WATER_VOLUME]),
            hot_water_flow=_float(current[ROLE_HOT_WATER_FLOW]),
            meter_configured=meter_configured,
        )
        state = model.update(sample, params_at(moment))
        if on_sample is not None:
            on_sample(sample, state, current)
        samples += 1
        moment += step
    return ReplayResult(model, samples, first, moment - step, state)


def changes(points: Iterable[tuple[datetime, Value]]) -> list[StateChange]:
    """Build a sorted list of state changes from ``(timestamp, value)`` pairs."""
    return sorted(
        (StateChange(timestamp, value) for timestamp, value in points),
        key=lambda change: change.timestamp,
    )


def _float(value: Value) -> float | None:
    if value is None or isinstance(value, bool | str):
        return None
    return float(value)


def _bool(value: Value) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return None
    return bool(value)


def _text(value: Value) -> str | None:
    return value if isinstance(value, str) else None
