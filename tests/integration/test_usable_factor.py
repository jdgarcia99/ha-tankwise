"""Resetting the useful litres factor, and its default with the probe in the sheath."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_PROBE_POSITION,
    DOMAIN,
    PROBE_SHEATH,
    PROBE_TOP,
)

from .conftest import set_input_states

LITERS = "sensor.tankwise_available_liters"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, position: str) -> None:
    set_input_states(hass, temperature="60.0")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PROBE_POSITION: position}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.model._usable_samples = [0.89] * 10
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("position", "default"), [(PROBE_SHEATH, 0.85), (PROBE_TOP, 1.0)]
)
async def test_reset_the_useful_litres_factor(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    position: str,
    default: float,
) -> None:
    await _setup(hass, mock_entry, position)
    assert hass.states.get(LITERS).attributes["usable_factor"] == 0.89

    response = await hass.services.async_call(
        DOMAIN, "reset_usable_factor", {}, blocking=True, return_response=True
    )
    [result] = response["results"]
    assert result["samples_removed"] == 10
    assert result["usable_factor"] == default
    await hass.async_block_till_done()
    assert hass.states.get(LITERS).attributes["usable_factor"] == default
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]
    assert stored["tank"]["usable_samples"] == []
