"""Config and options flow for TankWise."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.input_boolean import DOMAIN as INPUT_BOOLEAN_DOMAIN
from homeassistant.components.input_number import DOMAIN as INPUT_NUMBER_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util.unit_conversion import (
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)
import voluptuous as vol

from .const import (
    COMFORT_LEVELS,
    CONF_BOTTOM_TEMPERATURE_SENSOR,
    CONF_COMFORT_LEVEL,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_MAINS_WATER_TEMPERATURE,
    CONF_MANUAL_MODE_ENTITY,
    CONF_MIN_USEFUL_TEMPERATURE,
    CONF_PERSON_AUTOMATIONS,
    CONF_POWER_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_SENSOR,
    CONF_RATED_POWER,
    CONF_REGULATOR_SENSOR,
    CONF_SHOWER_ENERGY,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_SURPLUS_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOLUME,
    CONF_WATER_REGION,
    CONF_WORKDAY_SENSOR,
    DEFAULT_NAME,
    DEFAULT_PARAMETERS,
    DOMAIN,
    OPTIONAL_SENSOR_KEYS,
)
from .model.water import REGIONS

ENERGY_STATE_CLASSES = (SensorStateClass.TOTAL_INCREASING, SensorStateClass.TOTAL)

# A regulator output is a percentage; some integrations publish it unitless.
REGULATOR_UNITS: tuple[str | None, ...] = (PERCENTAGE, None)

AUTOMATION_DOMAIN = "automation"

ERROR_ENTITY_NOT_FOUND = "entity_not_found"
ERROR_INVALID_UNIT = "invalid_unit"
ERROR_INVALID_STATE_CLASS = "invalid_state_class"


def _sensor_selector(device_class: SensorDeviceClass) -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain=SENSOR_DOMAIN, device_class=device_class)
    )


def _number_selector(
    minimum: float, maximum: float, step: float, unit: str
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMPERATURE_SENSOR): _sensor_selector(
            SensorDeviceClass.TEMPERATURE
        ),
        vol.Required(CONF_POWER_SENSOR): _sensor_selector(SensorDeviceClass.POWER),
        vol.Required(CONF_ENERGY_SENSOR): _sensor_selector(SensorDeviceClass.ENERGY),
    }
)

REQUIRED_SENSORS_SCHEMA = vol.Schema(
    {vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector()}
).extend(RECONFIGURE_SCHEMA.schema)

OPTIONAL_SENSORS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SOLAR_FORECAST_SENSOR): _sensor_selector(
            SensorDeviceClass.ENERGY
        ),
        vol.Optional(CONF_SURPLUS_SENSOR): _sensor_selector(SensorDeviceClass.POWER),
        vol.Optional(CONF_PRICE_SENSOR): EntitySelector(
            EntitySelectorConfig(domain=SENSOR_DOMAIN)
        ),
        vol.Optional(CONF_PRESENCE_ENTITY): EntitySelector(
            EntitySelectorConfig(
                domain=[INPUT_BOOLEAN_DOMAIN, SWITCH_DOMAIN, BINARY_SENSOR_DOMAIN]
            )
        ),
        vol.Optional(CONF_BOTTOM_TEMPERATURE_SENSOR): _sensor_selector(
            SensorDeviceClass.TEMPERATURE
        ),
        vol.Optional(CONF_REGULATOR_SENSOR): EntitySelector(
            EntitySelectorConfig(
                domain=[SENSOR_DOMAIN, NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN]
            )
        ),
        vol.Optional(CONF_MANUAL_MODE_ENTITY): EntitySelector(
            EntitySelectorConfig(
                domain=[SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, BINARY_SENSOR_DOMAIN]
            )
        ),
        vol.Optional(CONF_PERSON_AUTOMATIONS): EntitySelector(
            EntitySelectorConfig(domain=AUTOMATION_DOMAIN, multiple=True)
        ),
        vol.Optional(CONF_WORKDAY_SENSOR): EntitySelector(
            EntitySelectorConfig(domain=BINARY_SENSOR_DOMAIN, integration="workday")
        ),
    }
)

PARAMETERS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_VOLUME): _number_selector(10, 500, 1, UnitOfVolume.LITERS),
        vol.Required(CONF_RATED_POWER): _number_selector(
            100, 6000, 10, UnitOfPower.WATT
        ),
        vol.Required(CONF_WATER_REGION): SelectSelector(
            SelectSelectorConfig(
                options=list(REGIONS),
                translation_key=CONF_WATER_REGION,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_MAINS_WATER_TEMPERATURE): _number_selector(
            0, 30, 0.5, UnitOfTemperature.CELSIUS
        ),
        vol.Required(CONF_MIN_USEFUL_TEMPERATURE): _number_selector(
            25, 60, 0.5, UnitOfTemperature.CELSIUS
        ),
        vol.Required(CONF_CUTOFF_POWER_THRESHOLD): _number_selector(
            0, 500, 1, UnitOfPower.WATT
        ),
        vol.Required(CONF_CUTOFF_TEMPERATURE): _number_selector(
            35, 85, 0.5, UnitOfTemperature.CELSIUS
        ),
        vol.Required(CONF_SHOWER_ENERGY): _number_selector(
            0.2, 10, 0.1, UnitOfEnergy.KILO_WATT_HOUR
        ),
        vol.Required(CONF_COMFORT_LEVEL): SelectSelector(
            SelectSelectorConfig(
                options=list(COMFORT_LEVELS),
                translation_key=CONF_COMFORT_LEVEL,
                mode=SelectSelectorMode.LIST,
            )
        ),
    }
)

INTEGER_PARAMETERS = (CONF_VOLUME, CONF_RATED_POWER, CONF_CUTOFF_POWER_THRESHOLD)


def _validate_measurement(
    hass: HomeAssistant,
    entity_id: str,
    valid_units: Collection[str | None],
    state_classes: Collection[str] | None = None,
) -> str | None:
    """Return an error key if the entity is not a usable measurement."""
    state = hass.states.get(entity_id)
    if state is None:
        return ERROR_ENTITY_NOT_FOUND
    if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in valid_units:
        return ERROR_INVALID_UNIT
    if (
        state_classes is not None
        and state.attributes.get(ATTR_STATE_CLASS) not in state_classes
    ):
        return ERROR_INVALID_STATE_CLASS
    return None


def _validate_required_sensors(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate units and state classes of the required sensors."""
    checks = {
        CONF_TEMPERATURE_SENSOR: (TemperatureConverter.VALID_UNITS, None),
        CONF_POWER_SENSOR: (PowerConverter.VALID_UNITS, None),
        CONF_ENERGY_SENSOR: (EnergyConverter.VALID_UNITS, ENERGY_STATE_CLASSES),
    }
    errors: dict[str, str] = {}
    for key, (units, state_classes) in checks.items():
        if error := _validate_measurement(hass, user_input[key], units, state_classes):
            errors[key] = error
    return errors


