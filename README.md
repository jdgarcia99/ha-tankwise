<p align="center">
  <img src="images/banner.png" alt="TankWise" width="100%">
</p>

# TankWise

[![Validate](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml)
[![Tests](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml)

*[Leer en español](README.es.md)*

TankWise is a Home Assistant integration that takes full control of an
electric storage water heater driven by a power regulator (for example a
FreeDS PWM diverter). It decides **when and how much to heat** so there is
always hot water when the household needs it, at the lowest cost (PVPC
purchase price plus the value of the solar surplus).

## Project status

> **Development (0.7.0, phase 4 of 7).** TankWise learns the tank and the
> household's usage, computes the heating plan and, in the auto, eco, comfort
> and holiday modes, **controls the regulator** with its safeties. It still
> starts in shadow mode (publishes the plan without acting): compare the
> planned cost with the real one, then switch to auto and disable the
> automations that also switch the regulator manual mode, or they will fight
> each other.

| Phase | Content | Status |
|-------|---------|--------|
| 1 | Thermal model, draw detection, history import, sensors (no control) | ✅ Done |
| 2 | Demand profile and forecast, hot water appointments | ✅ Done |
| 3 | Prices, surplus value and optimiser; shadow mode with the plan | ✅ Done |
| 4 | Controller, safeties and degraded mode; automatic mode | ✅ Done |
| 5 | Boost with a blueprint for external push buttons | Planned |
| 6 | Solar forecast, SolarSplit coordination and anti-legionella cycle | Planned |
| 7 | Migration from automations and full documentation | Planned |

## Installation

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/jdgarcia99/ha-tankwise` with category **Integration**.
3. Install **TankWise** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → TankWise**.

## Configuration

Everything is configured from the UI; nothing goes into `configuration.yaml`
and no entity ID is fixed in the code.

**Required inputs**

- Tank temperature: probe in the upper part of the tank (°C or °F).
- Water heater power (W or kW).
- Water heater power switch (it must always stay on).
- Regulator manual mode: switch that makes the regulator heat at full power
  (e.g. the FreeDS manual mode).
- Electricity purchase price (e.g. PVPC).

**Recommended and optional inputs**

- Water heater energy (kWh or Wh, `total_increasing` or `total`).
  Recommended; without it the power is integrated.
- Regulator output (%), e.g. the FreeDS PWM output.
- Entity that tells whether the regulator is master of the water heater.
- Home occupied.
- Grid injection price, grid import and export energy, current export power.
- Solar forecast (Forecast.Solar or Solcast).
- Ambient temperature where the tank is.
- Workday sensor (Workday integration): holidays are learned apart and
  appointments can be set for workdays or holidays.

**Options**

The options menu has three parts: installation parameters, inputs and hot
water appointments. The parameters show only the tank volume and the mains
water region; everything TankWise learns by itself is folded under
**Advanced options**:

| Parameter | Default |
|-----------|---------|
| Volume | 100 L |
| Mains water region | Balearic Islands |
| *Advanced:* heating power (initial value, then learned) | 1500 W |
| *Advanced:* ambient temperature without sensor | 20 °C |
| *Advanced:* thermostat cut-off temperature (initial value, then learned) | 68 °C |
| *Advanced:* thermostat cut-off power threshold | 50 W |
| *Advanced:* group days of the usage profile | Workdays and weekends/holidays |
| *Advanced:* days of each type in the usage profile | 16 |

**Settings (number entities)**

| Entity | Default |
|--------|---------|
| Shower temperature | 40 °C |
| Minimum comfort temperature | 38 °C |
| Mains water temperature | 0 = automatic (monthly table of the region, interpolated day by day) |

Entities that come from a 0.2 installation are migrated automatically and
keep what was learned. A repair issue asks you to complete the new required
inputs with **Reconfigure**.

## Hot water appointments

An appointment says "hot water ready by this time, with at least this much
water for the block that follows": name, start time, block duration (1 hour
by default, e.g. 3 hours for the evening showers), days of the week (or
workdays/holidays with the Workday sensor), minimum litres at the shower
temperature and active or not. The minimum litres must be available when the
block starts and cover the whole block without reheating. Up to
six, none by default. They are added in **Options → Hot water appointments**.

Each appointment gets entities to adjust it from a dashboard (changes are
saved in the options): `time.<name>_<appointment>_time`,
`number.<name>_<appointment>_liters` and `switch.<name>_<appointment>_enabled`.
The sensor `sensor.<name>_<appointment>_suggested_liters` shows the litres
actually used during the block (P90 of the last four weeks)
to help choosing the minimum, and `sensor.<name>_<appointment>_required_temperature`
the tank temperature needed when the block starts. If that temperature is
above the learned thermostat cut-off, `binary_sensor.tankwise_plan_at_risk`
turns on: even a full tank does not cover the block. From phase 3 the planner
treats every active appointment as a hard constraint on useful litres.

## Entities

Entity IDs are generated in English from the name of the instance (for
example `sensor.<name>_available_liters`) whatever the language; visible names
are translated.

| Entity | Description |
|--------|-------------|
| Stored energy | kWh stored above the mains water temperature |
| Useful energy | kWh stored above the minimum comfort temperature |
| Charge | Useful energy as a percentage of a full tank |
| Hot water available | Useful litres at the shower temperature before the water runs below the comfort temperature (theoretical litres as an attribute) |
| Plan at risk (binary) | On when hot water cannot be guaranteed: an appointment needs more than a full tank gives, the usual demand is not covered, or the regulator or the probe is unavailable before an appointment (`reasons` attribute) |
| Modelled temperature | Equivalent tank temperature tracked by the model |
| Model status | `calibrated`, `learning` or `uncertain` |
| Last draw | Start of the last hot water draw, with its energy, minimum temperature and whether there was a shortage |
| Hot water shortages (7 days) | Draws that took the water at least 1.5 °C below the minimum comfort temperature |
| Hot water demand today | Energy a strong day needs (P90), with the usual day (P50) and the rest of today as attributes |
| Hot water demand forecast | Demand of the next 36 hours (P90), with the 15-minute slots as an attribute |
| Heat loss coefficient (diagnostic, disabled by default) | Learned UA, in W/K |
| Learned heating power (diagnostic, disabled by default) | Mean power of the heating element at full output |
| Thermostat cut-off temperature (diagnostic, disabled by default) | Median of the last 20 cut-offs |
| Effective capacity factor (diagnostic, disabled by default) | Share of the heating energy seen by the probe |
| Mains water temperature (diagnostic) | Value in use and its source |

The diagnostics include the learned parameters, the history import result,
the observed days of the usage profile and the last 50 draws.

## Heating plan

Every 15 minutes, and whenever something relevant changes, TankWise computes
the cheapest heating plan for the next 36 hours: how much grid energy to put
in each slot and whether to take the solar surplus or let it go to the grid
(when the injection price pays more than heating at night costs). Every
appointment must start with enough useful litres for its whole block; the
rest of the demand is covered slot by slot plus a reserve. Grid heating runs
last two hours at most.

**Rescue recharge** (option, on by default): if an appointment block needs
more than a full tank, the tank is filled before the block and, inside the
block, only the energy missing to cover the usual demand left in it is
heated, never after the block. Rescues are logged and counted per month
(kWh and €). The plan at risk turns on only when not even the rescue covers
the appointment. With the option off (strict mode) nothing is heated inside
a block.

| Mode | Behaviour |
|------|-----------|
| Auto | Usual demand of a strong day (P90) |
| Eco | Usual demand of a normal day (P50): cheaper, more risk |
| Comfort | P90 plus 20 % |
| Holiday | No grid heating, solar surplus only |
| Shadow (default) | Publishes the plan without acting |
| Off | No plan, no action |

In shadow mode compare **Planned cost** with **Water heater cost** (real) and
look at the logbook: external orders on the regulator manual mode that
contradict the plan are logged there.

| Entity | Description |
|--------|-------------|
| Heating plan | Cost of the plan for the next 36 hours, with its grid heatings and appointment blocks as attributes. The slot by slot plan is too big for a state attribute: it is in the diagnostics and the `tankwise.get_plan` service |
| Next heating | Start of the next grid heating, with its duration, energy and cost |
| `<appointment>` predicted temperature | Temperature the plan expects when the block starts |
| Water heater cost today / this month | Real cost: grid at the purchase price, surplus at its value |
| Planned cost today / this month | What the plan spends |
| Estimated savings this month | Real cost minus planned cost |
| Rescue recharges energy / cost this month | kWh and € of rescue recharges |
| Planned grid heating (binary) | The current slot of the plan heats from the grid |
| Mode (select), hot water reserve and surplus value (numbers), compensation cap reached (switch), replan (button) | Settings |

## Control and safeties

In the auto, eco, comfort and holiday modes TankWise puts the regulator in
manual mode (full power from the grid) during the planned minutes of each
slot and back to automatic (surplus only) afterwards. Every order is written
to the logbook with its reason. Orders are sent when the plan changes its
mind (and repeated if they do not take effect), so an external order is
respected until the next change; the safeties still apply to it.

| Situation | Action |
|-----------|--------|
| Tank above the safety maximum (number, 72 °C) for 1 minute in manual mode | Back to automatic; no grid heating until the tank cools 3 °C |
| No power for 3 minutes in manual mode | Thermostat cut-off: back to automatic, same pause |
| More than 2 hours in a row in manual mode | Back to automatic for 15 minutes |
| Probe unavailable or not updated for 10 minutes | Back to automatic, degraded mode, no grid heating |
| Regulator unavailable beyond a margin (2 minutes, advanced option) while TankWise has it in manual mode, or while the element heats outside the plan | The heater switch is opened as a last resort; when the regulator returns it is set to automatic and the heater switch is closed again. A repair issue appears if this happens three times in a week |
| Heater switch off (not by TankWise) | No action; repair issue |
| Mode changed to shadow or off | The regulator is given back in automatic if TankWise had it in manual mode |

**Degraded mode** (binary sensor): without a plan (no prices, or the tank
model is not ready) TankWise follows a simple rule. Before each active
appointment it heats up to the temperature the appointment needs (limited to
the learned thermostat cut-off), starting as early as the learned power
requires. Only when there are no active appointments it uses two fixed
windows: up to 50 °C between 04:00 and 06:15 and up to the thermostat
cut-off between 15:00 and 16:00. The safeties are the same.

When the plan turns at risk for a new reason TankWise fires the
`tankwise_plan_at_risk` event (`reasons`, `new_reasons`, `appointments`), for
example to send a notification when the regulator is unavailable before an
appointment that depends on a planned heating.

## Services

| Service | Description |
|---------|-------------|
| `tankwise.replan` | Computes the heating plan now. Optional `config_entry_id`. |
| `tankwise.get_plan` | Returns the whole heating plan, slot by slot (response only). Optional `config_entry_id`. |
| `tankwise.reimport_history` | Imports the recorder history of the last days again and merges it with what was learned (also the "Import history again" button). Optional `config_entry_id`. |
| `tankwise.reset_learning` | Forgets the heat losses (`part: losses`), the usage profile (`part: demand`) or everything (`part: all`, default). Optional `config_entry_id`. |

## How the tank model works

- **Energy and litres.** `C = V · 0.001163` kWh/K. Stored energy
  `E = C · (T − T_mains)`. The incoming cold water mixes with the hot water,
  so only the heat above the comfort temperature is usable: useful litres at
  the shower temperature `L = k_use · V · (T − T_comfort) / (T_shower − T_mains)`,
  with `k_use` learned from real shortages.
- **Losses.** `P = UA · (T − T_ambient)`. UA is learned by recursive least
  squares from the slope of the probe over rest windows of at least two hours
  (no heating, no draws), within 0.3–3 W/K.
- **Effective capacity.** The probe is at the top of the tank, so it does not
  see all the energy. The factor `k_cap` (initially 0.9) is learned by
  comparing the heating energy with the probe rise in heatings without draws.
- **Heating power and thermostat.** The full heater power is learned from the
  power sensor. A thermostat cut-off is detected when the regulator asks for
  power (manual mode on, or PWM output above 10 %) and the power stays below
  50 W for 3 minutes; that instant is captured from state changes, so an
  automation that switches the manual mode off afterwards does not hide it.
- **Draws.** Every minute the observed change of the filtered probe is
  compared with the predicted one. A draw starts when the residual
  accumulated over 10 minutes falls below −1 °C and ends when it stays close to
  zero for 5 minutes. Its energy is `C · ΔT_residual / k_cap`.
- **Usage profile.** Each complete day (at least 20 h observed, home
  occupied) is stored as the energy drawn in its 96 slots of 15 minutes. The
  forecast uses the last 16 days of the same type, recent ones weighing more
  (half-life of three weeks). Percentiles are taken on the cumulative curve
  of each day, not slot by slot: the P90 of a day is a "strong" day, and
  showers that move by 15 to 30 minutes do not count twice. A draw that left
  the water below the comfort temperature raises the forecast of its time of
  day by 20 % for two weeks. With the home unoccupied the forecast is zero.
- **Energy reconciliation.** The upper probe sees a shower late and too
  small while the cold water stays at the bottom, and too large when the cold
  front reaches it. After each heating the tank is mixed, so the energy
  balance between two settled heatings gives the energy really drawn and the
  draws in between are scaled to it. A drop of more than 1 °C beyond the
  losses over an hour at rest is recorded as a slow draw.
- **Hot water shortage.** A draw that took the probe at least 1.5 °C below
  the minimum comfort temperature.
- **History import.** On the first start the last 10 days of the recorder are
  replayed through the models, so losses, cut-offs, draws and the usage
  profile are known from day one.
- **Safe mode.** Without the probe or the power, the model freezes and the
  tank sensors become unavailable; nothing is invented. After an outage longer
  than 30 minutes the state is uncertain until the next full tank.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest
```

The models in `custom_components/tankwise/model/` are pure Python and do not
import Home Assistant.

### Exporting history for the simulation bench

`tools/export_history.py` downloads the history of the inputs through the Home
Assistant REST API into `tests/fixtures/` (ignored by git). The URL and a
long-lived access token are read from the `HA_URL` and `HA_TOKEN` environment
variables or from a `.env` file (also ignored by git):

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...
python tools/export_history.py --start 2026-11-07 --end 2026-11-20 \
  --entity temperature=sensor.my_tank_temperature \
  --entity power=sensor.my_tank_power
```

Run `python tools/export_history.py --help` for all the roles.

## License

[MIT](LICENSE)
