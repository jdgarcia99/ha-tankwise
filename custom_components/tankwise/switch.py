"""Switch entities: enable or suspend each appointment."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TankWiseConfigEntry
from .entity import AppointmentEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one switch per appointment."""
    coordinator = entry.runtime_data
    async_add_entities(
        AppointmentSwitch(coordinator, appointment, "enabled")
        for appointment in coordinator.appointments
    )


class AppointmentSwitch(AppointmentEntity, SwitchEntity):
    """Whether the appointment is active."""

    _platform = "switch"

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        """Return True if the appointment is active."""
        appointment = self.appointment
        return appointment.enabled if appointment else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate the appointment."""
        self.coordinator.async_update_appointment(self._appointment_id, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Suspend the appointment."""
        self.coordinator.async_update_appointment(self._appointment_id, enabled=False)
