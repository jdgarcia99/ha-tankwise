"""Diagnostics for TankWise."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_AMBIENT_TEMPERATURE
from .coordinator import TankWiseConfigEntry
from .model.tank import WATER_HEAT_CAPACITY_KWH_PER_L_K

DIAGNOSTIC_CONSUMPTIONS = 50


def risk_report(coordinator: Any, plan: Any) -> list[dict[str, Any]]:
    """Return why the plan is at risk: where, which kind and how much is missing.

    ``kind`` is "appointment" (a block cannot be covered, even with the
    rescue recharge) or "demand" (forecast draws outside the blocks).
    """
    params = coordinator.tank_params
    tank = coordinator.data.tank if coordinator.data else None
    mains = tank.mains_temperature if tank is not None else params.mains_temperature
    span = params.shower_temperature - mains
    return [
        {
            "start": risk.start.isoformat(),
            "end": risk.end.isoformat(),
            "kind": risk.kind,
            "appointment": risk.block,
            "missing_kwh": round(risk.shortfall_kwh, 3),
            "missing_liters": round(
                risk.shortfall_kwh / (WATER_HEAT_CAPACITY_KWH_PER_L_K * span)
            )
            if span > 0
            else None,
        }
        for risk in plan.risks()
    ]


def plan_report(coordinator: Any) -> dict[str, Any]:
    """Return the whole plan, slot by slot (diagnostics and get_plan)."""
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
        "risks": risk_report(coordinator, plan),
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
                "name": block.name,
                "start": block.start.isoformat(),
                "required_temperature": block.required_temperature,
                "predicted_temperature": block.predicted_temperature,
                "predicted_liters": block.predicted_liters,
                "rescue_kwh": block.rescue_kwh,
                "at_risk": block.at_risk,
            }
            for block in plan.blocks
        ],
        "slots": [
            {
                "start": slot.start.isoformat(),
                "grid_kwh": round(slot.grid_kwh, 3),
                "solar_kwh": round(slot.solar_kwh, 3),
                "solar_allowed": slot.solar_allowed,
                "demand_kwh": round(slot.demand_kwh, 3),
                "temperature": round(slot.temperature, 1),
                "liters": round(slot.useful_liters),
                "cost": round(slot.cost, 4),
                "rescue": slot.rescue,
                "block": slot.block,
                "shortfall_kwh": round(slot.shortfall_kwh, 3),
            }
            for slot in plan.slots
        ],
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _controller(coordinator: Any) -> dict[str, Any]:
    controller = coordinator.controller
    state = controller.state
    degraded = controller.degraded
    return {
        **state.as_dict(),
        "active": controller.active,
        "desired_heating": state.desired,
        "degraded_reason": controller.degraded_reason,
        "degraded": (
            {
                key: _iso(value) if isinstance(value, datetime) else value
                for key, value in asdict(degraded).items()
            }
            if degraded is not None
            else None
        ),
        "regulator_lost_since": _iso(state.lost_since),
        "manual_since": _iso(state.manual_since),
        "full_latch": state.full_latch,
        "pause_until": _iso(state.pause_until),
        "risk_reasons": list(coordinator.data.risk_reasons),
    }


def _compensation(coordinator: Any) -> dict[str, Any] | None:
    """Automatic compensation cap: the estimate of the billing period."""
    planner = coordinator.planner
    estimate = planner.compensation_estimate
    if estimate is None:
        return None
    return {
        "period": [estimate.period_start.isoformat(), estimate.period_end.isoformat()],
        "compensation": round(estimate.compensation, 3),
        "energy_term": round(estimate.energy_term, 3),
        "projected_compensation": round(estimate.projected_compensation, 3),
        "projected_energy_term": round(estimate.projected_energy_term, 3),
        "hours_learned": estimate.hours_learned,
        "energy_term_factor": estimate.energy_term_factor,
        "hours_kept": len(planner.compensation.hours),
        "free_surplus": round(planner.free_surplus, 3),
        "forced": planner.compensation_cap,
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
            "stratification_ratio": tank.stratification_ratio,
            "stratification_samples": [
                [round(start, 2), round(ratio, 3)]
                for start, ratio in coordinator.model.stratification_samples
            ],
            "mains_source": data.mains_source,
            "mains_sensor": coordinator.mains_sensor.as_dict(),
            "tank_since": coordinator.model.tank_since.isoformat()
            if coordinator.model.tank_since
            else None,
            "outlet_usable_samples": coordinator.model.outlet_usable_samples,
        },
        "bootstrap": coordinator.bootstrap.as_dict(),
        "model": model,
        "consumptions": consumptions[-DIAGNOSTIC_CONSUMPTIONS:],
        "shortages_7d": data.shortages_7d,
        "plan": plan_report(coordinator),
        "ledger": coordinator.planner.ledger.as_dict(),
        "controller": _controller(coordinator),
        "solar": {
            "learned_hours": coordinator.planner.solar.learned_hours(),
            "base_load_kwh": {
                hour: round(coordinator.planner.solar.base_load(hour), 3)
                for hour in range(24)
            },
            "forecast_kwh": {
                moment.isoformat(): round(kwh, 3)
                for moment, kwh in (coordinator.planner.solar_forecast or {}).items()
            },
            "solar_needed": coordinator.planner.solar_needed,
            "power_request": list(coordinator.data.power_request),
        },
        "compensation": _compensation(coordinator),
        "anti_legionella": {
            "enabled": coordinator.planner.anti_legionella,
            **coordinator.planner.legionella.as_dict(),
        },
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
            "observed_today": list(data.observed_today),
            "profile_today_p50_p90": list(data.profile_today),
            "profile_liters_today_p50_p90": (
                list(data.profile_liters_today)
                if data.profile_liters_today is not None
                else None
            ),
            "forecast_p90_36h": data.forecast.total(0.9),
        },
        "appointments": [
            {
                **appointment.as_dict(),
                "suggested_liters_p50": usage.suggested_p50 if usage else None,
                "suggested_liters_p90": usage.suggested if usage else None,
                "measured_days": usage.days if usage else 0,
            }
            for appointment in coordinator.appointments
            for usage in [data.appointment_usage.get(appointment.id)]
        ],
    }
