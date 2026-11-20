"""Diagnostics for TankWise."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_AMBIENT_TEMPERATURE
from .coordinator import TankWiseConfigEntry

DIAGNOSTIC_CONSUMPTIONS = 50


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TankWiseConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    tank = data.tank
    params = coordinator.tank_params
    ambient = data.readings.ambient_temperature
    if ambient is None:
        ambient = float(coordinator.parameters[CONF_AMBIENT_TEMPERATURE])
    model = coordinator.model.as_dict()
    consumptions = model.pop("consumptions")
    tank_state = asdict(tank)
    tank_state.pop("last_consumption")
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "parameters": asdict(params),
        "settings": dict(coordinator.settings),
        "readings": asdict(data.readings),
        "tank": tank_state,
        "learned": {
            "ua_w_per_k": tank.ua_w_per_k,
            "loss_power_w_now": tank.loss_power_w(ambient),
            "loss_windows": tank.loss_windows,
            "capacity_factor": tank.capacity_factor,
            "capacity_samples": tank.capacity_samples,
            "heater_power_w": tank.heater_power_w,
            "cutoff_temperature": tank.cutoff_temperature,
            "cutoff_samples": list(tank.cutoff_samples),
            "mains_source": data.mains_source,
        },
        "bootstrap": coordinator.bootstrap.as_dict(),
        "model": model,
        "consumptions": consumptions[-DIAGNOSTIC_CONSUMPTIONS:],
        "shortages_7d": data.shortages_7d,
    }
