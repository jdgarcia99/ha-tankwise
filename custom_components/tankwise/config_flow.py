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
    CONFIG_ENTRY_VERSION,
    DEFAULT_NAME,
    DEFAULT_PARAMETERS,
    DOMAIN,
    INTEGER_PARAMETERS,
    OPTIONAL_INPUT_KEYS,
)
from .model.water import REGIONS

ENERGY_STATE_CLASSES = (SensorStateClass.TOTAL_INCREASING, SensorStateClass.TOTAL)

# A regulator output is a percentage; some integrations publish it unitless.
REGULATOR_UNITS: tuple[str | None, ...] = (PERCENTAGE, None)

ON_OFF_DOMAINS = [SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN]
STATE_DOMAINS = [INPUT_BOOLEAN_DOMAIN, SWITCH_DOMAIN, BINARY_SENSOR_DOMAIN]

ERROR_ENTITY_NOT_FOUND = "entity_not_found"
ERROR_INVALID_UNIT = "invalid_unit"
ERROR_INVALID_STATE_CLASS = "invalid_state_class"
ERROR_NOT_NUMERIC = "not_numeric"


def _sensor_selector(device_class: SensorDeviceClass | None = None) -> EntitySelector:
    if device_class is None:
        return EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN))
    return EntitySelector(
        EntitySelectorConfig(domain=SENSOR_DOMAIN, device_class=device_class)
    )


def _entity_selector(domains: list[str]) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domains))


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


REQUIRED_INPUTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMPERATURE_SENSOR): _sensor_selector(
            SensorDeviceClass.TEMPERATURE
        ),
        vol.Required(CONF_POWER_SENSOR): _sensor_selector(SensorDeviceClass.POWER),
        vol.Required(CONF_HEATER_SWITCH): _entity_selector(ON_OFF_DOMAINS),
        vol.Required(CONF_MANUAL_MODE_SWITCH): _entity_selector(ON_OFF_DOMAINS),
        vol.Required(CONF_PRICE_SENSOR): _sensor_selector(),
    }
)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector()}
).extend(REQUIRED_INPUTS_SCHEMA.schema)

OPTIONAL_INPUTS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENERGY_SENSOR): _sensor_selector(SensorDeviceClass.ENERGY),
        vol.Optional(CONF_REGULATOR_SENSOR): _entity_selector(
            [SENSOR_DOMAIN, NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN]
        ),
        vol.Optional(CONF_MASTER_ENTITY): _entity_selector(STATE_DOMAINS),
        vol.Optional(CONF_OCCUPANCY_ENTITY): _entity_selector(STATE_DOMAINS),
        vol.Optional(CONF_INJECTION_PRICE_SENSOR): _sensor_selector(),
        vol.Optional(CONF_GRID_IMPORT_SENSOR): _sensor_selector(
            SensorDeviceClass.ENERGY
        ),
        vol.Optional(CONF_GRID_EXPORT_SENSOR): _sensor_selector(
            SensorDeviceClass.ENERGY
        ),
        vol.Optional(CONF_EXPORT_POWER_SENSOR): _sensor_selector(
            SensorDeviceClass.POWER
        ),
        vol.Optional(CONF_SOLAR_FORECAST_SENSOR): _sensor_selector(),
        vol.Optional(CONF_AMBIENT_SENSOR): _sensor_selector(
            SensorDeviceClass.TEMPERATURE
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
        vol.Required(CONF_AMBIENT_TEMPERATURE): _number_selector(
            0, 40, 0.5, UnitOfTemperature.CELSIUS
        ),
        vol.Required(CONF_CUTOFF_TEMPERATURE): _number_selector(
            35, 85, 0.5, UnitOfTemperature.CELSIUS
        ),
        vol.Required(CONF_CUTOFF_POWER_THRESHOLD): _number_selector(
            0, 500, 1, UnitOfPower.WATT
        ),
    }
)


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


def _validate_exists(hass: HomeAssistant, entity_id: str) -> str | None:
    if hass.states.get(entity_id) is None:
        return ERROR_ENTITY_NOT_FOUND
    return None


def _validate_numeric(hass: HomeAssistant, entity_id: str) -> str | None:
    """A price may be unavailable for a moment, but never non-numeric."""
    state = hass.states.get(entity_id)
    if state is None:
        return ERROR_ENTITY_NOT_FOUND
    if state.state in ("unavailable", "unknown"):
        return None
    try:
        float(state.state)
    except ValueError:
        return ERROR_NOT_NUMERIC
    return None


