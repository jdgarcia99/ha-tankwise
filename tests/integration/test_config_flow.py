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
    CONF_AMBIENT_SENSOR,
    CONF_AMBIENT_TEMPERATURE,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MANUAL_MODE_SWITCH,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_REGULATOR_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    DEFAULT_PARAMETERS,
    DOMAIN,
)

from .conftest import (
    AMBIENT,
    ENERGY,
    EXPORT_POWER,
    GRID_EXPORT,
    GRID_IMPORT,
    HEATER_SWITCH,
    INJECTION,
    MANUAL,
    MASTER,
    OCCUPANCY,
    POWER,
    PRICE,
    REGULATOR,
    REQUIRED_INPUT,
    SOLAR,
    TEMPERATURE,
    set_input_states,
    set_optional_states,
)

ALL_OPTIONAL = {
    CONF_ENERGY_SENSOR: ENERGY,
    CONF_REGULATOR_SENSOR: REGULATOR,
    CONF_MASTER_ENTITY: MASTER,
    CONF_OCCUPANCY_ENTITY: OCCUPANCY,
    CONF_INJECTION_PRICE_SENSOR: INJECTION,
    CONF_GRID_IMPORT_SENSOR: GRID_IMPORT,
    CONF_GRID_EXPORT_SENSOR: GRID_EXPORT,
    CONF_EXPORT_POWER_SENSOR: EXPORT_POWER,
    CONF_SOLAR_FORECAST_SENSOR: SOLAR,
    CONF_AMBIENT_SENSOR: AMBIENT,
}


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
    """Valid inputs create a version 2 entry with default parameters."""
    set_input_states(hass)
    set_optional_states(hass)
    flow_id = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "inputs"

    result = await hass.config_entries.flow.async_configure(flow_id, ALL_OPTIONAL)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Water heater"
    assert result["data"] == REQUIRED_INPUT
    assert result["options"] == {**DEFAULT_PARAMETERS, **ALL_OPTIONAL}
    assert result["result"].unique_id == TEMPERATURE
    assert result["result"].version == 2
    assert len(mock_setup_entry.mock_calls) == 1


