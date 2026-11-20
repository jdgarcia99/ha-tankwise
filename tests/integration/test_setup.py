"""Setup, inputs, sensors, safe mode and persistence of TankWise."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tankwise.const import (
    CONF_AMBIENT_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_REGULATOR_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    DEFAULT_PARAMETERS,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.tankwise.model.water import mains_water_temperature

from .conftest import (
    AMBIENT,
    ENERGY,
    EXPORT_POWER,
    GRID_EXPORT,
    GRID_IMPORT,
    INJECTION,
    MASTER,
    OCCUPANCY,
    REGULATOR,
    SOLAR,
    TEMPERATURE,
    set_input_states,
    set_optional_states,
)

NOW = "2026-11-15 12:00:00+00:00"
CAPACITY = 0.1163
MAINS = mains_water_temperature("balearic_islands", date(2026, 11, 15))

STORED = "sensor.water_heater_stored_energy"
USEFUL = "sensor.water_heater_useful_energy"
CHARGE = "sensor.water_heater_charge"
LITERS = "sensor.water_heater_available_liters"
TANK_TEMPERATURE = "sensor.water_heater_tank_temperature"
STATUS = "sensor.water_heater_model_status"
LAST_DRAW = "sensor.water_heater_last_draw"
SHORTAGES = "sensor.water_heater_water_shortages_7d"
UA = "sensor.water_heater_heat_loss_coefficient"
HEATER_POWER = "sensor.water_heater_heater_power"
CUTOFF = "sensor.water_heater_cutoff_temperature"
CAPACITY_FACTOR = "sensor.water_heater_capacity_factor"
MAINS_SENSOR = "sensor.water_heater_mains_water_temperature"
TANK_SENSORS = (STORED, USEFUL, CHARGE, LITERS, TANK_TEMPERATURE, STATUS)
LEARNED_SENSORS = (
    LAST_DRAW,
    SHORTAGES,
    UA,
    HEATER_POWER,
    CUTOFF,
    CAPACITY_FACTOR,
    MAINS_SENSOR,
)

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")

MANIFEST = Path(__file__).parents[2] / "custom_components" / DOMAIN / "manifest.json"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _stored_tank(**overrides: Any) -> dict[str, Any]:
    tank = {
        "format": 2,
        "equivalent_temperature": 60.0,
        "ua": 0.9,
        "ua_covariance": 0.0005,
        "loss_windows": 5,
        "capacity_samples": [0.9, 0.91, 0.89],
        "heater_power_w": 1490.0,
        "heater_power_samples": 500,
        "cutoff_samples": [67.0, 68.0, 66.5],
        "uncertain": False,
        "calibration_count": 3,
        "last_calibration": "2026-11-15T08:00:00+00:00",
        "last_valid_time": "2026-11-15T11:58:00+00:00",
        "last_energy": 123.4,
        "consumptions": [],
    }
    tank.update(overrides)
    return tank


def _store(
    hass_storage: dict[str, Any], entry: MockConfigEntry, data: dict[str, Any]
) -> None:
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"bootstrap": {"status": "done"}, **data},
    }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


async def test_reads_required_inputs(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    set_input_states(
        hass, temperature="45.5", power="1450", manual="on", price="0.1234"
    )
    await _setup(hass, mock_entry)
    readings = mock_entry.runtime_data.data.readings
    assert readings.temperature == 45.5
    assert readings.power == 1450
    assert readings.energy == 123.4
    assert readings.manual_mode is True
    assert readings.heater_switch is True
    assert readings.price == 0.1234


async def test_units_are_normalised(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    set_input_states(
        hass,
        temperature="113",
        temperature_unit="°F",
        power="1.45",
        power_unit="kW",
        energy="123400",
        energy_unit="Wh",
    )
    await _setup(hass, mock_entry)
    readings = mock_entry.runtime_data.data.readings
    assert readings.temperature == pytest.approx(45.0)
    assert readings.power == pytest.approx(1450.0)
    assert readings.energy == pytest.approx(123.4)


@pytest.fixture
def all_options() -> dict[str, Any]:
    return {
        **DEFAULT_PARAMETERS,
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


async def test_reads_optional_inputs(
    hass: HomeAssistant, mock_entry: MockConfigEntry, all_options: dict[str, Any]
) -> None:
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title=mock_entry.title,
        unique_id=mock_entry.unique_id,
        version=2,
        data=dict(mock_entry.data),
        options=all_options,
    )
    set_input_states(hass)
    set_optional_states(hass)
    await _setup(hass, mock_entry)
    readings = mock_entry.runtime_data.data.readings
    assert readings.regulator == 0
    assert readings.master is True
    assert readings.occupancy is True
    assert readings.injection_price == 0.05
    assert readings.grid_import == 1500.5
    assert readings.grid_export == 800.25
    assert readings.export_power == pytest.approx(800.0)
    assert readings.solar_forecast == 6.4
    assert readings.ambient_temperature == 18.0


async def test_tolerates_unavailable_and_non_numeric_inputs(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(NOW)
    set_input_states(hass)
    await _setup(hass, mock_entry)

    set_input_states(hass, price="unavailable", manual="unknown")
    hass.states.async_set(ENERGY, "garbage", {"unit_of_measurement": "kWh"})
    await _tick(hass, freezer)
    readings = mock_entry.runtime_data.data.readings
    assert readings.price is None
    assert readings.manual_mode is None
    assert readings.energy is None
    assert "is not usable" in caplog.text
    # Without the energy meter the model still runs (power is integrated).
    assert hass.states.get(STORED).state != STATE_UNAVAILABLE

    set_input_states(hass)
    await _tick(hass, freezer)
    assert "is usable again" in caplog.text


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


async def test_sensors_from_first_reading(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Without stored state the model starts from the probe, uncertain."""
    freezer.move_to(NOW)
    set_input_states(hass, temperature="60.0")
    await _setup(hass, mock_entry)

    assert float(hass.states.get(STORED).state) == pytest.approx(
        CAPACITY * (60 - MAINS)
    )
    assert float(hass.states.get(USEFUL).state) == pytest.approx(CAPACITY * (60 - 38))
    assert float(hass.states.get(CHARGE).state) == pytest.approx(100 * 22 / 30)
    liters = hass.states.get(LITERS)
    assert float(liters.state) == pytest.approx(100 * (60 - MAINS) / (40 - MAINS))
    assert liters.attributes["unit_of_measurement"] == "L"
    assert liters.attributes["shower_temperature"] == 40.0
    assert float(hass.states.get(TANK_TEMPERATURE).state) == 60.0
    assert hass.states.get(STATUS).state == "uncertain"
    assert hass.states.get(LAST_DRAW).state == "unknown"
    assert hass.states.get(SHORTAGES).state == "0"
    assert float(hass.states.get(UA).state) == 1.0
    assert hass.states.get(UA).attributes["unit_of_measurement"] == "W/K"
    assert float(hass.states.get(HEATER_POWER).state) == 1500
    assert float(hass.states.get(CUTOFF).state) == 68.0
    assert float(hass.states.get(CAPACITY_FACTOR).state) == 0.9
    mains = hass.states.get(MAINS_SENSOR)
    assert float(mains.state) == pytest.approx(MAINS)
    assert mains.attributes["source"] == "table"


