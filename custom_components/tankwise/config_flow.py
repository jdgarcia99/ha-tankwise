"""Config and options flow for TankWise."""

from __future__ import annotations

from collections.abc import Collection
from datetime import time, timedelta
from typing import Any
from uuid import uuid4

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.input_boolean import DOMAIN as INPUT_BOOLEAN_DOMAIN
from homeassistant.components.input_number import DOMAIN as INPUT_NUMBER_DOMAIN
from homeassistant.components.input_select import DOMAIN as INPUT_SELECT_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
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
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)
from homeassistant.util.unit_conversion import (
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
    VolumeConverter,
    VolumeFlowRateConverter,
)
import voluptuous as vol

from .const import (
    ADVANCED_PARAMETERS,
    CONF_AMBIENT_SENSOR,
    CONF_AMBIENT_TEMPERATURE,
    CONF_APPOINTMENTS,
    CONF_BILLING_DAY,
    CONF_BOOST_AS_SHORTAGE,
    CONF_CUTOFF_POWER_THRESHOLD,
    CONF_CUTOFF_TEMPERATURE,
    CONF_ENERGY_SENSOR,
    CONF_ENERGY_TERM_FACTOR,
    CONF_EXPORT_POWER_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HEATER_SWITCH,
    CONF_HOT_WATER_FLOW_SENSOR,
    CONF_HOT_WATER_VOLUME_SENSOR,
    CONF_INJECTION_PRICE_SENSOR,
    CONF_MAINTENANCE_ENTITY,
    CONF_MAINTENANCE_MARGIN,
    CONF_MANUAL_MODE_SWITCH,
    CONF_MASTER_ENTITY,
    CONF_OCCUPANCY_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PROBE_POSITION,
    CONF_PROBE_VOLUME_BELOW,
    CONF_PROFILE_DAYS,
    CONF_PROFILE_MODE,
    CONF_RATED_POWER,
    CONF_REGULATOR_ACTIVE_MODES,
    CONF_REGULATOR_MARGIN,
    CONF_REGULATOR_MODE_ENTITY,
    CONF_REGULATOR_OUTPUT_SWITCH,
    CONF_REGULATOR_RESTART_REASON_SENSOR,
    CONF_REGULATOR_SENSOR,
    CONF_REGULATOR_UPTIME_SENSOR,
    CONF_RESCUE,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_TEMPERATURE_SENSOR,
    CONF_USABLE_FACTOR_DEFAULT,
    CONF_VOLUME,
    CONF_WATER_REGION,
    CONF_WORKDAY_ENTITY,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_MAINTENANCE_MARGIN,
    DEFAULT_NAME,
    DEFAULT_PARAMETERS,
    DOMAIN,
    INTEGER_PARAMETERS,
    OPTIONAL_INPUT_KEYS,
    PROBE_POSITIONS,
    SECTION_ADVANCED,
)
from .model.appointments import (
    DAY_HOLIDAY,
    DAY_WORKDAY,
    DEFAULT_LITERS,
    MAX_APPOINTMENTS,
    Appointment,
)
from .model.demand import WEEKDAY_NAMES, DayTypeMode
from .model.tank import DEFAULT_REGULATOR_ACTIVE_MODES
from .model.water import REGIONS
from .solar_forecast import async_forecast_entries

ENERGY_STATE_CLASSES = (SensorStateClass.TOTAL_INCREASING, SensorStateClass.TOTAL)
# A cumulative volume; pulse counters often publish it without a state class.
VOLUME_STATE_CLASSES = (
    SensorStateClass.TOTAL_INCREASING,
    SensorStateClass.TOTAL,
    None,
)
VOLUME_DEVICE_CLASSES = [
    SensorDeviceClass.WATER,
    SensorDeviceClass.VOLUME,
    SensorDeviceClass.VOLUME_STORAGE,
]

# A regulator output is a percentage; some integrations publish it unitless.
REGULATOR_UNITS: tuple[str | None, ...] = (PERCENTAGE, None)

ON_OFF_DOMAINS = [SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN]
# A script is on while it runs (e.g. the one that updates the ESPHome devices).
MAINTENANCE_DOMAINS = [
    "script",
    INPUT_BOOLEAN_DOMAIN,
    BINARY_SENSOR_DOMAIN,
    SWITCH_DOMAIN,
]
STATE_DOMAINS = [INPUT_BOOLEAN_DOMAIN, SWITCH_DOMAIN, BINARY_SENSOR_DOMAIN]

