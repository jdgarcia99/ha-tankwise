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

> **Development (0.3.0, phase 1 of 7).** TankWise learns how the tank
> behaves and detects every hot water draw, but **it does not control any
> device yet**. Keep your current automations until phase 4.

| Phase | Content | Status |
|-------|---------|--------|
| 1 | Thermal model, draw detection, history import, sensors (no control) | ✅ Done |
| 2 | Demand profile and forecast | Planned |
| 3 | Prices, surplus value and optimiser; shadow mode with the plan | Planned |
| 4 | Controller, safeties and degraded mode; automatic mode | Planned |
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

**Options (installation parameters)**

| Parameter | Default |
|-----------|---------|
| Volume | 100 L |
| Heating power (initial value, then learned) | 1500 W |
| Mains water region | Balearic Islands |
| Ambient temperature without sensor | 20 °C |
| Thermostat cut-off temperature (initial value, then learned) | 68 °C |
| Thermostat cut-off power threshold | 50 W |

**Settings (number entities)**

| Entity | Default |
|--------|---------|
| Shower temperature | 40 °C |
| Minimum comfort temperature | 38 °C |
| Mains water temperature | 0 = automatic (monthly table of the region, interpolated day by day) |

Entities that come from a 0.2 installation are migrated automatically and
keep what was learned. A repair issue asks you to complete the new required
inputs with **Reconfigure**.

## Entities

Entity IDs are generated in English from the name of the instance (for
example `sensor.<name>_available_liters`) whatever the language; visible names
are translated.

| Entity | Description |
|--------|-------------|
| Stored energy | kWh stored above the mains water temperature |
| Useful energy | kWh stored above the minimum comfort temperature |
| Charge | Useful energy as a percentage of a full tank |
| Hot water available | Litres available at the shower temperature |
| Modelled temperature | Equivalent tank temperature tracked by the model |
| Model status | `calibrated`, `learning` or `uncertain` |
| Last draw | Start of the last hot water draw, with its energy, minimum temperature and whether there was a shortage |
| Hot water shortages (7 days) | Draws that left the water below the minimum comfort temperature |
| Heat loss coefficient (diagnostic) | Learned UA, in W/K |
| Learned heating power (diagnostic) | Mean power of the heating element at full output |
| Thermostat cut-off temperature (diagnostic) | Median of the last 20 cut-offs |
| Effective capacity factor (diagnostic) | Share of the heating energy seen by the probe |
| Mains water temperature (diagnostic) | Value in use and its source |

The diagnostics include the learned parameters, the history import result
and the last 50 draws.

## How the tank model works

- **Energy and litres.** `C = V · 0.001163` kWh/K. Stored energy
  `E = C · (T − T_mains)` and litres at the shower temperature
  `L = V · (T − T_mains) / (T_shower − T_mains)`.
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
- **History import.** On the first start the last 10 days of the recorder are
  replayed through the model, so losses, cut-offs and draws are known from
  day one.
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
