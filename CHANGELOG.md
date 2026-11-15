# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Demand model (pure Python): 15-minute usage profile per day type (workday,
  Saturday, Sunday/holiday), exponential weighting of recent weeks (21-day
  half-life) and forecasts at the comfort percentile of cumulative demand.
- Forecast failures: a draw started with less useful energy than half a
  shower, or the regulator manual mode switched on by a person (directly or
  through an automation listed as acting for a person).
- Optional workday sensor (Workday integration) to recognise holidays.
- Cold start with a default profile blended in until 14 days of data.
- Learned energy per shower.
- `tankwise.import_history` action: raw minute readings from a CSV are
  replayed through the tank model to learn the profile from real history.
  An optional `manual_source` column attributes manual mode activations with
  the same rule as live.
- `tools/export_history.py`: exports the last N days from the Home Assistant
  REST API (history and logbook) as a CSV ready to import.
- Sensors: next expected draw and demand profile status.
- Demand profile, recent draws and failures in the diagnostics.

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

[Unreleased]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jdgarcia99/ha-tankwise/releases/tag/v0.2.0
