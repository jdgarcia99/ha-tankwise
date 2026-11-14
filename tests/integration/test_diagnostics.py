"""Tests for TankWise diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .conftest import set_input_states


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_entry: MockConfigEntry,
) -> None:
    """Diagnostics expose the loss coefficient, the model and the readings."""
    set_input_states(hass, power="0")
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(hass, hass_client, mock_entry)

    assert result["losses"]["rate_c_per_hour_at_46c"] == pytest.approx(0.25)
    assert result["losses"]["power_w_now"] > 0
    assert result["tank"]["status"] == "uncertain"
    assert result["readings"]["temperature"] == 45.5
    assert result["model"]["format"] == 1
    assert result["parameters"]["min_useful_temperature"] == 32.0
