"""TankWise: hot water tank energy demand planner for Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import TankWiseConfigEntry, TankWiseCoordinator

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Set up TankWise from a config entry."""
    coordinator = TankWiseCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # The tank model must keep running even when no entity is listening
    # (e.g. all entities disabled), so keep the coordinator polling.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