ERROR_ENTITY_NOT_FOUND = "entity_not_found"
ERROR_INVALID_UNIT = "invalid_unit"
ERROR_INVALID_STATE_CLASS = "invalid_state_class"
ERROR_NOT_NUMERIC = "not_numeric"

CONF_APPOINTMENT = "appointment"
CONF_APPOINTMENT_NAME = "name"
CONF_APPOINTMENT_TIME = "time"
CONF_APPOINTMENT_DAYS = "days"
CONF_APPOINTMENT_LITERS = "liters"
CONF_APPOINTMENT_ENABLED = "enabled"
CONF_APPOINTMENT_DURATION = "duration"


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
        vol.Optional(CONF_AMBIENT_SENSOR): _sensor_selector(
            SensorDeviceClass.TEMPERATURE
        ),
        vol.Optional(CONF_WORKDAY_ENTITY): EntitySelector(
            EntitySelectorConfig(domain=BINARY_SENSOR_DOMAIN, integration="workday")
        ),
        vol.Optional(CONF_REGULATOR_OUTPUT_SWITCH): _entity_selector(
            [SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN]
        ),
        vol.Optional(CONF_REGULATOR_MODE_ENTITY): _entity_selector(
            [SELECT_DOMAIN, INPUT_SELECT_DOMAIN]
        ),
        vol.Optional(CONF_REGULATOR_ACTIVE_MODES): SelectSelector(
            SelectSelectorConfig(
                options=list(DEFAULT_REGULATOR_ACTIVE_MODES),
                multiple=True,
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_REGULATOR_UPTIME_SENSOR): _sensor_selector(),
        vol.Optional(CONF_REGULATOR_RESTART_REASON_SENSOR): _sensor_selector(),
        vol.Optional(CONF_HOT_WATER_VOLUME_SENSOR): EntitySelector(
            EntitySelectorConfig(
                domain=SENSOR_DOMAIN, device_class=VOLUME_DEVICE_CLASSES
            )
        ),
        vol.Optional(CONF_HOT_WATER_FLOW_SENSOR): _sensor_selector(),
        vol.Optional(CONF_MAINTENANCE_ENTITY): _entity_selector(MAINTENANCE_DOMAINS),
        vol.Optional(
            CONF_MAINTENANCE_MARGIN, default=DEFAULT_MAINTENANCE_MARGIN
        ): _number_selector(0, 120, 1, UnitOfTime.MINUTES),
    }
)


