"""Cold start: rebuild the tank model from the recorder history.

When TankWise is installed (or upgraded to models that learn more) it
replays the last days of the probe, heater power and energy,
regulator output, manual mode and ambient temperature through a fresh tank
model. Losses, effective capacity, heater power, thermostat cut-offs and the
draws of that period are learned before the first live sample, so the demand
profile does not start empty. The coordinator merges the result with what it
had already learned (see ``TankModel.merge_learning``): an import never loses
learning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from functools import partial
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .inputs import InputError, parse_bool, parse_measurement, parse_numeric, state_unit
from .model.demand import DemandModel
from .model.replay import (
    ROLE_AMBIENT,
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_OCCUPANCY,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_TEMPERATURE,
    ROLE_WORKDAY,
    ReplayResult,
    StateChange,
    Value,
    replay,
)
from .model.tank import TankModel, TankParams, TankSample, TankState

_LOGGER = logging.getLogger(__name__)

RECORDER_DOMAIN = "recorder"

# How each role's states are interpreted.
_MEASUREMENT_KIND = {
    ROLE_TEMPERATURE: "temperature",
    ROLE_AMBIENT: "temperature",
    ROLE_POWER: "power",
    ROLE_ENERGY: "energy",
}


class BootstrapStatus(StrEnum):
    """Outcome of the history import."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    NO_RECORDER = "no_recorder"
    NO_HISTORY = "no_history"
    # Samples were replayed but nothing was learned (e.g. the units of the
    # inputs were unknown): not adopted, imported again later.
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BootstrapInfo:
    """Summary of the history import (shown in the diagnostics)."""

    status: BootstrapStatus
    finished: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    samples: int = 0
    draws: int = 0
    calibrations: int = 0
    days: int = 0
    error: str | None = None
    # Version of the models the history was imported with (1: TankWise 0.3).
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "status": self.status.value,
            "finished": _iso(self.finished),
            "start": _iso(self.start),
            "end": _iso(self.end),
            "samples": self.samples,
            "draws": self.draws,
            "calibrations": self.calibrations,
            "days": self.days,
            "version": self.version,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BootstrapInfo:
        """Rebuild from :meth:`as_dict` output."""
        return cls(
            status=BootstrapStatus(data["status"]),
            finished=_from_iso(data.get("finished")),
            start=_from_iso(data.get("start")),
            end=_from_iso(data.get("end")),
            samples=int(data.get("samples", 0)),
            draws=int(data.get("draws", 0)),
            calibrations=int(data.get("calibrations", 0)),
            days=int(data.get("days", 0)),
            version=int(data.get("version", 1)),
            error=data.get("error"),
        )


def states_to_changes(
    role: str, states: list[State], unit: str | None
) -> list[StateChange]:
    """Convert recorded states of one role to state changes in °C/W/kWh/%."""
    result: list[StateChange] = []
    for state in states:
        value: float | bool | None
        try:
            if role in (ROLE_MANUAL_MODE, ROLE_OCCUPANCY, ROLE_WORKDAY):
                value = parse_bool(state.state)
            elif role == ROLE_REGULATOR:
                value = parse_numeric(state.state)
            else:
                value = parse_measurement(state.state, unit, _MEASUREMENT_KIND[role])
        except InputError:
            value = None
        result.append(StateChange(state.last_changed, value))
    return result


def _fetch_history(
    hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime
) -> dict[str, list[State]]:
    """Read the state changes from the recorder (runs in the recorder thread)."""
    # Imported here: the recorder is an optional (after) dependency.
    from homeassistant.components.recorder import history

    states = history.get_significant_states(
        hass,
        start,
        end,
        entity_ids,
        None,
        include_start_time_state=True,
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=True,
    )
    return {
        entity_id: [state for state in entity_states if isinstance(state, State)]
        for entity_id, entity_states in states.items()
    }


async def async_bootstrap(
    hass: HomeAssistant,
    roles: Mapping[str, str | None],
    params_at: Callable[[datetime], TankParams],
    *,
    days: int,
    tz: tzinfo,
) -> tuple[BootstrapInfo, ReplayResult | None, DemandModel | None]:
    """Replay the recorder history of the given roles through new models.

    ``roles`` maps replay roles (see ``model.replay.ROLES``) to entity IDs.
    Returns the summary, the tank model replay and the demand profile.
    """
    if RECORDER_DOMAIN not in hass.config.components:
        info = BootstrapInfo(BootstrapStatus.NO_RECORDER, finished=dt_util.utcnow())
        return info, None, None

    from homeassistant.components.recorder import get_instance

    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    entity_ids = {role: entity_id for role, entity_id in roles.items() if entity_id}
    try:
        history = await get_instance(hass).async_add_executor_job(
            partial(_fetch_history, hass, list(entity_ids.values()), start, end)
        )
        series = {
            role: states_to_changes(
                role, history.get(entity_id, []), state_unit(hass.states.get(entity_id))
            )
            for role, entity_id in entity_ids.items()
        }
        if not series.get(ROLE_TEMPERATURE) or not series.get(ROLE_POWER):
            info = BootstrapInfo(BootstrapStatus.NO_HISTORY, finished=dt_util.utcnow())
            return info, None, None
        model = TankModel()
        demand = DemandModel(tz)

        def _feed_demand(
            sample: TankSample, state: TankState, values: Mapping[str, Value]
        ) -> None:
            occupancy = values.get(ROLE_OCCUPANCY)
            workday = values.get(ROLE_WORKDAY)
            demand.update(
                sample.timestamp,
                available=state.available,
                occupied=occupancy if isinstance(occupancy, bool) else None,
                draws=model.consumptions,
                workday=workday if isinstance(workday, bool) else None,
            )

        result = await hass.async_add_executor_job(
            partial(
                replay,
                series,
                params_at,
                model=model,
                end=end,
                on_sample=_feed_demand,
            )
        )
    except Exception as err:
        _LOGGER.warning("Could not import the tank history: %s", err)
        info = BootstrapInfo(
            BootstrapStatus.FAILED, finished=dt_util.utcnow(), error=str(err)
        )
        return info, None, None

    model_data = result.model.as_dict()
    learned = result.model.learned_samples() > 0 or demand.observations
    info = BootstrapInfo(
        BootstrapStatus.DONE if learned else BootstrapStatus.EMPTY,
        finished=dt_util.utcnow(),
        start=result.start,
        end=result.end,
        samples=result.samples,
        draws=len(result.model.consumptions),
        calibrations=int(model_data["calibration_count"]),
        days=len(demand.observations),
    )
    _LOGGER.info(
        "Imported %d samples of tank history (%s to %s): %d draws, %d cut-offs, "
        "%d days for the demand profile",
        info.samples,
        info.start,
        info.end,
        info.draws,
        info.calibrations,
        info.days,
    )
    return info, result, demand


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
