"""Heating plan: dynamic programming over 15-minute slots.

The state is the stored energy above the mains water temperature,
``E = C · (T - T_mains)``, discretised in steps of 0.05 kWh between the
equivalent of 25 °C and the learned thermostat cut-off. In each slot the
plan chooses how much grid energy to put in (nothing, half or a whole slot
at the learned heater power) and whether to take the expected solar surplus
(diverter in automatic mode) or let it go to the grid, which pays more than
night heating costs when the injection price is high.

* Transition: ``E' = E + k_cap · (grid + solar) - k_cap · demand - losses(E)``,
  with the heat limited by the room left below the cut-off.
* Cost: ``grid · purchase price + solar · surplus value``; the energy left at
  the end is valued at the median night purchase price, so the plan does not
  empty the tank at the end of the horizon.
* Comfort (useful energy ``k_use · (E - E_comfort)``):
  - outside appointment blocks, at the start of each slot with demand the
    useful energy must cover that demand plus the reserve;
  - at the start of an appointment block it must cover the larger of the
    appointment minimum and the demand quantile of the whole block (never
    a sum of hourly quantiles); if that is above a full tank, the target is
    a full tank;
  - inside a block the only heating allowed is the rescue recharge: the
    energy missing to cover the demand left in the block, if enabled. Never
    after the block.
  Comfort limits are soft (a large penalty per kWh missing), so a plan is
  always produced; any shortfall flags the plan at risk.
* Anti-legionella: optionally the tank must be at a target temperature at
  the start of one slot (a soft target that does not flag the plan at risk).

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math

from .tank import WATER_HEAT_CAPACITY_KWH_PER_L_K

ENERGY_STEP_KWH = 0.05
MIN_PLAN_TEMPERATURE = 25.0
SHORTFALL_PENALTY = 50.0  # € per kWh of useful energy missing
TARGET_PENALTY = (
    5.0  # € per kWh below a block target (full tank) that is not a shortfall
)
RISK_TOLERANCE_KWH = 0.05
# Letting the expected surplus go to the grid must pay at least this much
# per kWh: the forecast is uncertain, and taking the surplus when it comes
# is safer than counting on it (or on the grid) later. Without it the plan
# would sell for the small loss difference of heating an hour later.
SELL_MARGIN = 0.03  # € per kWh of surplus let go
# The regulator stays in manual mode at most two hours in a row (safety of
# the controller): longer grid heating runs are split with a pause.
MAX_RUN_SLOTS = 8
MAX_SPLIT_ROUNDS = 16
GRID_FRACTIONS = (0.0, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class Block:
    """An appointment block inside the horizon (slot indices)."""

    id: str
    name: str
    first: int  # first slot of the block
    last: int  # slot after the block
    minimum_kwh: float  # appointment minimum, as draw energy
    demand_kwh: float  # demand quantile of the whole block, as draw energy

    @property
    def required_kwh(self) -> float:
        """Draw energy the block must be able to deliver without reheating."""
        return max(self.minimum_kwh, self.demand_kwh)


@dataclass(frozen=True, slots=True)
class PlanInput:
    """Everything the plan depends on."""

    start: datetime
    initial_temperature: float
    volume_l: float
    mains_temperature: float
    ambient_temperature: float
    comfort_temperature: float
    shower_temperature: float
    max_temperature: float  # learned thermostat cut-off
    heater_power_w: float
    capacity_factor: float
    usable_factor: float
    ua_w_per_k: float
    buy: Sequence[float]
    surplus_value: Sequence[float]
    demand_kwh: Sequence[float]  # per slot, draw energy (quantile of the mode)
    solar_kwh: Sequence[float] | None = None  # expected surplus per slot
    blocks: Sequence[Block] = ()
    reserve_kwh: float = 0.0  # useful energy always kept for unplanned taps
    terminal_price: float | None = None
    allow_grid: bool = True
    rescue: bool = True
    slot_minutes: int = 15
    # Slots where grid heating is not allowed (pauses between long runs).
    no_grid_slots: frozenset[int] = frozenset()
    # Anti-legionella: (slot, temperature) the tank must reach at that slot.
    legionella: tuple[int, float] | None = None
    # Slots already heated from the grid in a row before ``start``: the
    # two-hour limit counts them.
    initial_run_slots: int = 0

    @property
    def slots(self) -> int:
        """Return the number of slots of the horizon."""
        return len(self.buy)

    @property
    def capacity(self) -> float:
        """Return the heat capacity in kWh/K."""
        return self.volume_l * WATER_HEAT_CAPACITY_KWH_PER_L_K


@dataclass(frozen=True, slots=True)
class PlanSlot:
    """One slot of the plan."""

    start: datetime
    grid_kwh: float
    solar_kwh: float
    solar_allowed: bool  # False: let the surplus go to the grid
    demand_kwh: float
    temperature: float  # at the start of the slot
    useful_liters: float  # at the start of the slot
    cost: float
    rescue: bool
    block: str | None
    shortfall_kwh: float


@dataclass(frozen=True, slots=True)
class BlockResult:
    """How the plan arrives at an appointment block."""

    id: str
    name: str
    start: datetime
    required_temperature: float  # to cover the block without reheating
    predicted_temperature: float
    predicted_liters: float
    rescue_kwh: float
    rescue_cost: float
    shortfall_kwh: float

    @property
    def at_risk(self) -> bool:
        """Return True if the block cannot be covered, even with rescue."""
        return self.shortfall_kwh > RISK_TOLERANCE_KWH


@dataclass(frozen=True, slots=True)
class Heating:
    """A run of consecutive slots heating from the grid."""

    start: datetime
    end: datetime
    energy_kwh: float
    cost: float
    rescue: bool


@dataclass(slots=True)
class Plan:
    """Result of the optimisation."""

    slots: list[PlanSlot]
    blocks: list[BlockResult] = field(default_factory=list)
    # Anti-legionella: when, the target and the temperature the plan reaches.
    legionella: tuple[datetime, float, float] | None = None

    @property
    def cost(self) -> float:
        """Return the cost of the energy the plan puts in."""
        return sum(slot.cost for slot in self.slots)

    @property
    def grid_kwh(self) -> float:
        """Return the grid energy of the plan."""
        return sum(slot.grid_kwh for slot in self.slots)

    @property
    def rescue_kwh(self) -> float:
        """Return the grid energy of rescue recharges."""
        return sum(slot.grid_kwh for slot in self.slots if slot.rescue)

    @property
    def rescue_cost(self) -> float:
        """Return the cost of rescue recharges."""
        return sum(slot.cost for slot in self.slots if slot.rescue)

    @property
    def shortfall_kwh(self) -> float:
        """Return the useful energy the plan cannot guarantee."""
        return sum(slot.shortfall_kwh for slot in self.slots)

    @property
    def at_risk(self) -> bool:
        """Return True if comfort cannot be guaranteed somewhere."""
        return self.shortfall_kwh > RISK_TOLERANCE_KWH

    def heatings(self) -> list[Heating]:
        """Return the grid heating runs of the plan."""
        runs: list[Heating] = []
        current: list[PlanSlot] = []
        for slot in [*self.slots, None]:
            if slot is not None and slot.grid_kwh > 0:
                current.append(slot)
                continue
            if current:
                step = (
                    current[1].start - current[0].start
                    if len(current) > 1
                    else timedelta(minutes=15)
                )
                runs.append(
                    Heating(
                        start=current[0].start,
                        end=current[-1].start + step,
                        energy_kwh=sum(item.grid_kwh for item in current),
                        cost=sum(item.cost for item in current),
                        rescue=any(item.rescue for item in current),
                    )
                )
                current = []
        return runs

    def next_heating(self) -> Heating | None:
        """Return the first grid heating run of the plan."""
        runs = self.heatings()
        return runs[0] if runs else None


class _Model:
    """Precomputed quantities of the optimisation."""

    def __init__(self, data: PlanInput) -> None:
        self.data = data
        capacity = data.capacity
        self.capacity = capacity
        self.k_cap = data.capacity_factor
        self.k_use = data.usable_factor
        self.e_floor = capacity * (MIN_PLAN_TEMPERATURE - data.mains_temperature)
        self.e_max = max(
            self.e_floor + ENERGY_STEP_KWH,
            capacity * (data.max_temperature - data.mains_temperature),
        )
        self.e_comfort = capacity * (data.comfort_temperature - data.mains_temperature)
        self.states = math.ceil((self.e_max - self.e_floor) / ENERGY_STEP_KWH) + 1
        self.grid_slot_kwh = data.heater_power_w / 1000 * data.slot_minutes / 60
        self.hours = data.slot_minutes / 60
        slots = data.slots
        self.solar = list(data.solar_kwh or [0.0] * slots)
        self.demand = list(data.demand_kwh)
        # Block of each slot, block start slots, and the demand left in a
        # block from each slot (the block quantile spread like the profile).
        self.block_of: list[Block | None] = [None] * slots
        self.block_start: dict[int, Block] = {}
        self.remaining: list[float] = [0.0] * slots
        for block in data.blocks:
            first, last = max(0, block.first), min(slots, block.last)
            if first >= last:
                continue
            shape = self.demand[first:last]
            total = sum(shape)
            if total > 0:
                scaled = [value * block.required_kwh / total for value in shape]
            else:
                scaled = [block.required_kwh / (last - first)] * (last - first)
            self.demand[first:last] = scaled
            left = 0.0
            for index in range(last - 1, first - 1, -1):
                left += self.demand[index]
                self.remaining[index] = left
                self.block_of[index] = block
            if block.first >= 0:
                self.block_start[block.first] = block
        self.terminal_price = (
            data.terminal_price
            if data.terminal_price is not None
            else (sorted(data.buy)[len(data.buy) // 2] if data.buy else 0.0)
        )

    def energy(self, index: int) -> float:
        return self.e_floor + index * ENERGY_STEP_KWH

    def temperature(self, energy: float) -> float:
        return self.data.mains_temperature + energy / self.capacity

    def useful_liters(self, energy: float) -> float:
        span = self.data.shower_temperature - self.data.mains_temperature
        if span <= 0:
            return 0.0
        useful = max(0.0, self.k_use * (energy - self.e_comfort))
        return useful / (WATER_HEAT_CAPACITY_KWH_PER_L_K * span)

    def block_target(self, block: Block) -> tuple[float, float]:
        """Return (energy needed, energy reachable target) at a block start."""
        needed = self.e_comfort + block.required_kwh / self.k_use
        return needed, min(needed, self.e_max)

    def options(self, slot: int, energy: float) -> list[tuple[float, bool, bool]]:
        """Allowed (grid energy, take solar, is a rescue) choices of a slot."""
        data = self.data
        solar_choices = (True, False) if self.solar[slot] > 0 else (False,)
        grid_choices: list[tuple[float, bool]]
        if not data.allow_grid or slot in data.no_grid_slots:
            grid_choices = [(0.0, False)]
        elif self.block_of[slot] is None:
            grid_choices = [
                (fraction * self.grid_slot_kwh, False) for fraction in GRID_FRACTIONS
            ]
        elif not data.rescue:
            grid_choices = [(0.0, False)]
        else:
            needed = self.e_comfort + self.remaining[slot] / self.k_use - energy
            grid_choices = [(0.0, False)]
            if needed > 0:
                grid_choices.append(
                    (min(self.grid_slot_kwh, needed / self.k_cap), True)
                )
        return [
            (grid, solar, rescue)
            for grid, rescue in grid_choices
            for solar in solar_choices
        ]

    def step(
        self, slot: int, energy: float, grid: float, take_solar: bool = True
    ) -> tuple[float, float, float, float, float]:
        """Return (next energy, effective grid, solar used, cost, shortfall)."""
        data = self.data
        room = max(0.0, (self.e_max - energy) / self.k_cap)
        grid = min(grid, room)
        solar = min(self.solar[slot], max(0.0, room - grid)) if take_solar else 0.0
        demand = self.demand[slot]
        temperature = self.temperature(energy)
        loss = (
            data.ua_w_per_k
            * (temperature - data.ambient_temperature)
            * self.hours
            / 1000
        )
        nxt = energy + self.k_cap * (grid + solar) - self.k_cap * demand - loss
        cost = grid * data.buy[slot] + solar * data.surplus_value[slot]
        if not take_solar:
            cost += SELL_MARGIN * min(self.solar[slot], max(0.0, room - grid))

        shortfall = 0.0
        block = self.block_of[slot]
        if slot in self.block_start:
            needed, target = self.block_target(self.block_start[slot])
            if needed <= self.e_max:
                # The block must be covered without reheating.
                shortfall += max(0.0, needed - energy) * self.k_use
            else:
                # More than a full tank: arrive full, the rescue does the rest.
                cost += TARGET_PENALTY * max(0.0, target - energy)
        legionella = data.legionella
        if legionella is not None and slot == legionella[0]:
            target = min(
                self.e_max,
                self.capacity * (legionella[1] - data.mains_temperature),
            )
            cost += TARGET_PENALTY * max(0.0, target - energy)
        if block is not None:
            # Inside a block (rescue counts): never below the comfort level.
            shortfall += max(0.0, self.e_comfort - nxt) * self.k_use
        elif demand > 0:
            required = self.e_comfort + (demand + data.reserve_kwh) / self.k_use
            shortfall += max(0.0, required - energy) * self.k_use
        cost += SHORTFALL_PENALTY * shortfall
        nxt = min(self.e_max, max(self.e_floor, nxt))
        return nxt, grid, solar, cost, shortfall


def _interpolate(values: list[float], position: float) -> float:
    if position <= 0:
        return values[0]
    if position >= len(values) - 1:
        return values[-1]
    low = int(position)
    fraction = position - low
    return values[low] * (1 - fraction) + values[low + 1] * fraction


def optimise(data: PlanInput) -> Plan:
    """Return the cheapest plan that keeps the comfort limits.

    Grid heating runs longer than two hours are split: the slot after the
    first two hours is forbidden and the plan is computed again.
    """
    plan = _optimise(data)
    for _ in range(MAX_SPLIT_ROUNDS):
        pauses = set(data.no_grid_slots)
        run = data.initial_run_slots
        for index, slot in enumerate(plan.slots):
            run = run + 1 if slot.grid_kwh > 0 else 0
            if run > MAX_RUN_SLOTS:
                pauses.add(index)
                run = 0
        if pauses == set(data.no_grid_slots):
            return plan
        data = replace(data, no_grid_slots=frozenset(pauses))
        plan = _optimise(data)
    return plan


def _optimise(data: PlanInput) -> Plan:
    model = _Model(data)
    slots = data.slots
    states = model.states
    terminal = [
        -model.terminal_price * (model.energy(index) - model.e_floor) / model.k_cap
        for index in range(states)
    ]
    values: list[list[float]] = [[0.0] * states for _ in range(slots)] + [terminal]
    for slot in range(slots - 1, -1, -1):
        following = values[slot + 1]
        current = values[slot]
        for index in range(states):
            energy = model.energy(index)
            best = math.inf
            for grid, take_solar, _rescue in model.options(slot, energy):
                nxt, _, _, cost, _ = model.step(slot, energy, grid, take_solar)
                total = cost + _interpolate(
                    following, (nxt - model.e_floor) / ENERGY_STEP_KWH
                )
                if total < best:
                    best = total
            current[index] = best

    # Forward pass from the real state, choosing with the value function.
    energy = min(
        model.e_max,
        max(
            model.e_floor,
            model.capacity * (data.initial_temperature - data.mains_temperature),
        ),
    )
    plan_slots: list[PlanSlot] = []
    step = timedelta(minutes=data.slot_minutes)
    for slot in range(slots):
        following = values[slot + 1]
        best = None
        for grid, take_solar, rescue in model.options(slot, energy):
            result = model.step(slot, energy, grid, take_solar)
            total = result[3] + _interpolate(
                following, (result[0] - model.e_floor) / ENERGY_STEP_KWH
            )
            if best is None or total < best[0] - 1e-9:
                best = (total, take_solar, rescue, result)
        assert best is not None
        _, take_solar, rescue, (nxt, grid_used, solar_used, _cost, shortfall) = best
        block = model.block_of[slot]
        energy_cost = grid_used * data.buy[slot] + solar_used * data.surplus_value[slot]
        plan_slots.append(
            PlanSlot(
                start=data.start + step * slot,
                grid_kwh=grid_used,
                solar_kwh=solar_used,
                solar_allowed=take_solar or model.solar[slot] == 0,
                demand_kwh=model.demand[slot],
                temperature=model.temperature(energy),
                useful_liters=model.useful_liters(energy),
                cost=energy_cost,
                rescue=rescue and grid_used > 0,
                block=block.id if block else None,
                shortfall_kwh=shortfall,
            )
        )
        energy = nxt

    results = []
    for block in data.blocks:
        first, last = max(0, block.first), min(slots, block.last)
        if first >= last:
            continue
        inside = plan_slots[first:last]
        needed, _target = model.block_target(block)
        results.append(
            BlockResult(
                id=block.id,
                name=block.name,
                start=plan_slots[first].start,
                required_temperature=model.temperature(needed),
                predicted_temperature=plan_slots[first].temperature,
                predicted_liters=plan_slots[first].useful_liters,
                rescue_kwh=sum(item.grid_kwh for item in inside if item.rescue),
                rescue_cost=sum(item.cost for item in inside if item.rescue),
                shortfall_kwh=sum(item.shortfall_kwh for item in inside),
            )
        )
    legionella = None
    if data.legionella is not None and 0 <= data.legionella[0] < slots:
        index, target = data.legionella
        legionella = (plan_slots[index].start, target, plan_slots[index].temperature)
    return Plan(plan_slots, results, legionella)


def slot_index(start: datetime, moment: datetime, slot_minutes: int = 15) -> int:
    """Return the index of the slot containing ``moment``."""
    return math.floor((moment - start).total_seconds() / (slot_minutes * 60))
