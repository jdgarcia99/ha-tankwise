"""Tests for tools/export_history.py with simulated Home Assistant responses."""

from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import pytest
from tankwise_export_history import (
    Entities,
    ExportError,
    HomeAssistantClient,
    export,
    main,
)
from tankwise_model.history import parse_history_csv, replay_history
from tankwise_model.tank import TankParams

TOKEN = "secret-token-1234567890"
URL = "http://ha.local:8123"
NOW = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
TEMPERATURE = "sensor.termo_temperatura"
POWER = "sensor.termo_potencia"
ENERGY = "sensor.termo_energia"
PWM = "sensor.freeds_pwm"
MANUAL = "switch.freeds_57d4_pwm_manual_mode"
BUTTON = "automation.zigbee_maneja_termo_desde_pulsador"
ENTITIES = Entities(TEMPERATURE, POWER, ENERGY, PWM, MANUAL)
ARGS = [
    "--url",
    URL,
    "--temperature",
    TEMPERATURE,
    "--power",
    POWER,
    "--energy",
    ENERGY,
    "--regulator",
    PWM,
    "--manual-mode",
    MANUAL,
    "--days",
    "1",
]


def _state(state: str, at: str, entity_id: str | None = None, unit: str | None = None):
    item: dict[str, Any] = {"state": state, "last_changed": at}
    if entity_id is not None:
        item["entity_id"] = entity_id
        item["attributes"] = {"unit_of_measurement": unit} if unit else {}
    return item


HISTORY = [
    [
        _state("46.0", "2026-11-13T22:00:00+00:00", TEMPERATURE, "°C"),
        _state("45.5", "2026-11-14T06:00:30+00:00"),
        _state("unavailable", "2026-11-14T12:00:00+00:00"),
        _state("44.0", "2026-11-14T12:30:00+00:00"),
    ],
    [
        _state("0", "2026-11-14T00:00:00+00:00", POWER, "kW"),
        _state("1.45", "2026-11-14T10:00:00Z"),
        _state("0", "2026-11-14T11:00:00Z"),
    ],
    [_state("123400", "2026-11-13T00:00:00+00:00", ENERGY, "Wh")],
    [_state("0", "2026-11-14T00:00:00+00:00", PWM, "%")],
    [
        _state("off", "2026-11-13T00:00:00+00:00", MANUAL),
        _state("on", "2026-11-14T07:00:10+00:00"),
        _state("off", "2026-11-14T07:30:00+00:00"),
        _state("on", "2026-11-14T09:00:00+00:00"),
        _state("off", "2026-11-14T09:30:00+00:00"),
        _state("on", "2026-11-14T13:00:00+00:00"),
        _state("off", "2026-11-14T13:30:00+00:00"),
    ],
]

LOGBOOK = [
    {
        "when": "2026-11-14T07:00:10+00:00",
        "state": "on",
        "entity_id": MANUAL,
        "context_user_id": "0123abcd",
    },
    {
        "when": "2026-11-14T07:30:00+00:00",
        "state": "off",
        "entity_id": MANUAL,
        "context_user_id": "0123abcd",
    },
    {
        "when": "2026-11-14T09:00:00+00:00",
        "state": "on",
        "entity_id": MANUAL,
        "context_entity_id": BUTTON,
        "context_event_type": "automation_triggered",
    },
    {"when": "2026-11-14T13:00:00+00:00", "state": "on", "entity_id": MANUAL},
]


class FakeResponse(io.BytesIO):
    """A urlopen response."""


