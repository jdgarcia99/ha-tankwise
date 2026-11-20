#!/usr/bin/env python3
"""Export the history of the TankWise inputs from the Home Assistant REST API.

The export feeds the simulation bench (``tests/simulation``). For each role
(see ``ROLES``) it downloads every state change of the selected entity in the
period, day by day, and writes one CSV per role:

    last_changed,state,attributes

``last_changed`` is an ISO 8601 UTC timestamp and ``attributes`` a JSON
object, only filled for the roles whose attributes are needed (the PVPC and
injection prices publish the hourly prices as attributes). A ``manifest.json``
next to the CSV files records the entity of each role, its unit, the number of
states and the first and last timestamps.

The Home Assistant URL and a long-lived access token are read from the
``HA_URL`` and ``HA_TOKEN`` environment variables (or from a ``.env`` file
with those two keys). They are never printed nor written to disk.

Example:

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...   # Profile → Security → Long-lived access tokens
    python tools/export_history.py \\
        --start 2026-11-07 --end 2026-11-20 \\
        --entity temperature=sensor.water_heater_temperature \\
        --entity power=sensor.water_heater_power \\
        ...

The output goes to ``tests/fixtures/history_<start>_<end>/`` by default.
Home Assistant keeps 10 days of history unless ``recorder: purge_keep_days``
is raised: the script warns when an entity has no data for the first days.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

URL_ENV = "HA_URL"
TOKEN_ENV = "HA_TOKEN"
DEFAULT_ENV_FILE = ".env"
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
TIMEOUT_S = 120
RETENTION_WARNING = timedelta(hours=6)

# Role -> True when the state attributes must be kept.
ROLES: dict[str, bool] = {
    "temperature": False,
    "power": False,
    "energy": False,
    "heater_switch": False,
    "manual_mode": False,
    "regulator": False,
    "master": False,
    "occupancy": False,
    "workday": False,
    "price": True,
    "injection_price": True,
    "grid_import": False,
    "grid_export": False,
    "export_power": False,
    "solar_forecast": True,
    "ambient": False,
}
REQUIRED_ROLES = ("temperature", "power")
CSV_COLUMNS = ("last_changed", "state", "attributes")

Opener = Callable[..., Any]


class ExportError(Exception):
    """The export cannot be completed (the message says why)."""


@dataclass(slots=True)
class RoleSummary:
    """What was exported for one role."""

    entity_id: str
    unit: str | None = None
    states: int = 0
    first: str | None = None
    last: str | None = None
    warnings: list[str] = field(default_factory=list)


class HomeAssistantClient:
    """Minimal client for the Home Assistant REST API."""

    def __init__(self, base_url: str, token: str, opener: Opener | None = None) -> None:
        """Initialise the client."""
        if not base_url.startswith(("http://", "https://")):
            raise ExportError(f"{URL_ENV} must start with http:// or https://")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._open = opener or urllib.request.urlopen

    def get(self, path: str, query: Mapping[str, str | None] | None = None) -> Any:
        """GET a JSON document. ``None`` query values are bare flags."""
        url = f"{self._base_url}{path}"
        if query:
            parts = [
                urllib.parse.quote(key)
                if value is None
                else f"{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                for key, value in query.items()
            ]
            url = f"{url}?{'&'.join(parts)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise ExportError("Home Assistant rejected the token (401)") from err
            if err.code == 404:
                raise ExportError(f"not found: {path}") from err
            raise ExportError(f"HTTP {err.code} for {path}") from err
        except urllib.error.URLError as err:
            raise ExportError(f"cannot reach Home Assistant: {err.reason}") from err

    def time_zone(self) -> str:
        """Return the time zone configured in Home Assistant."""
        return str(self.get("/api/config")["time_zone"])

    def unit(self, entity_id: str) -> str | None:
        """Return the current unit of measurement of an entity."""
        try:
            state = self.get(f"/api/states/{entity_id}")
        except ExportError:
            return None
        return state.get("attributes", {}).get("unit_of_measurement")

    def history(
        self, entity_id: str, start: datetime, end: datetime, *, attributes: bool
    ) -> list[dict[str, Any]]:
        """Return the state changes of one entity between two instants."""
        query: dict[str, str | None] = {
            "filter_entity_id": entity_id,
            "end_time": start_iso(end),
            "significant_changes_only": "0",
        }
        if not attributes:
            query["minimal_response"] = None
            query["no_attributes"] = None
        result = self.get(f"/api/history/period/{start_iso(start)}", query)
        return result[0] if result else []


def start_iso(moment: datetime) -> str:
    """Return an ISO 8601 UTC timestamp."""
    return moment.astimezone(UTC).isoformat()


def load_env_file(path: Path, environ: dict[str, str]) -> None:
    """Fill HA_URL and HA_TOKEN from a ``.env`` file (without overriding)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key in (URL_ENV, TOKEN_ENV) and key not in environ:
            environ[key] = value


