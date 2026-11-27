"""Switch entities: enable or suspend each appointment."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TankWiseConfigEntry
from .entity import AppointmentEntity, TankWiseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one switch per appointment."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CompensationCapSwitch(coordinator, "compensation_cap"),
            SpareSurplusSwitch(coordinator, "spare_surplus"),
            AntiLegionellaSwitch(coordinator, "anti_legionella"),
            *(
                AppointmentSwitch(coordinator, appointment, "enabled")
                for appointment in coordinator.appointments
            ),
        ]
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


class CompensationCapSwitch(TankWiseEntity, SwitchEntity):
    """The surplus compensation cap of the month was reached: surplus is free."""

    _platform = "switch"
    _attr_translation_key = "compensation_cap"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        """Return True when the surplus is worth nothing."""
        return self.coordinator.planner.compensation_cap

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mark the cap as reached."""
        await self.coordinator.async_set_compensation_cap(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mark the cap as not reached."""
        await self.coordinator.async_set_compensation_cap(False)


class SpareSurplusSwitch(TankWiseEntity, SwitchEntity):
    """While the plan sells, a tank that is not full takes the spare export."""

    _platform = "switch"
    _attr_translation_key = "spare_surplus"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        """Return True when the tank takes the spare surplus."""
        return self.coordinator.planner.spare_surplus

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Take the spare surplus."""
        await self.coordinator.async_set_spare_surplus(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Let the surplus go to the grid while the plan sells."""
        await self.coordinator.async_set_spare_surplus(False)


class AntiLegionellaSwitch(TankWiseEntity, SwitchEntity):
    """Weekly anti-legionella cycle: 60 °C or more for an hour."""

    _platform = "switch"
    _attr_translation_key = "anti_legionella"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        """Return True when the cycle is planned."""
        return self.coordinator.planner.anti_legionella

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Plan the weekly cycle."""
        await self.coordinator.async_set_anti_legionella(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Do not plan it."""
        await self.coordinator.async_set_anti_legionella(False)