class FakeApi:
    """Simulated Home Assistant REST API."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request)
        if self.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "Unauthorized", {}, None
            )
        path = urllib.parse.urlparse(request.full_url).path
        if path.startswith("/api/history/period/"):
            return FakeResponse(json.dumps(HISTORY).encode())
        if path.startswith("/api/logbook/"):
            return FakeResponse(json.dumps(LOGBOOK).encode())
        raise AssertionError(f"unexpected request {path}")


def _rows() -> tuple[list[dict[str, str]], FakeApi]:
    api = FakeApi()
    client = HomeAssistantClient(URL, TOKEN, opener=api)
    return export(client, ENTITIES, 1, now=NOW), api


def _row(rows: list[dict[str, str]], hhmm: str) -> dict[str, str]:
    stamp = f"2026-11-14T{hhmm}:00+00:00"
    return next(row for row in rows if row["timestamp"] == stamp)


def test_minute_rows_hold_values_and_convert_units() -> None:
    rows, _ = _rows()
    assert len(rows) == 24 * 60
    assert _row(rows, "06:00")["temperature"] == "46"
    assert _row(rows, "06:01")["temperature"] == "45.5"
    assert _row(rows, "12:15")["temperature"] == "unavailable"
    assert _row(rows, "12:30")["temperature"] == "44"
    assert _row(rows, "10:00")["power"] == "1450"  # kW → W
    assert _row(rows, "11:00")["power"] == "0"
    assert _row(rows, "00:00")["energy"] == "123.4"  # Wh → kWh
    assert _row(rows, "00:00")["pwm"] == "0"


def test_manual_source_from_logbook() -> None:
    rows, _ = _rows()
    assert _row(rows, "07:00")["manual_mode"] == "off"
    assert _row(rows, "07:01")["manual_mode"] == "on"
    assert _row(rows, "07:01")["manual_source"] == "user"
    assert _row(rows, "09:00")["manual_source"] == f"automation:{BUTTON}"
    assert _row(rows, "13:00")["manual_mode"] == "on"
    assert _row(rows, "13:00")["manual_source"] == ""
    assert sum(1 for row in rows if row["manual_source"]) == 2


def test_requests_use_the_token_only_in_the_header() -> None:
    _, api = _rows()
    assert len(api.requests) == 2
    for request in api.requests:
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert TOKEN not in request.full_url
    query = urllib.parse.parse_qs(urllib.parse.urlparse(api.requests[0].full_url).query)
    assert query["filter_entity_id"] == [
        f"{TEMPERATURE},{POWER},{ENERGY},{PWM},{MANUAL}"
    ]
    assert query["significant_changes_only"] == ["0"]


def test_csv_is_accepted_by_the_importer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "history.csv"
    code = main(
        [*ARGS, "--output", str(output)],
        environ={"HA_TOKEN": TOKEN},
        opener=FakeApi(),
        now=NOW,
    )
    assert code == 0
    text = output.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert TOKEN not in text
    assert TOKEN not in captured.out + captured.err

    parsed = parse_history_csv(text, UTC)
    assert parsed.skipped_rows == 0
    assert len(parsed.rows) == 24 * 60
    result = replay_history(parsed.rows, lambda _: TankParams(), UTC)
    assert [activation.source for activation in result.manual_activations] == [
        "user",
        f"automation:{BUTTON}",
        "",
    ]


def test_missing_token(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(ARGS, environ={}, opener=FakeApi(), now=NOW) == 2
    assert "HA_TOKEN" in capsys.readouterr().err


def test_http_error_does_not_leak_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "history.csv"
    code = main(
        [*ARGS, "--output", str(output)],
        environ={"HA_TOKEN": TOKEN},
        opener=FakeApi(status=401),
        now=NOW,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "HTTP 401" in err
    assert TOKEN not in err
    assert not output.exists()


def test_unsupported_unit_fails() -> None:
    history = json.loads(json.dumps(HISTORY))
    history[1][0]["attributes"]["unit_of_measurement"] = "BTU/h"

    def opener(request: urllib.request.Request, timeout: float) -> Any:
        path = urllib.parse.urlparse(request.full_url).path
        body = history if "history" in path else LOGBOOK
        return FakeResponse(json.dumps(body).encode())

    client = HomeAssistantClient(URL, TOKEN, opener=opener)
    with pytest.raises(ExportError, match="unsupported unit"):
        export(client, ENTITIES, 1, now=NOW)


def test_missing_entity_history_fails() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> Any:
        path = urllib.parse.urlparse(request.full_url).path
        body = HISTORY[:2] if "history" in path else LOGBOOK
        return FakeResponse(json.dumps(body).encode())

    client = HomeAssistantClient(URL, TOKEN, opener=opener)
    with pytest.raises(ExportError, match="no history for: energy, pwm, manual_mode"):
        export(client, ENTITIES, 1, now=NOW)


def test_rejects_url_without_scheme() -> None:
    with pytest.raises(ExportError):
        HomeAssistantClient("ha.local:8123", TOKEN)
