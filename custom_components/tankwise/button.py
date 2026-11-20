"""Buttons: boost, cancel boost, replan and import the history again."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
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
    coordinator = entry.runtime_data
    async_add_entities(
        [
            BoostButton(coordinator, "boost"),
            CancelBoostButton(coordinator, "cancel_boost"),
            ReplanButton(coordinator, "replan"),
            ReimportHistoryButton(coordinator, "reimport_history"),
        ]
    )


class BoostButton(TankWiseEntity, ButtonEntity):
    """Heat now; pressed again while the boost runs, cancel it."""

    _platform = "button"
    _attr_translation_key = "boost"

    async def async_press(self) -> None:
        """Start or cancel the boost."""
        await self.coordinator.controller.async_toggle_boost()


class CancelBoostButton(TankWiseEntity, ButtonEntity):
    """Cancel the boost in progress."""

    _platform = "button"
    _attr_translation_key = "cancel_boost"

    async def async_press(self) -> None:
        """Cancel the boost."""
        await self.coordinator.controller.async_cancel_boost()


class ReplanButton(TankWiseEntity, ButtonEntity):
    """Compute the heating plan now."""

    _platform = "button"
    _attr_translation_key = "replan"

    async def async_press(self) -> None:
        """Replan."""
        await self.coordinator.async_replan("button")


class ReimportHistoryButton(TankWiseEntity, ButtonEntity):
    """Import the recorder history again (merged with what was learned)."""

    _platform = "button"
    _attr_translation_key = "reimport_history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Start the import in the background."""
        self.coordinator.async_start_bootstrap()
