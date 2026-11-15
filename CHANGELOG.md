# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jdgarcia99/ha-tankwise/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jdgarcia99/ha-tankwise/releases/tag/v0.2.0
