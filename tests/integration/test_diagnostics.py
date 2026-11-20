"""Diagnostics and translation files."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tankwise.diagnostics import async_get_config_entry_diagnostics

from .conftest import set_input_states

COMPONENT = Path(__file__).parents[2] / "custom_components" / "tankwise"


async def test_diagnostics(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    set_input_states(hass)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)
    json.dumps(diagnostics, default=str)  # serialisable
    assert (
        diagnostics["entry"]["data"]["temperature_sensor"] == "sensor.tank_temperature"
    )
    assert diagnostics["parameters"]["volume_l"] == 100
    assert diagnostics["settings"]["shower_temperature"] == 40.0
    assert diagnostics["readings"]["temperature"] == 45.5
    assert diagnostics["learned"]["ua_w_per_k"] == 1.0
    assert diagnostics["learned"]["capacity_factor"] == 0.9
    assert diagnostics["learned"]["loss_power_w_now"] == 25.5
    assert diagnostics["bootstrap"]["status"] == "no_recorder"
    assert diagnostics["consumptions"] == []
    assert "consumptions" not in diagnostics["model"]
    assert diagnostics["demand"]["status"] == "learning"
    assert diagnostics["demand"]["params"] == {
        "mode": "workday_weekend",
        "observations": 16,
    }
    assert diagnostics["appointments"] == []


def _keys(data: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}/{key}"
        keys |= _keys(value, path) if isinstance(value, dict) else {path}
    return keys


def test_translations_have_the_same_keys() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    spanish = json.loads((COMPONENT / "translations" / "es.json").read_text())
    assert strings == english
    assert _keys(english) == _keys(spanish)


def test_every_entity_has_a_translation_and_an_icon() -> None:
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    icons = json.loads((COMPONENT / "icons.json").read_text())
    from custom_components.tankwise.number import NUMBERS
    from custom_components.tankwise.sensor import SENSORS

    for platform, descriptions in (("sensor", SENSORS), ("number", NUMBERS)):
        for description in descriptions:
            key = description.translation_key
            assert key in english["entity"][platform], key
            assert key in icons["entity"][platform], key
