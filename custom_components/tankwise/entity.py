"""Base entity for TankWise."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TankWiseCoordinator


class TankWiseEntity(CoordinatorEntity[TankWiseCoordinator]):
    """Entity attached to the TankWise service device of a config entry."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TankWiseCoordinator, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._object_id = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def suggested_object_id(self) -> str | None:
        """Return an English object ID, whatever the Home Assistant language.

        Home Assistant still prefixes it with the device name, and users can
        rename the entity. Only the generated entity ID is affected; the
        visible name stays translated.
        """
        return self._object_id
