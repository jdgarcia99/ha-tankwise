"""Expected solar surplus from the solar production forecast.

The forecast says what the panels will produce each hour; the surplus is
what is left after the house's own consumption. The model learns, for each
hour of the day, how much of the forecast does not reach the water heater
or the grid:

    shortfall_h = forecast_h - (exported_h + heater_from_surplus_h)

which is the house consumption during that hour plus the bias of the
forecast. The expected surplus is ``max(0, forecast_h - median shortfall_h)``
over the last days; until an hour has been observed a fixed base load is
used. Several forecasts (e.g. two roof planes) are summed before.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from statistics import median
from typing import Any

STORAGE_FORMAT = 1
DEFAULT_BASE_LOAD_KWH = 0.25  # per hour, before the hour is learned
HISTORY_DAYS = 14
MIN_FORECAST_KWH = 0.05  # hours with less forecast are not learned
MAX_GAP = timedelta(minutes=10)  # samples further apart break an hour


@dataclass(slots=True)
class _Hour:
    start: datetime
    forecast_kwh: float
    export_start: float | None
    export_last: float | None
    heater_solar_kwh: float = 0.0
    last_sample: datetime | None = None
    broken: bool = False


class SolarModel:
    """Learned hourly base load against the solar forecast."""

    def __init__(self, tz: tzinfo) -> None:
        """Create an empty model."""
        self._tz = tz
        # hour of day -> [(day, shortfall kWh)]
        self._shortfalls: dict[int, list[tuple[date, float]]] = {}
        self._hour: _Hour | None = None

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    def observe(
        self,
        now: datetime,
        *,
        forecast_kwh: float | None,
        export_total_kwh: float | None,
        heater_solar_kwh: float,
    ) -> None:
        """Account one sample.

        ``forecast_kwh`` is the forecast of the current hour, ``export_total``
        the cumulative grid export meter and ``heater_solar_kwh`` the energy
        the water heater took from the surplus since the previous sample.
        """
        local = now.astimezone(self._tz)
        start = local.replace(minute=0, second=0, microsecond=0)
        hour = self._hour
        if hour is not None and hour.start != start:
            # What happened since the last sample belongs to the hour ending.
            if export_total_kwh is not None and hour.export_last is not None:
                hour.export_last = export_total_kwh
            hour.heater_solar_kwh += max(0.0, heater_solar_kwh)
            hour.last_sample = local
            self._close(hour)
            hour = None
            heater_solar_kwh = 0.0
        if hour is None:
            self._hour = hour = _Hour(
                start=start,
                forecast_kwh=forecast_kwh or 0.0,
                export_start=export_total_kwh,
                export_last=export_total_kwh,
                # Started late: the hour is incomplete, not learned.
                broken=local - start > MAX_GAP,
            )
        if (
            forecast_kwh is None
            or export_total_kwh is None
            or (hour.last_sample is not None and local - hour.last_sample > MAX_GAP)
        ):
            hour.broken = True
        if export_total_kwh is not None:
            if hour.export_start is None:
                hour.export_start = export_total_kwh
                hour.broken = True
            hour.export_last = export_total_kwh
        hour.heater_solar_kwh += max(0.0, heater_solar_kwh)
        hour.last_sample = local

    def _close(self, hour: _Hour) -> None:
        if (
            hour.broken
            or hour.forecast_kwh < MIN_FORECAST_KWH
            or hour.export_start is None
            or hour.export_last is None
            or hour.export_last < hour.export_start
            or hour.last_sample is None
            or hour.start + timedelta(hours=1) - hour.last_sample > MAX_GAP
            or hour.last_sample - hour.start > timedelta(hours=1) + MAX_GAP
        ):
            return
        exported = hour.export_last - hour.export_start
        shortfall = hour.forecast_kwh - (exported + hour.heater_solar_kwh)
        items = self._shortfalls.setdefault(hour.start.hour, [])
        items.append((hour.start.date(), shortfall))
        oldest = hour.start.date() - timedelta(days=HISTORY_DAYS)
        self._shortfalls[hour.start.hour] = [item for item in items if item[0] > oldest]

    def reset(self) -> None:
        """Forget what was learned."""
        self._shortfalls.clear()
        self._hour = None

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def base_load(self, hour: int) -> float:
        """Return the kWh of an hour's forecast that will not be surplus."""
        items = self._shortfalls.get(hour)
        if not items:
            return DEFAULT_BASE_LOAD_KWH
        return median(value for _day, value in items)

    def learned_hours(self) -> int:
        """Return how many hours of the day have observations."""
        return len([hour for hour, items in self._shortfalls.items() if items])

    def surplus(self, forecast: Mapping[datetime, float]) -> dict[datetime, float]:
        """Return the expected surplus (kWh) of each forecast hour."""
        result: dict[datetime, float] = {}
        for start, kwh in forecast.items():
            hour = start.astimezone(self._tz).hour
            result[start] = max(0.0, kwh - self.base_load(hour))
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "format": STORAGE_FORMAT,
            "shortfalls": {
                str(hour): [[day.isoformat(), round(value, 4)] for day, value in items]
                for hour, items in self._shortfalls.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], tz: tzinfo) -> SolarModel:
        """Rebuild from :meth:`as_dict` output (raises ValueError if invalid)."""
        model = cls(tz)
        try:
            if data.get("format") != STORAGE_FORMAT:
                raise ValueError(f"unsupported format {data.get('format')!r}")
            for hour, items in data["shortfalls"].items():
                model._shortfalls[int(hour)] = [
                    (date.fromisoformat(day), float(value)) for day, value in items
                ]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"invalid solar model data: {err}") from err
        return model


# Integrations whose forecast timestamps end their period (Forecast.Solar:
# the value at 08:00 is the energy produced from 07:00 to 08:00, and the
# first value of the day, at sunrise, is 0). Others give the period start.
PERIOD_END_DOMAINS = frozenset({"forecast_solar"})


def sum_forecasts(
    forecasts: list[tuple[Mapping[str, float], bool]], tz: tzinfo
) -> dict[datetime, float]:
    """Sum hourly forecasts into kWh per local hour start.

    Each forecast is ``({iso timestamp: Wh}, period_end)``; with
    ``period_end`` the energy belongs to the hour before the timestamp.
    """
    total: dict[datetime, float] = {}
    for forecast, period_end in forecasts:
        for key, wh in forecast.items():
            moment = datetime.fromisoformat(key).astimezone(tz)
            if period_end:
                moment -= timedelta(microseconds=1)
            start = moment.replace(minute=0, second=0, microsecond=0)
            total[start] = total.get(start, 0.0) + float(wh) / 1000
    return total


def slot_surplus(
    surplus: Mapping[datetime, float],
    start: datetime,
    slots: int,
    *,
    slot_minutes: int,
    max_kwh: float,
) -> list[float]:
    """Spread the hourly surplus over the plan slots (capped per slot)."""
    per_hour = 60 // slot_minutes
    by_hour = {
        moment.astimezone(start.tzinfo).replace(minute=0, second=0, microsecond=0): kwh
        for moment, kwh in surplus.items()
    }
    result = []
    for index in range(slots):
        moment = start + timedelta(minutes=slot_minutes * index)
        hour = moment.replace(minute=0, second=0, microsecond=0)
        result.append(min(max_kwh, by_hour.get(hour, 0.0) / per_hour))
    return result
