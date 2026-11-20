"""Number entities for the TankWise runtime settings."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    SETTING_COMFORT_MIN_TEMPERATURE,
    SETTING_MAINS_WATER_TEMPERATURE,
    SETTING_SHOWER_TEMPERATURE,
)
from .coordinator import TankWiseConfigEntry, TankWiseCoordinator
from .entity import TankWiseEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TankWiseNumberDescription(NumberEntityDescription):
    """Describe a TankWise setting."""

    setting: str


NUMBERS: tuple[TankWiseNumberDescription, ...] = (
    TankWiseNumberDescription(
        key=SETTING_SHOWER_TEMPERATURE,
        translation_key=SETTING_SHOWER_TEMPERATURE,
        setting=SETTING_SHOWER_TEMPERATURE,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=30,
        native_max_value=50,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    TankWiseNumberDescription(
        key=SETTING_COMFORT_MIN_TEMPERATURE,
        translation_key=SETTING_COMFORT_MIN_TEMPERATURE,
        setting=SETTING_COMFORT_MIN_TEMPERATURE,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=25,
        native_max_value=55,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    TankWiseNumberDescription(
        # 0 means "automatic": monthly table of the configured region.
        key=SETTING_MAINS_WATER_TEMPERATURE,
        translation_key=SETTING_MAINS_WATER_TEMPERATURE,
        setting=SETTING_MAINS_WATER_TEMPERATURE,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0,
        native_max_value=30,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TankWise settings."""
    coordinator = entry.runtime_data
    async_add_entities(
        TankWiseNumber(coordinator, description) for description in NUMBERS
    )


class TankWiseNumber(TankWiseEntity, NumberEntity):
    """A runtime setting persisted with the learned state."""

    entity_description: TankWiseNumberDescription

    def __init__(
        self,
        coordinator: TankWiseCoordinator,
        description: TankWiseNumberDescription,
    ) -> None:
        """Initialise the number."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Return the current setting."""
        return self.coordinator.settings[self.entity_description.setting]

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting."""
        await self.coordinator.async_set_setting(self.entity_description.setting, value)