async def test_flow_without_optional_inputs(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """All optional inputs (even the energy sensor) can be skipped."""
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
        ({"price": "cheap"}, CONF_PRICE_SENSOR, "not_numeric"),
    ],
)
async def test_invalid_required_input(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    states: dict[str, str],
    field: str,
    error: str,
) -> None:
    """Inputs with a wrong unit or state are rejected, then recover."""
    set_input_states(hass, **states)
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}

    set_input_states(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["step_id"] == "inputs"


async def test_unavailable_price_is_accepted(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """The price sensor may be momentarily unavailable during setup."""
    set_input_states(hass, price="unavailable")
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["step_id"] == "inputs"


@pytest.mark.parametrize("missing", [TEMPERATURE, POWER, HEATER_SWITCH, MANUAL, PRICE])
async def test_missing_required_entity(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, missing: str
) -> None:
    set_input_states(hass)
    hass.states.async_remove(missing)
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    field = next(key for key, value in REQUIRED_INPUT.items() if value == missing)
    assert result["errors"] == {field: "entity_not_found"}


async def test_alternative_units_accepted(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    set_input_states(hass, temperature_unit="°F", power_unit="kW")
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["step_id"] == "inputs"


@pytest.mark.parametrize(
    ("field", "entity_id", "state", "attributes", "error"),
    [
        (CONF_ENERGY_SENSOR, ENERGY, "1", {"unit_of_measurement": "W"}, "invalid_unit"),
        (
            CONF_ENERGY_SENSOR,
            ENERGY,
            "1",
            {"unit_of_measurement": "kWh", "state_class": "measurement"},
            "invalid_state_class",
        ),
        (
            CONF_GRID_IMPORT_SENSOR,
            GRID_IMPORT,
            "1",
            {"unit_of_measurement": "kWh"},
            "invalid_state_class",
        ),
        (
            CONF_EXPORT_POWER_SENSOR,
            EXPORT_POWER,
            "1",
            {"unit_of_measurement": "kWh"},
            "invalid_unit",
        ),
        (
            CONF_AMBIENT_SENSOR,
            AMBIENT,
            "18",
            {"unit_of_measurement": "%"},
            "invalid_unit",
        ),
        (
            CONF_REGULATOR_SENSOR,
            REGULATOR,
            "50",
            {"unit_of_measurement": "W"},
            "invalid_unit",
        ),
        (CONF_INJECTION_PRICE_SENSOR, INJECTION, "free", {}, "not_numeric"),
        (CONF_OCCUPANCY_ENTITY, "input_boolean.nowhere", None, {}, "entity_not_found"),
    ],
)
async def test_invalid_optional_input(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    field: str,
    entity_id: str,
    state: str | None,
    attributes: dict[str, str],
    error: str,
) -> None:
    set_input_states(hass)
    set_optional_states(hass)
    if state is not None:
        hass.states.async_set(entity_id, state, attributes)
    flow_id = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    result = await hass.config_entries.flow.async_configure(flow_id, {field: entity_id})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}


async def test_unitless_regulator_is_accepted(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    set_input_states(hass)
    hass.states.async_set(REGULATOR, "50")
    flow_id = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REGULATOR_SENSOR: REGULATOR}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_already_configured(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    set_input_states(hass)
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, REQUIRED_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_entry: MockConfigEntry
) -> None:
    """Parameters first, then the optional inputs; integers stay integers."""
    mock_entry.add_to_hass(hass)
    set_input_states(hass)
    set_optional_states(hass)

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["step_id"] == "init"
    parameters = {
        CONF_VOLUME: 150.0,
        CONF_RATED_POWER: 2000.0,
        CONF_WATER_REGION: "mainland",
        CONF_AMBIENT_TEMPERATURE: 15.0,
        CONF_CUTOFF_TEMPERATURE: 65.0,
        CONF_CUTOFF_POWER_THRESHOLD: 40.0,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], parameters
    )
    assert result["step_id"] == "inputs"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AMBIENT_SENSOR: AMBIENT, CONF_ENERGY_SENSOR: ENERGY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {
        **parameters,
        CONF_VOLUME: 150,
        CONF_RATED_POWER: 2000,
        CONF_CUTOFF_POWER_THRESHOLD: 40,
        CONF_AMBIENT_SENSOR: AMBIENT,
        CONF_ENERGY_SENSOR: ENERGY,
    }
    assert isinstance(mock_entry.options[CONF_VOLUME], int)


async def test_options_flow_invalid_input(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    set_input_states(hass)
    hass.states.async_set(AMBIENT, "18", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(DEFAULT_PARAMETERS)
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AMBIENT_SENSOR: AMBIENT}
    )
    assert result["errors"] == {CONF_AMBIENT_SENSOR: "invalid_unit"}


async def test_reconfigure(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_entry: MockConfigEntry
) -> None:
    """The required inputs can be changed; the unique ID follows the probe."""
    mock_entry.add_to_hass(hass)
    set_input_states(hass)
    hass.states.async_set("switch.other_manual", "off")
    hass.states.async_set("sensor.other_probe", "40", {"unit_of_measurement": "°C"})

    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    new_input = {
        **{key: value for key, value in REQUIRED_INPUT.items() if key != "name"},
        CONF_TEMPERATURE_SENSOR: "sensor.other_probe",
        CONF_MANUAL_MODE_SWITCH: "switch.other_manual",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data[CONF_TEMPERATURE_SENSOR] == "sensor.other_probe"
    assert mock_entry.data[CONF_MANUAL_MODE_SWITCH] == "switch.other_manual"
    assert mock_entry.data["name"] == "Water heater"
    assert mock_entry.unique_id == "sensor.other_probe"


async def test_reconfigure_completes_missing_inputs(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """An entry migrated from 0.2 gets its new required inputs here."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        unique_id=TEMPERATURE,
        version=2,
        data={
            "name": "Water heater",
            CONF_TEMPERATURE_SENSOR: TEMPERATURE,
            CONF_POWER_SENSOR: POWER,
        },
        options=dict(DEFAULT_PARAMETERS),
    )
    entry.add_to_hass(hass)
    set_input_states(hass)
    result = await entry.start_reconfigure_flow(hass)
    new_input = {key: value for key, value in REQUIRED_INPUT.items() if key != "name"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HEATER_SWITCH] == HEATER_SWITCH


async def test_reconfigure_invalid_and_duplicate(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN, unique_id="sensor.other_probe", version=2)
    other.add_to_hass(hass)
    set_input_states(hass)
    new_input = {key: value for key, value in REQUIRED_INPUT.items() if key != "name"}

    result = await mock_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**new_input, CONF_POWER_SENSOR: TEMPERATURE}
    )
    assert result["errors"] == {CONF_POWER_SENSOR: "invalid_unit"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**new_input, CONF_TEMPERATURE_SENSOR: "sensor.other_probe"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
