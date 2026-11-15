"""Actions (services) of TankWise."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_FILE_PATH,
    ATTR_MANUAL_MODE_IS_FAILURE,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import TankWiseConfigEntry
from .model.history import HistoryFormatError

MAX_FILE_BYTES = 50 * 1024 * 1024

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_FILE_PATH): cv.string,
        vol.Optional(ATTR_MANUAL_MODE_IS_FAILURE, default=True): cv.boolean,
    }
)


def _validation_error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _read_history_file(hass: HomeAssistant, file_path: str) -> str:
    """Read the CSV after checking that it may be accessed (executor)."""
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(hass.config.path(file_path))
    if path.suffix.lower() != ".csv":
        raise _validation_error("invalid_file_type")
    resolved = path.resolve()
    config_dir = Path(hass.config.config_dir).resolve()
    if not (
        resolved.is_relative_to(config_dir) or hass.config.is_allowed_path(str(path))
    ):
        raise _validation_error("path_not_allowed", path=file_path)
    if not resolved.is_file():
        raise _validation_error("file_not_found", path=file_path)
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise _validation_error("file_too_large", path=file_path)
    return resolved.read_text(encoding="utf-8-sig", errors="replace")


async def _async_import_history(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry: TankWiseConfigEntry | None = hass.config_entries.async_get_entry(
        call.data[ATTR_CONFIG_ENTRY_ID]
    )
    if entry is None or entry.domain != DOMAIN:
        raise _validation_error("entry_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise _validation_error("entry_not_loaded")

    text = await hass.async_add_executor_job(
        _read_history_file, hass, call.data[ATTR_FILE_PATH]
    )
    try:
        summary, skipped_rows = await entry.runtime_data.async_import_history(
            text,
            manual_mode_is_failure=call.data[ATTR_MANUAL_MODE_IS_FAILURE],
        )
    except HistoryFormatError as err:
        raise _validation_error("invalid_file", error=str(err)) from err
    return {
        "imported_days": summary.days,
        "skipped_days": summary.skipped_days,
        "draws": summary.draws,
        "failures": summary.failures,
        "skipped_rows": skipped_rows,
    }


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the TankWise actions."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _async_import_history,
        schema=IMPORT_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
