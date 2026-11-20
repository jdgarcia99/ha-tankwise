"""Tests for tools/export_history.py (no network: the HTTP opener is faked)."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.parse

import pytest

TOOL = Path(__file__).parents[2] / "tools" / "export_history.py"
_spec = importlib.util.spec_from_file_location("export_history", TOOL)
assert _spec and _spec.loader
export_history = importlib.util.module_from_spec(_spec)
sys.modules["export_history"] = export_history
_spec.loader.exec_module(export_history)

TOKEN = "secret-token-value"
URL = "http://ha.local:8123"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeHomeAssistant:
    """Answer the REST endpoints used by the tool."""

    def __init__(
        self, history: dict[str, list[dict[str, Any]]], status: int = 200
    ) -> None:
        self.history = history
        self.status = status
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: int) -> FakeResponse:
        self.requests.append(request)
        if self.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "error", {}, None
            )
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/api/config":
            body: Any = {"time_zone": "Europe/Madrid"}
        elif parsed.path.startswith("/api/states/"):
            body = {"attributes": {"unit_of_measurement": "°C"}}
        else:
            entity_id = query["filter_entity_id"][0]
            start = parsed.path.rsplit("/", 1)[1]
            end = query["end_time"][0]
            states = [
                s
                for s in self.history.get(entity_id, [])
                if start <= s["last_changed"] < end
            ]
            body = [states] if states else []
        return FakeResponse(json.dumps(body).encode())


HISTORY = {
    "sensor.tank": [
        {"last_changed": "2026-11-07T00:10:00+00:00", "state": "55.0"},
        {"last_changed": "2026-11-07T12:00:00+00:00", "state": "unavailable"},
        {"last_changed": "2026-11-08T06:30:00+00:00", "state": "48.5"},
    ],
    "sensor.power": [
        {"last_changed": "2026-11-07T03:00:00+00:00", "state": "1480"},
    ],
    "sensor.pvpc": [
        {
            "last_changed": "2026-11-07T10:00:00+00:00",
            "state": "0.1234",
            "attributes": {"price_10h": 0.1234, "price_11h": 0.11},
        }
    ],
}
ARGS = [
    "--start",
    "2026-11-07",
    "--end",
    "2026-11-08",
    "--entity",
    "temperature=sensor.tank",
    "--entity",
    "power=sensor.power",
    "--entity",
    "price=sensor.pvpc",
]


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_export_writes_one_csv_per_role_and_a_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeHomeAssistant(HISTORY)
    env = {"HA_URL": URL, "HA_TOKEN": TOKEN}
    code = export_history.run(
        [*ARGS, "--output", str(tmp_path), "--env-file", str(tmp_path / "none")],
        opener=fake,
        environ=env,
    )
    assert code == 0

    temperature = _read(tmp_path / "temperature.csv")
    assert [row["state"] for row in temperature] == ["55.0", "unavailable", "48.5"]
    assert temperature[0]["last_changed"] == "2026-11-07T00:10:00+00:00"
    assert temperature[0]["attributes"] == ""

    price = _read(tmp_path / "price.csv")
    assert json.loads(price[0]["attributes"]) == {
        "price_10h": 0.1234,
        "price_11h": 0.11,
    }

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["time_zone"] == "Europe/Madrid"
    assert manifest["roles"]["temperature"] == {
        "entity_id": "sensor.tank",
        "unit": "°C",
        "states": 3,
        "first": "2026-11-07T00:10:00+00:00",
        "last": "2026-11-08T06:30:00+00:00",
        "attributes": False,
    }

    # The token is only sent in the Authorization header, never printed.
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert TOKEN not in (tmp_path / "manifest.json").read_text()
    assert all(
        r.get_header("Authorization") == f"Bearer {TOKEN}" for r in fake.requests
    )

    # Day by day, without attributes for plain roles.
    history_requests = [
        r.full_url for r in fake.requests if "/api/history/" in r.full_url
    ]
    assert len(history_requests) == 3 * 2
    assert any(
        "no_attributes" in url for url in history_requests if "sensor.tank" in url
    )
    assert not any(
        "no_attributes" in url for url in history_requests if "sensor.pvpc" in url
    )


def test_purged_history_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeHomeAssistant(
        {"sensor.tank": HISTORY["sensor.tank"][2:], "sensor.power": []}
    )
    code = export_history.run(
        [*ARGS[:8], "--output", str(tmp_path)],
        opener=fake,
        environ={"HA_URL": URL, "HA_TOKEN": TOKEN},
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "purge_keep_days" in err
    assert "no states in the period" in err


def test_env_file_is_read_without_overriding(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f'# comment\nexport HA_URL="{URL}"\nHA_TOKEN={TOKEN}\nOTHER=1\n'
    )
    env = {"HA_URL": "http://other:8123"}
    export_history.load_env_file(env_file, env)
    assert env == {"HA_URL": "http://other:8123", "HA_TOKEN": TOKEN}


@pytest.mark.parametrize(
    ("args", "environ", "message"),
    [
        (ARGS, {}, "set the HA_URL and HA_TOKEN"),
        (ARGS, {"HA_URL": "ha.local", "HA_TOKEN": TOKEN}, "must start with http"),
        (
            [
                "--start",
                "2026-11-07",
                "--end",
                "2026-11-08",
                "--entity",
                "power=sensor.p",
            ],
            {"HA_URL": URL, "HA_TOKEN": TOKEN},
            "missing required roles: temperature",
        ),
        (
            [*ARGS, "--entity", "humidity=sensor.h"],
            {"HA_URL": URL, "HA_TOKEN": TOKEN},
            "unknown role 'humidity'",
        ),
        (
            [*ARGS, "--entity", "power"],
            {"HA_URL": URL, "HA_TOKEN": TOKEN},
            "invalid --entity",
        ),
        (
            ["--start", "2026-11-08", "--end", "2026-11-07", *ARGS[4:]],
            {"HA_URL": URL, "HA_TOKEN": TOKEN},
            "--end is before --start",
        ),
    ],
)
def test_invalid_usage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    environ: dict[str, str],
    message: str,
) -> None:
    code = export_history.run(
        [*args, "--output", str(tmp_path), "--env-file", str(tmp_path / "none")],
        opener=FakeHomeAssistant(HISTORY),
        environ=environ,
    )
    assert code == 1
    assert message in capsys.readouterr().err


def test_rejected_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = export_history.run(
        [*ARGS, "--output", str(tmp_path)],
        opener=FakeHomeAssistant(HISTORY, status=401),
        environ={"HA_URL": URL, "HA_TOKEN": TOKEN},
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "rejected the token" in err
    assert TOKEN not in err
