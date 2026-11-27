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
  Only a fraction `k_cap` (effective capacity factor, learned) shows up at the
  probe.
- **Probe position** (`TankParams.probe_at_top`, option *Probe position*).
  At the top the probe sees the water leaving. In the thermostat's sheath
  (low, next to the element, where the cold water arrives) it stays hot for
  the first litres of a draw and falls quickly when the cold front reaches it
  (24/11: 68.5 → 66 °C for the first 30 L, then down to 32 °C by 65 L, with
  some 55 L at 40 °C still available at the top). With the sheath:
  - a shortage is not the probe below comfort but the model with no useful
    energy left (`_is_shortage`), and `k_use` is not learned from the probe
    crossing the comfort temperature;
  - the estimated outlet temperature is the hot layer (`_hot`): set at a full
    charge, lowered by the losses, raised by heating (never below the model),
    not lowered by draws;
  - with a hot water meter, a draw leaves at the hot layer's temperature, and
    the probe does not correct the metered balance between full charges;
  - a partial heating (the sun, without reaching the cut-off) puts its
    metered energy into the whole tank (the mean rises `k_cap · E / C`): the
    element heats the cold water at the bottom, which cannot rise through
    water hotter than itself, so the hot layer only follows once the mean
    passes it, and the useful litres follow the energy meanwhile;
  - the probe bounds the model from below, at every sample: with a stably
    stratified tank the water above the probe is at least at its
    temperature and the water below it at least at the mains temperature,
    so the mean is at least `p · T_mains + (1 − p) · T_probe` (`p`: the
    *Volume below the probe* parameter, 30 %), and the hot layer at least
    the probe. Not the probe itself: the cold water fills the bottom below
    the sheath first (24/11: the probe stayed at 68 °C for the first 30 L
    while the mean fell to 60 °C). A model below the bound lost energy the
    tank did not (draws counted at the temperature of a hot layer that was
    running out) and is raised to it;
  - `k_use` cannot be learned from the probe: until it is (an outlet
    temperature sensor would), the *Default useful litres factor* parameter
    (0.85) is used. With the probe at the top the default stays 1.0.
    `tankwise.reset_usable_factor` forgets the learned one.
  The stratification ratio stays an empirical indicator (probe drop against
  the metered energy): with the probe in the sheath it is far from linear
  (small draws barely move it, larger ones more than a mixed tank), so it is
  only used to tell a stratified tank.
- **Losses**: `P = UA · (T − T_ambient)`. `UA` is learned by recursive least
  squares from the slope (Theil–Sen) of the probe over rest windows of two to
  four hours: no heating, no draw, no meter activity. A window ended by a draw
  drops its last hour (slow draws are detected an hour late), and once `UA` is
  learned a window implying less than half or more than 1.8 times the current
  value is rejected: a slow draw cannot be told apart from losses inside one
  window.
- **Status**: `uncertain` until a heating reaches the thermostat cut-off
  (never calibrated, or readings missing for more than 30 minutes), then
  `learning` until three rest windows have measured the losses, then
  `calibrated`. The status sensor says why (`reason`) and what is missing.
- **Thermostat cut-off**: the regulator asks for power and the element draws
  none for three minutes. The regulator state is captured at the instant the
  power falls, from state change events, because an external order may switch
  it off seconds later. The cut-off temperature is the median of the last 20.
  A drop is discarded when the regulator, or its own power supply, stopped
  the element. Before the drop (from one minute before to the drop itself)
  its mode, output and manual mode must show it was asking for power: a mode
  that does not heat, an output at 0 %, the manual mode or the output
  stopping just before (`REGULATOR_FALLING_WINDOW`: down to 10 % or by
  `REGULATOR_FALL_STEP` points; a regulator modulating in Auto moves a few
  points and cannot stop the element) discard it, and so do unavailability
  and restarts (uptime lower, new restart reason). After the drop only
  unavailability or a restart discard it, up to
  `REGULATOR_UNAVAILABLE_AFTER` (3 min): its entities are reported
  unavailable up to about two minutes after a power cut, and the cut-off is
  only confirmed after three. A mode or output change after the drop is a
  consequence of the cut-off (an automation, SolarSplit taking the surplus
  elsewhere). The exception is the output reported at rest (or the manual
  mode off) within `REGULATOR_STOP_AFTER` (60 s) of the drop: the regulator's
  state reaches Home Assistant a few seconds after the power, so that is its
  own stop arriving late (a cloud), unless it asked for more power first
  (`PowerDrop.insisted`: output at 95 %, `REGULATOR_INSIST_RISE` points above
  its value at the drop, or the manual mode switched on; or, at full output
  or in manual mode at the drop and unable to rise, held there for
  `REGULATOR_HOLD`, 5 s: its own stop is reported within a couple of
  seconds). With the element
  drawing nothing, a regulator raises its output: that is the thermostat.
  The sample is the filtered probe, never the mixed equivalent, and a
  confirmed cut-off sets the model full at it (stratification offset reset). A sample must be within 4 °C of the known
  cut-off (within 12 °C below the configured one until the first sample).
  Single samples can be discarded with a service.
- **Draws**: the residual between the observed and the predicted change of the
  filtered probe. A fast detector (10-minute window) catches showers, a slow
  one (60-minute window) taps left running. After each settled heating the
  energy balance between two heatings reconciles the energy of the draws.
  When the probe or the power comes back after a gap (up to 12 hours), a drop
  beyond what the losses and the metered heat explain is recorded as an
  `estimated` draw spread over the gap, so it reaches the demand profile.
  Only a gap of the probe or the power makes the state uncertain: the
  regulator is not needed to follow the tank.
