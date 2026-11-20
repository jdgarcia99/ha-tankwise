"""Controller: orders for the regulator, safeties and degraded rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tankwise_model.control import (
    Action,
    ControlInput,
    Controller,
    DegradedTarget,
    Due,
    Order,
    Reason,
    degraded_target,
    heating_time,
)

TZ = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 11, 18, 14, 0, tzinfo=TZ)
MARGIN = timedelta(minutes=2)
CAPACITY = 100 * 0.001163


def _input(minute: float = 0, **changes) -> ControlInput:
    values = {
        "now": T0 + timedelta(minutes=minute),
        "active": True,
        "temperature": 45.0,
        "power": 0.0,
        "manual": False,
        "heater_switch": True,
        "plan_heat": False,
        "degraded": None,
        "max_temperature": 72.0,
        "power_threshold": 50.0,
        "regulator_margin": MARGIN,
    }
    values.update(changes)
    return ControlInput(**values)


def _orders(controller: Controller, data: ControlInput) -> list[Order]:
    return controller.step(data).orders


ON = [Order(Action.MANUAL_ON, Reason.PLAN)]


def test_plan_heating_is_ordered_once_and_released_at_the_end() -> None:
    controller = Controller()
    assert _orders(controller, _input(0)) == []
    assert _orders(controller, _input(1, plan_heat=True)) == ON
    assert controller.owns_manual
    # The order took effect: nothing more while heating.
    assert _orders(controller, _input(2, plan_heat=True, manual=True, power=1500)) == []
    assert _orders(controller, _input(3, plan_heat=False, manual=True, power=1500)) == [
        Order(Action.MANUAL_OFF, Reason.PLAN_DONE)
    ]
    assert not controller.owns_manual


def test_order_is_retried_if_it_did_not_take_effect() -> None:
    controller = Controller()
    assert _orders(controller, _input(0, plan_heat=True)) == ON
    assert _orders(controller, _input(1, plan_heat=True)) == []
    assert _orders(controller, _input(2, plan_heat=True)) == ON


def test_external_order_is_respected_until_the_plan_changes() -> None:
    controller = Controller()
    assert _orders(controller, _input(0, plan_heat=True)) == ON
    assert controller.on_manual_change(T0 + timedelta(seconds=2), True)  # ours
    # An automation switches the manual mode off: not fought.
    assert not controller.on_manual_change(T0 + timedelta(minutes=5), False)
    for minute in range(6, 15):
        assert _orders(controller, _input(minute, plan_heat=True)) == []
    # Next planned heating after a pause: ordered again.
    assert _orders(controller, _input(15)) == []
    assert _orders(controller, _input(30, plan_heat=True)) == ON


def test_external_manual_mode_outside_the_plan_is_left_alone() -> None:
    controller = Controller()
    assert _orders(controller, _input(0)) == []
    controller.on_manual_change(T0 + timedelta(seconds=30), True)
    assert _orders(controller, _input(1, manual=True, power=1500)) == []
    assert not controller.owns_manual


def test_thermostat_cutoff_ends_the_manual_mode_and_latches() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    heating = {"plan_heat": True, "manual": True, "temperature": 68.0}
    assert _orders(controller, _input(1, power=1500, **heating)) == []
    assert _orders(controller, _input(2, power=1.6, **heating)) == []
    assert _orders(controller, _input(4, power=1.6, **heating)) == []
    assert _orders(controller, _input(5, power=1.6, **heating)) == [
        Order(Action.MANUAL_OFF, Reason.CUTOFF)
    ]
    # The plan still asks for heat, but the tank is full.
    assert _orders(controller, _input(6, plan_heat=True, temperature=67.0)) == []
    assert _orders(controller, _input(30, plan_heat=True, temperature=66.0)) == []
    # Cooled down 3 °C: heating is allowed again.
    assert _orders(controller, _input(60, plan_heat=True, temperature=64.5)) == ON


def test_safety_maximum_temperature_for_one_minute() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    hot = {"plan_heat": True, "manual": True, "power": 1500, "temperature": 73.0}
    assert _orders(controller, _input(1, **hot)) == []
    assert _orders(controller, _input(2, **hot)) == [
        Order(Action.MANUAL_OFF, Reason.MAX_TEMPERATURE)
    ]


def test_safeties_apply_to_external_manual_mode_too() -> None:
    controller = Controller()
    hot = {"manual": True, "power": 1500, "temperature": 73.0}
    assert _orders(controller, _input(0, **hot)) == []
    assert _orders(controller, _input(1, **hot)) == [
        Order(Action.MANUAL_OFF, Reason.MAX_TEMPERATURE)
    ]


def test_more_than_two_hours_in_manual_mode() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    heating = {"plan_heat": True, "manual": True, "power": 1500}
    for minute in range(1, 120):
        assert _orders(controller, _input(minute, **heating)) == []
    assert _orders(controller, _input(121, **heating)) == [
        Order(Action.MANUAL_OFF, Reason.TOO_LONG)
    ]
    # Fifteen-minute pause, then the plan may heat again.
    assert _orders(controller, _input(130, plan_heat=True)) == []
    assert _orders(controller, _input(137, plan_heat=True)) == ON


def test_probe_lost_ends_the_manual_mode_and_blocks_heating() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    assert _orders(
        controller, _input(1, plan_heat=True, manual=True, power=1500, temperature=None)
    ) == [Order(Action.MANUAL_OFF, Reason.PROBE_LOST)]
    assert _orders(controller, _input(5, plan_heat=True, temperature=None)) == []


def test_mode_without_action_gives_the_regulator_back() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    assert _orders(
        controller, _input(1, active=False, manual=True, power=1500, plan_heat=True)
    ) == [Order(Action.MANUAL_OFF, Reason.MODE)]
    # An external manual mode is not touched in shadow mode.
    assert _orders(controller, _input(2, active=False, manual=True)) == []


def test_regulator_lost_while_heating_cuts_the_heater_switch() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    lost = {"plan_heat": True, "manual": None, "power": 1500}
    assert _orders(controller, _input(1, **lost)) == []
    assert _orders(controller, _input(2, **lost)) == []
    assert _orders(controller, _input(3, **lost)) == [
        Order(Action.HEATER_OFF, Reason.REGULATOR_LOST)
    ]
    assert controller.heater_cut
    assert _orders(controller, _input(4, **lost)) == []
    # Back: automatic first, then the heater switch.
    assert _orders(
        controller, _input(10, manual=True, heater_switch=False, plan_heat=True)
    ) == [
        Order(Action.MANUAL_OFF, Reason.REGULATOR_BACK),
        Order(Action.HEATER_ON, Reason.REGULATOR_BACK),
    ]
    assert not controller.heater_cut


def test_regulator_lost_at_rest_is_not_cut_unless_it_heats() -> None:
    controller = Controller()
    assert _orders(controller, _input(0)) == []
    for minute in range(1, 10):
        assert _orders(controller, _input(minute, manual=None)) == []
    assert _orders(controller, _input(10, manual=None, power=1500)) == [
        Order(Action.HEATER_OFF, Reason.REGULATOR_LOST)
    ]


def test_heater_cut_survives_a_restart() -> None:
    controller = Controller()
    _orders(controller, _input(0, plan_heat=True))
    _orders(controller, _input(1, plan_heat=True, manual=None))
    _orders(controller, _input(4, plan_heat=True, manual=None))
    restored = Controller.from_dict(controller.as_dict())
    assert restored.heater_cut and restored.cuts == controller.cuts
    assert _orders(restored, _input(10, heater_switch=False)) == [
        Order(Action.HEATER_ON, Reason.REGULATOR_BACK)
    ]


def test_repeated_cuts() -> None:
    controller = Controller()
    controller.cuts = [T0 - timedelta(days=8), T0 - timedelta(days=2), T0]
    assert not controller.repeated_cuts(T0)
    controller.cuts.append(T0 + timedelta(hours=1))
    assert controller.repeated_cuts(T0 + timedelta(hours=1))


def test_heater_switch_off_is_reported_not_switched() -> None:
    controller = Controller()
    decision = controller.step(_input(0, heater_switch=False))
    assert decision.heater_switch_off and decision.orders == []


def test_degraded_heating_follows_the_rule() -> None:
    controller = Controller()
    target = DegradedTarget(heat=True, target=60.0, name="Tarde")
    assert _orders(controller, _input(0, plan_heat=None, degraded=target)) == [
        Order(Action.MANUAL_ON, Reason.DEGRADED)
    ]
    assert controller.degraded == target
    done = replace(target, heat=False)
    assert _orders(
        controller, _input(1, plan_heat=None, degraded=done, manual=True, power=1500)
    ) == [Order(Action.MANUAL_OFF, Reason.DEGRADED_DONE)]


# ----------------------------------------------------------------------
# Degraded rule
# ----------------------------------------------------------------------


def _degraded(now: datetime, temperature: float, dues=(), **changes) -> DegradedTarget:
    values = {
        "use_fallback": not dues,
        "max_temperature": 68.0,
        "heat_capacity": CAPACITY,
        "heater_power_w": 1500.0,
        "capacity_factor": 0.9,
        "heating": False,
    }
    values.update(changes)
    return degraded_target(now, temperature, list(dues), **values)


def test_heating_time() -> None:
    needed = heating_time(
        40.0, 60.0, heat_capacity=CAPACITY, heater_power_w=1500, capacity_factor=0.9
    )
    assert abs(needed.total_seconds() / 3600 - CAPACITY * 20 / (0.9 * 1.5)) < 1e-6
    assert heating_time(
        60, 50, heat_capacity=CAPACITY, heater_power_w=1500, capacity_factor=0.9
    ) == timedelta(0)


def test_degraded_starts_as_early_as_the_power_requires() -> None:
    due = datetime(2026, 11, 18, 17, 0, tzinfo=TZ)
    tarde = Due("Tarde", due, 62.0)
    # 40 -> 62 °C: 2.56 kWh at 1.35 kW effective = 114 min, x1.25 + 15 = 157 min.
    early = _degraded(datetime(2026, 11, 18, 14, 0, tzinfo=TZ), 40.0, [tarde])
    assert not early.heat
    assert early.target == 62.0
    assert early.start is not None
    assert 156 <= (due - early.start).total_seconds() / 60 <= 158
    on_time = _degraded(early.start, 40.0, [tarde])
    assert on_time.heat and on_time.name == "Tarde"


def test_degraded_target_is_limited_to_the_cutoff() -> None:
    due = datetime(2026, 11, 18, 17, 0, tzinfo=TZ)
    target = _degraded(
        datetime(2026, 11, 18, 16, 0, tzinfo=TZ), 50.0, [Due("Tarde", due, 75.0)]
    )
    assert target.heat and target.target == 68.0


def test_degraded_hysteresis() -> None:
    due = datetime(2026, 11, 18, 17, 0, tzinfo=TZ)
    tarde = [Due("Tarde", due, 60.0)]
    now = datetime(2026, 11, 18, 16, 50, tzinfo=TZ)
    assert not _degraded(now, 59.5, tarde).heat  # not 1 °C below yet
    assert _degraded(now, 59.5, tarde, heating=True).heat  # keeps going
    assert not _degraded(now, 60.0, tarde, heating=True).heat


def test_no_fixed_windows_with_appointments() -> None:
    dawn = datetime(2026, 11, 18, 5, 0, tzinfo=TZ)
    later = Due("Tarde", datetime(2026, 11, 18, 17, 0, tzinfo=TZ), 55.0)
    assert not _degraded(dawn, 40.0, [later]).heat
    # An appointment whose time is past is not a reason to heat either.
    assert not _degraded(dawn, 40.0, [], use_fallback=False).heat


def test_fixed_windows_without_appointments() -> None:
    dawn = _degraded(datetime(2026, 11, 18, 5, 0, tzinfo=TZ), 40.0)
    assert dawn.heat and dawn.target == 50.0 and dawn.name is None
    assert not _degraded(datetime(2026, 11, 18, 5, 0, tzinfo=TZ), 50.0).heat
    afternoon = _degraded(datetime(2026, 11, 18, 15, 30, tzinfo=TZ), 60.0)
    assert afternoon.heat and afternoon.target == 68.0
    assert not _degraded(datetime(2026, 11, 18, 12, 0, tzinfo=TZ), 30.0).heat
