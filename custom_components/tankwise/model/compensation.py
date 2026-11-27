"""Compensation cap: the surplus compensation of a billing period cannot
exceed the energy term of that period.

Spain, simplified self-consumption compensation (PVPC): the surplus exported
in a billing period is paid at the injection price of each hour, but the
compensation is limited to the energy term of the bill of that period. Once
the compensation reaches it, exporting more pays nothing.

The tracker accounts, hour by hour, what was exported and imported and what
it is worth at the prices of that hour:

* compensation = Σ export of the hour * injection price of the hour,
* energy term = Σ import of the hour * purchase price of the hour,

and projects both to the end of the billing period with the hourly rate of
the last days. When the projected compensation exceeds the projected energy
term, the excess (€) is surplus that will be exported for nothing: the plan
may take that much surplus at no cost (see :func:`free_surplus_values`).

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

# The rate of the last days projects the rest of the period.
PROJECTION_DAYS = 14
# Hours kept: a whole period plus the projection days.
KEEP = timedelta(days=45)
# A gap between two readings longer than this is not accounted (the energy
# in between cannot be given an hour or a price).
MAX_GAP = timedelta(hours=2)
# The projection needs at least this many hours of data.
MIN_HOURS = 24


@dataclass(slots=True)
class HourTotals:
    """Energy and its value in one hour."""

    import_kwh: float = 0.0
    export_kwh: float = 0.0
    energy_eur: float = 0.0
    compensation_eur: float = 0.0

    def as_list(self) -> list[float]:
        return [
            round(self.import_kwh, 5),
            round(self.export_kwh, 5),
            round(self.energy_eur, 5),
            round(self.compensation_eur, 5),
        ]


@dataclass(frozen=True, slots=True)
class CompensationEstimate:
    """The compensation and the energy term of a billing period."""

    period_start: datetime
    period_end: datetime
    compensation: float  # € so far
    energy_term: float  # € so far
    projected_compensation: float  # € at the end of the period
    projected_energy_term: float  # € at the end of the period
    hours_learned: int  # hours behind the projection rate
    # Factor already applied to the energy term (calibration with a bill).
    energy_term_factor: float = 1.0

    @property
    def margin(self) -> float:
        """Projected energy term minus projected compensation (€)."""
        return self.projected_energy_term - self.projected_compensation

    @property
    def excess(self) -> float:
        """Compensation that will not be paid this period (€, 0 if none)."""
        return max(0.0, -self.margin)

    @property
    def margin_now(self) -> float:
        """Energy term minus compensation so far (€)."""
        return self.energy_term - self.compensation


def billing_period(now: datetime, tz: tzinfo, day: int) -> tuple[datetime, datetime]:
    """Return the billing period containing ``now``: from ``day`` of a month
    to ``day`` of the next one, at local midnight (day 1: the calendar month)."""
    local = now.astimezone(tz)
    day = min(max(int(day), 1), 28)
    start = date(local.year, local.month, day)
    if local.date() < start:
        start = _add_month(start, -1)
    end = _add_month(start, 1)
    return (
        datetime(start.year, start.month, start.day, tzinfo=tz),
        datetime(end.year, end.month, end.day, tzinfo=tz),
    )


def _add_month(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, day.day)


def _hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class CompensationTracker:
    """Hourly export, import and their value, for the compensation cap."""

    def __init__(self) -> None:
        """Initialise an empty tracker."""
        # UTC hour start -> totals of the hour.
        self.hours: dict[datetime, HourTotals] = {}
        # Last meter reading: (time, import total kWh, export total kWh).
        self._last: tuple[datetime, float, float] | None = None
        self._buy: float | None = None
        self._injection: float | None = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the state that must survive a restart."""
        return {
            "hours": {
                start.isoformat(): totals.as_list()
                for start, totals in sorted(self.hours.items())
            },
            "last": (
                [self._last[0].isoformat(), self._last[1], self._last[2]]
                if self._last
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompensationTracker:
        """Rebuild the tracker from :meth:`as_dict` output."""
        tracker = cls()
        for start, values in (data.get("hours") or {}).items():
            tracker.hours[datetime.fromisoformat(start)] = HourTotals(
                *(float(value) for value in values)
            )
        if last := data.get("last"):
            tracker._last = (
                datetime.fromisoformat(last[0]),
                float(last[1]),
                float(last[2]),
            )
        return tracker

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------

    def add_reading(
        self,
        now: datetime,
        import_total: float | None,
        export_total: float | None,
        buy: float | None,
        injection: float | None,
    ) -> None:
        """Account the energy since the last reading of the grid meters.

        ``now`` in UTC. The energy goes to the hour of ``now`` at the prices
        given (or the last known ones). A meter reset or a gap longer than
        :data:`MAX_GAP` is not accounted: only the baseline moves.
        """
        if buy is not None:
            self._buy = buy
        if injection is not None:
            self._injection = injection
        if import_total is None or export_total is None:
            return
        last = self._last
        self._last = (now, import_total, export_total)
        if last is None or now <= last[0] or now - last[0] > MAX_GAP:
            return
        imported = import_total - last[1]
        exported = export_total - last[2]
        if imported < 0 or exported < 0:
            return  # meter replaced or reset
        totals = self.hours.setdefault(_hour(now), HourTotals())
        totals.import_kwh += imported
        totals.export_kwh += exported
        totals.energy_eur += imported * (self._buy or 0.0)
        totals.compensation_eur += exported * (self._injection or 0.0)
        self.prune(now)

    def add_hour(
        self,
        start: datetime,
        import_kwh: float,
        export_kwh: float,
        buy: float,
        injection: float,
    ) -> bool:
        """Add a whole past hour (history import). Hours already known are kept.

        Returns True if it was added.
        """
        start = _hour(start)
        if start in self.hours:
            return False
        self.hours[start] = HourTotals(
            import_kwh=import_kwh,
            export_kwh=export_kwh,
            energy_eur=import_kwh * buy,
            compensation_eur=export_kwh * injection,
        )
        return True

    def prune(self, now: datetime) -> None:
        """Forget the hours older than :data:`KEEP`."""
        limit = now - KEEP
        for start in [start for start in self.hours if start < limit]:
            del self.hours[start]

    # ------------------------------------------------------------------
    # Estimate
    # ------------------------------------------------------------------

    def estimate(
        self,
        now: datetime,
        period: tuple[datetime, datetime],
        energy_term_factor: float = 1.0,
    ) -> CompensationEstimate:
        """Return the compensation and the energy term of the period.

        The rest of the period is projected with the hourly rate of the last
        :data:`PROJECTION_DAYS` days (hours with data only); with less than
        :data:`MIN_HOURS` hours of data nothing is projected. The energy term
        (so far and projected) is multiplied by ``energy_term_factor``: the
        bill may count the energy without the tolls and charges included in
        the purchase price.
        """
        start, end = period
        compensation = energy = 0.0
        for hour, totals in self.hours.items():
            if start <= hour < end and hour <= now:
                compensation += totals.compensation_eur
                energy += totals.energy_eur
        recent = [
            totals
            for hour, totals in self.hours.items()
            if now - timedelta(days=PROJECTION_DAYS) <= hour < _hour(now)
        ]
        projected_compensation, projected_energy = compensation, energy
        if len(recent) >= MIN_HOURS and end > now:
            remaining = (end - now).total_seconds() / 3600
            projected_compensation += (
                sum(t.compensation_eur for t in recent) / len(recent) * remaining
            )
            projected_energy += (
                sum(t.energy_eur for t in recent) / len(recent) * remaining
            )
        return CompensationEstimate(
            period_start=start,
            period_end=end,
            compensation=compensation,
            energy_term=energy * energy_term_factor,
            projected_compensation=projected_compensation,
            projected_energy_term=projected_energy * energy_term_factor,
            hours_learned=len(recent),
            energy_term_factor=energy_term_factor,
        )


def free_surplus_values(
    values: Sequence[float],
    solar_kwh: Sequence[float],
    excess: float,
    slots: int | None = None,
) -> list[float]:
    """Surplus value per slot once ``excess`` € of compensation will be lost.

    The first slots with expected surplus are free (value 0) until the surplus
    they hold is worth the excess at their own price; a slot that only partly
    fits keeps the value of the part that does not. Only the part that
    exceeds the cap is free: the rest keeps the injection price. ``slots``
    limits it to the first slots (those of the current billing period).
    """
    result = list(values)
    budget = max(0.0, excess)
    count = len(values) if slots is None else max(0, slots)
    for index, (value, kwh) in enumerate(
        zip(values[:count], solar_kwh[:count], strict=False)
    ):
        if budget <= 0:
            break
        worth = value * kwh
        if worth <= 0:
            continue
        if worth <= budget:
            result[index] = 0.0
            budget -= worth
        else:
            result[index] = value * (1 - budget / worth)
            budget = 0.0
    return result
