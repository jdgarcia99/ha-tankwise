"""Button entities: compute the plan again."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TankWiseConfigEntry
from .entity import TankWiseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([ReplanButton(entry.runtime_data, "replan")])


class ReplanButton(TankWiseEntity, ButtonEntity):
    """Compute the heating plan now."""

    _platform = "button"
    _attr_translation_key = "replan"

    async def async_press(self) -> None:
        """Replan."""
        await self.coordinator.async_replan("button")
