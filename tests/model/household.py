"""Synthetic household draws for the demand profile tests.

Pattern of the reference home: a shower at 06:30 every day, a shower after
the gym every evening (17:00-19:00) and, every other day, one or two more
showers in the evening (17:30-19:30). Start times move by a few minutes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
import random
from zoneinfo import ZoneInfo

from tankwise_model.demand import DemandModel
from tankwise_model.tank import ConsumptionEvent

TZ = ZoneInfo("Europe/Madrid")
MORNING_KWH = 1.4
EVENING_KWH = 1.3
SHOWER = timedelta(minutes=10)


def shower(
    start: datetime, energy: float, *, shortage: bool = False
) -> ConsumptionEvent:
    return ConsumptionEvent(
        start=start,
        end=start + SHOWER,
        energy_kwh=energy,
        start_temperature=60.0,
        min_temperature=36.0 if shortage else 48.0,
        while_heating=False,
        shortage=shortage,
    )


def household(first: date, days: int, seed: int = 1) -> list[ConsumptionEvent]:
    """Draws of the reference household; even day numbers are "strong"."""
    rng = random.Random(seed)
    draws = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        draws.append(
            shower(
                datetime.combine(day, time(6, 30), TZ)
                + timedelta(minutes=rng.randint(-10, 10)),
                MORNING_KWH,
            )
        )
        gym = datetime.combine(day, time(17, 0), TZ) + timedelta(
            minutes=rng.randint(0, 120)
        )
        draws.append(shower(gym, EVENING_KWH))
        if offset % 2 == 0:
            for _ in range(rng.choice((1, 2))):
                extra = datetime.combine(day, time(17, 30), TZ) + timedelta(
                    minutes=rng.randint(0, 120)
                )
                draws.append(shower(extra, EVENING_KWH))
    return sorted(draws, key=lambda draw: draw.start)


def feed(
    model: DemandModel,
    first: date,
    days: int,
    draws: Sequence[ConsumptionEvent],
    *,
    occupied: Callable[[datetime], bool | None] = lambda moment: True,
    available: Callable[[datetime], bool] = lambda moment: True,
    workday: Callable[[date], bool | None] = lambda day: None,
    step: timedelta = timedelta(minutes=5),
) -> datetime:
    """Run the demand model over ``days`` days (plus the finalize delay)."""
    moment = datetime.combine(first, time.min, TZ)
    end = moment + timedelta(days=days, hours=2)
    index = 0
    closed: list[ConsumptionEvent] = []
    while moment <= end:
        while index < len(draws) and draws[index].end <= moment:
            closed.append(draws[index])
            index += 1
        model.update(
            moment,
            available=available(moment),
            occupied=occupied(moment),
            draws=closed,
            workday=workday(moment.astimezone(TZ).date()),
        )
        moment += step
    return moment
