"""Select entity: the operating mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TankWiseConfigEntry
from .entity import TankWiseEntity
from .model.planning import Mode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the mode select."""
    async_add_entities([ModeSelect(entry.runtime_data, "mode")])


class ModeSelect(TankWiseEntity, SelectEntity):
    """Auto, eco, comfort, holiday, shadow or off."""

    _platform = "select"
    _attr_translation_key = "mode"

    @property
    def options(self) -> list[str]:
        """Return the modes."""
        return [mode.value for mode in Mode]

    @property
    def current_option(self) -> str:
        """Return the current mode."""
        return self.coordinator.planner.mode.value

    async def async_select_option(self, option: str) -> None:
        """Change the mode."""
        await self.coordinator.async_set_mode(Mode(option))
