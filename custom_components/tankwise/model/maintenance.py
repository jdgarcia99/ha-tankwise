"""Maintenance windows: readings are not trusted while devices update or restart.

A firmware update of the ESPHome devices (or any restart) leaves the probe, the
power meter or the regulator without data for a few minutes, or with values
that do not describe the tank. During a maintenance window TankWise keeps
running its plan and its safeties, but learns nothing and counts nothing
against the plan.

A window comes from:

- the maintenance entity (a script while it runs, a helper...): while it is on
  and for the margin after it goes off;
- a device of the inputs that becomes unavailable: while it is unavailable, up
  to ``MAX_OUTAGE`` (beyond it is a real outage, handled by the usual rules for
  unknown data), and for the margin after it returns within that time;
- a device that restarts (its uptime goes back): for the margin after it.

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

MAX_OUTAGE = timedelta(minutes=15)
# Uptime in seconds that falls by more than this is a restart; a boot time
# that moves later by more than this is a restart too.
UPTIME_TOLERANCE_S = 5.0
BOOT_TIME_TOLERANCE = timedelta(seconds=60)

SOURCE_ENTITY = "entity"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_RESTART = "restart"
SOURCES = (SOURCE_ENTITY, SOURCE_UNAVAILABLE, SOURCE_RESTART)


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    """Whether a maintenance window is open, why and until when."""

    active: bool = False
    source: str | None = None
    # End of the window; None while it lasts with no known end (the entity is
    # still on, or a device is still unavailable).
    until: datetime | None = None
    devices: tuple[str, ...] = ()


@dataclass(slots=True)
class _Device:
    unavailable_since: datetime | None = None
    uptime: float | None = None  # seconds since boot
    boot: datetime | None = None  # boot time


@dataclass(slots=True)
class MaintenanceTracker:
    """Follow the maintenance entity and the devices of the inputs."""

    margin: timedelta = timedelta(minutes=10)
    _entity_on: bool = False
    _entity_until: datetime | None = None
    _devices: dict[str, _Device] = field(default_factory=dict)
    # Windows after a device came back or restarted: device -> (end, source).
    _after: dict[str, tuple[datetime, str]] = field(default_factory=dict)

    def entity(self, now: datetime, on: bool | None) -> None:
        """The maintenance entity changed; None (unknown) does not open a window."""
        if on:
            self._entity_on = True
        elif self._entity_on:
            self._entity_on = False
            self._entity_until = now + self.margin

    def availability(self, device: str, now: datetime, available: bool) -> None:
        """A device of the inputs became unavailable or came back."""
        state = self._devices.setdefault(device, _Device())
        if not available:
            if state.unavailable_since is None:
                state.unavailable_since = now
            return
        since = state.unavailable_since
        state.unavailable_since = None
        if since is not None and now - since <= MAX_OUTAGE:
            self._open_after(device, now, SOURCE_UNAVAILABLE)

    def uptime(
        self, device: str, now: datetime, value: float | datetime | None
    ) -> None:
        """A device's uptime sensor reported: seconds since boot, or the boot time."""
        if value is None:
            return  # unavailable: its return is followed by `availability`
        state = self._devices.setdefault(device, _Device())
        if isinstance(value, datetime):
            restarted = (
                state.boot is not None and value - state.boot > BOOT_TIME_TOLERANCE
            )
            state.boot = value
        else:
            restarted = (
                state.uptime is not None and value < state.uptime - UPTIME_TOLERANCE_S
            )
            state.uptime = value
        if restarted:
            self._open_after(device, now, SOURCE_RESTART)

    def _open_after(self, device: str, now: datetime, source: str) -> None:
        end = now + self.margin
        previous = self._after.get(device)
        if previous is None or previous[0] < end:
            self._after[device] = (end, source)

    def status(self, now: datetime) -> MaintenanceStatus:
        """The window open now, if any."""
        if self._entity_on:
            return MaintenanceStatus(True, SOURCE_ENTITY)
        unavailable = sorted(
            device
            for device, state in self._devices.items()
            if state.unavailable_since is not None
            and now - state.unavailable_since <= MAX_OUTAGE
        )
        if unavailable:
            return MaintenanceStatus(True, SOURCE_UNAVAILABLE, None, tuple(unavailable))
        self._after = {
            device: item for device, item in self._after.items() if item[0] > now
        }
        windows: list[tuple[datetime, str, str]] = [
            (end, source, device) for device, (end, source) in self._after.items()
        ]
        if self._entity_until is not None and self._entity_until > now:
            windows.append((self._entity_until, SOURCE_ENTITY, ""))
        if not windows:
            return MaintenanceStatus()
        end, source, _device = max(windows)
        devices = tuple(sorted(device for _end, _source, device in windows if device))
        return MaintenanceStatus(True, source, end, devices)
