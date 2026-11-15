"""Import of raw tank history (minute readings) through the tank model.

The CSV holds one row per reading with a timestamp, the tank temperature,
the heating element power and energy, the regulator output (%), the
regulator manual mode state and, optionally, who switched the manual mode on
(``manual_source``: ``user``, ``automation:<entity_id>`` or empty). Rows are
replayed through a fresh :class:`TankModel`, which detects the draws exactly
as it does live; gaps
and ``unavailable`` values are handled by the model itself (gap recovery
and safe mode).

This module is pure Python and must not import Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
import io
import re

from .demand import DayType, DemandModel, FailureReason
from .tank import ConsumptionEvent, TankModel, TankParams, TankSample

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "datetime", "date", "last_changed", "fecha"),
    "temperature": ("temperature", "temp", "temperatura"),
    "power": ("power", "potencia"),
    "energy": ("energy", "energia", "energía"),
    "regulator": ("regulator", "pwm", "regulador"),
    "manual_mode": ("manual_mode", "manual", "modo_manual"),
    "manual_source": ("manual_source", "source", "origen"),
}
REQUIRED_COLUMNS = ("timestamp", "temperature", "power", "energy")
# A day counts as observed only if the readings cover most of it, so that a
# partial first or last day does not look like a day without draws.
MIN_DAY_COVERAGE = timedelta(hours=20)
MISSING_VALUES = {"", "unavailable", "unknown", "none", "null", "nan", "-"}
TRUE_VALUES = {"on", "true", "1", "yes", "si", "sí"}
FALSE_VALUES = {"off", "false", "0", "no"}
SOURCE_USER = "user"
SOURCE_AUTOMATION_PREFIX = "automation:"
TIMESTAMP_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
)


class HistoryFormatError(ValueError):
    """The CSV cannot be interpreted as tank history."""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One reading of the history."""

    sample: TankSample
    manual_mode: bool | None
    # None: the file has no manual_source column; "": unknown origin.
    manual_source: str | None = None


@dataclass(frozen=True, slots=True)
class ManualActivation:
    """The regulator manual mode switched on (off to on)."""

    timestamp: datetime
    source: str | None


def is_person_source(source: str, person_automations: Collection[str]) -> bool:
    """Return True if a manual_source value means a person acted.

    Same criterion as live: a user, or an automation listed as acting on
    behalf of a person. Any other automation or an unknown origin does not
    count.
    """
    source = source.strip()
    if source == SOURCE_USER:
        return True
    if source.startswith(SOURCE_AUTOMATION_PREFIX):
        return source[len(SOURCE_AUTOMATION_PREFIX) :] in person_automations
    return False


@dataclass(slots=True)
class ParsedHistory:
    """Result of parsing a history CSV."""

    rows: list[HistoryRow]
    skipped_rows: int


@dataclass(frozen=True, slots=True)
class ReplayedDraw:
    """A draw detected while replaying the history."""

    event: ConsumptionEvent
    useful_at_start_kwh: float


@dataclass(slots=True)
class ReplayResult:
    """Outcome of replaying a history through the tank model."""

    draws: list[ReplayedDraw] = field(default_factory=list)
    manual_activations: list[ManualActivation] = field(default_factory=list)
    observed_days: set[date] = field(default_factory=set)
    valid_samples: int = 0


def _normalise_header(name: str) -> str:
    name = re.sub(r"[\(\[].*?[\)\]]", "", name.strip().lower())
    return re.sub(r"[\s\-]+", "_", name).strip("_")


def _map_columns(header: list[str]) -> dict[str, int]:
    names = [_normalise_header(raw) for raw in header]
    mapping: dict[str, int] = {}
    # Exact names first, so that e.g. "manual_source" is not taken for
    # "manual_mode" through the "manual" prefix; then "<alias>_<unit>" names.
    for exact in (True, False):
        for index, name in enumerate(names):
            if index in mapping.values():
                continue
            for column, aliases in COLUMN_ALIASES.items():
                if column in mapping:
                    continue
                if any(
                    name == alias if exact else name.startswith(f"{alias}_")
                    for alias in aliases
                ):
                    mapping[column] = index
                    break
    missing = [column for column in REQUIRED_COLUMNS if column not in mapping]
    if missing:
        raise HistoryFormatError(f"missing columns: {', '.join(missing)}")
    return mapping


