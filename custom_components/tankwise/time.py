"""Time entities: the "water ready by" time of each appointment."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
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
    """Set up one time entity per appointment."""
    coordinator = entry.runtime_data
    async_add_entities(
        AppointmentTime(coordinator, appointment, "time")
        for appointment in coordinator.appointments
    )


class AppointmentTime(AppointmentEntity, TimeEntity):
    """Time by which the water must be ready."""

    _platform = "time"

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> time | None:
        """Return the time of the appointment."""
        appointment = self.appointment
        return appointment.time if appointment else None

    async def async_set_value(self, value: time) -> None:
        """Change the time of the appointment."""
        self.coordinator.async_update_appointment(
            self._appointment_id, time=value.strftime("%H:%M")
        )
