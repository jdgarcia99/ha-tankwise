"""Cost accounting: real heating cost, planned cost and rescue recharges.

* Real: every kWh the water heater takes is valued at the purchase price
  when it comes from the grid, or at the surplus value when the house was
  exporting at the time (the diverter heated with solar surplus).
* Planned: when a slot starts, the plan in force commits its decision for
  that slot (grid and solar energy and their cost). In shadow mode this is
  what TankWise would have done; it is compared with the real cost.
* Rescue recharges: the planned slots marked as rescue, in kWh and €.

Totals are kept for the current local day and month and reset when they
change. This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Any

STORAGE_FORMAT = 1
PERIODS = ("day", "month")


@dataclass(slots=True)
class Totals:
    """Accumulated values of one period."""

    real_cost: float = 0.0
    real_grid_kwh: float = 0.0
    real_solar_kwh: float = 0.0
    planned_cost: float = 0.0
    planned_grid_kwh: float = 0.0
    rescue_kwh: float = 0.0
    rescue_cost: float = 0.0
    deviations: int = 0

    def as_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable representation."""
        return {
            "real_cost": round(self.real_cost, 5),
            "real_grid_kwh": round(self.real_grid_kwh, 5),
            "real_solar_kwh": round(self.real_solar_kwh, 5),
            "planned_cost": round(self.planned_cost, 5),
            "planned_grid_kwh": round(self.planned_grid_kwh, 5),
            "rescue_kwh": round(self.rescue_kwh, 5),
            "rescue_cost": round(self.rescue_cost, 5),
            "deviations": self.deviations,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Totals:
        """Rebuild from :meth:`as_dict` output."""
        return cls(
            **{
                key: float(data.get(key, 0.0))
                for key in cls.__dataclass_fields__
                if key != "deviations"
            },
            deviations=int(data.get("deviations", 0)),
        )


@dataclass(slots=True)
class Ledger:
    """Totals of the current day and month."""

    tz: tzinfo
    day: date | None = None
    month: tuple[int, int] | None = None
    today: Totals = field(default_factory=Totals)
    this_month: Totals = field(default_factory=Totals)
    last_committed_slot: datetime | None = None

    def _roll(self, moment: datetime) -> None:
        local = moment.astimezone(self.tz).date()
        if self.day != local:
            self.day = local
            self.today = Totals()
        if self.month != (local.year, local.month):
            self.month = (local.year, local.month)
            self.this_month = Totals()

    def _add(self, moment: datetime, **values: float) -> None:
        self._roll(moment)
        for totals in (self.today, self.this_month):
            for key, value in values.items():
                setattr(totals, key, getattr(totals, key) + value)

    def add_real(
        self,
        moment: datetime,
        energy_kwh: float,
        *,
        from_surplus: bool,
        buy_price: float | None,
        surplus_value: float,
    ) -> None:
        """Account the energy the water heater took since the last sample."""
        if energy_kwh <= 0:
            self._roll(moment)
            return
        if from_surplus:
            self._add(
                moment,
                real_solar_kwh=energy_kwh,
                real_cost=energy_kwh * surplus_value,
            )
        else:
            self._add(
                moment,
                real_grid_kwh=energy_kwh,
                real_cost=energy_kwh * (buy_price or 0.0),
            )

    def commit_slot(
        self,
        slot_start: datetime,
        *,
        grid_kwh: float,
        cost: float,
        rescue: bool,
    ) -> bool:
        """Account the planned decision of a slot once; False if already done."""
        if (
            self.last_committed_slot is not None
            and slot_start <= self.last_committed_slot
        ):
            return False
        self.last_committed_slot = slot_start
        values = {"planned_grid_kwh": grid_kwh, "planned_cost": cost}
        if rescue:
            values |= {"rescue_kwh": grid_kwh, "rescue_cost": cost}
        self._add(slot_start, **values)
        return True

    def add_deviation(self, moment: datetime) -> None:
        """Count an external order that contradicted the plan."""
        self._roll(moment)
        self.today.deviations += 1
        self.this_month.deviations += 1

    def current(self, moment: datetime) -> tuple[Totals, Totals]:
        """Return the totals of today and this month at ``moment``."""
        self._roll(moment)
        return self.today, self.this_month

    def as_dict(self) -> dict[str, Any]:
        """Return the persistent state."""
        return {
            "format": STORAGE_FORMAT,
            "day": self.day.isoformat() if self.day else None,
            "month": list(self.month) if self.month else None,
            "today": self.today.as_dict(),
            "this_month": self.this_month.as_dict(),
            "last_committed_slot": (
                self.last_committed_slot.isoformat()
                if self.last_committed_slot
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], tz: tzinfo) -> Ledger:
        """Restore a ledger. Raises ValueError if the data is not usable."""
        try:
            if data.get("format") != STORAGE_FORMAT:
                raise ValueError(f"unsupported format {data.get('format')!r}")
            month = data.get("month")
            last = data.get("last_committed_slot")
            return cls(
                tz=tz,
                day=date.fromisoformat(data["day"]) if data.get("day") else None,
                month=(int(month[0]), int(month[1])) if month else None,
                today=Totals.from_dict(data["today"]),
                this_month=Totals.from_dict(data["this_month"]),
                last_committed_slot=datetime.fromisoformat(last) if last else None,
            )
        except (KeyError, TypeError, ValueError, IndexError) as err:
            raise ValueError(f"invalid ledger data: {err}") from err
