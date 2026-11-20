"""Binary sensors of TankWise."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import TankWiseConfigEntry
from .entity import TankWiseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TankWise binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            PlanAtRiskSensor(coordinator, "plan_at_risk"),
            PlannedGridHeatingSensor(coordinator, "planned_grid_heating"),
        ]
    )


class PlanAtRiskSensor(TankWiseEntity, BinarySensorEntity):
    """On when hot water cannot be guaranteed for an active appointment.

    With a plan, an appointment block (or the demand outside blocks) cannot
    be covered even with the rescue recharge, or there is no time left to
    heat. Without a plan (no prices), an appointment needs more than a full
    tank plus what the rescue recharge can add during its block.
    """

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "plan_at_risk"

    @property
    def is_on(self) -> bool:
        """Return True if an active appointment cannot be covered."""
        plan = self.coordinator.planner.plan
        if plan is not None and plan.at_risk and not self.coordinator.data.at_risk:
            # Demand outside the appointment blocks cannot be covered.
            return True
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


class PlannedGridHeatingSensor(TankWiseEntity, BinarySensorEntity):
    """On when the plan heats from the grid in the current slot."""

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_translation_key = "planned_grid_heating"

    @property
    def is_on(self) -> bool:
        """Return True if the current slot of the plan uses grid energy."""
        slot = self.coordinator.planner.current_slot(dt_util.utcnow())
        return slot is not None and slot.grid_kwh > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether it is a rescue recharge and if surplus is allowed."""
        slot = self.coordinator.planner.current_slot(dt_util.utcnow())
        if slot is None:
            return {}
        return {
            "rescue": slot.rescue,
            "solar_allowed": slot.solar_allowed,
            "grid_kwh": round(slot.grid_kwh, 3),
        }
