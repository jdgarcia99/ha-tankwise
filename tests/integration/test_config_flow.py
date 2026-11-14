"""Tests for the TankWise config and options flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.const import (
    CONF_BOTTOM_TEMPERATURE_SENSOR,
    CONF_COMFORT_LEVEL,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_ENERGY_SENSOR,
    CONF_MAINS_WATER_TEMPERATURE,
    CONF_MIN_USEFUL_TEMPERATURE,
    CONF_POWER_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_SURPLUS_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    DEFAULT_PARAMETERS,
    DOMAIN,
)

from .conftest import (
    BOTTOM,
    ENERGY,
    PRESENCE,
    PRICE,
    REQUIRED_INPUT,
    SURPLUS,
    TEMPERATURE,
    set_input_states,
    set_optional_states,
)


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Avoid setting up the integration when a flow creates an entry."""
    with patch(
        "custom_components.tankwise.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def _start_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    return result["flow_id"]


async def test_full_flow(hass: HomeAssistant, mock_setup_entry: AsyncMock) -> None:
    """Selecting valid sensors creates an entry with default parameters."""
    set_input_states(hass)
    set_optional_states(hass)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "optional"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SURPLUS_SENSOR: SURPLUS,
            CONF_BOTTOM_TEMPERATURE_SENSOR: BOTTOM,
            CONF_PRESENCE_ENTITY: PRESENCE,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Water heater"
    assert result["data"] == REQUIRED_INPUT
    assert result["options"] == {
        **DEFAULT_PARAMETERS,
        CONF_SURPLUS_SENSOR: SURPLUS,
        CONF_BOTTOM_TEMPERATURE_SENSOR: BOTTOM,
        CONF_PRESENCE_ENTITY: PRESENCE,
    }
    assert result["result"].unique_id == TEMPERATURE
    assert len(mock_setup_entry.mock_calls) == 1


async def test_flow_without_optional_sensors(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """All optional sensors can be skipped."""
    set_input_states(hass)
    flow_id = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == DEFAULT_PARAMETERS


@pytest.mark.parametrize(
    ("states", "field", "error"),
    [
        ({"temperature_unit": "W"}, CONF_TEMPERATURE_SENSOR, "invalid_unit"),
        ({"power_unit": "kWh"}, CONF_POWER_SENSOR, "invalid_unit"),
        ({"energy_unit": "W"}, CONF_ENERGY_SENSOR, "invalid_unit"),
        (
            {"energy_state_class": "measurement"},
            CONF_ENERGY_SENSOR,
            "invalid_state_class",
        ),
        ({"energy_state_class": None}, CONF_ENERGY_SENSOR, "invalid_state_class"),
    ],
)
async def test_invalid_required_sensor(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    states: dict[str, str | None],
    field: str,
    error: str,
) -> None:
    """Sensors with a wrong unit or state class are rejected, then recover."""
    set_input_states(hass, **states)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {field: error}

    set_input_states(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "optional"


async def test_alternative_units_accepted(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Convertible units (°F, kW, Wh) and the 'total' state class are valid."""
    set_input_states(
        hass,
        temperature_unit="°F",
        power_unit="kW",
        energy_unit="Wh",
        energy_state_class="total",
    )
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["step_id"] == "optional"


async def test_missing_entity(hass: HomeAssistant, mock_setup_entry: AsyncMock) -> None:
    """An entity without state is reported as not found."""
    set_input_states(hass)
    hass.states.async_remove(ENERGY)
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["errors"] == {CONF_ENERGY_SENSOR: "entity_not_found"}


async def test_invalid_optional_sensor(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Optional sensors that are converted must have a valid unit."""
    set_input_states(hass)
    hass.states.async_set(SURPLUS, "1", {"unit_of_measurement": "kWh"})
    flow_id = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_SURPLUS_SENSOR: SURPLUS}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "optional"
    assert result["errors"] == {CONF_SURPLUS_SENSOR: "invalid_unit"}


async def test_already_configured(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The same tank temperature sensor cannot be configured twice."""
    mock_entry.add_to_hass(hass)
    set_input_states(hass)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """The options flow updates parameters and replaces the optional sensors."""
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry, options={**DEFAULT_PARAMETERS, CONF_PRICE_SENSOR: PRICE}
    )
    set_input_states(hass)
    set_optional_states(hass)

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VOLUME: 80.0,
            CONF_RATED_POWER: 2000.0,
            CONF_WATER_REGION: "mainland",
            CONF_MAINS_WATER_TEMPERATURE: 14.5,
            CONF_MIN_USEFUL_TEMPERATURE: 40.0,
            CONF_CUTOFF_POWER_THRESHOLD: 30.0,
            CONF_COMFORT_LEVEL: "p95",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors"

    # Price sensor is removed, presence sensor is added.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRESENCE_ENTITY: PRESENCE}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {
        CONF_VOLUME: 80,
        CONF_RATED_POWER: 2000,
        CONF_WATER_REGION: "mainland",
        CONF_MAINS_WATER_TEMPERATURE: 14.5,
        CONF_MIN_USEFUL_TEMPERATURE: 40.0,
        CONF_CUTOFF_POWER_THRESHOLD: 30,
        CONF_COMFORT_LEVEL: "p95",
        CONF_PRESENCE_ENTITY: PRESENCE,
    }
    assert isinstance(mock_entry.options[CONF_VOLUME], int)


async def test_options_flow_invalid_optional_sensor(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The options flow validates optional sensor units too."""
    mock_entry.add_to_hass(hass)
    hass.states.async_set(BOTTOM, "30", {"unit_of_measurement": "W"})

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(DEFAULT_PARAMETERS)
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BOTTOM_TEMPERATURE_SENSOR: BOTTOM}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BOTTOM_TEMPERATURE_SENSOR: "invalid_unit"}
