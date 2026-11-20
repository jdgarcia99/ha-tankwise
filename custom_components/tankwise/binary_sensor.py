"""Binary sensors of TankWise."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up the TankWise binary sensors."""
    async_add_entities([PlanAtRiskSensor(entry.runtime_data, "plan_at_risk")])


class PlanAtRiskSensor(TankWiseEntity, BinarySensorEntity):
    """On when hot water cannot be guaranteed for an active appointment.

    In this version: an appointment needs a tank temperature above the
    learned thermostat cut-off, so even a full tank does not cover its block.
    """

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "plan_at_risk"

    @property
    def is_on(self) -> bool:
        """Return True if an active appointment cannot be covered."""
        return bool(self.coordinator.data.at_risk)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the appointments at risk and what they need."""
        data = self.coordinator.data
        names = {
            appointment.id: appointment.name
            for appointment in self.coordinator.appointments
        }
        return {
            "appointments": [
                {
                    "name": names.get(appointment_id, appointment_id),
                    "required_temperature": round(
                        data.appointment_requirements[appointment_id].temperature, 1
                    ),
                    "liters": round(
                        data.appointment_requirements[appointment_id].liters, 1
                    ),
                }
                for appointment_id in data.at_risk
            ],
            "cutoff_temperature": round(data.tank.cutoff_temperature, 1),
        }