def _validate_required_inputs(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate the required inputs."""
    errors: dict[str, str] = {}
    checks = {
        CONF_TEMPERATURE_SENSOR: _validate_measurement(
            hass, user_input[CONF_TEMPERATURE_SENSOR], TemperatureConverter.VALID_UNITS
        ),
        CONF_POWER_SENSOR: _validate_measurement(
            hass, user_input[CONF_POWER_SENSOR], PowerConverter.VALID_UNITS
        ),
        CONF_HEATER_SWITCH: _validate_exists(hass, user_input[CONF_HEATER_SWITCH]),
        CONF_MANUAL_MODE_SWITCH: _validate_exists(
            hass, user_input[CONF_MANUAL_MODE_SWITCH]
        ),
        CONF_PRICE_SENSOR: _validate_numeric(hass, user_input[CONF_PRICE_SENSOR]),
    }
    for key, error in checks.items():
        if error:
            errors[key] = error
    return errors


def _validate_optional_inputs(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate units and state classes of the optional inputs."""
    measurement_checks: dict[
        str, tuple[Collection[str | None], Collection[str] | None]
    ] = {
        CONF_ENERGY_SENSOR: (EnergyConverter.VALID_UNITS, ENERGY_STATE_CLASSES),
        CONF_GRID_IMPORT_SENSOR: (EnergyConverter.VALID_UNITS, ENERGY_STATE_CLASSES),
        CONF_GRID_EXPORT_SENSOR: (EnergyConverter.VALID_UNITS, ENERGY_STATE_CLASSES),
        CONF_EXPORT_POWER_SENSOR: (PowerConverter.VALID_UNITS, None),
        CONF_AMBIENT_SENSOR: (TemperatureConverter.VALID_UNITS, None),
        CONF_REGULATOR_SENSOR: (REGULATOR_UNITS, None),
    }
    errors: dict[str, str] = {}
    for key, (units, state_classes) in measurement_checks.items():
        if (entity_id := user_input.get(key)) and (
            error := _validate_measurement(hass, entity_id, units, state_classes)
        ):
            errors[key] = error
    for key in (
        CONF_INJECTION_PRICE_SENSOR,
        CONF_SOLAR_FORECAST_SENSOR,
    ):
        if (entity_id := user_input.get(key)) and (
            error := _validate_numeric(hass, entity_id)
        ):
            errors[key] = error
    for key in (CONF_MASTER_ENTITY, CONF_OCCUPANCY_ENTITY):
        if (entity_id := user_input.get(key)) and (
            error := _validate_exists(hass, entity_id)
        ):
            errors[key] = error
    return errors


def _clean_optional_inputs(user_input: dict[str, Any]) -> dict[str, Any]:
    """Keep only the optional inputs that were actually selected."""
    return {key: user_input[key] for key in OPTIONAL_INPUT_KEYS if user_input.get(key)}


def _clean_parameters(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise parameter types coming from number selectors."""
    parameters = dict(user_input)
    for key in INTEGER_PARAMETERS:
        parameters[key] = int(parameters[key])
    return parameters


class TankWiseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the TankWise config flow."""

    VERSION = CONFIG_ENTRY_VERSION

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
        """Select the required inputs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_TEMPERATURE_SENSOR])
            self._abort_if_unique_id_configured()
            errors = _validate_required_inputs(self.hass, user_input)
            if not errors:
                self._data = user_input
                return await self.async_step_inputs()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the required inputs of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            new_unique_id = user_input[CONF_TEMPERATURE_SENSOR]
            if new_unique_id != entry.unique_id:
                await self.async_set_unique_id(new_unique_id)
                self._abort_if_unique_id_configured()
            errors = _validate_required_inputs(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, unique_id=new_unique_id, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                REQUIRED_INPUTS_SCHEMA,
                user_input if user_input is not None else entry.data,
            ),
            errors=errors,
        )

    async def async_step_inputs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the recommended and optional inputs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_optional_inputs(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=self._data[CONF_NAME],
                    data=self._data,
                    options={
                        **DEFAULT_PARAMETERS,
                        **_clean_optional_inputs(user_input),
                    },
                )

        return self.async_show_form(
            step_id="inputs",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONAL_INPUTS_SCHEMA, user_input
            ),
            errors=errors,
        )


class TankWiseOptionsFlow(OptionsFlowWithReload):
    """Handle TankWise options: installation parameters and optional inputs."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._parameters: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the installation parameters."""
        if user_input is not None:
            self._parameters = _clean_parameters(user_input)
            return await self.async_step_inputs()

        suggested = {**DEFAULT_PARAMETERS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                PARAMETERS_SCHEMA, suggested
            ),
        )

    async def async_step_inputs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the recommended and optional inputs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_optional_inputs(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    data={**self._parameters, **_clean_optional_inputs(user_input)}
                )

        return self.async_show_form(
            step_id="inputs",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONAL_INPUTS_SCHEMA,
                user_input if user_input is not None else self.config_entry.options,
            ),
            errors=errors,
        )
