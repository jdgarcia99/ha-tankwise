"""TankWise: hot water tank energy demand planner for Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import TankWiseConfigEntry, TankWiseCoordinator
from .storage import TankWiseStore

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Set up TankWise from a config entry."""
    coordinator = TankWiseCoordinator(hass, entry)
    integration = await async_get_integration(hass, DOMAIN)
    coordinator.sw_version = str(integration.version) if integration.version else None
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_listeners()

    # The tank model must keep running even when no entity is listening
    # (e.g. all entities disabled), so keep the coordinator polling.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> bool:
    """Unload a config entry, saving the model state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save_now()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TankWiseConfigEntry) -> None:
    """Delete the persisted state when the integration is removed."""
    await TankWiseStore(hass, entry.entry_id).async_remove()
