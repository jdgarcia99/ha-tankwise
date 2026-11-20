# TankWise architecture

This document explains how TankWise decides when and how much to heat an
electric water heater. It is meant for contributors and for users who want
to understand a decision. The README covers installation, configuration and
entities.

## Overview

```
inputs (probe, power, energy, regulator, prices, forecast, occupancy)
   │
   ▼  every 60 s
coordinator ──► tank model ──► draws ──► demand profile ──► forecast
   │                                                            │
   │                          prices + solar surplus ───────────┤
   │                                                            ▼
   │                                              optimiser (36 h, 15 min)
   │                                                            │
   └──────────────► controller (orders + safeties) ◄────── plan ┘
```

| Module | Role |
|--------|------|
| `coordinator.py` | Reads and normalises the inputs every 60 s, runs the models, persists what is learned |
| `model/tank.py` | Thermal model: stored energy, losses, heater power, thermostat cut-off, draw detection |
| `model/demand.py` | Usage profile per day type and its quantile forecast |
| `model/appointments.py` | Hot water appointments: litres needed and required temperature |
| `model/prices.py` | Purchase price and value of the surplus per 15-minute slot |
| `model/solar.py` | Expected solar surplus from the production forecast |
| `model/optimizer.py` | Dynamic programming over the 36-hour horizon |
| `model/control.py` | Controller state machine: orders, safeties, degraded rule, boost |
| `model/legionella.py` | Anti-legionella tracking and scheduling |
| `bootstrap.py` | Rebuilds the models from the recorder history |
| `planner.py`, `controller.py` | Home Assistant glue: prices, forecast, logbook, service calls, repairs |

Everything under `model/` is pure Python (no Home Assistant import), so it can
be tested in isolation and replayed over exported history (`tests/simulation`).

## Tank model

The tank is modelled as an equivalent fully mixed volume tracked by an energy
balance, one sample per minute:

- **Energy**: `E = C · (T − T_mains)`, with `C = V · 0.001163` kWh/K.
- **Heat in**: increments of the energy meter (or the integral of the power).
  Only a fraction `k_cap` (effective capacity factor, learned) shows up at a
  probe in the upper part of the tank.
- **Losses**: `P = UA · (T − T_ambient)`. `UA` is learned by recursive least
  squares from the slope (Theil–Sen) of the probe over rest windows of two to
  four hours: no heating, no draw, no meter activity. A window ended by a draw
  drops its last hour (slow draws are detected an hour late), and once `UA` is
  learned a window implying less than half or more than 1.8 times the current
  value is rejected: a slow draw cannot be told apart from losses inside one
  window.
- **Thermostat cut-off**: the regulator asks for power and the element draws
  none for three minutes. The regulator state is captured at the instant the
  power falls, from state change events, because an external order may switch
  it off seconds later. The cut-off temperature is the median of the last 20.
- **Draws**: the residual between the observed and the predicted change of the
  filtered probe. A fast detector (10-minute window) catches showers, a slow
  one (60-minute window) taps left running. After each settled heating the
  energy balance between two heatings reconciles the energy of the draws.

**Useful litres.** The cold water entering mixes with the hot water, so only
the heat above the comfort temperature can be used:

```
L_useful = k_use · V · (T − T_comfort) / (T_shower − T_mains)
```

`k_use` is learned from real shortages (litres drawn since the last settled
heating until the probe crossed the comfort temperature, over the litres
predicted then). The inverse gives the temperature an appointment needs.

## Demand

Draws are spread over 15-minute slots of each observed day (at least 20 hours
of data with the house occupied). For each day type (weekday, workday and
weekend, or all alike; holidays from the Workday integration count as
non-working days) the last N days are kept with a three-week half-life.

Quantiles are taken on the **cumulative curve** of each day, never slot by
slot: the P90 of a window is the P90 of the energy drawn in that window across
the past days. A per-slot P90 would put a shower in every slot where there ever
was one. Shortages raise the forecast around their time for two weeks.

**Appointments** are hard constraints: at the start of the block, the useful
litres must cover the larger of the appointment minimum and the P90 of the
litres used in the whole block, without reheating inside it.

## Prices and solar surplus

- Purchase price per slot from the hourly attributes of the price sensor;
  missing prices for tomorrow repeat today's until they arrive.
- The surplus is valued at the injection price (zero once the monthly
  compensation cap is reached, or a fixed value without a price sensor).
- **Expected surplus** = production forecast − house consumption of each hour.
  The latter is learned per hour of the day as
  `forecast − (exported + heater energy from the surplus)`, which also absorbs
  the bias of the forecast. Several forecasts are summed.

## Optimiser

Dynamic programming over 144 slots of 15 minutes:

- **State**: stored energy in steps of 0.05 kWh between 25 °C and the learned
  cut-off.
- **Decision per slot**: grid energy (none, half or a whole slot at the learned
  power) and whether to take the expected surplus or let it go to the grid.
- **Transition**: `E' = E + k_cap · (grid + solar − demand) − losses(E)`.
- **Cost**: `grid · purchase price + solar · surplus value`, plus the energy
  left at the end valued at the median night price, so the plan does not
  empty the tank at the end of the horizon. Letting the surplus go costs a
  small risk margin per kWh: selling must clearly pay.
- **Comfort** (soft, large penalty per kWh missing): outside blocks each slot
  with demand must be covered plus a reserve; at the start of a block, its
  requirement; inside a block only the rescue recharge (the energy missing
  for the demand left in the block) may heat, never after it. Any shortfall
  flags the plan at risk.
- **Anti-legionella**: optionally a target temperature at one slot, chosen
  after the cheapest hour to heat (surplus first) before the weekly deadline.
- **Runs**: grid heating runs of more than two hours (counting the heating
  already done) are split with a pause and the plan is computed again.

The plan is recomputed at every slot and when something relevant changes
(prices, a draw, mode, occupancy, an appointment, a setting, a deviation of
more than 3 °C from the plan, the solar forecast).

## Controller

Every minute the controller decides whether the regulator must be in manual
mode (full power from the grid) or automatic (surplus only):

1. **Boost** (any mode except off): heat now until the target, the cut-off,
   the maximum duration, a safety or a cancellation.
2. **Plan**: heat during the planned minutes of the current slot; switch the
   regulator output off when the plan sells the surplus.
3. **Degraded rule** without a plan: before each appointment heat to its
   required temperature, starting as early as the learned power requires;
   fixed windows only without appointments.

Safeties apply whoever switched the manual mode on: safety maximum
temperature, thermostat cut-off, two hours in manual mode, probe unavailable.
If the regulator becomes unavailable while heating, the heater switch is
opened as a last resort and restored when the regulator returns.

Orders are sent on changes of the desired state and retried if they do not
take effect; an external order is respected until the next change. Every
order is written to the logbook with its reason.

## Learning across upgrades

The history import (`bootstrap.py`) replays the recorder history through new
models when TankWise is installed or its models change (bootstrap version).
It waits until the entry is complete and the inputs report values, is not
adopted if it learned nothing, and is **merged** with what was learned:
lists of measurements keep the source with more of them, and an estimate
that is not coherent with the history (heat losses, heater power) gives way
to the history.

## Testing

- `tests/model`: unit tests of the pure models, with a tank simulator (noise,
  thermostat hysteresis, regulator).
- `tests/integration`: Home Assistant tests (config flow, entities, services,
  controller, boost, blueprint, upgrades).
- `tests/simulation`: replay of an exported history (opt-in, personal data
  never committed): the learned profile and a walk-forward bench comparing the
  plan with what really happened, with and without a surplus forecast.
