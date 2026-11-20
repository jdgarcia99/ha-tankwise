"""Diagnostics for TankWise."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_AMBIENT_TEMPERATURE
from .coordinator import TankWiseConfigEntry

DIAGNOSTIC_CONSUMPTIONS = 50


def _plan(coordinator: Any) -> dict[str, Any] | None:
    planner = coordinator.planner
    plan = planner.plan
    if plan is None:
        return {"mode": planner.mode.value, "plan": None}
    heating = plan.next_heating()
    return {
        "mode": planner.mode.value,
        "computed_at": planner.plan_computed.isoformat()
        if planner.plan_computed
        else None,
        "reason": planner.plan_reason,
        "tomorrow_prices": planner.prices.tomorrow_known if planner.prices else None,
        "cost": plan.cost,
        "grid_kwh": plan.grid_kwh,
        "rescue_kwh": plan.rescue_kwh,
        "rescue_cost": plan.rescue_cost,
        "at_risk": plan.at_risk,
        "next_heating": (
            {
                "start": heating.start.isoformat(),
                "end": heating.end.isoformat(),
                "energy_kwh": heating.energy_kwh,
                "cost": heating.cost,
            }
            if heating
            else None
        ),
        "blocks": [
            {
                "id": block.id,
                "required_temperature": block.required_temperature,
                "predicted_temperature": block.predicted_temperature,
                "rescue_kwh": block.rescue_kwh,
                "at_risk": block.at_risk,
            }
            for block in plan.blocks
        ],
        "ledger": planner.ledger.as_dict(),
    }


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
        "plan": _plan(coordinator),
        "reconciliations": [
            item.as_dict() for item in coordinator.model.reconciliations
        ],
        "demand": {
            "status": coordinator.demand.status().value,
            "params": {
                "mode": coordinator.demand_params.mode.value,
                "observations": coordinator.demand_params.observations,
            },
            "observed_days": [
                {
                    "day": obs.day.isoformat(),
                    "workday": obs.workday,
                    "kwh": round(obs.total, 3),
                }
                for obs in coordinator.demand.observations
            ],
            "marks": [
                {"start": mark.start.isoformat(), "reason": mark.reason}
                for mark in coordinator.demand.marks
            ],
            "today_p50_p90": list(data.demand_today),
            "remaining_p50_p90": list(data.demand_remaining),
            "forecast_p90_36h": data.forecast.total(0.9),
        },
        "appointments": [
            {
                **appointment.as_dict(),
                "suggested_liters_p50": usage.p50 if usage else None,
                "suggested_liters_p90": usage.p90 if usage else None,
                "measured_days": usage.days if usage else 0,
            }
            for appointment in coordinator.appointments
            for usage in [data.appointment_usage.get(appointment.id)]
        ],
    }
