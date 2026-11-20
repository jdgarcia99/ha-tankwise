# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-20

Second phase: TankWise learns the household's hot water usage and forecasts
the demand. It still does not act on any device.

### Added

- Usage profile: each complete day (at least 20 hours observed, home
  occupied) is kept as the energy drawn in 96 slots of 15 minutes. Days are
  grouped as workdays and weekends/holidays (or per weekday, or all alike);
  the last 16 days of each type are used, recent ones weighing more.
- Forecast of the next 36 hours with P50 and P90 per slot, computed on the
  cumulative curve of each day so that a strong day is not inflated by
  showers that move from one slot to another.
- A draw that left the water below the comfort temperature raises the
  forecast of its time of day by 20 % for two weeks.
- Energy reconciliation: with the probe at the top of the tank, a draw is
  seen late and too small while the cold water stays at the bottom (morning
  showers) and too large when the cold front reaches the probe (evening
  showers). After each heating the probe sees the mixed tank, so the energy
  balance between two settled heatings gives the energy really drawn, and
  the draws detected in between are scaled to match (×0.5 to ×5). On the
  reference installation the morning showers went from 0.4-1.1 kWh to their
  real 0.6-1.0 kWh and the evening ones were lowered by 10-30 %.
- Slow draws: a drop of more than 1 °C beyond the standing losses over an
  hour at rest, confirmed by the robust slope of the hour, is recorded as a
  draw (e.g. a shower with a hot, full tank where the cold water barely
  reaches the probe). This also keeps such drops out of the loss learning:
  the learned coefficient went from 1.39 to 0.94 W/K on real data.
- Days of the usage profile are closed at 09:00 the next day, once the night
  heating has reconciled the evening draws.
- Optional Workday sensor: holidays are learned apart from workdays, and the
  coming days are checked with the Workday integration.
- Hot water appointments (up to six): start time, block duration (1 hour by
  default), days, minimum litres at the shower temperature and active or
  not. The minimum litres must be available when the block starts and cover
  the whole block without reheating. Each appointment has time, litres and
  active entities synchronised with the options, and a suggested litres
  sensor (P90 of the water used during the block in the last four weeks).
- Sensors: hot water demand today and hot water demand forecast (36 hours,
  slots as an attribute that is not recorded).
- `tankwise.reset_learning` service (heat losses, usage profile or
  everything).
- The history import also rebuilds the usage profile.

### Changed

- The options are a menu: installation parameters, inputs and hot water
  appointments. Heating power, ambient temperature, thermostat cut-off and
  the usage profile settings are folded under "Advanced options".
- The heat loss coefficient, learned heating power, thermostat cut-off
  temperature and effective capacity factor sensors are disabled by default
  for new installations.
- Entity IDs no longer depend on the device name or area: they are always
  `<platform>.tankwise_<key>` (e.g. `sensor.tankwise_stored_energy`).
  Entities created by 0.3.0 with the area in their ID (e.g.
  `sensor.coladuria_tankwise_stored_energy`) are renamed in the entity
  registry and keep their history. Entity IDs changed by hand are kept.
- The 0.2 entities `sensor.tankwise_useful_energy`, `sensor.tankwise_charge`
  and `sensor.tankwise_model_status` are kept: they are the same entities as
  before (same unique IDs and history), not duplicates. Since 0.3.0 useful
  energy and charge are measured above the minimum comfort temperature
  (38 °C) instead of the 0.2 "minimum useful temperature" (32 °C).

### Fixed

- Heating stopped by an automation (e.g. a boost to 45 °C that switches the
  diverter manual mode off) was learned as a thermostat cut-off, which left
  the learned cut-off temperature around 46 °C:
  - the manual mode switched off, or the PWM output falling to rest, up to
    60 seconds after the power falls now means the regulator stopped the
    heating (its state reaches Home Assistant a few seconds after the power);
  - when the regulator is configured but unavailable, a stop of the heating
    is no longer decided with the temperature backup criterion;
  - a cut-off more than 12 °C below the configured cut-off temperature is
    not accepted, and such cut-offs learned before are forgotten;
  - entries that still had the 0.2 defaults (47 °C cut-off and 1450 W) get
    the current ones (68 °C and 1500 W).
- The effective capacity factor is limited to 1.0 (it reached 1.057).
- A draw counts as a hot water shortage only if the probe fell at least
  1.5 °C below the comfort temperature (water kept being drawn below it), not
  when the end of a shower just touches it. On real data this kept the 8
  real shortages of a week and dropped the 2 that only touched 38 °C.

## [0.3.0] - 2026-11-20

First phase of the full control of the water heater: TankWise learns the
tank and the draws with the inputs it will use to control it. It still does
not act on any device.

### Added

- New required inputs: water heater power switch, regulator manual mode
  switch (e.g. FreeDS manual mode) and electricity purchase price.
- New optional inputs: regulator master entity, home occupied, grid
  injection price, grid import and export energy, current export power and
  ambient temperature around the tank.
