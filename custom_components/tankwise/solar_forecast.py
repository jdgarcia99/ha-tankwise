"""Hourly solar production forecast from the energy platforms.

Integrations such as Forecast.Solar, Solcast or Open-Meteo Solar Forecast
give the Energy dashboard an hourly forecast through their ``energy``
platform (``async_get_solar_forecast``). TankWise reads the same forecast
from the config entries chosen in the options, and sums them (e.g. one
entry per roof plane).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .model.solar import PERIOD_END_DOMAINS, sum_forecasts

_LOGGER = logging.getLogger(__name__)

ENERGY_PLATFORM = "energy"


async def _forecast_function(hass: HomeAssistant, domain: str) -> Any | None:
    """Return the ``async_get_solar_forecast`` of an integration, if any."""
    try:
        integration = await async_get_integration(hass, domain)
        if not integration.platforms_exists([ENERGY_PLATFORM]):
            return None
        platform = await integration.async_get_platform(ENERGY_PLATFORM)
    except IntegrationNotFound, ImportError:
        return None
    return getattr(platform, "async_get_solar_forecast", None)


async def async_forecast_entries(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Return (entry ID, label) of the entries that give a solar forecast."""
    result = []
    for entry in hass.config_entries.async_entries():
        if entry.disabled_by is not None:
            continue
        if await _forecast_function(hass, entry.domain) is None:
            continue
        label = f"{entry.title} ({entry.domain}, {entry.entry_id[-6:]})"
        result.append((entry.entry_id, label))
    return result


async def async_hourly_forecast(
    hass: HomeAssistant, entry_ids: Iterable[str], tz: tzinfo
) -> dict[datetime, float] | None:
    """Return the summed forecast in kWh per local hour start.

    None when no chosen entry gives a forecast right now (not loaded yet, or
    removed): the plan then counts on no surplus.
    """
    forecasts = []
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            continue
        function = await _forecast_function(hass, entry.domain)
        if function is None:
            continue
        try:
            data = await function(hass, entry_id)
        except Exception as err:
            _LOGGER.debug("Solar forecast of %s failed: %s", entry.title, err)
            continue
        if data and (hours := data.get("wh_hours")):
            forecasts.append((hours, entry.domain in PERIOD_END_DOMAINS))
    if not forecasts:
        return None
    return sum_forecasts(forecasts, tz)
