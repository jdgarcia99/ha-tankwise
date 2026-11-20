"""Logbook messages of TankWise (English and Spanish)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__package__)

LOGBOOK_DOMAIN = "logbook"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "rescue": "Rescue recharge planned for {name} ({time}): {kwh} kWh, {cost} €",
        "manual_on": "The regulator manual mode was switched on outside the plan",
        "manual_off": (
            "The regulator manual mode was switched off during a planned heating"
        ),
        "plan": "Heating from the grid until {end} (plan: {kwh} kWh at {price} €/kWh)",
        "plan_done": "Regulator back to automatic: the planned heating is over",
        "degraded": "Degraded mode: heating to {target} °C for {name} ({time})",
        "degraded_fixed": "Degraded mode: heating to {target} °C until {time}",
        "degraded_done": "Degraded mode: heating finished at {temperature} °C",
        "mode": "Regulator back to automatic: mode changed to {mode}",
        "max_temperature": (
            "Safety: {temperature} °C is above the maximum of {max} °C, "
            "regulator back to automatic"
        ),
        "cutoff": "Thermostat cut-off at {temperature} °C, regulator back to automatic",
        "too_long": (
            "Safety: more than 2 hours in manual mode, "
            "regulator back to automatic for 15 minutes"
        ),
        "probe_lost": (
            "Safety: the tank temperature is not available, regulator back to automatic"
        ),
        "regulator_lost": (
            "The regulator has been unavailable for {minutes} min: "
            "heater switch opened as a last resort"
        ),
        "regulator_back": (
            "The regulator is back: set to automatic and heater switch closed again"
        ),
        "degraded_on_probe": (
            "Degraded mode: the tank temperature is not available, "
            "no grid heating until it is back"
        ),
        "degraded_on_no_plan": (
            "Degraded mode: there is no plan (prices or tank model missing), "
            "simple rule before each appointment"
        ),
        "degraded_off": "Degraded mode over: back to the plan",
        "anti_legionella_done": "Anti-legionella cycle done: 60 °C or more for an hour",
        "anti_legionella_impossible": (
            "The anti-legionella cycle cannot be planned: the thermostat cuts off "
            "at {cutoff} °C, below 60 °C"
        ),
        "sell": (
            "Selling the solar surplus ({price} €/kWh pays more than heating "
            "later): regulator output off"
        ),
        "sell_done": "Regulator output on again: the surplus heats the water",
        "boost_started": "Boost: heating to {target} °C{limit}",
        "boost_limit": " (at most until {time})",
        "boost_not_needed": (
            "Boost not needed: the water is already at {temperature} °C "
            "(target {target} °C)"
        ),
        "boost_finished": (
            "Boost finished ({reason}) at {temperature} °C: {kwh} kWh, {cost} €"
        ),
        "boost_reason_target": "target reached",
        "boost_reason_thermostat": "thermostat cut-off",
        "boost_reason_duration": "maximum duration",
        "boost_reason_safety": "safety",
        "boost_reason_cancelled": "cancelled",
    },
    "es": {
        "rescue": (
            "Recarga de rescate prevista para {name} ({time}): {kwh} kWh, {cost} €"
        ),
        "manual_on": "El modo manual del regulador se encendió fuera del plan",
        "manual_off": (
            "El modo manual del regulador se apagó durante un calentamiento planificado"
        ),
        "plan": "Calentando de red hasta las {end} (plan: {kwh} kWh a {price} €/kWh)",
        "plan_done": "Regulador en automático: terminó el calentamiento planificado",
        "degraded": "Modo degradado: calentando hasta {target} °C para {name} ({time})",
        "degraded_fixed": (
            "Modo degradado: calentando hasta {target} °C hasta las {time}"
        ),
        "degraded_done": "Modo degradado: calentamiento terminado a {temperature} °C",
        "mode": "Regulador en automático: el modo pasó a {mode}",
        "max_temperature": (
            "Seguridad: {temperature} °C supera el máximo de {max} °C, "
            "regulador en automático"
        ),
        "cutoff": "Corte del termostato a {temperature} °C, regulador en automático",
        "too_long": (
            "Seguridad: más de 2 horas en modo manual, "
            "regulador en automático durante 15 minutos"
        ),
        "probe_lost": (
            "Seguridad: la temperatura del termo no está disponible, "
            "regulador en automático"
        ),
        "regulator_lost": (
            "El regulador lleva {minutes} min sin estar disponible: "
            "se abre el interruptor del termo como último recurso"
        ),
        "regulator_back": (
            "El regulador ha vuelto: en automático y el interruptor del termo "
            "cerrado de nuevo"
        ),
        "degraded_on_probe": (
            "Modo degradado: la temperatura del termo no está disponible, "
            "no se calienta de red hasta que vuelva"
        ),
        "degraded_on_no_plan": (
            "Modo degradado: no hay plan (faltan precios o el modelo del termo), "
            "regla simple antes de cada cita"
        ),
        "degraded_off": "Fin del modo degradado: se vuelve al plan",
        "anti_legionella_done": (
            "Ciclo antilegionela hecho: 60 °C o más durante una hora"
        ),
        "anti_legionella_impossible": (
            "No se puede planificar el ciclo antilegionela: el termostato corta a "
            "{cutoff} °C, por debajo de 60 °C"
        ),
        "sell": (
            "Vendiendo el excedente solar ({price} €/kWh paga más que calentar "
            "después): salida del regulador apagada"
        ),
        "sell_done": "Salida del regulador encendida: el excedente calienta el agua",
        "boost_started": "Boost: calentando hasta {target} °C{limit}",
        "boost_limit": " (como mucho hasta las {time})",
        "boost_not_needed": (
            "Boost innecesario: el agua ya está a {temperature} °C "
            "(objetivo {target} °C)"
        ),
        "boost_finished": (
            "Boost terminado ({reason}) a {temperature} °C: {kwh} kWh, {cost} €"
        ),
        "boost_reason_target": "objetivo alcanzado",
        "boost_reason_thermostat": "corte del termostato",
        "boost_reason_duration": "duración máxima",
        "boost_reason_safety": "seguridad",
        "boost_reason_cancelled": "cancelado",
    },
}


def message(hass: HomeAssistant, key: str, /, **values: Any) -> str:
    """Return a message in the language of Home Assistant."""
    language = "es" if (hass.config.language or "").startswith("es") else "en"
    return MESSAGES[language][key].format(**values)


def log(hass: HomeAssistant, title: str, key: str, /, **values: Any) -> None:
    """Write a logbook entry (if the logbook is loaded) and an INFO log."""
    text = message(hass, key, **values)
    _LOGGER.info(text)
    if LOGBOOK_DOMAIN not in hass.config.components:
        return
    from homeassistant.components.logbook import async_log_entry

    async_log_entry(hass, title, text, DOMAIN)