- History import: on the first start the last 10 days of the recorder are
  replayed through the tank model, so losses, cut-offs and draws are known
  from day one.
- Sensors: stored energy, hot water available (litres at the shower
  temperature), modelled temperature, last draw (with energy, minimum
  temperature and shortage), hot water shortages in the last 7 days, and the
  diagnostic sensors heat loss coefficient (UA), learned heating power,
  thermostat cut-off temperature, effective capacity factor and mains water
  temperature.
- Number entities: shower temperature (40 °C), minimum comfort temperature
  (38 °C) and mains water temperature (0 = automatic).
- Repair issue when required inputs are missing (e.g. after upgrading).
- `tools/export_history.py` to export the history of the inputs through the
  Home Assistant REST API for the simulation bench.

### Changed

- Standing losses are modelled as `UA · (T − T_ambient)` and UA is learned by
  recursive least squares over rest windows of at least two hours.
- The effective capacity factor (share of the heating energy seen by the
  upper probe) is learned from heatings without draws.
- Draws are detected from the residual between the observed and the predicted
  probe change; each draw records its minimum temperature and whether it left
  the water below the minimum comfort temperature.
- The thermostat cut-off is detected with the manual mode switch as well as
  the regulator output, and the last 20 cut-offs are kept.
- The water heater energy sensor is now optional: without it the power is
  integrated.
- The mains water temperature of the region is interpolated day by day.
- Defaults follow the reference installation: 1500 W and 68 °C cut-off.
- Useful energy and charge are measured above the minimum comfort
  temperature.

### Removed

- The "showers available" sensor and the energy per shower, minimum useful
  temperature and comfort level options (replaced by the litres available and
  the comfort settings). Existing entries are migrated automatically.
- The bottom temperature probe input.

## [0.2.3] - 2026-11-17

### Added

- The TankWise device now shows its manufacturer, model, software version
  (taken from the integration manifest) and a link to the repository.
- The "Showers available" sensor has the attribute `energy_per_shower_kwh`,
  the energy per shower used to compute it.

## [0.2.2] - 2026-11-16

### Fixed

- A thermostat cut-off was missed when the regulator was switched off shortly
  afterwards (e.g. an automation turning off the diverter manual mode three
  minutes after the power drops): with 60-second samples only about two
  minutes of regulator demand were seen. The regulator value is now captured
  at the instant the power falls, from the state changes of the power and
  regulator sensors, and a full tank is confirmed if the power stays below
  the threshold for 3 minutes, even if the regulator goes to 0 afterwards.
  A power drop caused by the regulator being lowered first is still not a
  full tank.

## [0.2.1] - 2026-11-15

### Fixed

- Entity IDs are now generated from fixed English object IDs
  (`useful_energy`, `charge`, `showers_available`, `model_status`) whatever
  the Home Assistant language, so they no longer depend on the language
  (e.g. `sensor.<name>_useful_energy` instead of a Spanish entity ID).
  Visible names stay translated. Existing entities keep their current entity
  IDs.

## [0.2.0] - 2026-11-15

First public release (0.1.0 was an internal milestone and was not published).
TankWise models the tank and publishes its state of charge; usage learning
and the heating plan will follow.

### Added

- Config flow to select the tank temperature, heating element power and
  heating element energy sensors, with unit and state class validation.
- Reconfigure step to change the required sensors without removing the
  integration.
- Optional inputs: solar forecast, solar surplus, electricity price, presence
  switch, bottom temperature probe and regulator output (%).
- Options flow with the tank parameters: volume, rated power, mains water
  region and optional temperature override, minimum useful temperature
  (measured at the probe, 32 °C by default), cut-off power threshold,
  thermostat cut-off temperature, energy per shower and comfort level.
- Monthly mains water temperature tables for the Balearic Islands (default),
  generic mainland Spain and the Canary Islands.
- Tank thermal model (pure Python): filtered probe, energy balance with the
  heating element energy meter, draw detection (also while heating),
  recalibration to full on thermostat cut-off (regulator output as main
  criterion, cut-off temperature as backup), learned cut-off temperature and
  slowly learned standing losses.
- Sensors: useful energy, charge, showers available and model status.
- Model state persisted across restarts; outages longer than 30 minutes mark
  it as uncertain until the next full tank.
- Safe mode: if a required sensor is missing the model freezes and the
  sensors become unavailable.
- Diagnostics with the loss coefficient, the learned cut-off temperature and
  the recent draws.
- English and Spanish translations.
- Brand icons for Home Assistant 2026.3 and later.

### Notes

- Requires Home Assistant 2026.3 or later.
- The solar forecast selection is stored but not used yet. Reading the hourly
  forecast curve (Forecast.Solar through the Energy dashboard solar forecast
  platform, Solcast through the sensor attributes) will arrive with the
  planner.
- The output contract entities (energy needed, deadline, grid allowed,
  requested power and urgency) arrive in a later release.

[Unreleased]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jdgarcia99/ha-tankwise/releases/tag/v0.2.0
