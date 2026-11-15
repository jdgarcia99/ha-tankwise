"""Sensors for the TankWise tank state and demand forecast."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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

from .coordinator import TankWiseConfigEntry, TankWiseCoordinator, TankWiseData
from .entity import TankWiseEntity
from .model.demand import DemandStatus
from .model.tank import ModelStatus

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TankWiseSensorDescription(SensorEntityDescription):
    """Describe a TankWise sensor."""

    value_fn: Callable[[TankWiseData], Any]
    attributes_fn: Callable[[TankWiseData], Mapping[str, Any] | None] = lambda data: (
        None
    )
    # Tank sensors are unavailable while the tank model is frozen (safe mode).
    requires_tank: bool = True


def _next_draw_attributes(data: TankWiseData) -> Mapping[str, Any] | None:
    draw = data.demand.next_draw
    if draw is None:
        return None
    return {
        "energy_kwh": round(draw.energy_kwh, 2),
        "probability": round(draw.probability, 2),
        "day_type": draw.day_type.value,
    }


def _demand_attributes(data: TankWiseData) -> Mapping[str, Any]:
    demand = data.demand
    learned = demand.learned_shower_energy_kwh
    return {
        "days_with_data": demand.days_with_data,
        "today_type": demand.today_type.value,
        "shower_energy_kwh": round(demand.shower_energy_kwh, 2),
        "learned_shower_energy_kwh": None if learned is None else round(learned, 2),
    }


SENSORS: tuple[TankWiseSensorDescription, ...] = (
    TankWiseSensorDescription(
        key="useful_energy",
        translation_key="useful_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.tank.useful_energy_kwh,
    ),
    TankWiseSensorDescription(
        key="charge",
        translation_key="charge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.tank.charge_percent,
    ),
    TankWiseSensorDescription(
        key="showers_available",
        translation_key="showers_available",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.tank.showers_available,
    ),
    TankWiseSensorDescription(
        key="model_status",
        translation_key="model_status",
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in ModelStatus],
        value_fn=lambda data: data.tank.status.value,
    ),
    TankWiseSensorDescription(
        key="next_expected_draw",
        translation_key="next_expected_draw",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (
            data.demand.next_draw.start if data.demand.next_draw else None
        ),
        attributes_fn=_next_draw_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="demand_profile",
        translation_key="demand_profile",
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in DemandStatus],
        value_fn=lambda data: data.demand.status.value,
        attributes_fn=_demand_attributes,
        requires_tank=False,
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
    """A sensor derived from the TankWise models."""

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
        """Tank sensors are unavailable while the tank model is frozen."""
        if not super().available:
            return False
        return (
            not self.entity_description.requires_tank
            or self.coordinator.data.tank.available
        )

    @property
    def native_value(self) -> StateType | Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return extra attributes."""
        return self.entity_description.attributes_fn(self.coordinator.data)
