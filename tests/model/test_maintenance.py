"""Maintenance windows: nothing is learned while devices update or restart."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tankwise_model.maintenance import (
    SOURCE_ENTITY,
    SOURCE_RESTART,
    SOURCE_UNAVAILABLE,
    MaintenanceTracker,
)
from tankwise_model.tank import TankModel

from .simulator import PARAMS, TankSimulator, drive

T0 = datetime(2026, 11, 25, 8, 55, tzinfo=UTC)
MARGIN = timedelta(minutes=10)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_script_running_and_margin_after() -> None:
    """The update script runs 09:55-09:56; the devices reboot until ~10:01."""
    tracker = MaintenanceTracker(MARGIN)
    assert not tracker.status(_at(0)).active
    tracker.entity(_at(0), True)
    status = tracker.status(_at(0.5))
    assert (status.active, status.source, status.until) == (True, SOURCE_ENTITY, None)
    tracker.entity(_at(1), False)
    status = tracker.status(_at(6))  # the Shelly without response, inside the margin
    assert (status.active, status.source, status.until) == (
        True,
        SOURCE_ENTITY,
        _at(11),
    )
    assert not tracker.status(_at(11)).active


def test_unknown_entity_opens_nothing() -> None:
    tracker = MaintenanceTracker(MARGIN)
    tracker.entity(_at(0), None)
    assert not tracker.status(_at(0)).active


def test_short_outage_is_maintenance() -> None:
    tracker = MaintenanceTracker(MARGIN)
    tracker.availability("probe", _at(0), available=False)
    status = tracker.status(_at(3))
    assert (status.active, status.source, status.devices) == (
        True,
        SOURCE_UNAVAILABLE,
        ("probe",),
    )
    tracker.availability("probe", _at(4), available=True)
    status = tracker.status(_at(5))
    assert (status.source, status.until) == (SOURCE_UNAVAILABLE, _at(14))
    assert not tracker.status(_at(14)).active


def test_long_outage_is_not_maintenance() -> None:
    """Beyond 15 minutes it is a real outage: the usual unknown-data rules."""
    tracker = MaintenanceTracker(MARGIN)
    tracker.availability("probe", _at(0), available=False)
    assert tracker.status(_at(14)).active
    assert not tracker.status(_at(16)).active
    tracker.availability("probe", _at(20), available=True)
    assert not tracker.status(_at(21)).active


def test_restart_from_uptime_in_seconds() -> None:
    tracker = MaintenanceTracker(MARGIN)
    tracker.uptime("diverter", _at(0), 31_000.0)
    tracker.uptime("diverter", _at(1), 31_060.0)
    tracker.uptime("diverter", _at(1.5), 31_058.0)  # jitter: not a restart
    assert not tracker.status(_at(2)).active
    tracker.uptime("diverter", _at(3), 12.0)
    status = tracker.status(_at(4))
    assert (status.source, status.until, status.devices) == (
        SOURCE_RESTART,
        _at(13),
        ("diverter",),
    )
    tracker.uptime("diverter", _at(4), None)  # unavailable: kept as it was
    tracker.uptime("diverter", _at(5), 130.0)
    assert tracker.status(_at(12)).active and not tracker.status(_at(13)).active


def test_restart_from_boot_time() -> None:
    tracker = MaintenanceTracker(MARGIN)
    boot = datetime(2026, 11, 22, 20, 12, 36, tzinfo=UTC)
    tracker.uptime("pump", _at(0), boot)
    tracker.uptime("pump", _at(1), boot + timedelta(seconds=1))  # rounding
    assert not tracker.status(_at(1)).active
    tracker.uptime("pump", _at(2), _at(1.9))
    assert tracker.status(_at(2)).source == SOURCE_RESTART


def _learning(model: TankModel) -> tuple:
    data = model.as_dict()
    return (
        data["heater_power_samples"],
        data["loss_windows"],
        len(data["capacity_samples"]),
        data["calibration_count"],
    )


def test_model_learns_nothing_in_a_maintenance_window() -> None:
    """A full heat-up and its cut-off inside the window: no power, losses, k_cap."""
    sim = TankSimulator(temperature=40.0)
    model = TankModel()
    drive(model, sim, 30)
    before = _learning(model)
    for _ in range(240):
        model.update(replace(sim.step(regulator=100), maintenance=True), PARAMS)
    for _ in range(120):
        model.update(replace(sim.step(), maintenance=True), PARAMS)
    assert _learning(model) == before
    # The same outside the window learns
    reference = TankModel()
    sim = TankSimulator(temperature=40.0)
    drive(reference, sim, 30)
    drive(reference, sim, 240, regulator=100)
    learned = _learning(reference)
    assert learned[0] > before[0] and learned[3] > before[3]


def test_draws_in_a_maintenance_window_are_not_shortages() -> None:
    sim = TankSimulator(temperature=35.0)
    model = TankModel()
    drive(model, sim, 60)
    for _ in range(15):
        model.update(replace(sim.step(draw_kw=9.0), maintenance=True), PARAMS)
    drive(model, sim, 60)
    draws = model.consumptions
    assert draws, "the draw is still detected"
    assert all(draw.maintenance and not draw.shortage for draw in draws)
    assert TankModel.from_dict(model.as_dict()).consumptions[-1].maintenance
