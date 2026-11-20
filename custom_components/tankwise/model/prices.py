"""Prices per slot: purchase price and value of the solar surplus.

The purchase price comes from a sensor with the hourly prices of today and
tomorrow as attributes (PVPC: ``price_00h`` … ``price_23h`` and, once
published, ``price_next_day_00h`` …). The surplus is valued at the injection
price (same attribute layout), at a fixed value when there is no injection
sensor, or at zero when the compensation cap of the month has been reached.

Hours without a published price (tomorrow before ~20:15, and always the
day after tomorrow at the end of a 36-hour horizon) use the price of the day
before at the same hour. ``tomorrow_known`` tells whether tomorrow's prices
were published, so the plan can be computed again when they arrive.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
import re
from statistics import median
from typing import Any

TODAY_KEY = re.compile(r"^price_(\d{2})h$")
TOMORROW_KEY = re.compile(r"^price_next_day_(\d{2})h$")


@dataclass(frozen=True, slots=True)
class DayPrices:
    """Hourly prices (€/kWh) of the local days they are known for."""

    days: dict[date, tuple[float | None, ...]]

    def at(self, moment: datetime) -> float | None:
        """Return the price of the hour containing ``moment`` (local time)."""
        hours = self.days.get(moment.date())
        if hours is None:
            return None
        return hours[moment.hour]


def parse_hourly_prices(attributes: Mapping[str, Any], today: date) -> DayPrices:
    """Read ``price_HHh`` (today) and ``price_next_day_HHh`` attributes."""
    days: dict[date, list[float | None]] = {}
    for key, value in attributes.items():
        for pattern, day in (
            (TODAY_KEY, today),
            (TOMORROW_KEY, today + timedelta(days=1)),
        ):
            match = pattern.match(key)
            if not match:
                continue
            hour = int(match.group(1))
            if not 0 <= hour <= 23:
                continue
            try:
                price = float(value)
            except TypeError, ValueError:
                continue
            days.setdefault(day, [None] * 24)[hour] = price
    return DayPrices({day: tuple(hours) for day, hours in days.items()})


@dataclass(frozen=True, slots=True)
class SlotPrices:
    """Purchase price and surplus value of each slot of a horizon."""

    start: datetime
    buy: tuple[float, ...]
    surplus: tuple[float, ...]
    tomorrow_known: bool

    def terminal_value(self) -> float:
        """Value of the energy left at the end: median night purchase price."""
        night = [
            price
            for index, price in enumerate(self.buy)
            if (self.start + timedelta(minutes=15 * index)).hour < 6
        ]
        return median(night) if night else median(self.buy)


def slot_prices(
    start: datetime,
    slots: int,
    tz: tzinfo,
    *,
    buy: DayPrices,
    injection: DayPrices | None,
    fixed_surplus_value: float | None,
    compensation_cap_reached: bool,
    current_buy: float | None = None,
    slot_minutes: int = 15,
) -> SlotPrices | None:
    """Build the prices of each slot from ``start`` on.

    Missing hours use today's price at the same hour (or the previous known
    price). Returns None if no purchase price is known at all.
    """
    known = [
        price for hours in buy.days.values() for price in hours if price is not None
    ]
    if not known and current_buy is None:
        return None
    buy_values: list[float] = []
    surplus_values: list[float] = []
    tomorrow = start.astimezone(tz).date() + timedelta(days=1)
    tomorrow_known = tomorrow in buy.days
    last_buy = current_buy if current_buy is not None else known[0]
    last_injection = 0.0
    for index in range(slots):
        moment = (start + timedelta(minutes=slot_minutes * index)).astimezone(tz)
        price = buy.at(moment)
        for days_back in (1, 2):
            if price is None:
                price = buy.at(moment - timedelta(days=days_back))
        if price is None:
            price = last_buy
        last_buy = price
        buy_values.append(price)

        if compensation_cap_reached:
            value = 0.0
        elif injection is not None:
            value = injection.at(moment)
            for days_back in (1, 2):
                if value is None:
                    value = injection.at(moment - timedelta(days=days_back))
            if value is None:
                value = last_injection
            last_injection = value
        else:
            value = fixed_surplus_value or 0.0
        surplus_values.append(max(0.0, value))
    return SlotPrices(start, tuple(buy_values), tuple(surplus_values), tomorrow_known)


def median_price(values: Sequence[float]) -> float:
    """Return the median of a non-empty sequence of prices."""
    return median(values)
