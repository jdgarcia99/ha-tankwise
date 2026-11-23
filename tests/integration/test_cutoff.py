"""Thermostat cut-off detection between samples, through state listeners."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import voluptuous as vol

from custom_components.tankwise.const import (
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_REGULATOR_MODE_ENTITY,
    CONF_REGULATOR_RESTART_REASON_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_REGULATOR_UPTIME_SENSOR,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_PARAMETERS,
    DOMAIN,
    UPDATE_INTERVAL,
)

from .conftest import (
    ENERGY,
    MANUAL,
    POWER,
    REGULATOR,
    REQUIRED_INPUT,
    TEMPERATURE,
    set_input_states,
)

START = "2026-11-15 21:58:30+00:00"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options with the energy sensor, the PWM output and a 47 °C cut-off."""
    return {
        **DEFAULT_PARAMETERS,
        CONF_ENERGY_SENSOR: ENERGY,
        CONF_REGULATOR_SENSOR: REGULATOR,
        CONF_CUTOFF_TEMPERATURE: 47.0,
    }


class Heater:
    """Drive the input entities like the real installation."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
        self.hass = hass
        self.freezer = freezer
        self.energy = 500.0
        self.power = 1441.0

    def set_power(self, watts: float) -> None:
        self.power = watts
        self.hass.states.async_set(POWER, f"{watts}", {"unit_of_measurement": "W"})

    def set_manual(self, on: bool) -> None:
        self.hass.states.async_set(MANUAL, "on" if on else "off")
        self.hass.states.async_set(
            REGULATOR, "100" if on else "0", {"unit_of_measurement": "%"}
        )

    async def wait(self, seconds: float) -> None:
        """Advance time; the coordinator polls when its interval is due."""
        self.freezer.tick(timedelta(seconds=seconds))
        self.energy += self.power / 1000 * seconds / 3600
        self.hass.states.async_set(
            ENERGY,
            f"{self.energy:.4f}",
            {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
        )
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()


async def _heating(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry
) -> Heater:
    freezer.move_to(START)
    set_input_states(
        hass, temperature="46.5", power="1441", energy="500.0", manual="on"
    )
    hass.states.async_set(REGULATOR, "100", {"unit_of_measurement": "%"})
    hass.states.async_set(TEMPERATURE, "46.5", {"unit_of_measurement": "°C"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    heater = Heater(hass, freezer)
    for _ in range(10):
        await heater.wait(UPDATE_INTERVAL.total_seconds())
    return heater


async def test_cutoff_then_manual_mode_switched_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """15/11 23:08:57: cut-off in manual mode, switched off three minutes later."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(27)
    heater.set_power(1.8)  # thermostat cut-off between two samples
    await heater.wait(181)
    heater.set_manual(False)  # an automation switches the manual mode off
    for _ in range(5):
        await heater.wait(60)

    data = mock_entry.runtime_data.data
    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 1
    assert data.tank.cutoff_samples[-1] == pytest.approx(46.5, abs=0.2)


async def test_manual_mode_switched_off_first_is_not_full(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_entry: MockConfigEntry
) -> None:
    """Manual mode off at :03, power falls at :04: not a full tank."""
    heater = await _heating(hass, freezer, mock_entry)
    await heater.wait(3)
    heater.set_manual(False)
    await heater.wait(1)
    heater.set_power(0.5)
    for _ in range(40):
        await heater.wait(60)

    assert mock_entry.runtime_data.model.as_dict()["calibration_count"] == 0


MODE = "select.diverter_mode"
UPTIME = "sensor.diverter_uptime"
RESTART = "sensor.diverter_restart_reason"


@pytest.fixture
def health_options(entry_options: dict[str, Any]) -> dict[str, Any]:
    """Options with the regulator health entities of the x10 diverter."""
    return {
        **entry_options,
        CONF_REGULATOR_MODE_ENTITY: MODE,
        CONF_REGULATOR_UPTIME_SENSOR: UPTIME,
        CONF_REGULATOR_RESTART_REASON_SENSOR: RESTART,
    }


def _health_entry(options: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Water heater",
        unique_id=TEMPERATURE,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data=dict(REQUIRED_INPUT),
        options=options,
    )


def _diverter_up(hass: HomeAssistant, uptime: float) -> None:
    hass.states.async_set(MODE, "Auto")
    hass.states.async_set(UPTIME, f"{uptime}", {"unit_of_measurement": "s"})
    hass.states.async_set(RESTART, "Reboot request from esphome.ota")


def _diverter_unavailable(hass: HomeAssistant) -> None:
    for entity_id in (MODE, UPTIME, RESTART, REGULATOR, MANUAL):
        hass.states.async_set(entity_id, "unavailable")


