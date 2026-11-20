"""Services of TankWise."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_PART,
    DOMAIN,
    PART_ALL,
    RESET_PARTS,
    SERVICE_RESET_LEARNING,
)
from .coordinator import TankWiseConfigEntry

RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_PART, default=PART_ALL): vol.In(RESET_PARTS),
    }
)


def _loaded_entries(
    hass: HomeAssistant, call: ServiceCall
) -> list[TankWiseConfigEntry]:
    """Return the entries a service call applies to."""
    entries: list[TankWiseConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": entry_id},
            )
    return entries


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the TankWise services."""

    async def _reset_learning(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            await entry.runtime_data.async_reset_learning(call.data[ATTR_PART])

    hass.services.async_register(
        DOMAIN, SERVICE_RESET_LEARNING, _reset_learning, RESET_LEARNING_SCHEMA
    )
