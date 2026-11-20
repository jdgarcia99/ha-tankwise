"""The demand profile learned from the real history of the house.

Skipped unless an export made with tools/export_history.py is present in
tests/fixtures (it is personal data and is never committed). Run with
``pytest tests/simulation -s`` to print the learned profile hour by hour.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta

import pytest

from custom_components.tankwise.model.demand import (
    DayTypeMode,
    DemandModel,
    DemandParams,
)
from custom_components.tankwise.model.replay import (
    ROLE_OCCUPANCY,
    ROLE_WORKDAY,
    Value,
    replay,
)
from custom_components.tankwise.model.tank import (
    TankModel,
    TankParams,
    TankSample,
    TankState,
)
from custom_components.tankwise.model.water import mains_water_temperature

from .history import History, find_export, load

EXPORT = find_export()
pytestmark = pytest.mark.skipif(
    EXPORT is None, reason="no history export in tests/fixtures"
)
PARAMS = DemandParams(mode=DayTypeMode.ALL)


@pytest.fixture(scope="module")
def learned() -> tuple[History, TankModel, DemandModel, datetime]:
    assert EXPORT is not None
    history = load(EXPORT)
    model = TankModel()
    demand = DemandModel(history.tz)

    def params_at(moment: datetime) -> TankParams:
        day = moment.astimezone(history.tz).date()
        return TankParams(
            mains_temperature=mains_water_temperature("balearic_islands", day)
        )

    def feed(sample: TankSample, state: TankState, values: Mapping[str, Value]) -> None:
        occupancy, workday = values.get(ROLE_OCCUPANCY), values.get(ROLE_WORKDAY)
        demand.update(
            sample.timestamp,
            available=state.available,
            occupied=occupancy if isinstance(occupancy, bool) else None,
            draws=model.consumptions,
            workday=workday if isinstance(workday, bool) else None,
        )

    result = replay(history.series, params_at, model=model, on_sample=feed)
    assert result.end is not None
    return history, model, demand, result.end


def _hourly_report(demand: DemandModel, now: datetime, tz) -> str:
    day = (now + timedelta(days=1)).astimezone(tz).date()
    start = datetime.combine(day, time.min, tz)
    forecast = demand.forecast(start, PARAMS, hours=24)
    lines = ["hour   P50 kWh  P90 kWh"]
    for hour in range(24):
        begin = start + timedelta(hours=hour)
        end = begin + timedelta(hours=1)
        p50, p90 = forecast.window(begin, end, 0.5), forecast.window(begin, end, 0.9)
        bar = "#" * round(p90 * 10)
        lines.append(f"{hour:02d}:00  {p50:7.2f}  {p90:7.2f}  {bar}")
    p50, p90 = demand.day_totals(day, now, PARAMS)
    lines.append(f"day    {p50:7.2f}  {p90:7.2f}")
    return "\n".join(lines)


def test_days_are_observed(learned) -> None:
    history, model, demand, now = learned
    print(
        f"\n{len(model.consumptions)} draws, {len(demand.observations)} observed days"
    )
    for obs in demand.observations:
        print(f"  {obs.day}  {obs.total:5.2f} kWh")
    print(_hourly_report(demand, now, history.tz))
    assert len(demand.observations) >= 5


def test_morning_shower_at_0630(learned) -> None:
    history, _model, demand, now = learned
    day = (now + timedelta(days=1)).astimezone(history.tz).date()
    start = datetime.combine(day, time.min, history.tz)
    forecast = demand.forecast(start, PARAMS, hours=24)
    morning = forecast.window(
        start + timedelta(hours=6), start + timedelta(hours=7, minutes=30), 0.5
    )
    night = forecast.window(start, start + timedelta(hours=5, minutes=30), 0.9)
    assert morning >= 0.5, "no shower learned around 06:30"
    assert night < morning / 2


def test_evening_block_1700_1930(learned) -> None:
    history, _model, demand, now = learned
    day = (now + timedelta(days=1)).astimezone(history.tz).date()
    start = datetime.combine(day, time.min, history.tz)
    forecast = demand.forecast(start, PARAMS, hours=24)
    evening = forecast.window(
        start + timedelta(hours=17), start + timedelta(hours=19, minutes=30), 0.9
    )
    midday = forecast.window(
        start + timedelta(hours=9), start + timedelta(hours=16), 0.9
    )
    assert evening >= 1.0, "no evening showers learned between 17:00 and 19:30"
    assert evening > midday
