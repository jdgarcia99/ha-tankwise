"""Simulation bench: the plan against what really happened.

Walk-forward over the exported history, without looking ahead:

1. A first replay finds the real draws (with reconciled energy): they are
   the demand the simulated tank must serve.
2. A second replay learns the tank model and the usage profile minute by
   minute, as live. From ``start`` on, every 15 minutes it computes a plan
   with what it knows at that moment and drives a simulated, fully mixed
   tank: the real draws, the grid heating of the plan, and the real solar
   surplus when the plan allows it (diverter in automatic mode).
3. Costs and hot water shortages of the simulated tank are compared with
   the real ones, day by day.

Run ``python -m tests.simulation.bench`` for the report.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path

from custom_components.tankwise.model.appointments import Appointment
from custom_components.tankwise.model.demand import (
    DayTypeMode,
    DemandModel,
    DemandParams,
)
from custom_components.tankwise.model.optimizer import Plan, optimise
from custom_components.tankwise.model.planning import Mode, build_plan_input
from custom_components.tankwise.model.prices import (
    DayPrices,
    parse_hourly_prices,
    slot_prices,
)
from custom_components.tankwise.model.replay import (
    ROLE_MANUAL_MODE,
    ROLE_OCCUPANCY,
    ROLE_POWER,
    ROLE_WORKDAY,
    replay,
)
from custom_components.tankwise.model.tank import (
    ConsumptionEvent,
    TankModel,
    TankParams,
)
from custom_components.tankwise.model.water import mains_water_temperature

from .history import History, find_export, load

COMFORT = 38.0
SHOWER = 40.0
SHORTAGE_MARGIN = 1.5
SURPLUS_EXPORT_W = 50.0
DAYS7 = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
REFERENCE_APPOINTMENTS = (
    Appointment("m", "06:30", time(6, 30), frozenset({"workday"}), 40),
    Appointment("t", "17:00", time(17), DAYS7, 0, duration=timedelta(hours=3)),
)


@dataclass(slots=True)
class DayResult:
    """Simulated plan and reality for one local day."""

    day: date
    plan_cost: float = 0.0
    plan_grid_kwh: float = 0.0
    plan_solar_kwh: float = 0.0
    rescue_kwh: float = 0.0
    rescue_cost: float = 0.0
    real_cost: float = 0.0
    real_grid_kwh: float = 0.0
    real_solar_kwh: float = 0.0
    longest_heating_min: int = 0  # longest run of grid heating
    plan_shortages: list[str] = field(default_factory=list)
    real_shortages: list[str] = field(default_factory=list)


class PriceTimeline:
    """Price sensor states (with attributes) over time."""

    def __init__(self, path: Path) -> None:
        self.times: list[datetime] = []
        self.states: list[tuple[float | None, dict]] = []
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    value = float(row["state"])
                except ValueError:
                    value = None
                self.times.append(datetime.fromisoformat(row["last_changed"]))
                self.states.append((value, json.loads(row["attributes"] or "{}")))

    def at(self, moment: datetime, today: date) -> tuple[DayPrices, float | None]:
        index = bisect_right(self.times, moment) - 1
        value, attributes = self.states[max(index, 0)]
        return parse_hourly_prices(attributes, today), value


def _value_at(changes, moment: datetime):
    times = [change.timestamp for change in changes]
    index = bisect_right(times, moment) - 1
    return changes[index].value if index >= 0 else None


def _params_at(history: History):
    def params_at(moment: datetime) -> TankParams:
        day = moment.astimezone(history.tz).date()
        return TankParams(
            mains_temperature=mains_water_temperature("balearic_islands", day),
            comfort_min_temperature=COMFORT,
            shower_temperature=SHOWER,
        )

    return params_at


def real_draws(history: History) -> list[ConsumptionEvent]:
    """Draws of the whole period, with energy reconciled after each heating."""
    model = TankModel()
    replay(history.series, _params_at(history), model=model)
    return model.consumptions


def run_bench(
    history: History,
    *,
    start: datetime,
    appointments=REFERENCE_APPOINTMENTS,
    mode: Mode = Mode.AUTO,
    rescue: bool = True,
    reserve_liters: float = 10.0,
) -> list[DayResult]:
    """Simulate the plan from ``start`` to the end of the history."""
    tz = history.tz
    params_at = _params_at(history)
    draws = real_draws(history)
    buy_prices = PriceTimeline(history.folder / "price.csv")
    injection_prices = PriceTimeline(history.folder / "injection_price.csv")
    export = _load_numeric(history.folder / "export_power.csv")
    export_times = [moment for moment, _ in export]
    power_series = history.series[ROLE_POWER]
    manual_series = history.series.get(ROLE_MANUAL_MODE, [])

    # Draw energy per minute (start..end) and the draw each minute belongs to.
    draw_minutes: dict[datetime, tuple[float, int]] = {}
    for index, draw in enumerate(draws):
        begin = draw.start.replace(second=0, microsecond=0)
        minutes = max(1, int((draw.end - draw.start).total_seconds() // 60))
        for offset in range(minutes):
            draw_minutes[begin + timedelta(minutes=offset)] = (
                draw.energy_kwh / minutes,
                index,
            )

    model = TankModel()
    demand = DemandModel(tz)
    results: dict[date, DayResult] = defaultdict(lambda: DayResult(date.min))
    state = {
        "plan": None,
        "energy": None,
        "last_real": None,
        "short": set(),
        "run": 0,
    }

    def day_result(moment: datetime) -> DayResult:
        day = moment.astimezone(tz).date()
        result = results[day]
        result.day = day
        return result

    def export_at(moment: datetime) -> float:
        index = bisect_right(export_times, moment) - 1
        return export[index][1] if index >= 0 and export[index][1] is not None else 0.0

    def on_sample(sample, tank_state, values) -> None:
        moment = sample.timestamp
        occupancy, workday = values.get(ROLE_OCCUPANCY), values.get(ROLE_WORKDAY)
        demand.update(
            moment,
            available=tank_state.available,
            occupied=occupancy if isinstance(occupancy, bool) else None,
            draws=model.consumptions,
            workday=workday if isinstance(workday, bool) else None,
        )
        if moment < start or not tank_state.available:
            return
        params = params_at(moment)
        capacity = params.heat_capacity
        local = moment.astimezone(tz)
        buy, buy_now = buy_prices.at(moment, local.date())
        injection, _ = injection_prices.at(moment, local.date())

        # Real accounting (energy meter delta).
        energy_now = sample.energy
        if state["last_real"] is not None and energy_now is not None:
            used = max(0.0, energy_now - state["last_real"])
            result = day_result(moment)
            if export_at(moment) > SURPLUS_EXPORT_W:
                result.real_solar_kwh += used
                result.real_cost += used * (injection.at(local) or 0.0)
            else:
                result.real_grid_kwh += used
                result.real_cost += used * (buy.at(local) or buy_now or 0.0)
        state["last_real"] = energy_now

        if state["energy"] is None:  # the simulated tank starts as the real one
            state["energy"] = capacity * (
                tank_state.equivalent_temperature - params.mains_temperature
            )

        # Replan at each slot.
        if local.minute % 15 == 0:
            slot_begin = local.replace(second=0, microsecond=0)
            prices = slot_prices(
                slot_begin,
                144,
                tz,
                buy=buy,
                injection=injection,
                fixed_surplus_value=None,
                compensation_cap_reached=False,
                current_buy=buy_now,
            )
            forecast = demand.forecast(
                slot_begin,
                DemandParams(mode=DayTypeMode.WORKDAY_WEEKEND),
                hours=36,
                occupied=occupancy if isinstance(occupancy, bool) else None,
            )
            data = build_plan_input(
                start=slot_begin,
                tz=tz,
                mode=mode,
                temperature=params.mains_temperature + state["energy"] / capacity,
                volume_l=params.volume_l,
                mains_temperature=params.mains_temperature,
                ambient_temperature=params.ambient_temperature,
                comfort_temperature=COMFORT,
                shower_temperature=SHOWER,
                max_temperature=tank_state.cutoff_temperature,
                heater_power_w=tank_state.heater_power_w,
                capacity_factor=tank_state.capacity_factor,
                usable_factor=tank_state.usable_factor,
                ua_w_per_k=tank_state.ua_w_per_k,
                prices=prices,
                forecast=forecast,
                appointments=appointments,
                workdays={},
                occupied=occupancy if isinstance(occupancy, bool) else None,
                reserve_liters=reserve_liters,
                rescue=rescue,
            )
            state["plan"] = optimise(data) if data is not None else None

        plan: Plan | None = state["plan"]
        slot = plan.slots[0] if plan is not None else None
        result = day_result(moment)
        energy = state["energy"]
        e_max = capacity * (tank_state.cutoff_temperature - params.mains_temperature)
        room = max(0.0, e_max - energy)

        grid = min(room, slot.grid_kwh / 15) if slot else 0.0
        state["run"] = state["run"] + 1 if grid > 0 else 0
        result.longest_heating_min = max(result.longest_heating_min, state["run"])
        result.plan_grid_kwh += grid
        price_now = buy.at(local) or buy_now or 0.0
        result.plan_cost += grid * price_now
        if slot is not None and slot.rescue:
            result.rescue_kwh += grid
            result.rescue_cost += grid * price_now

        # Solar surplus really available: exported power plus what the
        # diverter really took in automatic mode.
        real_power = _value_at(power_series, moment) or 0.0
        manual = _value_at(manual_series, moment)
        surplus_w = export_at(moment) + (real_power if not manual else 0.0)
        solar = 0.0
        if slot is None or slot.solar_allowed:
            solar = min(max(0.0, room - grid), max(0.0, surplus_w) / 1000 / 60)
        result.plan_solar_kwh += solar
        result.plan_cost += solar * (injection.at(local) or 0.0)

        drawn, draw_index = draw_minutes.get(
            moment.replace(second=0, microsecond=0), (0.0, None)
        )
        temperature = params.mains_temperature + energy / capacity
        loss = 1.0 * (temperature - params.ambient_temperature) / 60 / 1000
        energy = energy + grid + solar - drawn - loss
        energy = max(0.0, min(e_max, energy))
        state["energy"] = energy
        temperature = params.mains_temperature + energy / capacity
        if (
            draw_index is not None
            and temperature < COMFORT - SHORTAGE_MARGIN
            and draw_index not in state["short"]
        ):
            state["short"].add(draw_index)
            result.plan_shortages.append(local.strftime("%H:%M"))

    replay(history.series, params_at, model=model, on_sample=on_sample)

    for draw in draws:
        if draw.shortage and draw.start >= start:
            day_result(draw.start).real_shortages.append(
                draw.start.astimezone(tz).strftime("%H:%M")
            )
    return [results[day] for day in sorted(results)]


def _load_numeric(path: Path) -> list[tuple[datetime, float | None]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                value = float(row["state"])
            except ValueError:
                value = None
            rows.append((datetime.fromisoformat(row["last_changed"]), value))
    return rows


def report(results: list[DayResult]) -> str:
    """Return the day by day table and the averages."""
    lines = [
        "day         plan €  grid kWh  solar kWh  rescue €   real €  real grid  real solar"
        "  longest run  shortages plan / real"
    ]
    for item in results:
        lines.append(
            f"{item.day:%a %d}   {item.plan_cost:6.2f}  {item.plan_grid_kwh:8.2f}"
            f"  {item.plan_solar_kwh:9.2f}  {item.rescue_cost:8.2f}  {item.real_cost:7.2f}"
            f"  {item.real_grid_kwh:9.2f}  {item.real_solar_kwh:10.2f}"
            f"  {item.longest_heating_min:7d} min"
            f"  {','.join(item.plan_shortages) or '-'} / {','.join(item.real_shortages) or '-'}"
        )
    full = [item for item in results if item.real_grid_kwh + item.real_solar_kwh > 0.5]
    if full:
        days = len(full)
        lines.append(
            f"average over {days} days: plan {sum(i.plan_cost for i in full) / days:.2f} €/day,"
            f" real {sum(i.real_cost for i in full) / days:.2f} €/day;"
            f" shortages plan {sum(len(i.plan_shortages) for i in full)},"
            f" real {sum(len(i.real_shortages) for i in full)};"
            f" rescue {sum(i.rescue_kwh for i in full):.2f} kWh /"
            f" {sum(i.rescue_cost for i in full):.2f} €"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    folder = find_export()
    if folder is None:
        raise SystemExit("no history export in tests/fixtures")
    data = load(folder)
    begin = datetime(2026, 11, 13, tzinfo=data.tz)
    print(report(run_bench(data, start=begin)))
