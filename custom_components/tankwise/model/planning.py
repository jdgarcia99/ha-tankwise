"""Build the optimiser input from the learned models, prices and settings.

Shared by the coordinator and the simulation bench, so both plan alike.

Modes:

* ``auto``: P90 demand; ``eco``: P50; ``comfort``: P90 plus 20 %;
* ``holiday``: no demand, no grid heating (solar surplus only);
* ``shadow``: like ``auto``, the plan is published but nothing is controlled;
* ``off``: no plan.

Until the controller exists (phase 4) every mode is a shadow mode.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum
import math

from .appointments import Appointment, energy_from_liters
from .demand import DemandForecast
from .optimizer import Block, PlanInput
from .prices import SlotPrices

HORIZON_SLOTS = 36 * 4
SLOT = timedelta(minutes=15)
COMFORT_MARGIN = 1.2


class Mode(StrEnum):
    """Operating mode (``select.tankwise_mode``)."""

    AUTO = "auto"
    ECO = "eco"
    COMFORT = "comfort"
    HOLIDAY = "holiday"
    SHADOW = "shadow"
    OFF = "off"


def _quantile(mode: Mode) -> float:
    return 0.5 if mode is Mode.ECO else 0.9


def _factor(mode: Mode) -> float:
    return COMFORT_MARGIN if mode is Mode.COMFORT else 1.0


def appointment_blocks(
    appointments: Sequence[Appointment],
    *,
    start: datetime,
    slots: int,
    tz: tzinfo,
    forecast: DemandForecast,
    workdays: Mapping[date, bool],
    mode: Mode,
    shower_temperature: float,
    mains_temperature: float,
) -> list[Block]:
    """Return the appointment blocks that touch the horizon.

    A block already in progress keeps only the demand left (``first`` is -1:
    no start condition, only the rescue recharge applies).
    """
    end = start + SLOT * slots
    quantile, factor = _quantile(mode), _factor(mode)
    blocks: list[Block] = []
    local_start = start.astimezone(tz)
    for appointment in appointments:
        if not appointment.enabled or not appointment.days:
            continue
        for offset in range(-1, math.ceil(slots / 96) + 2):
            day = local_start.date() + timedelta(days=offset)
            if not appointment.applies(day, workdays.get(day)):
                continue
            due = datetime.combine(day, appointment.time, tz)
            block_end = due + appointment.duration
            if block_end <= start or due >= end:
                continue
            last = min(slots, math.ceil((block_end - start) / SLOT))
            if due < start:
                first = -1
                demand = forecast.window(start, block_end, quantile) * factor
                minimum = 0.0
            else:
                first = math.floor((due - start) / SLOT)
                demand = forecast.window(due, block_end, quantile) * factor
                minimum = energy_from_liters(
                    appointment.liters, shower_temperature, mains_temperature
                )
            blocks.append(
                Block(
                    id=f"{appointment.id}@{due.isoformat()}",
                    name=appointment.name,
                    first=first,
                    last=last,
                    minimum_kwh=minimum,
                    demand_kwh=demand,
                )
            )
    return blocks


def build_plan_input(
    *,
    start: datetime,
    tz: tzinfo,
    mode: Mode,
    temperature: float,
    volume_l: float,
    mains_temperature: float,
    ambient_temperature: float,
    comfort_temperature: float,
    shower_temperature: float,
    max_temperature: float,
    heater_power_w: float,
    capacity_factor: float,
    usable_factor: float,
    ua_w_per_k: float,
    prices: SlotPrices,
    forecast: DemandForecast,
    appointments: Sequence[Appointment],
    workdays: Mapping[date, bool],
    occupied: bool | None,
    reserve_liters: float,
    rescue: bool,
    solar_kwh: Sequence[float] | None = None,
) -> PlanInput | None:
    """Return the optimiser input, or None in ``off`` mode."""
    if mode is Mode.OFF:
        return None
    slots = len(prices.buy)
    holiday = mode is Mode.HOLIDAY
    away = occupied is False or holiday
    quantile, factor = _quantile(mode), _factor(mode)
    if away:
        demand = [0.0] * slots
        blocks: list[Block] = []
    else:
        demand = [
            (slot.p50 if quantile < 0.9 else slot.p90) * factor
            for slot in forecast.slots[:slots]
        ]
        demand += [0.0] * (slots - len(demand))
        blocks = appointment_blocks(
            appointments,
            start=start,
            slots=slots,
            tz=tz,
            forecast=forecast,
            workdays=workdays,
            mode=mode,
            shower_temperature=shower_temperature,
            mains_temperature=mains_temperature,
        )
    return PlanInput(
        start=start,
        initial_temperature=temperature,
        volume_l=volume_l,
        mains_temperature=mains_temperature,
        ambient_temperature=ambient_temperature,
        comfort_temperature=comfort_temperature,
        shower_temperature=shower_temperature,
        max_temperature=max_temperature,
        heater_power_w=heater_power_w,
        capacity_factor=capacity_factor,
        usable_factor=usable_factor,
        ua_w_per_k=ua_w_per_k,
        buy=prices.buy,
        surplus_value=prices.surplus,
        demand_kwh=demand,
        solar_kwh=solar_kwh,
        blocks=blocks,
        reserve_kwh=0.0
        if away
        else energy_from_liters(reserve_liters, shower_temperature, mains_temperature),
        terminal_price=prices.terminal_value(),
        allow_grid=not holiday,
        rescue=rescue,
    )
