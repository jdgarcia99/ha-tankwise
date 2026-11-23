"""Sensors for the TankWise tank state and learned parameters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import CONF_AMBIENT_TEMPERATURE
from .coordinator import TankWiseConfigEntry, TankWiseCoordinator, TankWiseData
from .diagnostics import risk_report
from .entity import AppointmentEntity, TankWiseEntity
from .model.demand import MIN_READY_DAYS, day_type
from .model.tank import MIN_CUTOFF_SAMPLES, MIN_LOSS_WINDOWS, ModelStatus

PARALLEL_UPDATES = 0

UNIT_W_PER_K = "W/K"


@dataclass(frozen=True, kw_only=True)
class TankWiseSensorDescription(SensorEntityDescription):
    """Describe a TankWise sensor."""

    value_fn: Callable[[TankWiseData], StateType | Any]
    attributes_fn: Callable[[TankWiseCoordinator], dict[str, Any]] | None = None
    # Unavailable while the tank model is frozen (safe mode).
    requires_tank: bool = True


def _last_draw_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    event = coordinator.data.tank.last_consumption
    if event is None:
        return {}
    return {
        "end": event.end.isoformat(),
        "energy_kwh": round(event.energy_kwh, 3),
        "start_temperature": round(event.start_temperature, 1),
        "min_temperature": round(event.min_temperature, 1),
        "shortage": event.shortage,
        "while_heating": event.while_heating,
        "slow": event.slow,
        "reconciled": event.reconciled is not None,
        "estimated": event.estimated,
    }


def _ambient(coordinator: TankWiseCoordinator) -> float:
    reading = coordinator.data.readings.ambient_temperature
    if reading is not None:
        return reading
    return float(coordinator.parameters[CONF_AMBIENT_TEMPERATURE])


def _loss_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    tank = coordinator.data.tank
    loss = tank.loss_power_w(_ambient(coordinator))
    return {
        "learning_windows": tank.loss_windows,
        "loss_power_w": round(loss, 1) if loss is not None else None,
    }


def _demand_today_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    data = coordinator.data
    today = dt_util.now().date()
    params = coordinator.demand_params
    return {
        "p50": round(data.demand_today[0], 3),
        "remaining_p50": round(data.demand_remaining[0], 3),
        "remaining_p90": round(data.demand_remaining[1], 3),
        "day_type": day_type(today, params.mode, data.readings.workday),
        "observed_days": len(coordinator.demand.observations),
        "profile_status": coordinator.demand.status().value,
    }


def _model_status_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    """Why the model is uncertain or learning, and what it has learned."""
    tank = coordinator.data.tank
    missing = []
    if tank.status is ModelStatus.UNCERTAIN:
        # A heating to the thermostat cut-off anchors the stored energy.
        missing.append("heating_to_cutoff")
    if tank.loss_windows < MIN_LOSS_WINDOWS:
        missing.append("loss_windows")
    demand = coordinator.demand
    return {
        "reason": tank.status_reason,
        "missing": missing,
        "uncertain_since": tank.uncertain_since.isoformat()
        if tank.uncertain_since
        else None,
        "last_calibration": tank.last_calibration.isoformat()
        if tank.last_calibration
        else None,
        "calibration_count": tank.calibration_count,
        "samples": {
            "cutoff": len(tank.cutoff_samples),
            "heater_power": tank.heater_power_samples,
            "loss_windows": tank.loss_windows,
            "capacity_factor": tank.capacity_samples,
            "usable_factor": tank.usable_samples,
            "profile_days": len(demand.observations),
        },
        "samples_needed": {
            "cutoff": MIN_CUTOFF_SAMPLES,
            "loss_windows": MIN_LOSS_WINDOWS,
            "profile_days": MIN_READY_DAYS,
        },
    }


def _forecast_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    forecast = coordinator.data.forecast
    return {
        "start": forecast.start.isoformat(),
        "end": forecast.end.isoformat(),
        "p50": round(forecast.total(0.5), 3),
        "slots": [
            {
                "start": slot.start.isoformat(),
                "p50": round(slot.p50, 3),
                "p90": round(slot.p90, 3),
            }
            for slot in forecast.slots
            if slot.p90 > 0 or slot.p50 > 0
        ],
    }


CURRENCY = "EUR"


def _plan_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    planner = coordinator.planner
    plan = coordinator.data.plan
    attributes: dict[str, Any] = {
        "mode": planner.mode.value,
        "computed_at": planner.plan_computed.isoformat()
        if planner.plan_computed
        else None,
        "reason": planner.plan_reason,
        "tomorrow_prices": planner.prices.tomorrow_known if planner.prices else None,
        "deviations_today": coordinator.data.totals_today.deviations,
    }
    if plan is None:
        return attributes
    return attributes | {
        "grid_kwh": round(plan.grid_kwh, 3),
        "rescue_kwh": round(plan.rescue_kwh, 3),
        "rescue_cost": round(plan.rescue_cost, 3),
        "at_risk": plan.at_risk,
        "risks": risk_report(coordinator, plan),
        # Compact summary; the slot by slot plan is in the diagnostics and
        # the tankwise.get_plan service (too big for a state attribute).
        "end": plan.slots[-1].start.isoformat() if plan.slots else None,
        "heatings": [
            {
                "start": heating.start.isoformat(),
                "end": heating.end.isoformat(),
                "kwh": round(heating.energy_kwh, 2),
                "cost": round(heating.cost, 3),
                "rescue": heating.rescue,
            }
            for heating in plan.heatings()
        ],
        "blocks": [
            {
                "name": block.name,
                "start": block.start.isoformat(),
                "required_temperature": round(block.required_temperature, 1),
                "predicted_temperature": round(block.predicted_temperature, 1),
                "at_risk": block.at_risk,
            }
            for block in plan.blocks
        ],
    }


def _legionella_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    planner = coordinator.planner
    now = dt_util.utcnow()
    plan = coordinator.data.plan
    planned = plan.legionella if plan is not None else None
    return {
        "enabled": planner.anti_legionella,
        "deadline": planner.legionella.deadline(now).isoformat(),
        "planned_at": planned[0].isoformat() if planned else None,
        "planned_temperature": round(planned[1], 1) if planned else None,
    }


def _next_heating(data: TankWiseData) -> Any:
    heating = data.plan.next_heating() if data.plan else None
    return heating.start if heating else None


def _next_heating_attributes(coordinator: TankWiseCoordinator) -> dict[str, Any]:
    plan = coordinator.data.plan
    heating = plan.next_heating() if plan else None
    if heating is None:
        return {}
    return {
        "end": heating.end.isoformat(),
        "duration_min": round((heating.end - heating.start).total_seconds() / 60),
        "energy_kwh": round(heating.energy_kwh, 3),
        "cost": round(heating.cost, 3),
        "rescue": heating.rescue,
    }


def _money(
    description_key: str, value_fn: Callable[[TankWiseData], Any]
) -> TankWiseSensorDescription:
    return TankWiseSensorDescription(
        key=description_key,
        translation_key=description_key,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=value_fn,
        requires_tank=False,
    )


SENSORS: tuple[TankWiseSensorDescription, ...] = (
    TankWiseSensorDescription(
        key="stored_energy",
        translation_key="stored_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.tank.stored_energy_kwh,
    ),
    TankWiseSensorDescription(
        key="useful_energy",
        translation_key="useful_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.tank.useful_energy_kwh,
    ),
    TankWiseSensorDescription(
        key="charge",
        translation_key="charge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.tank.charge_percent,
    ),
    TankWiseSensorDescription(
        key="available_liters",
        translation_key="available_liters",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.tank.liters_available,
        attributes_fn=lambda coordinator: {
            "shower_temperature": coordinator.tank_params.shower_temperature,
            "comfort_min_temperature": coordinator.tank_params.comfort_min_temperature,
            "mains_water_temperature": round(
                coordinator.data.tank.mains_temperature, 1
            ),
            "theoretical_liters": (
                round(coordinator.data.tank.liters_theoretical, 1)
                if coordinator.data.tank.liters_theoretical is not None
                else None
            ),
            "usable_factor": round(coordinator.data.tank.usable_factor, 3),
        },
    ),
    TankWiseSensorDescription(
        key="tank_temperature",
        translation_key="tank_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.tank.equivalent_temperature,
    ),
    TankWiseSensorDescription(
        key="model_status",
        translation_key="model_status",
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in ModelStatus],
        value_fn=lambda data: data.tank.status.value,
        attributes_fn=_model_status_attributes,
    ),
    TankWiseSensorDescription(
        key="last_draw",
        translation_key="last_draw",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (
            data.tank.last_consumption.start if data.tank.last_consumption else None
        ),
        attributes_fn=_last_draw_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="water_shortages_7d",
        translation_key="water_shortages_7d",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.shortages_7d,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="demand_today",
        translation_key="demand_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.demand_today[1],
        attributes_fn=_demand_today_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="demand_forecast",
        translation_key="demand_forecast",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.forecast.total(0.9),
        attributes_fn=_forecast_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="plan",
        translation_key="plan",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda data: data.plan.cost if data.plan else None,
        attributes_fn=_plan_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="next_heating",
        translation_key="next_heating",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_heating,
        attributes_fn=_next_heating_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="power_request",
        translation_key="power_request",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.power_request[0],
        attributes_fn=lambda coordinator: {
            "priority": coordinator.data.power_request[1]
        },
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="last_anti_legionella",
        translation_key="last_anti_legionella",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.legionella_last,
        attributes_fn=_legionella_attributes,
        requires_tank=False,
    ),
    _money("cost_today", lambda data: data.totals_today.real_cost),
    _money("cost_month", lambda data: data.totals_month.real_cost),
    _money("planned_cost_today", lambda data: data.totals_today.planned_cost),
    _money("planned_cost_month", lambda data: data.totals_month.planned_cost),
    _money(
        "estimated_savings_month",
        lambda data: data.totals_month.real_cost - data.totals_month.planned_cost,
    ),
    _money("rescue_cost_month", lambda data: data.totals_month.rescue_cost),
    TankWiseSensorDescription(
        key="rescue_energy_month",
        translation_key="rescue_energy_month",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.totals_month.rescue_kwh,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="heat_loss_coefficient",
        translation_key="heat_loss_coefficient",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UNIT_W_PER_K,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.tank.ua_w_per_k,
        attributes_fn=_loss_attributes,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="heater_power",
        translation_key="heater_power",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.tank.heater_power_w,
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="cutoff_temperature",
        translation_key="cutoff_temperature",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.tank.cutoff_temperature,
        attributes_fn=lambda coordinator: {
            "samples": [
                round(value, 1) for value in coordinator.data.tank.cutoff_samples
            ]
        },
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="capacity_factor",
        translation_key="capacity_factor",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.tank.capacity_factor,
        attributes_fn=lambda coordinator: {
            "samples": coordinator.data.tank.capacity_samples
        },
        requires_tank=False,
    ),
    TankWiseSensorDescription(
        key="mains_water_temperature",
        translation_key="mains_water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.tank.mains_temperature,
        attributes_fn=lambda coordinator: {"source": coordinator.data.mains_source},
        requires_tank=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TankWiseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TankWise sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(TankWiseSensor(coordinator, description) for description in SENSORS),
            *(
                SuggestedLitersSensor(coordinator, appointment, "suggested_liters")
                for appointment in coordinator.appointments
            ),
            *(
                RequiredTemperatureSensor(
                    coordinator, appointment, "required_temperature"
                )
                for appointment in coordinator.appointments
            ),
            *(
                PredictedTemperatureSensor(
                    coordinator, appointment, "predicted_temperature"
                )
                for appointment in coordinator.appointments
            ),
        ]
    )


class TankWiseSensor(TankWiseEntity, SensorEntity):
    """A sensor derived from the coordinator data."""

    _platform = "sensor"
    # Home Assistant reads this at class creation: it cannot be per instance.
    # The demand forecast lists its slots; the recorder keeps the totals.
    _unrecorded_attributes = frozenset({"slots", "heatings", "blocks", "risks"})

    entity_description: TankWiseSensorDescription

    def __init__(
        self,
        coordinator: TankWiseCoordinator,
        description: TankWiseSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Tank values are unavailable while the model is frozen (safe mode)."""
        if not super().available:
            return False
        return not self.entity_description.requires_tank or (
            self.coordinator.data.tank.available
        )

    @property
    def native_value(self) -> StateType | Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the reference values the sensor value is derived from."""
        if (attributes_fn := self.entity_description.attributes_fn) is None:
            return None
        return attributes_fn(self.coordinator)


class SuggestedLitersSensor(AppointmentEntity, SensorEntity):
    """Litres drawn in the hour after an appointment (P90 of recent weeks)."""

    _platform = "sensor"

    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        """Return the P90 of the litres used after the appointment."""
        usage = self.coordinator.data.appointment_usage.get(self._appointment_id)
        return usage.p90 if usage else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the P50 and the number of days measured."""
        usage = self.coordinator.data.appointment_usage.get(self._appointment_id)
        if usage is None:
            return {}
        return {
            "p50": round(usage.p50, 1) if usage.p50 is not None else None,
            "days": usage.days,
        }