def _validate_optional_sensors(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate units of the optional sensors that are converted."""
    checks: dict[str, Collection[str | None]] = {
        CONF_SURPLUS_SENSOR: PowerConverter.VALID_UNITS,
        CONF_BOTTOM_TEMPERATURE_SENSOR: TemperatureConverter.VALID_UNITS,
        CONF_REGULATOR_SENSOR: REGULATOR_UNITS,
    }
    errors: dict[str, str] = {}
    for key, units in checks.items():
        if (entity_id := user_input.get(key)) and (
            error := _validate_measurement(hass, entity_id, units)
        ):
            errors[key] = error
    return errors


def _clean_optional_sensors(user_input: dict[str, Any]) -> dict[str, Any]:
    """Keep only the optional sensors that were actually selected."""
    return {key: user_input[key] for key in OPTIONAL_SENSOR_KEYS if user_input.get(key)}


def _clean_parameters(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise parameter types coming from number selectors."""
    parameters = dict(user_input)
    for key in INTEGER_PARAMETERS:
        parameters[key] = int(parameters[key])
    return parameters


class TankWiseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the TankWise config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TankWiseOptionsFlow:
        """Return the options flow handler."""
        return TankWiseOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the required tank sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_TEMPERATURE_SENSOR])
            self._abort_if_unique_id_configured()
            errors = _validate_required_sensors(self.hass, user_input)
            if not errors:
                self._data = user_input
                return await self.async_step_optional()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                REQUIRED_SENSORS_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the required sensors of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            new_unique_id = user_input[CONF_TEMPERATURE_SENSOR]
            if new_unique_id != entry.unique_id:
                await self.async_set_unique_id(new_unique_id)
                self._abort_if_unique_id_configured()
            errors = _validate_required_sensors(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, unique_id=new_unique_id, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA,
                user_input if user_input is not None else entry.data,
            ),
            errors=errors,
        )

    async def async_step_optional(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the optional sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_optional_sensors(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=self._data[CONF_NAME],
                    data=self._data,
                    options={
                        **DEFAULT_PARAMETERS,
                        **_clean_optional_sensors(user_input),
                    },
                )

        return self.async_show_form(
            step_id="optional",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONAL_SENSORS_SCHEMA, user_input
            ),
            errors=errors,
        )


class TankWiseOptionsFlow(OptionsFlowWithReload):
    """Handle TankWise options: physical parameters and optional sensors."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._parameters: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the physical parameters."""
        if user_input is not None:
            self._parameters = _clean_parameters(user_input)
            return await self.async_step_sensors()

        suggested = {**DEFAULT_PARAMETERS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                PARAMETERS_SCHEMA, suggested
            ),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the optional sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_optional_sensors(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    data={**self._parameters, **_clean_optional_sensors(user_input)}
                )

        return self.async_show_form(
            step_id="sensors",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONAL_SENSORS_SCHEMA,
                user_input if user_input is not None else self.config_entry.options,
            ),
            errors=errors,
        )
