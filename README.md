# TankWise

[![Validate](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/validate.yml)
[![Tests](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml/badge.svg)](https://github.com/jdgarcia99/ha-tankwise/actions/workflows/tests.yml)

*[Leer en español](README.es.md)*

TankWise is a Home Assistant integration that continuously models an electric
storage water heater and the household's hot water usage. It publishes **how
much energy the tank needs and by when**, so there is always hot water when it
is going to be used, at the lowest grid cost.

**TankWise never controls any device.** It does not switch relays or change
the mode of a solar diverter. It only publishes the demand; another
integration or your own automations decide how to fulfil it.

## Project status

> **Early development (0.1.0, phase 1).** The integration can be installed and
> configured, and it reads the tank sensors every 60 seconds. It does **not**
> create any entities yet.

| Phase | Content | Status |
|-------|---------|--------|
| 1 | Skeleton, config flow, sensor reading | ✅ Done |
| 2 | Tank model (state of charge, recalibration, loss learning) | Planned |
| 3 | Usage detection and learned profile | Planned |
| 4 | Planner and output contract | Planned |
| 5 | Boost button, comfort selector, holiday mode, diagnostics | Planned |
| 6 | Full documentation and first stable release | Planned |

## Installation

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/jdgarcia99/ha-tankwise` with category **Integration**.
3. Install **TankWise** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → TankWise**.

## Configuration

Everything is configured from the UI; nothing goes into `configuration.yaml`.

**Required sensors**

- Tank temperature (°C or °F).
- Heating element power (W or kW).
- Heating element energy (kWh or Wh, state class `total_increasing` or `total`).

**Optional inputs**

- Solar forecast (Forecast.Solar or Solcast). In this version the selection
  is only stored: the hourly forecast curve (36 h horizon) will be read when
  the planner is implemented (phase 4).
- Current solar surplus power.
- Electricity price (e.g. PVPC).
- Presence / "home active" switch: when off, TankWise enters holiday mode.
- Bottom tank temperature probe.

**Options (tank parameters)**

| Parameter | Default |
|-----------|---------|
| Volume | 100 L |
| Rated heating power | 1450 W |
| Mains water region | Balearic Islands |
| Mains water temperature | Monthly value of the region (optional override) |
| Minimum useful temperature | 38 °C |
| Thermostat cut-off power threshold | 50 W |
| Comfort level | P90 |

The mains water temperature tables (Balearic Islands, generic mainland Spain,
Canary Islands) are monthly averages approximated from the Spanish reference
tables for domestic hot water sizing.

## License

[MIT](LICENSE)