class RequiredTemperatureSensor(AppointmentEntity, SensorEntity):
    """Tank temperature needed when the appointment block starts."""

    _platform = "sensor"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the temperature that gives the litres of the block."""
        requirement = self.coordinator.data.appointment_requirements.get(
            self._appointment_id
        )
        return requirement.temperature if requirement else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the litres it covers and whether a full tank reaches it."""
        requirement = self.coordinator.data.appointment_requirements.get(
            self._appointment_id
        )
        if requirement is None:
            return {}
        return {
            "liters": round(requirement.liters, 1),
            "liters_source": requirement.source,
            "cutoff_temperature": round(
                self.coordinator.data.tank.cutoff_temperature, 1
            ),
            "feasible": requirement.feasible,
        }


class PredictedTemperatureSensor(AppointmentEntity, SensorEntity):
    """Tank temperature the plan expects at the next start of the block."""

    _platform = "sensor"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def _block(self) -> Any:
        plan = self.coordinator.data.plan
        if plan is None:
            return None
        return next(
            (
                block
                for block in plan.blocks
                if block.id.split("@", 1)[0] == self._appointment_id
            ),
            None,
        )

    @property
    def native_value(self) -> float | None:
        """Return the planned temperature when the block starts."""
        block = self._block()
        return block.predicted_temperature if block else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the block start, litres, rescue and risk."""
        block = self._block()
        if block is None:
            return {}
        return {
            "start": block.start.isoformat(),
            "predicted_liters": round(block.predicted_liters),
            "required_temperature": round(block.required_temperature, 1),
            "rescue_kwh": round(block.rescue_kwh, 3),
            "rescue_cost": round(block.rescue_cost, 3),
            "at_risk": block.at_risk,
        }