async def test_power_cut_of_the_diverter_is_not_a_cutoff(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, health_options: dict[str, Any]
) -> None:
    """23/11 11:47:56: the diverter loses its supply; unavailable 1:55 later."""
    entry = _health_entry(health_options)
    _diverter_up(hass, 11_000)
    heater = await _heating(hass, freezer, entry)
    await heater.wait(27)
    heater.set_power(0.0)  # the diverter's power supply fails
    await heater.wait(115)
    _diverter_unavailable(hass)  # its entities are reported unavailable
    for _ in range(10):
        await heater.wait(60)

    assert entry.runtime_data.model.as_dict()["calibration_count"] == 0
    assert entry.runtime_data.model.cutoff_samples == ()


async def test_cutoff_with_the_diverter_running_is_a_cutoff(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, health_options: dict[str, Any]
) -> None:
    entry = _health_entry(health_options)
    _diverter_up(hass, 11_000)
    heater = await _heating(hass, freezer, entry)
    await heater.wait(27)
    heater.set_power(1.8)  # thermostat cut-off
    for minute in range(6):
        hass.states.async_set(
            UPTIME, f"{11_700 + 60 * minute}", {"unit_of_measurement": "s"}
        )
        await heater.wait(60)

    assert entry.runtime_data.model.as_dict()["calibration_count"] == 1
    assert entry.runtime_data.model.cutoff_samples == (pytest.approx(46.5, abs=0.2),)


# ---------------------------------------------------------------------------
# Services that discard cut-off samples
# ---------------------------------------------------------------------------

REAL_SAMPLES = [
    69.10578359006531,
    68.51358522993043,
    60.91136935455522,
    67.93412395900349,
]


async def _with_samples(
    hass: HomeAssistant, entry: MockConfigEntry, samples: list[float]
) -> None:
    set_input_states(hass, temperature="66.0")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.model._cutoff_samples = list(samples)
    await entry.runtime_data.async_refresh()


def _cutoff_state(hass: HomeAssistant) -> float:
    return float(hass.states.get("sensor.tankwise_cutoff_temperature").state)


async def test_discard_the_60_9_cutoff_sample(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    entity_registry_enabled_by_default: None,
    hass_storage: dict[str, Any],
) -> None:
    await _with_samples(hass, mock_entry, REAL_SAMPLES)
    assert _cutoff_state(hass) == pytest.approx(68.22, abs=0.01)

    response = await hass.services.async_call(
        DOMAIN,
        "discard_cutoff_sample",
        {"value": 60.9},
        blocking=True,
        return_response=True,
    )
    kept = [REAL_SAMPLES[0], REAL_SAMPLES[1], REAL_SAMPLES[3]]
    assert response["results"][0]["removed"] == pytest.approx(60.91)
    assert response["results"][0]["cutoff_samples"] == [round(s, 2) for s in kept]
    assert mock_entry.runtime_data.model.cutoff_samples == tuple(kept)
    assert _cutoff_state(hass) == pytest.approx(68.51, abs=0.01)
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]
    assert stored["tank"]["cutoff_samples"] == kept


async def test_discard_by_index_and_the_last_sample(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    entity_registry_enabled_by_default: None,
) -> None:
    await _with_samples(hass, mock_entry, REAL_SAMPLES)
    await hass.services.async_call(
        DOMAIN, "discard_cutoff_sample", {"index": 2}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "discard_last_cutoff_sample", {}, blocking=True
    )
    assert mock_entry.runtime_data.model.cutoff_samples == tuple(REAL_SAMPLES[:2])
    # Fewer than three samples: back to the configured cut-off.
    assert _cutoff_state(hass) == pytest.approx(
        mock_entry.options[CONF_CUTOFF_TEMPERATURE]
    )


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("discard_cutoff_sample", {"value": 62.0}),
        ("discard_cutoff_sample", {"index": 7}),
        ("discard_last_cutoff_sample", {}),
    ],
)
async def test_discard_nothing_matches(
    hass: HomeAssistant, mock_entry: MockConfigEntry, service: str, data: dict
) -> None:
    samples = [] if service == "discard_last_cutoff_sample" else REAL_SAMPLES
    await _with_samples(hass, mock_entry, samples)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert mock_entry.runtime_data.model.cutoff_samples == tuple(samples)


async def test_discard_needs_a_value_or_an_index(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    await _with_samples(hass, mock_entry, REAL_SAMPLES)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "discard_cutoff_sample", {}, blocking=True
        )
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "discard_cutoff_sample", {"index": 1, "value": 60.9}, blocking=True
        )