def parse_entities(values: Sequence[str]) -> dict[str, str]:
    """Parse ``role=entity_id`` arguments."""
    entities: dict[str, str] = {}
    for value in values:
        role, sep, entity_id = value.partition("=")
        role, entity_id = role.strip(), entity_id.strip()
        if not sep or not entity_id or "." not in entity_id:
            raise ExportError(f"invalid --entity {value!r}: use role=domain.object_id")
        if role not in ROLES:
            raise ExportError(f"unknown role {role!r}; valid roles: {', '.join(ROLES)}")
        entities[role] = entity_id
    missing = [role for role in REQUIRED_ROLES if role not in entities]
    if missing:
        raise ExportError(f"missing required roles: {', '.join(missing)}")
    return entities


def day_ranges(start: date, end: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Return one (start, end) pair per local day, ``end`` included."""
    ranges = []
    day = start
    while day <= end:
        begin = datetime.combine(day, time.min, tz)
        finish = datetime.combine(day + timedelta(days=1), time.min, tz)
        ranges.append((begin, finish))
        day += timedelta(days=1)
    return ranges


def export_role(
    client: HomeAssistantClient,
    role: str,
    entity_id: str,
    ranges: Sequence[tuple[datetime, datetime]],
    output: Path,
) -> RoleSummary:
    """Download one role, day by day, and write its CSV."""
    keep_attributes = ROLES[role]
    summary = RoleSummary(entity_id=entity_id, unit=client.unit(entity_id))
    rows: dict[str, tuple[str, str]] = {}
    for begin, finish in ranges:
        for state in client.history(
            entity_id, begin, finish, attributes=keep_attributes
        ):
            changed = state.get("last_changed") or state.get("last_updated")
            if not changed:
                continue
            moment = datetime.fromisoformat(changed)
            if not begin <= moment < finish and rows:
                continue  # start-of-day state repeated by the API
            attributes = (
                json.dumps(
                    state.get("attributes", {}), ensure_ascii=False, sort_keys=True
                )
                if keep_attributes
                else ""
            )
            rows[start_iso(moment)] = (str(state.get("state", "")), attributes)

    with (output / f"{role}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for moment in sorted(rows):
            writer.writerow((moment, *rows[moment]))

    if rows:
        ordered = sorted(rows)
        summary.states = len(ordered)
        summary.first, summary.last = ordered[0], ordered[-1]
        first = datetime.fromisoformat(ordered[0])
        if ranges and first - ranges[0][0] > RETENTION_WARNING:
            summary.warnings.append(
                f"no data before {ordered[0]}: the recorder may have purged it "
                "(recorder: purge_keep_days)"
            )
    else:
        summary.warnings.append("no states in the period")
    return summary


def run(
    argv: Sequence[str] | None = None,
    *,
    opener: Opener | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Command line entry point; returns the exit code."""
    parser = argparse.ArgumentParser(
        description="Export the history of the TankWise inputs to CSV files."
    )
    parser.add_argument(
        "--start",
        required=True,
        type=date.fromisoformat,
        help="first local day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        required=True,
        type=date.fromisoformat,
        help="last local day, included (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--entity",
        action="append",
        default=[],
        metavar="ROLE=ENTITY_ID",
        help=f"entity of a role; roles: {', '.join(ROLES)}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output folder (default: tests/fixtures/history_<start>_<end>)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help="file with HA_URL and HA_TOKEN (default: .env)",
    )
    args = parser.parse_args(argv)
    env = os.environ.copy() if environ is None else environ

    try:
        if args.end < args.start:
            raise ExportError("--end is before --start")
        entities = parse_entities(args.entity)
        load_env_file(args.env_file, env)
        base_url, token = env.get(URL_ENV), env.get(TOKEN_ENV)
        if not base_url or not token:
            raise ExportError(
                f"set the {URL_ENV} and {TOKEN_ENV} environment variables"
            )
        client = HomeAssistantClient(base_url, token, opener)
        tz = ZoneInfo(client.time_zone())
        output = args.output or DEFAULT_OUTPUT_ROOT / f"history_{args.start}_{args.end}"
        output.mkdir(parents=True, exist_ok=True)
        ranges = day_ranges(args.start, args.end, tz)

        summaries: dict[str, RoleSummary] = {}
        for role, entity_id in entities.items():
            print(f"{role}: {entity_id} ...", file=sys.stderr)
            summary = export_role(client, role, entity_id, ranges, output)
            summaries[role] = summary
            print(f"  {summary.states} states", file=sys.stderr)
            for warning in summary.warnings:
                print(f"  warning: {warning}", file=sys.stderr)
    except ExportError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    manifest = {
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "time_zone": str(tz),
        "exported": datetime.now(UTC).isoformat(timespec="seconds"),
        "roles": {
            role: {
                "entity_id": summary.entity_id,
                "unit": summary.unit,
                "states": summary.states,
                "first": summary.first,
                "last": summary.last,
                "attributes": ROLES[role],
            }
            for role, summary in summaries.items()
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Exported to {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
