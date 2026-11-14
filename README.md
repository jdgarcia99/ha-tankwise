<p align="center">
  <img src="images/banner.png" alt="TankWise" width="100%">
</p>

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

> **Early development (0.1.0, phase 2).** TankWise models the tank and
> publishes its state of charge. Usage learning and the heating plan (the
> output for other integrations) are not available yet.

| Phase | Content | Status |
|-------|---------|--------|
| 1 | Skeleton, config flow, sensor reading | ✅ Done |
| 2 | Tank model (state of charge, recalibration, loss learning) | ✅ Done |
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
- Regulator output (%): output of the regulator that drives the heating
  element, e.g. the PWM output of a solar diverter. When available, it is the
  main criterion to detect a full tank (the regulator asks for power, the
  element draws none: the thermostat is open).

**Options (tank parameters)**

| Parameter | Default |
|-----------|---------|
| Volume | 100 L |
| Rated heating power | 1450 W |
| Mains water region | Balearic Islands |
| Mains water temperature | Monthly value of the region (optional override) |
| Minimum useful temperature (measured at the probe) | 32 °C |
| Thermostat cut-off power threshold | 50 W |
| Thermostat cut-off temperature (initial value, then learned) | 47 °C |
| Energy per shower | 1.8 kWh |
| Comfort level | P90 |

The mains water temperature tables (Balearic Islands, generic mainland Spain,
Canary Islands) are monthly averages approximated from the Spanish reference
tables for domestic hot water sizing.

## Entities

| Entity | Description |
|--------|-------------|
| Useful energy | kWh stored above the minimum useful temperature |
| Charge | Useful energy as a percentage of a full tank |
| Showers available | Useful energy divided by the energy per shower |
| Model status | `calibrated`, `learning` (heat losses still being learned) or `uncertain` (not recalibrated since start-up or since a long outage) |

The learned heat loss coefficient, the learned cut-off temperature and the
recent hot water draws are available in the integration diagnostics.

If a required sensor becomes unavailable, the model freezes and the entities
become unavailable until it recovers; no draws are invented.

## How the tank model works

- The probe is filtered (median + moving average) so its ±1 °C jitter looks
  neither like a draw nor like heating. Heating is only taken from the heating
  element energy meter, never from the probe.
- The state of charge follows an energy balance: heater energy in, standing
  losses (learned slowly during quiet periods, starting at 0.25 °C/h) and
  detected draws out.
- A draw is a sustained drop of the filtered probe, also while heating.
- The tank is recalibrated to "full" when the thermostat cuts off: with a
  regulator sensor, when the regulator asks for power for 3 minutes after
  heating and the element draws none; without it, when heating stops close to
  the cut-off temperature.
- The state is saved and restored across restarts. Heat added while Home
  Assistant was down is taken from the energy meter; after an outage longer
  than 30 minutes the state is marked uncertain until the next full tank.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The models in `custom_components/tankwise/model/` are pure Python and do not
import Home Assistant. Home Assistant's test harness does not run on Windows,
so on Windows run only the model tests:

```bash
pytest tests/model -p no:homeassistant
```

The full suite runs in GitHub Actions.

## License

[MIT](LICENSE)
