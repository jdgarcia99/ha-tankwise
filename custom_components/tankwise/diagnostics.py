"""Diagnostics for TankWise."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import TankWiseConfigEntry
from .model.demand import DayType
from .model.tank import AMBIENT_TEMPERATURE, WATER_HEAT_CAPACITY_KWH_PER_L_K

RECENT_ITEMS = 20


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TankWiseConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    tank = data.tank
    params = coordinator.tank_params
    loss_power_w = None
    if tank.equivalent_temperature is not None:
        loss_power_w = (
            tank.loss_coefficient
            * params.volume_l
            * WATER_HEAT_CAPACITY_KWH_PER_L_K
            * 1000
            * (tank.equivalent_temperature - AMBIENT_TEMPERATURE)
        )
    demand = coordinator.demand
    today = dt_util.now().date()
    profile = {
        day_type.value: {
            "probability": [
                round(slot.probability, 2)
                for slot in demand.slot_profile(day_type, today)
            ],
            "mean_energy_kwh": [
                round(slot.mean_energy_kwh, 3)
                for slot in demand.slot_profile(day_type, today)
            ],
        }
        for day_type in DayType
    }
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "parameters": asdict(params),
        "readings": asdict(data.readings),
        "tank": asdict(tank),
        "losses": {
            "coefficient_per_hour": tank.loss_coefficient,
            "rate_c_per_hour_at_46c": tank.loss_rate,
            "power_w_now": loss_power_w,
            "learning_windows": tank.loss_windows,
            "ambient_temperature": AMBIENT_TEMPERATURE,
        },
        "model": coordinator.model.as_dict(),
        "demand": {
            "snapshot": asdict(data.demand),
            "profile": profile,
            "recent_draws": [d.as_dict() for d in demand.draws[-RECENT_ITEMS:]],
            "recent_failures": [f.as_dict() for f in demand.failures[-RECENT_ITEMS:]],
        },
    }
