"""Services of TankWise."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION,
    ATTR_INDEX,
    ATTR_LITERS,
    ATTR_PART,
    ATTR_TEMPERATURE,
    ATTR_VALUE,
    DOMAIN,
    PART_ALL,
    RESET_PARTS,
    SERVICE_BOOST,
    SERVICE_CANCEL_BOOST,
    SERVICE_DISCARD_CUTOFF_SAMPLE,
    SERVICE_DISCARD_LAST_CUTOFF_SAMPLE,
    SERVICE_GET_PLAN,
    SERVICE_REIMPORT_HISTORY,
    SERVICE_REPLAN,
    SERVICE_RESET_LEARNING,
    SERVICE_RESET_USABLE_FACTOR,
)
from .coordinator import TankWiseConfigEntry
from .diagnostics import plan_report

RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_PART, default=PART_ALL): vol.In(RESET_PARTS),
    }
)


REPLAN_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
GET_PLAN_SCHEMA = REPLAN_SCHEMA
BOOST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Exclusive(ATTR_TEMPERATURE, "target"): vol.All(
            vol.Coerce(float), vol.Range(min=30, max=90)
        ),
        vol.Exclusive(ATTR_LITERS, "target"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=500)
        ),
        vol.Optional(ATTR_DURATION): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=480)
        ),
    }
)


DISCARD_CUTOFF_SAMPLE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Exclusive(ATTR_INDEX, "sample"): vol.Coerce(int),
            vol.Exclusive(ATTR_VALUE, "sample"): vol.Coerce(float),
        }
    ),
    cv.has_at_least_one_key(ATTR_INDEX, ATTR_VALUE),
)


def _loaded_entries(
    hass: HomeAssistant, call: ServiceCall
) -> list[TankWiseConfigEntry]:
    """Return the entries a service call applies to."""
    entries: list[TankWiseConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": entry_id},
            )
    return entries


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the TankWise services."""

    async def _reset_learning(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            await entry.runtime_data.async_reset_learning(call.data[ATTR_PART])

    hass.services.async_register(
        DOMAIN, SERVICE_RESET_LEARNING, _reset_learning, RESET_LEARNING_SCHEMA
    )

    async def _replan(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            await entry.runtime_data.async_replan("service")

    hass.services.async_register(DOMAIN, SERVICE_REPLAN, _replan, REPLAN_SCHEMA)

    async def _get_plan(call: ServiceCall) -> ServiceResponse:
        return {
            "plans": [
                {
                    "config_entry_id": entry.entry_id,
                    "name": entry.title,
                    **plan_report(entry.runtime_data),
                }
                for entry in _loaded_entries(hass, call)
            ]
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PLAN,
        _get_plan,
        GET_PLAN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def _reimport_history(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            entry.runtime_data.async_start_bootstrap()

    hass.services.async_register(
        DOMAIN, SERVICE_REIMPORT_HISTORY, _reimport_history, REPLAN_SCHEMA
    )

    async def _boost(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            await entry.runtime_data.controller.async_start_boost(
                temperature=call.data.get(ATTR_TEMPERATURE),
                liters=call.data.get(ATTR_LITERS),
                duration=call.data.get(ATTR_DURATION),
            )

    hass.services.async_register(DOMAIN, SERVICE_BOOST, _boost, BOOST_SCHEMA)

    async def _discard(
        call: ServiceCall, *, index: int | None, value: float | None
    ) -> ServiceResponse:
        results = []
        for entry in _loaded_entries(hass, call):
            coordinator = entry.runtime_data
            try:
                removed = await coordinator.async_discard_cutoff_sample(
                    index=index, value=value
                )
            except ValueError as err:
                samples = coordinator.model.cutoff_samples
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="cutoff_sample_not_found",
                    translation_placeholders={
                        "name": entry.title,
                        "samples": ", ".join(f"{sample:.1f}" for sample in samples)
                        or "-",
                    },
                ) from err
            results.append(
                {
                    "config_entry_id": entry.entry_id,
                    "name": entry.title,
                    "removed": round(removed, 2),
                    "cutoff_samples": [
                        round(sample, 2) for sample in coordinator.model.cutoff_samples
                    ],
                }
            )
        return {"results": results}

    async def _discard_last(call: ServiceCall) -> ServiceResponse:
        return await _discard(call, index=-1, value=None)

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCARD_LAST_CUTOFF_SAMPLE,
        _discard_last,
        REPLAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _discard_sample(call: ServiceCall) -> ServiceResponse:
        return await _discard(
            call, index=call.data.get(ATTR_INDEX), value=call.data.get(ATTR_VALUE)
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCARD_CUTOFF_SAMPLE,
        _discard_sample,
        DISCARD_CUTOFF_SAMPLE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _reset_usable_factor(call: ServiceCall) -> ServiceResponse:
        results = []
        for entry in _loaded_entries(hass, call):
            coordinator = entry.runtime_data
            removed = await coordinator.async_reset_usable_factor()
            results.append(
                {
                    "config_entry_id": entry.entry_id,
                    "name": entry.title,
                    "samples_removed": removed,
                    "usable_factor": round(coordinator.data.tank.usable_factor, 3)
                    if coordinator.data
                    else None,
                }
            )
        return {"results": results}

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_USABLE_FACTOR,
        _reset_usable_factor,
        REPLAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _cancel_boost(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass, call):
            await entry.runtime_data.controller.async_cancel_boost()

    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_BOOST, _cancel_boost, REPLAN_SCHEMA
    )
