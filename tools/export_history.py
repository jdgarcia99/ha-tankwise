#!/usr/bin/env python3
"""Export tank history from Home Assistant as a CSV for tankwise.import_history.

Downloads the last N days of the tank probe, heating element power and
energy, regulator output (%) and regulator manual mode from the Home
Assistant REST API (``/api/history/period`` and ``/api/logbook``) and writes
one row per minute:

    timestamp,temperature,power,energy,pwm,manual_mode,manual_source

``manual_source`` tells who switched the manual mode on, taken from the
logbook: ``user`` when a person did it, ``automation:<entity_id>`` when an
automation did it, empty otherwise.

The access token is read from the ``HA_TOKEN`` environment variable. It is
only sent in the ``Authorization`` header; it is never written to disk or
printed.

Usage:

    HA_TOKEN=... python tools/export_history.py \\
        --url http://homeassistant.local:8123 \\
        --temperature sensor.termo_temperatura \\
        --power sensor.termo_potencia \\
        --energy sensor.termo_energia \\
        --regulator sensor.freeds_pwm \\
        --manual-mode switch.freeds_57d4_pwm_manual_mode \\
        --days 10 --output tankwise_history.csv

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "HA_TOKEN"
COLUMNS = (
    "timestamp",
    "temperature",
    "power",
    "energy",
    "pwm",
    "manual_mode",
    "manual_source",
)
STEP = timedelta(minutes=1)

# Unit conversions to what the importer expects: °C, W and kWh.
CONVERSIONS: dict[str, dict[str, Callable[[float], float]]] = {
    "temperature": {
        "°C": lambda v: v,
        "°F": lambda v: (v - 32) * 5 / 9,
        "K": lambda v: v - 273.15,
    },
    "power": {"W": lambda v: v, "kW": lambda v: v * 1000, "MW": lambda v: v * 1e6},
    "energy": {
        "kWh": lambda v: v,
        "Wh": lambda v: v / 1000,
        "MWh": lambda v: v * 1000,
    },
}

Opener = Callable[..., Any]


class ExportError(Exception):
    """The export cannot be completed."""


@dataclass(frozen=True, slots=True)
class Entities:
    """Entity IDs to export, by CSV column."""

    temperature: str
    power: str
    energy: str
    regulator: str | None = None
    manual_mode: str | None = None

    def by_column(self) -> dict[str, str]:
        """Return {column: entity_id} for the configured entities."""
        columns = {
            "temperature": self.temperature,
            "power": self.power,
            "energy": self.energy,
            "pwm": self.regulator,
            "manual_mode": self.manual_mode,
        }
        return {column: entity for column, entity in columns.items() if entity}


class HomeAssistantClient:
    """Minimal client for the Home Assistant REST API."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        opener: Opener = urllib.request.urlopen,
        timeout: float = 120,
    ) -> None:
        """Initialise the client."""
        if not url.startswith(("http://", "https://")):
            raise ExportError("the URL must start with http:// or https://")
        self._url = url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def get(self, path: str, params: Mapping[str, str]) -> Any:
        """GET a JSON resource."""
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self._url}{path}?{query}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise ExportError(
                f"Home Assistant returned HTTP {err.code} for {path}"
            ) from None
        except urllib.error.URLError as err:
            raise ExportError(f"cannot reach Home Assistant: {err.reason}") from None
        except json.JSONDecodeError:
            raise ExportError(f"invalid JSON in the response of {path}") from None

    def history(
        self, start: datetime, end: datetime, entity_ids: Sequence[str]
    ) -> list[list[dict[str, Any]]]:
        """Return the state history of the entities."""
        return self.get(
            f"/api/history/period/{urllib.parse.quote(start.isoformat())}",
            {
                "filter_entity_id": ",".join(entity_ids),
                "end_time": end.isoformat(),
                "minimal_response": "",
                "significant_changes_only": "0",
            },
        )

    def logbook(
        self, start: datetime, end: datetime, entity_id: str
    ) -> list[dict[str, Any]]:
        """Return the logbook entries of an entity."""
        return self.get(
            f"/api/logbook/{urllib.parse.quote(start.isoformat())}",
            {"entity": entity_id, "end_time": end.isoformat()},
        )


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _convert(column: str, state: str, unit: str | None) -> str:
    """Convert a numeric state to the importer units; pass others through."""
    conversions = CONVERSIONS.get(column)
    if conversions is None:
        return state
    try:
        value = float(state)
    except ValueError:
        return state  # unavailable, unknown...
    convert = conversions.get(unit or "")
    if convert is None:
        if unit:
            raise ExportError(f"unsupported unit {unit!r} for {column}")
        return state
    return _format_number(convert(value))


