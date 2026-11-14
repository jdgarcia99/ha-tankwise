# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - Unreleased

### Added

- Config flow to select the tank temperature, heating element power and
  heating element energy sensors, with unit and state class validation.
- Optional inputs: solar forecast, solar surplus, electricity price, presence
  switch and bottom temperature probe.
- Options flow with the tank parameters (volume, rated power, mains water
  region and optional temperature override, minimum useful temperature,
  cut-off power threshold and comfort level).
- Monthly mains water temperature tables for the Balearic Islands (default),
  generic mainland Spain and the Canary Islands.
- Coordinator that reads and normalises the input sensors every 60 seconds and
  tolerates `unavailable`/`unknown` states.
- English and Spanish translations.

### Notes

- The solar forecast selection is stored but not used yet. Reading the hourly
  forecast curve (Forecast.Solar through the Energy dashboard solar forecast
  platform, Solcast through the sensor attributes) will arrive with the
  planner.
