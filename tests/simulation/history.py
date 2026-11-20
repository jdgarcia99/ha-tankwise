"""Load a history export (tools/export_history.py) as replay series."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.tankwise.inputs import (
    InputError,
    parse_bool,
    parse_measurement,
    parse_numeric,
)
from custom_components.tankwise.model.replay import (
    ROLE_AMBIENT,
    ROLE_ENERGY,
    ROLE_MANUAL_MODE,
    ROLE_OCCUPANCY,
    ROLE_POWER,
    ROLE_REGULATOR,
    ROLE_TEMPERATURE,
    ROLE_WORKDAY,
    StateChange,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"

# Export role -> how its states are read.
MEASUREMENTS = {
    ROLE_TEMPERATURE: "temperature",
    ROLE_AMBIENT: "temperature",
    ROLE_POWER: "power",
    ROLE_ENERGY: "energy",
}
BOOLEANS = (ROLE_MANUAL_MODE, ROLE_OCCUPANCY, ROLE_WORKDAY)
NUMBERS = (ROLE_REGULATOR,)


@dataclass(frozen=True, slots=True)
class History:
    """A loaded export."""

    folder: Path
    tz: ZoneInfo
    series: dict[str, list[StateChange]]
    manifest: dict


def find_export() -> Path | None:
    """Return the most recent export folder in tests/fixtures, if any."""
    folders = sorted(FIXTURES.glob("history_*/manifest.json"))
    return folders[-1].parent if folders else None


def load(folder: Path) -> History:
    """Read the CSV files of the roles the tank and demand models use."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    series: dict[str, list[StateChange]] = {}
    for role, info in manifest["roles"].items():
        if role not in (*MEASUREMENTS, *BOOLEANS, *NUMBERS):
            continue
        path = folder / f"{role}.csv"
        if not path.exists():
            continue
        changes = []
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                state = row["state"]
                value: float | bool | None
                try:
                    if role in MEASUREMENTS:
                        value = parse_measurement(
                            state, info["unit"], MEASUREMENTS[role]
                        )
                    elif role in BOOLEANS:
                        value = parse_bool(state)
                    else:
                        value = parse_numeric(state)
                except InputError:
                    value = None
                changes.append(
                    StateChange(datetime.fromisoformat(row["last_changed"]), value)
                )
        series[role] = changes
    return History(folder, ZoneInfo(manifest["time_zone"]), series, manifest)
