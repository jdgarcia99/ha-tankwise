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
            DegradedModeSensor(coordinator, "degraded_mode"),
            BoostActiveSensor(coordinator, "boost_active"),
        ]
    )


class PlanAtRiskSensor(TankWiseEntity, BinarySensorEntity):
    """On when hot water cannot be guaranteed for an active appointment.

    With a plan, an appointment block (or the demand outside blocks) cannot
    be covered even with the rescue recharge, or there is no time left to
    heat. Without a plan (no prices), an appointment needs more than a full
    tank plus what the rescue recharge can add during its block. Also on
    when the regulator is unavailable and an appointment depends on a
    planned heating, or the probe is unavailable before an appointment.
    """

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "plan_at_risk"

    @property
    def is_on(self) -> bool:
        """Return True if hot water cannot be guaranteed."""
        return bool(self.coordinator.data.risk_reasons)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the appointments at risk and what they need."""
        data = self.coordinator.data
        names = {
            appointment.id: appointment.name
            for appointment in self.coordinator.appointments
        }
        return {
            "reasons": list(data.risk_reasons),
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


class DegradedModeSensor(TankWiseEntity, BinarySensorEntity):
    """On when TankWise acts without a plan (simple rule) or without probe."""

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "degraded_mode"

    @property
    def is_on(self) -> bool:
        """Return True in degraded mode."""
        return self.coordinator.controller.degraded_reason is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why, and what the simple rule is heating for."""
        controller = self.coordinator.controller
        attributes: dict[str, Any] = {"reason": controller.degraded_reason}
        target = controller.degraded
        if target is not None and target.target is not None:
            attributes |= {
                "target_temperature": round(target.target, 1),
                "appointment": target.name,
                "due": target.due.isoformat() if target.due else None,
                "heating_from": target.start.isoformat() if target.start else None,
            }
        return attributes


class BoostActiveSensor(TankWiseEntity, BinarySensorEntity):
    """On while a boost is in progress."""

    _platform = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_translation_key = "boost_active"

    @property
    def is_on(self) -> bool:
        """Return True while boosting."""
        return self.coordinator.controller.boost is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the start, target, expected end, energy and cost."""
        controller = self.coordinator.controller
        boost = controller.boost
        if boost is None:
            return {}
        end = controller.expected_boost_end(dt_util.utcnow())
        return {
            "start": boost.start.isoformat(),
            "target_temperature": round(boost.target, 1),
            "expected_end": end.isoformat() if end else None,
            "deadline": boost.deadline.isoformat() if boost.deadline else None,
            "energy_kwh": round(boost.energy_kwh, 3),
            "cost": round(boost.cost, 3),
        }