async def test_safe_mode(
    hass: HomeAssistant, mock_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Without probe the tank sensors are unavailable; learned ones are not."""
    freezer.move_to(NOW)
    set_input_states(hass, temperature="50.0")
    await _setup(hass, mock_entry)
    before = hass.states.get(USEFUL).state

    hass.states.async_set(TEMPERATURE, "unavailable", {"unit_of_measurement": "°C"})
    await _tick(hass, freezer)
    for entity_id in TANK_SENSORS:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
    for entity_id in LEARNED_SENSORS:
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE, entity_id

    set_input_states(hass, temperature="50.0")
    await _tick(hass, freezer)
    assert float(hass.states.get(USEFUL).state) == pytest.approx(
        float(before), abs=0.01
    )
    assert mock_entry.runtime_data.model.consumptions == []


async def test_last_draw_and_shortages(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    draws = [
        {
            "start": "2026-11-10T17:00:00+00:00",  # 5 days ago
            "end": "2026-11-10T17:12:00+00:00",
            "energy_kwh": 1.8,
            "start_temperature": 55.0,
            "min_temperature": 35.0,
            "while_heating": False,
            "shortage": True,
        },
        {
            "start": "2026-11-01T17:00:00+00:00",  # 14 days ago
            "end": "2026-11-01T17:12:00+00:00",
            "energy_kwh": 1.8,
            "start_temperature": 55.0,
            "min_temperature": 30.0,
            "while_heating": False,
            "shortage": True,
        },
        {
            "start": "2026-11-15T06:30:00+00:00",
            "end": "2026-11-15T06:42:00+00:00",
            "energy_kwh": 1.2,
            "start_temperature": 60.0,
            "min_temperature": 48.0,
            "while_heating": True,
            "shortage": False,
        },
    ]
    _store(hass_storage, mock_entry, {"tank": _stored_tank(consumptions=draws)})
    set_input_states(hass, temperature="60.0")
    await _setup(hass, mock_entry)

    assert hass.states.get(SHORTAGES).state == "1"
    last = hass.states.get(LAST_DRAW)
    assert dt_util.parse_datetime(last.state) == dt_util.parse_datetime(
        "2026-11-15T06:30:00+00:00"
    )
    assert last.attributes["energy_kwh"] == 1.2
    assert last.attributes["min_temperature"] == 48.0
    assert last.attributes["shortage"] is False
    assert last.attributes["while_heating"] is True


async def test_entity_ids_are_english_in_any_language(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    hass.config.language = "es"
    set_input_states(hass)
    await _setup(hass, mock_entry)

    for entity_id in (*TANK_SENSORS, *LEARNED_SENSORS):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(USEFUL).name == "Water heater Energía útil"
    assert hass.states.get(LITERS).name == "Water heater Agua caliente disponible"


async def test_native_values_are_not_rounded(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    set_input_states(hass, temperature="45.5")
    await _setup(hass, mock_entry)
    value = float(hass.states.get(USEFUL).state)
    assert value == pytest.approx(CAPACITY * (45.5 - 38), rel=1e-12)
    options = entity_registry.async_get(USEFUL).options
    assert options["sensor"]["suggested_display_precision"] == 2


async def test_device_info(
    hass: HomeAssistant, mock_entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    set_input_states(hass)
    await _setup(hass, mock_entry)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, mock_entry.entry_id), mock_entry.entry_id
    )
    assert device is not None
    assert device.name == "Water heater"
    assert device.manufacturer == "jdgarcia99"
    assert device.model == "TankWise"
    assert device.sw_version == json.loads(MANIFEST.read_text())["version"]
    assert device.entry_type is dr.DeviceEntryType.SERVICE


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_restores_state_after_short_restart(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    _store(hass_storage, mock_entry, {"tank": _stored_tank()})
    set_input_states(hass, temperature="60.0")
    await _setup(hass, mock_entry)

    assert hass.states.get(STATUS).state == "calibrated"
    assert float(hass.states.get(UA).state) == 0.9
    assert float(hass.states.get(HEATER_POWER).state) == 1490.0
    assert float(hass.states.get(CUTOFF).state) == 67.0
    assert hass.states.get(CUTOFF).attributes["samples"] == [67.0, 68.0, 66.5]
    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 3


async def test_long_restart_makes_state_uncertain(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    stored_time = dt_util.utcnow() - timedelta(hours=6)
    _store(
        hass_storage,
        mock_entry,
        {
            "tank": _stored_tank(
                equivalent_temperature=40.0,
                last_valid_time=stored_time.isoformat(),
                last_energy=122.4,
            )
        },
    )
    set_input_states(hass, temperature="48.0", energy="123.4")
    await _setup(hass, mock_entry)
    assert hass.states.get(STATUS).state == "uncertain"
    equivalent = mock_entry.runtime_data.data.tank.equivalent_temperature
    assert 45.0 < equivalent < 50.0


async def test_corrupt_storage_is_discarded(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _store(
        hass_storage,
        mock_entry,
        {"tank": {"format": 99}, "bootstrap": {"status": "bogus"}},
    )
    set_input_states(hass)
    await _setup(hass, mock_entry)
    assert "Discarding stored tank state" in caplog.text
    assert "Discarding stored bootstrap status" in caplog.text
    assert hass.states.get(STATUS).state == "uncertain"


async def test_unload_saves_and_remove_deletes_storage(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    set_input_states(hass)
    await _setup(hass, mock_entry)
    key = f"{DOMAIN}.{mock_entry.entry_id}"

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    data = hass_storage[key]["data"]
    assert data["tank"]["equivalent_temperature"] == 45.5
    assert data["settings"]["shower_temperature"] == 40.0
    assert data["bootstrap"]["status"] == "no_recorder"

    assert await hass.config_entries.async_remove(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
