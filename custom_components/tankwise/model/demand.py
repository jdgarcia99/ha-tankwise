"""Hot water demand model: learned usage profile.

The model keeps the history of detected draws (and failure marks) and the
set of days it observed, and derives the profile on demand:

* 96 local-time slots of 15 minutes per day, for three day types
  (workday, Saturday, Sunday/holiday);
* recent weeks weigh more (exponential decay with a configurable half-life);
* forecasts use a percentile of the *cumulative* demand curves of past days,
  which keeps the correlation between slots (someone who showers at 07:00
  rarely showers again at 07:15) and does not overestimate the total demand
  the way summing per-slot percentiles would;
* forecast failures (a draw started without enough useful energy, or the
  regulator manual mode switched on by a person) make the affected day weigh
  more and its demand count a bit higher;
* with fewer than ``min_days`` of data the forecast is blended with a
  default profile and the model reports itself as learning.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

STORAGE_FORMAT = 1
SLOTS_PER_DAY = 96
SLOT_MINUTES = 15

HALF_LIFE_DAYS = 21.0
HISTORY_DAYS = 90
MIN_DAYS = 14
SHOWER_MIN_KWH = 0.8
MIN_SHOWERS = 5
FAILURE_WEIGHT = 2.0
FAILURE_ENERGY_FACTOR = 1.25
FAILURE_USEFUL_FRACTION = 0.5  # of the shower energy, at the start of a draw
MANUAL_LINK_WINDOW = timedelta(hours=2)
ACTIVE_SLOT_KWH = 0.05  # a slot with more energy than this "has a draw"
NEXT_DRAW_MIN_KWH = 0.3
NEXT_DRAW_EPSILON_KWH = 0.02
NEXT_DRAW_GAP_SLOTS = 4

COMFORT_PERCENTILES = {"p80": 0.80, "p90": 0.90, "p95": 0.95}


class DayType(StrEnum):
    """Kind of day for the usage profile."""

    WORKDAY = "workday"
    SATURDAY = "saturday"
    SUNDAY_HOLIDAY = "sunday_holiday"


class DemandStatus(StrEnum):
    """Whether the profile has enough data."""

    LEARNING = "learning"
    READY = "ready"


class FailureReason(StrEnum):
    """Why a forecast failure was recorded."""

    LOW_ENERGY = "low_energy"
    MANUAL_MODE = "manual_mode"


def day_type_for(day: date, is_workday: bool | None = None) -> DayType:
    """Return the day type of a date.

    Saturdays are always ``SATURDAY`` (also when they are holidays). A Monday
    to Friday that is not a workday is a holiday. Without workday information
    the day of the week decides.
    """
    weekday = day.weekday()
    if weekday == 5:
        return DayType.SATURDAY
    if weekday == 6 or is_workday is False:
        return DayType.SUNDAY_HOLIDAY
    return DayType.WORKDAY


def slot_of(moment: datetime) -> int:
    """Return the 15-minute slot (0-95) of a local datetime."""
    return (moment.hour * 60 + moment.minute) // SLOT_MINUTES


def comfort_percentile(level: str) -> float:
    """Return the percentile (0-1) of a comfort level such as ``"p90"``."""
    return COMFORT_PERCENTILES.get(level, COMFORT_PERCENTILES["p90"])


def _default_vector(entries: Iterable[tuple[int, int, float]]) -> tuple[float, ...]:
    vector = [0.0] * SLOTS_PER_DAY
    for hour, minute, energy in entries:
        vector[(hour * 60 + minute) // SLOT_MINUTES] += energy
    return tuple(vector)


# Typical use of the reference household (short shower on workday mornings,
# strong showers on weekend mornings and two showers in the evening).
DEFAULT_PROFILE: dict[DayType, tuple[float, ...]] = {
    DayType.WORKDAY: _default_vector([(6, 0, 0.9), (19, 0, 1.8), (21, 0, 1.8)]),
    DayType.SATURDAY: _default_vector(
        [(8, 0, 1.8), (8, 45, 1.8), (19, 0, 1.8), (21, 0, 1.8)]
    ),
    DayType.SUNDAY_HOLIDAY: _default_vector(
        [(8, 0, 1.8), (8, 45, 1.8), (19, 0, 1.8), (21, 0, 1.8)]
    ),
}


@dataclass(frozen=True, slots=True)
class Draw:
    """A hot water draw used by the profile."""

    start: datetime
    energy_kwh: float
    day_type: DayType
    failure: bool = False
    imported: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "start": self.start.isoformat(),
            "energy_kwh": round(self.energy_kwh, 4),
            "day_type": self.day_type.value,
            "failure": self.failure,
            "imported": self.imported,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Draw:
        """Rebuild a draw from :meth:`as_dict` output."""
        return cls(
            start=datetime.fromisoformat(data["start"]),
            energy_kwh=float(data["energy_kwh"]),
            day_type=DayType(data["day_type"]),
            failure=bool(data["failure"]),
            imported=bool(data["imported"]),
        )


@dataclass(frozen=True, slots=True)
class FailureMark:
    """A forecast failure not tied (yet) to a draw."""

    timestamp: datetime
    day_type: DayType
    reason: FailureReason
    linked: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "day_type": self.day_type.value,
            "reason": self.reason.value,
            "linked": self.linked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureMark:
        """Rebuild a mark from :meth:`as_dict` output."""
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            day_type=DayType(data["day_type"]),
            reason=FailureReason(data["reason"]),
            linked=bool(data["linked"]),
        )


@dataclass(frozen=True, slots=True)
class ForecastSlot:
    """Forecast demand of one 15-minute slot."""

    start: datetime
    energy_kwh: float
    probability: float


@dataclass(frozen=True, slots=True)
class NextDraw:
    """The next expected draw."""

    start: datetime
    energy_kwh: float
    probability: float
    day_type: DayType


@dataclass(frozen=True, slots=True)
class SlotProfile:
    """Learned statistics of one slot (for diagnostics)."""

    probability: float
    mean_energy_kwh: float


class DemandModel:
    """Learned hot water usage profile."""

    def __init__(
        self,
        *,
        half_life_days: float = HALF_LIFE_DAYS,
        history_days: int = HISTORY_DAYS,
        min_days: int = MIN_DAYS,
    ) -> None:
        """Create an empty model."""
        self.half_life_days = half_life_days
        self.history_days = history_days
        self.min_days = min_days
        self._draws: list[Draw] = []
        self._failures: list[FailureMark] = []
        self._observed: dict[date, DayType] = {}

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    @property
    def draws(self) -> list[Draw]:
        """Return the recorded draws, oldest first."""
        return list(self._draws)

    @property
    def failures(self) -> list[FailureMark]:
        """Return the failure marks, oldest first."""
        return list(self._failures)

    def observe(self, now: datetime, day_type: DayType) -> None:
        """Record that the model was running on the local day of ``now``."""
        self._observed[now.date()] = day_type
        self._prune(now.date())

    def is_observed(self, day: date) -> bool:
        """Return True if the day is already in the history."""
        return day in self._observed

    def add_draw(
        self,
        start: datetime,
        energy_kwh: float,
        day_type: DayType,
        *,
        useful_at_start_kwh: float,
        shower_energy_kwh: float,
        imported: bool = False,
    ) -> Draw:
        """Record a draw and decide whether it reveals a forecast failure.

        A draw is a failure when it starts with less useful energy than half
        a shower, or when a manual failure mark (e.g. the regulator manual
        mode switched on by a person) precedes it by less than two hours.
        A draw that simply ends below the minimum useful temperature is
        normal (a strong shower does that) and is not a failure.
        """
        failure = useful_at_start_kwh < FAILURE_USEFUL_FRACTION * shower_energy_kwh
        for index, mark in enumerate(self._failures):
            if (
                not mark.linked
                and mark.reason is FailureReason.MANUAL_MODE
                and timedelta(0) <= start - mark.timestamp <= MANUAL_LINK_WINDOW
            ):
                self._failures[index] = replace(mark, linked=True)
                failure = True
                break
        draw = Draw(
            start=start,
            energy_kwh=energy_kwh,
            day_type=day_type,
            failure=failure,
            imported=imported,
        )
        self._draws.append(draw)
        self._draws.sort(key=lambda item: item.start)
        return draw

    def add_failure(
        self, timestamp: datetime, day_type: DayType, reason: FailureReason
    ) -> None:
        """Record a forecast failure (e.g. manual mode switched on by a person)."""
        self._failures.append(FailureMark(timestamp, day_type, reason))
        self._failures.sort(key=lambda item: item.timestamp)

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def days_with_data(self, today: date) -> int:
        """Return the number of complete observed days."""
        return sum(1 for day in self._observed if day < today)

    def status(self, today: date) -> DemandStatus:
        """Return whether the profile is still learning."""
        if self.days_with_data(today) < self.min_days:
            return DemandStatus.LEARNING
        return DemandStatus.READY

    def shower_energy(self, today: date) -> float | None:
        """Return the learned energy of a shower, or None if not enough data."""
        showers = [
            (draw.energy_kwh, self._age_weight(draw.start.date(), today))
            for draw in self._draws
            if draw.energy_kwh >= SHOWER_MIN_KWH
        ]
        if len(showers) < MIN_SHOWERS:
            return None
        return _weighted_percentile(showers, 0.5)

    def slot_profile(self, day_type: DayType, today: date) -> list[SlotProfile]:
        """Return the learned probability and mean energy of every slot."""
        days = self._day_vectors(day_type, today)
        total = sum(weight for weight, _ in days)
        profile = []
        for slot in range(SLOTS_PER_DAY):
            if total <= 0:
                profile.append(SlotProfile(0.0, 0.0))
                continue
            active = sum(w for w, vec in days if vec[slot] > ACTIVE_SLOT_KWH)
            mean = sum(w * vec[slot] for w, vec in days) / total
            profile.append(SlotProfile(active / total, mean))
        return profile

    def forecast(
        self,
        now: datetime,
        hours: float,
        percentile: float,
        day_type_of: Callable[[date], DayType],
    ) -> list[ForecastSlot]:
        """Forecast the demand of every slot from ``now`` for ``hours``.

        Within each day, the cumulative demand is the given percentile of
        the cumulative curves of past days of the same type; days are then
        chained. ``now`` must be a timezone-aware local datetime.
        """
        today = now.date()
        start = now.replace(
            minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES, second=0, microsecond=0
        )
        slots: list[datetime] = []
        moment = start
        end = now + timedelta(hours=hours)
        while moment < end:
            slots.append(moment)
            moment = _next_slot(moment)

        result: list[ForecastSlot] = []
        index = 0
        while index < len(slots):
            day = slots[index].date()
            segment = [s for s in slots[index:] if s.date() == day]
            index += len(segment)
            result.extend(
                self._forecast_segment(segment, day_type_of(day), percentile, today)
            )
        return result

    def next_draw(
        self,
        now: datetime,
        percentile: float,
        day_type_of: Callable[[date], DayType],
        hours: float = 36,
    ) -> NextDraw | None:
        """Return the next expected draw within ``hours``, if any."""
        forecast = self.forecast(now, hours, percentile, day_type_of)
        cumulative = 0.0
        run_start: int | None = None
        for index, slot in enumerate(forecast):
            if slot.energy_kwh > NEXT_DRAW_EPSILON_KWH:
                if run_start is None:
                    run_start = index
                cumulative += slot.energy_kwh
            elif run_start is not None and cumulative < NEXT_DRAW_MIN_KWH:
                run_start, cumulative = None, 0.0
            if run_start is not None and cumulative >= NEXT_DRAW_MIN_KWH:
                break
        else:
            return None
        assert run_start is not None

        energy = 0.0
        probability = 0.0
        quiet = 0
        for slot in forecast[run_start:]:
            if slot.energy_kwh > NEXT_DRAW_EPSILON_KWH:
                quiet = 0
                energy += slot.energy_kwh
                probability = max(probability, slot.probability)
            else:
                quiet += 1
                if quiet >= NEXT_DRAW_GAP_SLOTS:
                    break
        first = forecast[run_start].start
        return NextDraw(
            start=first,
            energy_kwh=energy,
            probability=probability,
            day_type=day_type_of(first.date()),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent state."""
        return {
            "format": STORAGE_FORMAT,
            "observed_days": {
                day.isoformat(): kind.value for day, kind in self._observed.items()
            },
            "draws": [draw.as_dict() for draw in self._draws],
            "failures": [mark.as_dict() for mark in self._failures],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DemandModel:
        """Restore a model saved with :meth:`as_dict`.

        Raises ValueError if the data is not usable.
        """
        try:
            if data.get("format") != STORAGE_FORMAT:
                raise ValueError(f"unsupported format {data.get('format')!r}")
            model = cls()
            model._observed = {
                date.fromisoformat(day): DayType(kind)
                for day, kind in data["observed_days"].items()
            }
            model._draws = sorted(
                (Draw.from_dict(item) for item in data["draws"]),
                key=lambda item: item.start,
            )
            model._failures = sorted(
                (FailureMark.from_dict(item) for item in data["failures"]),
                key=lambda item: item.timestamp,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise ValueError(f"invalid demand model data: {err}") from err
        return model

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prune(self, today: date) -> None:
        oldest = today - timedelta(days=self.history_days)
        self._observed = {d: k for d, k in self._observed.items() if d >= oldest}
        self._draws = [d for d in self._draws if d.start.date() >= oldest]
        self._failures = [f for f in self._failures if f.timestamp.date() >= oldest]

    def _age_weight(self, day: date, today: date) -> float:
        age = max((today - day).days, 0)
        return 0.5 ** (age / self.half_life_days)

    def _day_vectors(
        self, day_type: DayType, today: date
    ) -> list[tuple[float, list[float]]]:
        """Return (weight, energy per slot) of every complete observed day."""
        vectors: dict[date, list[float]] = {
            day: [0.0] * SLOTS_PER_DAY
            for day, kind in self._observed.items()
            if kind is day_type and day < today
        }
        failed_days: set[date] = set()
        for draw in self._draws:
            day = draw.start.date()
            if day not in vectors:
                continue
            energy = draw.energy_kwh
            if draw.failure:
                energy *= FAILURE_ENERGY_FACTOR
                failed_days.add(day)
            vectors[day][slot_of(draw.start)] += energy
        failed_days.update(
            mark.timestamp.date()
            for mark in self._failures
            if mark.timestamp.date() in vectors
        )
        return [
            (
                self._age_weight(day, today)
                * (FAILURE_WEIGHT if day in failed_days else 1.0),
                vector,
            )
            for day, vector in sorted(vectors.items())
        ]

    def _forecast_segment(
        self,
        segment: list[datetime],
        day_type: DayType,
        percentile: float,
        today: date,
    ) -> list[ForecastSlot]:
        slots = [slot_of(moment) for moment in segment]
        days = self._day_vectors(day_type, today)
        # Cold start: blend with the default profile until min_days of data.
        blend = min(self.days_with_data(today) / self.min_days, 1.0) if days else 0.0
        default = DEFAULT_PROFILE[day_type]

        learned_cum: list[float] = []
        learned_prob: list[float] = []
        if days:
            running = [0.0] * len(days)
            total = sum(weight for weight, _ in days)
            previous = 0.0
            for slot in slots:
                for i, (_, vector) in enumerate(days):
                    running[i] += vector[slot]
                value = _weighted_percentile(
                    [(running[i], days[i][0]) for i in range(len(days))], percentile
                )
                previous = max(previous, value)
                learned_cum.append(previous)
                learned_prob.append(
                    sum(w for w, vec in days if vec[slot] > ACTIVE_SLOT_KWH) / total
                )

        result = []
        cumulative_before = 0.0
        default_running = 0.0
        for index, moment in enumerate(segment):
            default_running += default[slots[index]]
            learned = learned_cum[index] if days else 0.0
            cumulative = blend * learned + (1 - blend) * default_running
            probability = (blend * learned_prob[index] if days else 0.0) + (
                1 - blend
            ) * (1.0 if default[slots[index]] > 0 else 0.0)
            result.append(
                ForecastSlot(
                    start=moment,
                    energy_kwh=max(cumulative - cumulative_before, 0.0),
                    probability=probability,
                )
            )
            cumulative_before = max(cumulative_before, cumulative)
        return result


def _next_slot(moment: datetime) -> datetime:
    """Return the start of the next local 15-minute slot (DST-safe)."""
    tzinfo = moment.tzinfo
    return (moment.astimezone(UTC) + timedelta(minutes=SLOT_MINUTES)).astimezone(tzinfo)


def _weighted_percentile(values: list[tuple[float, float]], percentile: float) -> float:
    """Return the weighted percentile of (value, weight) pairs."""
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return 0.0
    threshold = percentile * total
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= threshold - 1e-12:
            return value
    return ordered[-1][0]
