"""A stratified tank in Home Assistant: useful floor, outlet temperature, plan."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import CONF_HOT_WATER_VOLUME_SENSOR
from custom_components.tankwise.model import planning

from .conftest import set_input_states

METER = "sensor.hot_water_meter_total"
OUTLET = "sensor.tankwise_outlet_temperature"
AVAILABLE = "sensor.tankwise_available_liters"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config.async_set_time_zone("Europe/Madrid")
    set_input_states(hass, temperature="56.9")
    hass.states.async_set(METER, "1000", {"unit_of_measurement": "L"})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_HOT_WATER_VOLUME_SENSOR: METER}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_stratified_tank_in_home_assistant(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    assert hass.states.get(AVAILABLE).attributes["stratified"] is False

    # Learned: 15 metered draws where the probe fell half of a mixed tank.
    model = coordinator.model
    model._stratification = [(66.0, 0.5)] * 15
    model._equivalent = 44.3
    model._stratification_offset = 12.6
    with (
        patch.object(
            planning, "build_plan_input", wraps=planning.build_plan_input
        ) as build,
        patch("custom_components.tankwise.planner.build_plan_input", build),
    ):
        await coordinator.async_refresh()
        await coordinator.async_replan("test")
        await hass.async_block_till_done()

    mains = coordinator.data.tank.mains_temperature
    available = hass.states.get(AVAILABLE)
    assert available.attributes["stratified"] is True
    assert available.attributes["useful_floor_temperature"] == round(mains, 1)
    # The plan counts the energy down to the mains water.
    assert build.called
    assert build.call_args.kwargs["comfort_temperature"] == pytest.approx(mains)

    outlet = hass.states.get(OUTLET)
    tank = coordinator.data.tank
    assert tank.equivalent_temperature is not None
    assert float(outlet.state) == pytest.approx(
        tank.equivalent_temperature + model._stratification_offset, abs=0.01
    )
    assert float(outlet.state) > tank.equivalent_temperature + 10
    assert outlet.attributes["stratified"] is True