- **Hot water meter** (optional, at the outlet of the tank): the draws are
  measured instead. A draw starts with the first new litre and ends after
  3 minutes without one (and without flow, when the flow rate is known); its
  energy is `litres · c · (T_out − T_mains)`, with `T_out` the probe when it
  started (or the model when the probe is older than an hour). The litres are
  counted before the validity check, so a gap of the probe loses none; a gap
  of the meter gives one `estimated` draw. The model drops by
  `k_cap · E / C` (the convention of the heat input and of the optimiser),
  and the probe is expected above it by `(1 − r) · k_cap · E / C`, where `r`
  is the **stratification ratio**: the probe drop (losses removed, settled
  10 minutes after the last litre, no heating) over the drop the metered
  energy gives a mixed tank, learned from draws of at least 4 °C, by start
  temperature (±5 °C, median of the last 20, used from 3 samples). That
  offset shrinks with the heat added and is reset by a heating to the
  cut-off. The probe corrects the model only beyond 1 °C. Metered draws are
  never scaled by the reconciliation (the balance only scales the estimated
  ones), so the shortages give `k_use` from exact energies. Without a meter
  every path is the one of 0.11.1 (a golden test checks it).

**Useful litres.** The cold water entering mixes with the hot water, so only
the heat above the comfort temperature can be used:

```
L_useful = k_use · V · (T − T_comfort) / (T_shower − T_mains)
```

`k_use` is learned from real shortages (litres drawn since the last settled
heating until the probe crossed the comfort temperature, over the litres
predicted then). The inverse gives the temperature an appointment needs.

In a **stratified** tank (learned with a hot water meter: at least
`MIN_STRATIFICATION_SAMPLES` samples and a ratio at most
`STRATIFIED_MAX_RATIO`, 0.8) the cold water stays at the bottom and the water
leaves hot until the hot layer is used up, so the useful floor is the mains
water instead of the comfort temperature: useful energy `k_use · C · (T − T_mains)`
(the mixing zone is lost), `L = k_use · V · (T − T_mains) / (T_shower − T_mains)`.
The modelled temperature stays the mixed equivalent of the stored energy;
the estimated outlet temperature (modelled plus the stratification offset)
is the hot layer the probe sees. The useful floor (`TankState.useful_floor_temperature`)
is what the appointments, the boost by litres and the optimiser use as their
comfort level, so the plan compares useful energy needed with useful energy
available on the same basis.

## Demand

Draws are spread over 15-minute slots of each observed day (at least 20 hours
of data with the house occupied). Days measured by a hot water meter for at
least 20 hours also keep the litres of each slot: the energy drives the
plan, the litres are compared with the appointments (`hot_liters_p50/p90`)
and summarised per day type. With metered days the suggested litres of an
appointment come only from them: the metered energy of the block over
`c · (T_shower − T_mains)`, i.e. each draw's
`litres · (T_tank − T_mains) / (T_shower − T_mains)`. For each day type (weekday, workday and
weekend, or all alike; holidays from the Workday integration count as
non-working days) the last N days are kept with a three-week half-life.

Quantiles are never taken slot by slot: a per-slot P90 would put a shower in
every slot where there ever was one. Each slot is the weighted mean shape of
the days (recent days weigh more) scaled so the day adds up to the quantile
of the daily total: `slot = shape · Q(total) / mean(total)`. P50 and P90 share
the shape, so the P90 is never below the P50 in any slot or window and peaks
at the same time. The demand of a window (an appointment block, the rest of
today) is the quantile of the energy drawn in that window across the past
days, which keeps the correlation between the slots of a day. Shortages raise the forecast around their time by up to 20 %, fading
out over two weeks; several shortages at the same time do not compound.

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
  the bias of the forecast. Several forecasts are summed. Forecasts usually
  cover today and tomorrow: later days of the horizon repeat the last
  forecast day until the real forecast arrives.

## Optimiser

Dynamic programming over 144 slots of 15 minutes:

- **State**: stored energy in steps of 0.05 kWh between 25 °C and the learned
  cut-off.
- **Decision per slot**: grid energy (none, half or a whole slot at the learned
  power) and whether to take the expected surplus or let it go to the grid.
- **Transition**: `E' = E + k_cap · (grid + solar − demand) − losses(E)`.
- **Cost**: `grid · purchase price + solar · surplus value`, plus the energy
  left at the end valued at the median night price (never above the cheapest
  price of the last 24 hours), so the plan neither empties the tank at the
  end of the horizon nor buys energy only to leave it there. Letting the surplus go costs a
  small risk margin per kWh: selling must clearly pay.
- **Comfort** (soft, large penalty per kWh missing): outside blocks each slot
  with demand must be covered plus a reserve, unless the demand until a
  cheaper slot (or one with surplus) is below the reserve: a stray small draw
  does not justify an expensive heating; at the start of a block, its
  requirement; inside a block only the rescue recharge (the energy missing
  for the demand left in the block) may heat, never after it. Any shortfall
  flags the plan at risk; each run of slots with shortfall is reported with
  its kind (`appointment` or `demand`).
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
   regulator output off when the plan sells the surplus, unless the spare
   surplus option is on and the tank is more than 5 °C below the cut-off
   (then the output stays on and the power is requested with the `spare`
   priority).
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
It waits until the entry is complete and the inputs report values, runs
again when a hot water meter is added (the draws are measured with its
history), is not adopted if it learned nothing, and is **merged** with what was learned:
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
