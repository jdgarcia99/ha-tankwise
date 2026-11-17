"""Sensors for the TankWise tank state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_SHOWER_ENERGY
from .coordinator import TankWiseConfigEntry, TankWiseCoordinator
from .entity import TankWiseEntity
from .model.tank import ModelStatus, TankState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TankWiseSensorDescription(SensorEntityDescription):
    """Describe a TankWise tank sensor."""

    value_fn: Callable[[TankState], StateType]
    attributes_fn: Callable[[TankWiseCoordinator], dict[str, Any]] | None = None


SENSORS: tuple[TankWiseSensorDescription, ...] = (
    TankWiseSensorDescription(
        key="useful_energy",
        translation_key="useful_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda tank: tank.useful_energy_kwh,
    ),
    TankWiseSensorDescription(
        key="charge",
        translation_key="charge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda tank: tank.charge_percent,
    ),
    TankWiseSensorDescription(
        key="showers_available",
        # No unit on purpose: Home Assistant takes custom units from the English
        # translations in every language, and adding one would also raise a
        # statistics repair for the history recorded without a unit.
        translation_key="showers_available",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda tank: tank.showers_available,
        attributes_fn=lambda coordinator: {
            "energy_per_shower_kwh": float(coordinator.parameters[CONF_SHOWER_ENERGY])
        },
    ),
    TankWiseSensorDescription(
        key="model_status",
        translation_key="model_status",
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in ModelStatus],
        value_fn=lambda tank: tank.status.value,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TankWise sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        TankWiseSensor(coordinator, description) for description in SENSORS
    )


class TankWiseSensor(TankWiseEntity, SensorEntity):
    """A sensor derived from the tank model state."""

    entity_description: TankWiseSensorDescription

    def __init__(
        self,
        coordinator: TankWiseCoordinator,
        description: TankWiseSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable while the model is frozen (safe mode)."""
        return super().available and self.coordinator.data.tank.available

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data.tank)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the reference values the sensor value is derived from."""
        if (attributes_fn := self.entity_description.attributes_fn) is None:
            return None
        return attributes_fn(self.coordinator)