def build_series(
    history: list[list[dict[str, Any]]], entities: Entities
) -> dict[str, list[tuple[datetime, str]]]:
    """Return {column: [(time, value), ...]} from a history API response."""
    column_of = {entity: column for column, entity in entities.by_column().items()}
    series: dict[str, list[tuple[datetime, str]]] = {}
    for states in history:
        if not states:
            continue
        entity_id = states[0].get("entity_id")
        column = column_of.get(entity_id or "")
        if column is None:
            continue
        unit = (states[0].get("attributes") or {}).get("unit_of_measurement")
        points = [
            (
                _parse_time(state.get("last_changed") or state["last_updated"]),
                _convert(column, str(state["state"]), unit),
            )
            for state in states
        ]
        points.sort(key=lambda point: point[0])
        series[column] = points
    missing = [c for c in entities.by_column() if c not in series]
    if missing:
        raise ExportError(f"no history for: {', '.join(missing)}")
    return series


def manual_sources(logbook: list[dict[str, Any]]) -> list[tuple[datetime, str]]:
    """Return (time, source) of every manual mode activation in the logbook."""
    sources = []
    for entry in logbook:
        if entry.get("state") != "on" or "when" not in entry:
            continue
        context_entity = entry.get("context_entity_id") or ""
        if entry.get("context_user_id"):
            source = "user"
        elif context_entity.startswith("automation."):
            source = f"automation:{context_entity}"
        else:
            source = ""
        sources.append((_parse_time(entry["when"]), source))
    sources.sort(key=lambda item: item[0])
    return sources


def _ceil_minute(moment: datetime) -> datetime:
    floor = moment.replace(second=0, microsecond=0)
    return floor if floor == moment else floor + STEP


def build_rows(
    series: Mapping[str, list[tuple[datetime, str]]],
    sources: list[tuple[datetime, str]],
    start: datetime,
    end: datetime,
) -> list[dict[str, str]]:
    """Resample to one row per minute, holding the last known value."""
    times = {column: [t for t, _ in points] for column, points in series.items()}
    source_at = {_ceil_minute(moment): source for moment, source in sources}
    rows = []
    moment = start.astimezone(UTC).replace(second=0, microsecond=0)
    while moment < end:
        row = {column: "" for column in COLUMNS}
        row["timestamp"] = moment.isoformat()
        for column, points in series.items():
            index = bisect_right(times[column], moment) - 1
            if index >= 0:
                row[column] = points[index][1]
        row["manual_source"] = source_at.get(moment, "")
        rows.append(row)
        moment += STEP
    return rows


def write_csv(rows: list[dict[str, str]], output: str) -> None:
    """Write the rows to a CSV file."""
    with open(output, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def export(
    client: HomeAssistantClient,
    entities: Entities,
    days: float,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Download the history and return the CSV rows."""
    end = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    history = client.history(start, end, list(entities.by_column().values()))
    series = build_series(history, entities)
    sources: list[tuple[datetime, str]] = []
    if entities.manual_mode:
        sources = manual_sources(client.logbook(start, end, entities.manual_mode))
    return build_rows(series, sources, start, end)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export tank history from Home Assistant as a CSV for "
            f"tankwise.import_history. The token is read from {TOKEN_ENV}."
        )
    )
    parser.add_argument("--url", required=True, help="Home Assistant URL")
    parser.add_argument("--temperature", required=True, help="tank probe sensor")
    parser.add_argument("--power", required=True, help="heating element power")
    parser.add_argument("--energy", required=True, help="heating element energy")
    parser.add_argument("--regulator", help="regulator output (%%) sensor")
    parser.add_argument("--manual-mode", help="regulator manual mode switch")
    parser.add_argument("--days", type=float, default=10, help="days to export")
    parser.add_argument(
        "--output", default="tankwise_history.csv", help="CSV file to write"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    opener: Opener = urllib.request.urlopen,
    now: datetime | None = None,
) -> int:
    """Run the export; return the process exit code."""
    args = _parser().parse_args(argv)
    token = environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"Set the {TOKEN_ENV} environment variable.", file=sys.stderr)
        return 2
    if args.days <= 0:
        print("--days must be positive.", file=sys.stderr)
        return 2
    entities = Entities(
        temperature=args.temperature,
        power=args.power,
        energy=args.energy,
        regulator=args.regulator,
        manual_mode=args.manual_mode,
    )
    try:
        client = HomeAssistantClient(args.url, token, opener=opener)
        rows = export(client, entities, args.days, now=now)
        write_csv(rows, args.output)
    except (ExportError, OSError) as err:
        print(f"Export failed: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
