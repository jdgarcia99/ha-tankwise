"""Fill the compensation tracker from the recorder statistics.

At every start the hours of the last days that the tracker does not know
(TankWise was not running, or it was just installed) are added from the
hourly long-term statistics: the change of the grid import and export energy
sensors (from their hourly sums) and the mean of the purchase and injection
price sensors. Hours
already known are kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .model.compensation import KEEP, CompensationTracker

_LOGGER = logging.getLogger(__name__)

RECORDER_DOMAIN = "recorder"


def _fetch(
    hass: HomeAssistant, statistic_ids: set[str], start: datetime, end: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Read the hourly statistics (runs in the recorder thread)."""
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    return statistics_during_period(
        hass,
        start,
        end,
        statistic_ids,
        "hour",
        None,
        {"sum", "mean"},
    )


def _deltas(rows: list[dict[str, Any]]) -> dict[datetime, float]:
    """Energy of each hour: its sum minus the sum of the hour before.

    Not the recorder's ``change``: for the first hour it returns the whole
    meter total. An hour without the hour before it is skipped.
    """
    sums = _by_hour(rows, "sum")
    return {
        hour: value - sums[hour - timedelta(hours=1)]
        for hour, value in sums.items()
        if hour - timedelta(hours=1) in sums
    }


def _by_hour(rows: list[dict[str, Any]], key: str) -> dict[datetime, float]:
    values: dict[datetime, float] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        start = row["start"]
        if not isinstance(start, datetime):
            start = dt_util.utc_from_timestamp(float(start))
        values[dt_util.as_utc(start)] = float(value)
    return values


async def async_import_compensation(
    hass: HomeAssistant,
    tracker: CompensationTracker,
    *,
    import_sensor: str,
    export_sensor: str,
    buy_sensor: str,
    injection_sensor: str,
) -> int:
    """Add the missing past hours; return how many were added."""
    if RECORDER_DOMAIN not in hass.config.components:
        return 0
    from homeassistant.components.recorder import get_instance

    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - KEEP
    ids = {import_sensor, export_sensor, buy_sensor, injection_sensor}
    try:
        stats = await get_instance(hass).async_add_executor_job(
            partial(_fetch, hass, ids, start, end)
        )
    except Exception:
        _LOGGER.warning("Could not read the statistics for the compensation cap")
        return 0
    imported = _deltas(stats.get(import_sensor, []))
    exported = _deltas(stats.get(export_sensor, []))
    buy = _by_hour(stats.get(buy_sensor, []), "mean")
    injection = _by_hour(stats.get(injection_sensor, []), "mean")
    added = 0
    for hour in sorted(set(imported) & set(exported) & set(buy) & set(injection)):
        if imported[hour] < 0 or exported[hour] < 0:
            continue  # meter reset in that hour
        added += tracker.add_hour(
            hour, imported[hour], exported[hour], buy[hour], injection[hour]
        )
    tracker.prune(dt_util.utcnow())
    return added