def _parse_number(value: str, decimal_comma: bool) -> float | None:
    text = value.strip().lower()
    if text in MISSING_VALUES:
        return None
    if decimal_comma and "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_bool(value: str) -> bool | None:
    text = value.strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def _parse_timestamp(value: str, default_tz: tzinfo) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def parse_history_csv(text: str, default_tz: tzinfo) -> ParsedHistory:
    """Parse a history CSV (comma, semicolon or tab separated).

    Naive timestamps are interpreted in ``default_tz``. With a semicolon or
    tab separator, decimal commas are accepted. Rows without a valid
    timestamp are skipped; missing or ``unavailable`` values become None.
    """
    lines = text.lstrip("﻿")
    if not lines.strip():
        raise HistoryFormatError("the file is empty")
    try:
        dialect = csv.Sniffer().sniff(lines.splitlines()[0], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(lines), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration as err:
        raise HistoryFormatError("the file is empty") from err
    columns = _map_columns(header)
    decimal_comma = delimiter != ","

    rows: list[HistoryRow] = []
    skipped = 0
    seen: set[datetime] = set()

    def cell(values: list[str], column: str) -> str:
        index = columns.get(column)
        return values[index] if index is not None and index < len(values) else ""

    for values in reader:
        if not values or all(not value.strip() for value in values):
            continue
        timestamp = _parse_timestamp(cell(values, "timestamp"), default_tz)
        if timestamp is None or timestamp in seen:
            skipped += 1
            continue
        seen.add(timestamp)
        rows.append(
            HistoryRow(
                sample=TankSample(
                    timestamp=timestamp,
                    temperature=_parse_number(
                        cell(values, "temperature"), decimal_comma
                    ),
                    power=_parse_number(cell(values, "power"), decimal_comma),
                    energy=_parse_number(cell(values, "energy"), decimal_comma),
                    regulator=_parse_number(cell(values, "regulator"), decimal_comma),
                ),
                manual_mode=_parse_bool(cell(values, "manual_mode")),
                manual_source=(
                    cell(values, "manual_source").strip()
                    if "manual_source" in columns
                    else None
                ),
            )
        )
    rows.sort(key=lambda row: row.sample.timestamp)
    return ParsedHistory(rows=rows, skipped_rows=skipped)


def replay_history(
    rows: list[HistoryRow],
    params_for: Callable[[datetime], TankParams],
    local_tz: tzinfo,
) -> ReplayResult:
    """Run the readings through a fresh tank model and collect the draws.

    ``params_for`` returns the tank parameters for a moment (e.g. the mains
    water temperature of that month). The manual mode activations (off to
    on) are returned as well; the caller decides whether they count as
    failures.
    """
    model = TankModel()
    result = ReplayResult()
    last_event: ConsumptionEvent | None = None
    manual: bool | None = None
    day_span: dict[date, tuple[datetime, datetime]] = {}

    for row in rows:
        sample = row.sample
        params = params_for(sample.timestamp)
        model.update(sample, params)
        if sample.valid:
            result.valid_samples += 1
            day = sample.timestamp.astimezone(local_tz).date()
            first, _ = day_span.get(day, (sample.timestamp, sample.timestamp))
            day_span[day] = (first, sample.timestamp)

        event = model.last_consumption
        if event is not None and event is not last_event:
            last_event = event
            assert event.start_temperature is not None
            useful = max(
                0.0,
                params.heat_capacity
                * (event.start_temperature - params.min_useful_temperature),
            )
            result.draws.append(ReplayedDraw(event, useful))

        if row.manual_mode is not None:
            if row.manual_mode and manual is False:
                result.manual_activations.append(
                    ManualActivation(sample.timestamp, row.manual_source)
                )
            manual = row.manual_mode

    result.observed_days = {
        day
        for day, (first, last) in day_span.items()
        if last - first >= MIN_DAY_COVERAGE
    }
    return result


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """What an import added to the demand model."""

    days: int
    draws: int
    failures: int
    skipped_days: int


def apply_to_demand(
    result: ReplayResult,
    demand: DemandModel,
    day_type_of: Callable[[date], DayType],
    shower_energy_kwh: float,
    local_tz: tzinfo,
    *,
    manual_mode_is_failure: bool = False,
    person_automations: Collection[str] = (),
) -> ImportSummary:
    """Merge a replayed history into the demand model.

    Days already present in the demand history (observed live or imported
    before) are skipped, so importing twice does not count draws twice.
    Only fully covered days are imported.

    A manual mode activation is a forecast failure when its ``manual_source``
    is a user or one of ``person_automations``. Files without a
    ``manual_source`` column use ``manual_mode_is_failure`` for every
    activation instead.
    """
    new_days = {day for day in result.observed_days if not demand.is_observed(day)}
    skipped = len(result.observed_days) - len(new_days)

    def local_day(moment: datetime) -> date:
        return moment.astimezone(local_tz).date()

    for activation in result.manual_activations:
        day = local_day(activation.timestamp)
        if day not in new_days:
            continue
        if activation.source is None:
            is_failure = manual_mode_is_failure
        else:
            is_failure = is_person_source(activation.source, person_automations)
        if is_failure:
            demand.add_failure(
                activation.timestamp.astimezone(local_tz),
                day_type_of(day),
                FailureReason.MANUAL_MODE,
            )

    draws = failures = 0
    for replayed in result.draws:
        start = replayed.event.start.astimezone(local_tz)
        if start.date() not in new_days:
            continue
        draw = demand.add_draw(
            start,
            replayed.event.energy_kwh,
            day_type_of(start.date()),
            useful_at_start_kwh=replayed.useful_at_start_kwh,
            shower_energy_kwh=shower_energy_kwh,
            imported=True,
        )
        draws += 1
        failures += draw.failure

    for day in sorted(new_days):
        demand.observe(
            datetime.combine(day, datetime.min.time(), tzinfo=local_tz),
            day_type_of(day),
        )
    return ImportSummary(
        days=len(new_days), draws=draws, failures=failures, skipped_days=skipped
    )