async def _optional_inputs_schema(hass: HomeAssistant) -> vol.Schema:
    """Optional inputs, with the config entries that give a solar forecast."""
    entries = await async_forecast_entries(hass)
    if not entries:
        return OPTIONAL_INPUTS_SCHEMA
    return OPTIONAL_INPUTS_SCHEMA.extend(
        {
            vol.Optional(CONF_SOLAR_FORECAST_ENTRIES): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=entry_id, label=label)
                        for entry_id, label in entries
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


PARAMETERS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_VOLUME): _number_selector(10, 500, 1, UnitOfVolume.LITERS),
        vol.Required(CONF_WATER_REGION): SelectSelector(
            SelectSelectorConfig(
                options=list(REGIONS),
                translation_key=CONF_WATER_REGION,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_PROBE_POSITION): SelectSelector(
            SelectSelectorConfig(
                options=list(PROBE_POSITIONS),
                translation_key=CONF_PROBE_POSITION,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_RESCUE): BooleanSelector(),
        vol.Required(CONF_BOOST_AS_SHORTAGE): BooleanSelector(),
        vol.Required(SECTION_ADVANCED): section(
            vol.Schema(
                {
                    vol.Required(CONF_RATED_POWER): _number_selector(
                        100, 6000, 10, UnitOfPower.WATT
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
                    vol.Required(CONF_PROFILE_MODE): SelectSelector(
                        SelectSelectorConfig(
                            options=[mode.value for mode in DayTypeMode],
                            translation_key=CONF_PROFILE_MODE,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_PROFILE_DAYS): _number_selector(4, 60, 1, "d"),
                    vol.Required(CONF_REGULATOR_MARGIN): _number_selector(
                        1, 30, 1, UnitOfTime.MINUTES
                    ),
                    vol.Required(CONF_PROBE_VOLUME_BELOW): _number_selector(
                        0, 90, 5, PERCENTAGE
                    ),
                    vol.Required(CONF_BILLING_DAY): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=28, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(CONF_ENERGY_TERM_FACTOR): NumberSelector(
                        NumberSelectorConfig(
                            min=0.1, max=2.0, step=0.01, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(CONF_USABLE_FACTOR_DEFAULT): NumberSelector(
                        NumberSelectorConfig(
                            min=0.5, max=1.0, step=0.01, mode=NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
            {"collapsed": True},
        ),
    }
)


def _appointment_schema(with_workday: bool) -> vol.Schema:
    days = [*WEEKDAY_NAMES, *((DAY_WORKDAY, DAY_HOLIDAY) if with_workday else ())]
    return vol.Schema(
        {
            vol.Required(CONF_APPOINTMENT_NAME): TextSelector(),
            vol.Required(CONF_APPOINTMENT_TIME): TimeSelector(),
            vol.Required(CONF_APPOINTMENT_DAYS): SelectSelector(
                SelectSelectorConfig(
                    options=days,
                    multiple=True,
                    translation_key=CONF_APPOINTMENT_DAYS,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_APPOINTMENT_DURATION): _number_selector(
                15, 720, 15, UnitOfTime.MINUTES
            ),
            vol.Required(CONF_APPOINTMENT_LITERS): _number_selector(
                0, 300, 5, UnitOfVolume.LITERS
            ),
            vol.Required(CONF_APPOINTMENT_ENABLED): BooleanSelector(),
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
        CONF_HOT_WATER_VOLUME_SENSOR: (
            VolumeConverter.VALID_UNITS,
            VOLUME_STATE_CLASSES,
        ),
        CONF_HOT_WATER_FLOW_SENSOR: (VolumeFlowRateConverter.VALID_UNITS, None),
    }
    errors: dict[str, str] = {}
    for key, (units, state_classes) in measurement_checks.items():
        if (entity_id := user_input.get(key)) and (
            error := _validate_measurement(hass, entity_id, units, state_classes)
        ):
            errors[key] = error
    for key in (CONF_INJECTION_PRICE_SENSOR,):
        if (entity_id := user_input.get(key)) and (
            error := _validate_numeric(hass, entity_id)
        ):
            errors[key] = error
    for key in (
        CONF_MASTER_ENTITY,
        CONF_OCCUPANCY_ENTITY,
        CONF_WORKDAY_ENTITY,
        CONF_REGULATOR_OUTPUT_SWITCH,
        CONF_REGULATOR_MODE_ENTITY,
        CONF_REGULATOR_UPTIME_SENSOR,
        CONF_REGULATOR_RESTART_REASON_SENSOR,
    ):
        if (entity_id := user_input.get(key)) and (
            error := _validate_exists(hass, entity_id)
        ):
            errors[key] = error
    return errors


def _clean_optional_inputs(user_input: dict[str, Any]) -> dict[str, Any]:
    """Keep only the optional inputs actually selected."""
    cleaned = {
        key: user_input[key] for key in OPTIONAL_INPUT_KEYS if user_input.get(key)
    }
    # The margin is stored only when it is not the default (0 included).
    margin = user_input.get(CONF_MAINTENANCE_MARGIN)
    if margin is None or int(margin) == DEFAULT_MAINTENANCE_MARGIN:
        cleaned.pop(CONF_MAINTENANCE_MARGIN, None)
    else:
        cleaned[CONF_MAINTENANCE_MARGIN] = int(margin)
    return cleaned


def _clean_parameters(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise parameter types coming from number selectors."""
    parameters = dict(user_input)
    for key in INTEGER_PARAMETERS:
        parameters[key] = int(parameters[key])
    return parameters


class TankWiseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the TankWise config flow."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

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
                await _optional_inputs_schema(self.hass), user_input
            ),
            errors=errors,
        )


class TankWiseOptionsFlow(OptionsFlowWithReload):
    """Handle TankWise options: parameters, inputs and hot water appointments."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._editing: str | None = None

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    @property
    def _appointments(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_APPOINTMENTS, []))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what to change."""
        return self.async_show_menu(
            step_id="init", menu_options=["parameters", "inputs", "appointments"]
        )

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the installation parameters (advanced ones folded)."""
        if user_input is not None:
            flat = {
                key: value
                for key, value in user_input.items()
                if key != SECTION_ADVANCED
            }
            flat.update(user_input.get(SECTION_ADVANCED, {}))
            return self._save(**_clean_parameters(flat))

        current = {**DEFAULT_PARAMETERS, **self.config_entry.options}
        suggested = {
            key: value
            for key, value in current.items()
            if key in PARAMETERS_SCHEMA.schema
        }
        suggested[SECTION_ADVANCED] = {key: current[key] for key in ADVANCED_PARAMETERS}
        return self.async_show_form(
            step_id="parameters",
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
                options = {
                    key: value
                    for key, value in self.config_entry.options.items()
                    if key not in OPTIONAL_INPUT_KEYS
                }
                return self.async_create_entry(
                    data={**options, **_clean_optional_inputs(user_input)}
                )

        return self.async_show_form(
            step_id="inputs",
            data_schema=self.add_suggested_values_to_schema(
                await _optional_inputs_schema(self.hass),
                user_input if user_input is not None else self.config_entry.options,
            ),
            errors=errors,
        )

    async def async_step_appointments(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add, edit or remove hot water appointments."""
        options = ["add_appointment"]
        if self._appointments:
            options += ["edit_appointment", "remove_appointment"]
        return self.async_show_menu(step_id="appointments", menu_options=options)

    async def async_step_add_appointment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an appointment (at most six)."""
        if len(self._appointments) >= MAX_APPOINTMENTS:
            return self.async_abort(reason="too_many_appointments")
        self._editing = None
        return await self.async_step_appointment()

    async def async_step_edit_appointment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the appointment to edit."""
        if user_input is not None:
            self._editing = user_input[CONF_APPOINTMENT]
            return await self.async_step_appointment()
        return self.async_show_form(
            step_id="edit_appointment",
            data_schema=vol.Schema(
                {vol.Required(CONF_APPOINTMENT): self._appointment_selector()}
            ),
        )

    async def async_step_remove_appointment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the appointments to remove."""
        if user_input is not None:
            removed = set(user_input[CONF_APPOINTMENT])
            return self._save(
                **{
                    CONF_APPOINTMENTS: [
                        item
                        for item in self._appointments
                        if item.get("id") not in removed
                    ]
                }
            )
        return self.async_show_form(
            step_id="remove_appointment",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APPOINTMENT): self._appointment_selector(
                        multiple=True
                    )
                }
            ),
        )

    def _appointment_selector(self, *, multiple: bool = False) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=item["id"], label=f"{item['name']} ({item['time']})"
                    )
                    for item in self._appointments
                ],
                multiple=multiple,
                mode=SelectSelectorMode.LIST,
            )
        )

    async def async_step_appointment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the fields of an appointment."""
        with_workday = bool(self.config_entry.options.get(CONF_WORKDAY_ENTITY))
        errors: dict[str, str] = {}
        current = next(
            (item for item in self._appointments if item.get("id") == self._editing),
            None,
        )
        if user_input is not None:
            if not user_input[CONF_APPOINTMENT_DAYS]:
                errors[CONF_APPOINTMENT_DAYS] = "no_days"
            elif not str(user_input[CONF_APPOINTMENT_NAME]).strip():
                errors[CONF_APPOINTMENT_NAME] = "no_name"
            else:
                item = Appointment(
                    id=current["id"] if current else uuid4().hex[:8],
                    name=str(user_input[CONF_APPOINTMENT_NAME]).strip(),
                    time=time.fromisoformat(user_input[CONF_APPOINTMENT_TIME]),
                    days=frozenset(user_input[CONF_APPOINTMENT_DAYS]),
                    liters=float(user_input[CONF_APPOINTMENT_LITERS]),
                    enabled=bool(user_input[CONF_APPOINTMENT_ENABLED]),
                    duration=timedelta(
                        minutes=float(user_input[CONF_APPOINTMENT_DURATION])
                    ),
                ).as_dict()
                items = [
                    item if existing.get("id") == item["id"] else existing
                    for existing in self._appointments
                ]
                if current is None:
                    items.append(item)
                return self._save(**{CONF_APPOINTMENTS: items})

        if user_input is not None:
            suggested = user_input
        elif current is not None:
            suggested = {
                CONF_APPOINTMENT_NAME: current["name"],
                CONF_APPOINTMENT_TIME: f"{current['time']}:00",
                CONF_APPOINTMENT_DAYS: current["days"],
                CONF_APPOINTMENT_LITERS: current["liters"],
                CONF_APPOINTMENT_DURATION: current.get("duration", 60),
                CONF_APPOINTMENT_ENABLED: current.get("enabled", True),
            }
        else:
            suggested = {
                CONF_APPOINTMENT_DAYS: list(WEEKDAY_NAMES),
                CONF_APPOINTMENT_LITERS: DEFAULT_LITERS,
                CONF_APPOINTMENT_DURATION: 60,
                CONF_APPOINTMENT_ENABLED: True,
            }
        return self.async_show_form(
            step_id="appointment",
            data_schema=self.add_suggested_values_to_schema(
                _appointment_schema(with_workday), suggested
            ),
            errors=errors,
        )
