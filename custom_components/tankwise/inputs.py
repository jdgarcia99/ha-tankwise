"""Interpretation of input entity states (numbers, units and on/off)."""

from __future__ import annotations

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import State
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

ON_STATES = frozenset({STATE_ON, "home", "true"})
OFF_STATES = frozenset({"off", "not_home", "away", "false"})

# Kind of measurement -> (converter, target unit)
MEASUREMENTS: dict[str, tuple[type[BaseUnitConverter], str]] = {
    "temperature": (TemperatureConverter, UnitOfTemperature.CELSIUS),
    "power": (PowerConverter, UnitOfPower.WATT),
    "energy": (EnergyConverter, UnitOfEnergy.KILO_WATT_HOUR),
}


class InputError(ValueError):
    """A state cannot be interpreted (the message says why)."""


def parse_numeric(value: str) -> float:
    """Return a numeric state value or raise InputError."""
    if value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        raise InputError(value)
    try:
        number = float(value)
    except ValueError as err:
        raise InputError(f"non-numeric state {value!r}") from err
    if number != number:  # NaN
        raise InputError("NaN")
    return number


def parse_measurement(value: str, unit: str | None, kind: str) -> float:
    """Return a measurement converted to °C, W or kWh, or raise InputError."""
    converter, target = MEASUREMENTS[kind]
    number = parse_numeric(value)
    if unit not in converter.VALID_UNITS:
        raise InputError(f"unsupported unit {unit!r}")
    return converter.convert(number, unit, target)


def parse_bool(value: str) -> bool:
    """Return an on/off state or raise InputError."""
    if value in ON_STATES:
        return True
    if value in OFF_STATES:
        return False
    raise InputError(value)


def state_unit(state: State | None) -> str | None:
    """Return the unit of measurement of a state."""
    if state is None:
        return None
    return state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
