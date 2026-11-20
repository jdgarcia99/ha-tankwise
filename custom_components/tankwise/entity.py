"""Base entities for TankWise."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONFIGURATION_URL, DOMAIN, MANUFACTURER, MODEL
from .coordinator import TankWiseCoordinator
from .model.appointments import Appointment


def tankwise_entity_id(platform: str, object_id: str) -> str:
    """Return the entity ID TankWise gives an entity (``sensor.tankwise_x``)."""
    return f"{platform}.{DOMAIN}_{object_id}"


class TankWiseEntity(CoordinatorEntity[TankWiseCoordinator]):
    """Entity attached to the TankWise service device of a config entry.

    The entity ID is fixed to ``<platform>.tankwise_<object id>`` so it does
    not depend on the device name or its area (Home Assistant adds the area
    to generated entity IDs). Users can still rename it.
    """

    _attr_has_entity_name = True
    _platform: str = ""

    def __init__(
        self,
        coordinator: TankWiseCoordinator,
        key: str,
        object_id: str | None = None,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._object_id = object_id or key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        if self._platform:
            self.entity_id = tankwise_entity_id(self._platform, self._object_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=coordinator.sw_version,
            configuration_url=CONFIGURATION_URL,
        )

    @property
    def suggested_object_id(self) -> str | None:
        """Return an English object ID, whatever the Home Assistant language.

        Home Assistant still prefixes it with the device name, and users can
        rename the entity. Only the generated entity ID is affected; the
        visible name stays translated.
        """
        return self._object_id


def appointment_unique_key(appointment_id: str, kind: str) -> str:
    """Return the unique ID suffix of an appointment entity."""
    return f"appointment_{appointment_id}_{kind}"


class AppointmentEntity(TankWiseEntity):
    """Entity of one hot water appointment (e.g. ``time.<name>_<slug>_time``)."""

    def __init__(
        self, coordinator: TankWiseCoordinator, appointment: Appointment, kind: str
    ) -> None:
        """Initialise the entity; the unique ID follows the appointment ID."""
        super().__init__(
            coordinator,
            appointment_unique_key(appointment.id, kind),
            f"{appointment.slug}_{kind}",
        )
        self._appointment_id = appointment.id
        self._attr_translation_key = f"appointment_{kind}"
        self._attr_translation_placeholders = {"appointment": appointment.name}

    @property
    def appointment(self) -> Appointment | None:
        """Return the current settings of the appointment."""
        return next(
            (
                appointment
                for appointment in self.coordinator.appointments
                if appointment.id == self._appointment_id
            ),
            None,
        )

    @property
    def available(self) -> bool:
        """Unavailable if the appointment was removed."""
        return super().available and self.appointment is not None
