"""Hot water demand: learned usage profile and forecast.

The profile is built from observed days. Each observed day is the energy of
its detected draws spread over 96 local slots of 15 minutes. A day only
counts when the tank model was running for most of it and the home was
occupied, so a partial day does not look like a day without showers.

Days are grouped by type: per weekday, workday/weekend, or all alike. With a
workday sensor, holidays are grouped with the weekend (or with Sundays in
the per-weekday grouping) whatever the day of the week.

Forecasts use quantiles of the **cumulative** demand curves of past days of
the same type:

* the P50/P90 of a slot is the increment of the P50/P90 cumulative curve, so
  the slots of a day add up to the P50/P90 of the daily total. Summing
  per-slot quantiles instead would put a shower in every slot where one ever
  happened (showers move by 15 to 30 minutes) and would count every possible
  shower of a "strong" day on every day;
* the demand of a window (e.g. until the next price valley) is the quantile
  of that window across past days, which keeps the correlation between the
  slots of a day.

Recent days weigh more (half-life of three weeks). A draw that left the water
below the comfort temperature raises the forecast of its slots by 20 %; the
uplift fades out in two weeks. With the home unoccupied the forecast is zero.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
import math
from typing import Any

from .tank import ConsumptionEvent

STORAGE_FORMAT = 1

SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
SLOT = timedelta(minutes=SLOT_MINUTES)

DEFAULT_OBSERVATIONS = 16  # days of each type used for the profile
DEFAULT_HALF_LIFE_DAYS = 21.0
SHORTAGE_FACTOR = 1.2
SHORTAGE_DECAY = timedelta(days=14)
SHORTAGE_SPREAD_SLOTS = 1  # also raise the neighbouring slots (time jitter)
MIN_TYPE_OBSERVATIONS = 3  # below this, days of every type are used
MIN_READY_DAYS = 7
MIN_DAY_COVERAGE = timedelta(hours=20)
# Days are closed the next morning, once the night heating has reconciled
# the energy of the evening draws.
FINALIZE_DELAY = timedelta(hours=9)
MAX_STEP = timedelta(minutes=5)  # longer gaps do not count as coverage
HISTORY_DAYS = 120

Q50 = 0.5
Q90 = 0.9

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class DayTypeMode(StrEnum):
    """How days are grouped in the profile."""

    WEEKDAY = "weekday"
    WORKDAY_WEEKEND = "workday_weekend"
    ALL = "all"


class DemandStatus(StrEnum):
    """Whether the profile has enough days."""

    LEARNING = "learning"
    READY = "ready"


def is_workday(day: date, workday: bool | None) -> bool:
    """Return whether a day is a workday (calendar fallback without sensor)."""
    if workday is not None:
        return workday
    return day.weekday() < 5


def day_type(day: date, mode: DayTypeMode, workday: bool | None = None) -> str:
    """Return the profile group of a day.

    ``workday`` comes from a workday sensor (None: not known). A holiday on a
    weekday is grouped with Sundays (per weekday) or with the weekend.
    """
    if mode is DayTypeMode.ALL:
        return "all"
    working = is_workday(day, workday)
    if mode is DayTypeMode.WORKDAY_WEEKEND:
        return "workday" if working else "weekend"
    if working or day.weekday() >= 5:
        return WEEKDAY_NAMES[day.weekday()]
    return "sun"


@dataclass(frozen=True, slots=True)
class DemandParams:
    """Settings of the demand profile."""

    mode: DayTypeMode = DayTypeMode.WORKDAY_WEEKEND
    observations: int = DEFAULT_OBSERVATIONS
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS


@dataclass(frozen=True, slots=True)
class DayObservation:
    """Energy drawn (kWh) in each local slot of an observed day."""

    day: date
    slots: tuple[float, ...]
    workday: bool | None = None

    @property
    def total(self) -> float:
        """Return the energy drawn in the day."""
        return sum(self.slots)

    def window(self, first: int, last: int) -> float:
        """Return the energy drawn between two slots (``last`` excluded)."""
        return sum(self.slots[max(0, first) : min(SLOTS_PER_DAY, last)])


@dataclass(frozen=True, slots=True)
class ShortageMark:
    """A draw that left the water below the comfort temperature."""

    start: datetime
    end: datetime
    reason: str = "shortage"


@dataclass(frozen=True, slots=True)
class ForecastSlot:
    """Forecast demand of one slot (kWh)."""

    start: datetime
    p50: float
    p90: float


@dataclass(slots=True)
class _DayCurves:
    """Adjusted observations of the days of one type, with their weights."""

    rows: list[list[float]]
    weights: list[float]
    cumulative_p50: list[float] = field(default_factory=list)
    cumulative_p90: list[float] = field(default_factory=list)

    def window(self, first: int, last: int, quantile: float) -> float:
        """Quantile of the demand between slots ``first`` and ``last`` (excl.)."""
        if not self.rows or last <= first:
            return 0.0
        totals = [sum(row[first:last]) for row in self.rows]
        return weighted_quantile(totals, self.weights, quantile)


@dataclass(slots=True)
class DemandForecast:
    """Forecast for the next hours, slot by slot."""

    start: datetime
    slots: list[ForecastSlot]
    occupied: bool
    tz: tzinfo
    days: dict[date, _DayCurves] = field(default_factory=dict, repr=False)

    @property
    def end(self) -> datetime:
        """Return the end of the horizon."""
        return (self.start.astimezone(UTC) + SLOT * len(self.slots)).astimezone(self.tz)

    def total(self, quantile: float = Q90) -> float:
        """Return the demand of the whole horizon."""
        return self.window(self.start, self.end, quantile)

    def window(self, start: datetime, end: datetime, quantile: float = Q90) -> float:
        """Return the demand between two instants of the horizon.

        Within a day the quantile is taken across past days (correlation of
        the slots is kept); across days the quantiles are added, which is
        conservative.
        """
        if not self.occupied or end <= start:
            return 0.0
        moment = max(start, self.start).astimezone(self.tz)
        end = min(end, self.end).astimezone(self.tz)
        total = 0.0
        while moment < end:
            day = moment.date()
            midnight = datetime.combine(day + timedelta(days=1), time.min, self.tz)
            stop = min(end, midnight)
            curves = self.days.get(day)
            if curves is not None:
                last = SLOTS_PER_DAY if stop >= midnight else _slot_index_ceil(stop)
                total += curves.window(_slot_index(moment), last, quantile)
            moment = stop
        return total


def weighted_quantile(
    values: Sequence[float], weights: Sequence[float], quantile: float
) -> float:
    """Return the weighted quantile (inverted CDF) of the values."""
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return 0.0
    target = quantile * total
    accumulated = 0.0
    for value, weight in pairs:
        accumulated += weight
        if accumulated >= target - 1e-9:
            return value
    return pairs[-1][0]


def slot_of(moment: datetime) -> int:
    """Return the slot index (0-95) of a local time."""
    return _slot_index(moment)


def _slot_index(moment: datetime) -> int:
    return (moment.hour * 60 + moment.minute) // SLOT_MINUTES


def _slot_index_ceil(moment: datetime) -> int:
    seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
    return min(SLOTS_PER_DAY, math.ceil(seconds / (SLOT_MINUTES * 60)))


def _slot_start(moment: datetime) -> datetime:
    return moment.replace(
        minute=moment.minute - moment.minute % SLOT_MINUTES, second=0, microsecond=0
    )


def spread_draws(
    day: date, draws: Iterable[ConsumptionEvent], tz: tzinfo
) -> list[float]:
    """Spread the energy of the draws evenly over the slots of a local day."""
    slots = [0.0] * SLOTS_PER_DAY
    day_start = datetime.combine(day, time.min, tz).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    for draw in draws:
        start = draw.start.astimezone(UTC)
        end = draw.end.astimezone(UTC)
        if end <= start:
            if day_start <= start < day_end:
                slots[_slot_index(start.astimezone(tz))] += draw.energy_kwh
            continue
        duration = (end - start).total_seconds()
        moment = start
        while moment < end:
            local = moment.astimezone(tz)
            boundary = (_slot_start(local) + SLOT).astimezone(UTC)
            step_end = min(end, boundary)
            if day_start <= moment < day_end:
                slots[_slot_index(local)] += (
                    draw.energy_kwh * (step_end - moment).total_seconds() / duration
                )
            moment = step_end
    return slots


@dataclass(slots=True)
class _Coverage:
    covered: timedelta = timedelta(0)
    occupied: timedelta = timedelta(0)
    workday: bool | None = None


class DemandModel:
    """Learned usage profile, updated with the tank model draws."""

    def __init__(self, tz: tzinfo) -> None:
        """Create an empty profile for a local time zone."""
        self._tz = tz
        self._observations: dict[date, DayObservation] = {}
        self._marks: list[ShortageMark] = []
        self._coverage: dict[date, _Coverage] = {}
        self._last_update: datetime | None = None
        self._last_draw_start: datetime | None = None

    @property
    def tz(self) -> tzinfo:
        """Return the local time zone of the profile."""
        return self._tz

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    @property
    def observations(self) -> list[DayObservation]:
        """Return the observed days, oldest first."""
        return [self._observations[day] for day in sorted(self._observations)]

    @property
    def marks(self) -> list[ShortageMark]:
        """Return the active shortage (or boost) marks."""
        return list(self._marks)

    def status(self) -> DemandStatus:
        """Return whether the profile has enough days."""
        if len(self._observations) < MIN_READY_DAYS:
            return DemandStatus.LEARNING
        return DemandStatus.READY

    def update(
        self,
        moment: datetime,
        *,
        available: bool,
        occupied: bool | None,
        draws: Sequence[ConsumptionEvent],
        workday: bool | None = None,
    ) -> None:
        """Account one sample: coverage, new shortages and finished days.

        ``occupied`` None means unknown (no occupancy entity): occupied.
        ``workday`` is the workday sensor state (None: no sensor).
        """
        local = moment.astimezone(self._tz)
        previous = self._last_update
        self._last_update = local
        coverage = self._coverage.setdefault(local.date(), _Coverage())
        if workday is not None:
            coverage.workday = workday
        if previous is not None and local > previous:
            step = min(local - previous, MAX_STEP)
            if available:
                coverage.covered += step
                if occupied is not False:
                    coverage.occupied += step

        for draw in draws:
            if (
                self._last_draw_start is not None
                and draw.start <= self._last_draw_start
            ):
                continue
            self._last_draw_start = draw.start
            if draw.shortage:
                self.add_mark(ShortageMark(draw.start, draw.end))

        for day in sorted(self._coverage):
            day_end = datetime.combine(day + timedelta(days=1), time.min, self._tz)
            if local < day_end + FINALIZE_DELAY:
                break
            self._finalize(day, draws)

        self._prune(local)

    def add_mark(self, mark: ShortageMark) -> None:
        """Raise the forecast around a draw (shortage, or a boost if enabled)."""
        if any(existing.start == mark.start for existing in self._marks):
            return
        self._marks.append(mark)

    def _finalize(self, day: date, draws: Sequence[ConsumptionEvent]) -> None:
        coverage = self._coverage.pop(day)
        if coverage.covered < MIN_DAY_COVERAGE or coverage.occupied < MIN_DAY_COVERAGE:
            return
        day_draws = [
            draw for draw in draws if draw.start.astimezone(self._tz).date() == day
        ]
        self._observations[day] = DayObservation(
            day, tuple(spread_draws(day, day_draws, self._tz)), coverage.workday
        )

    def _prune(self, now: datetime) -> None:
        oldest = now.date() - timedelta(days=HISTORY_DAYS)
        for day in [day for day in self._observations if day < oldest]:
            del self._observations[day]
        self._marks = [
            mark for mark in self._marks if now - mark.start < SHORTAGE_DECAY
        ]

    def reset(self) -> None:
        """Forget the learned profile."""
        self._observations.clear()
        self._marks.clear()
        self._coverage.clear()

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def similar_days(
        self,
        day: date,
        params: DemandParams,
        workday: bool | None = None,
    ) -> list[DayObservation]:
        """Return the last observed days of the same type before ``day``."""
        group = day_type(day, params.mode, workday)
        earlier = [obs for obs in self.observations if obs.day < day]
        candidates = [
            obs
            for obs in earlier
            if day_type(obs.day, params.mode, obs.workday) == group
        ]
        if len(candidates) < MIN_TYPE_OBSERVATIONS:
            candidates = earlier
        return candidates[-params.observations :]

    def weight(
        self, observation: DayObservation, now: datetime, params: DemandParams
    ) -> float:
        """Return the recency weight of an observed day."""
        age = (now.astimezone(self._tz).date() - observation.day).days
        return 0.5 ** (age / params.half_life_days)

    def _curves(
        self,
        day: date,
        now: datetime,
        params: DemandParams,
        workday: bool | None,
    ) -> _DayCurves:
        """Weighted, shortage-adjusted observations for a day of the forecast."""
        candidates = self.similar_days(day, params, workday)
        factors = self._slot_factors(now)
        rows = [
            [value * factor for value, factor in zip(obs.slots, factors, strict=True)]
            for obs in candidates
        ]
        weights = [self.weight(obs, now, params) for obs in candidates]
        curves = _DayCurves(rows, weights)
        if not rows:
            return curves
        cumulative = [list(_accumulate(row)) for row in rows]
        for quantile, target in (
            (Q50, curves.cumulative_p50),
            (Q90, curves.cumulative_p90),
        ):
            for index in range(SLOTS_PER_DAY + 1):
                target.append(
                    weighted_quantile(
                        [row[index] for row in cumulative], weights, quantile
                    )
                )
        return curves

    def _slot_factors(self, now: datetime) -> list[float]:
        factors = [1.0] * SLOTS_PER_DAY
        for mark in self._marks:
            age = now - mark.start
            if age < timedelta(0) or age >= SHORTAGE_DECAY:
                continue
            uplift = 1 + (SHORTAGE_FACTOR - 1) * (1 - age / SHORTAGE_DECAY)
            start = mark.start.astimezone(self._tz)
            end = max(mark.end.astimezone(self._tz), start)
            first = _slot_index(start) - SHORTAGE_SPREAD_SLOTS
            last = _slot_index(end) + SHORTAGE_SPREAD_SLOTS
            for index in range(first, last + 1):
                factors[index % SLOTS_PER_DAY] *= uplift
        return factors

    def forecast(
        self,
        now: datetime,
        params: DemandParams,
        *,
        hours: float = 36,
        occupied: bool | None = True,
        workdays: Mapping[date, bool] | None = None,
    ) -> DemandForecast:
        """Return the P50/P90 demand of each slot from ``now`` on.

        ``workdays`` gives the workday status of the days of the horizon
        (from a workday sensor); missing days follow the calendar.
        """
        workdays = workdays or {}
        local = now.astimezone(self._tz)
        start = _slot_start(local)
        result = DemandForecast(
            start=start, slots=[], occupied=occupied is not False, tz=self._tz
        )
        moment = start
        for _ in range(int(hours * 60 // SLOT_MINUTES)):
            day = moment.date()
            curves = result.days.get(day)
            if curves is None:
                curves = self._curves(day, local, params, workdays.get(day))
                result.days[day] = curves
            index = _slot_index(moment)
            if result.occupied and curves.rows:
                p50 = curves.cumulative_p50[index + 1] - curves.cumulative_p50[index]
                p90 = curves.cumulative_p90[index + 1] - curves.cumulative_p90[index]
            else:
                p50 = p90 = 0.0
            result.slots.append(ForecastSlot(moment, max(0.0, p50), max(0.0, p90)))
            moment = (moment.astimezone(UTC) + SLOT).astimezone(self._tz)
        return result

    def day_totals(
        self,
        day: date,
        now: datetime,
        params: DemandParams,
        workday: bool | None = None,
    ) -> tuple[float, float]:
        """Return the P50 and P90 of the total demand of a local day."""
        curves = self._curves(day, now, params, workday)
        if not curves.rows:
            return 0.0, 0.0
        return curves.cumulative_p50[-1], curves.cumulative_p90[-1]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent part of the profile."""
        return {
            "format": STORAGE_FORMAT,
            "observations": [
                {
                    "day": obs.day.isoformat(),
                    "workday": obs.workday,
                    "slots": [round(value, 4) for value in obs.slots],
                }
                for obs in self.observations
            ],
            "marks": [
                {
                    "start": mark.start.isoformat(),
                    "end": mark.end.isoformat(),
                    "reason": mark.reason,
                }
                for mark in self._marks
            ],
            "coverage": {
                day.isoformat(): {
                    "covered": cov.covered.total_seconds(),
                    "occupied": cov.occupied.total_seconds(),
                    "workday": cov.workday,
                }
                for day, cov in self._coverage.items()
            },
            "last_update": _iso(self._last_update),
            "last_draw_start": _iso(self._last_draw_start),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], tz: tzinfo) -> DemandModel:
        """Restore a profile saved with :meth:`as_dict`.

        Raises ValueError if the data is not usable.
        """
        try:
            if data.get("format") != STORAGE_FORMAT:
                raise ValueError(f"unsupported format {data.get('format')!r}")
            model = cls(tz)
            for item in data["observations"]:
                slots = tuple(float(value) for value in item["slots"])
                if len(slots) != SLOTS_PER_DAY:
                    raise ValueError("wrong number of slots")
                day = date.fromisoformat(item["day"])
                workday = item.get("workday")
                model._observations[day] = DayObservation(
                    day, slots, None if workday is None else bool(workday)
                )
            for item in data["marks"]:
                model._marks.append(
                    ShortageMark(
                        datetime.fromisoformat(item["start"]),
                        datetime.fromisoformat(item["end"]),
                        str(item.get("reason", "shortage")),
                    )
                )
            for day, item in data["coverage"].items():
                workday = item.get("workday")
                model._coverage[date.fromisoformat(day)] = _Coverage(
                    timedelta(seconds=float(item["covered"])),
                    timedelta(seconds=float(item["occupied"])),
                    None if workday is None else bool(workday),
                )
            model._last_update = _from_iso(data["last_update"])
            model._last_draw_start = _from_iso(data["last_draw_start"])
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise ValueError(f"invalid demand data: {err}") from err
        return model


def _accumulate(values: Sequence[float]) -> Iterable[float]:
    total = 0.0
    yield total
    for value in values:
        total += value
        yield total


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
