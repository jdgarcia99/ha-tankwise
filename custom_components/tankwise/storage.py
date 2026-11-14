"""Persistence of the TankWise model state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_SAVE_DELAY, STORAGE_VERSION

type StoredData = dict[str, Any]


class TankWiseStore:
    """Store the model state of one config entry in ``.storage``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store."""
        self._store: Store[StoredData] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )

    async def async_load(self) -> StoredData | None:
        """Load the stored data, if any."""
        return await self._store.async_load()

    def async_schedule_save(self, data_func: Callable[[], StoredData]) -> None:
        """Save after a delay; pending saves are flushed when HA stops."""
        self._store.async_delay_save(data_func, STORAGE_SAVE_DELAY)

    async def async_save(self, data: StoredData) -> None:
        """Save immediately."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Delete the stored data."""
        await self._store.async_remove()
