"""Tests for the raw history import (CSV parsing and replay)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.demand import DemandModel, day_type_for
from tankwise_model.history import (
    HistoryFormatError,
    apply_to_demand,
    parse_history_csv,
    replay_history,
)

from .simulator import PARAMS, TankSimulator

TZ = ZoneInfo("Europe/Madrid")


def test_parse_semicolon_decimal_comma_local_time() -> None:
    text = (
        "﻿timestamp;Temperature (°C);Power [W];energy_kwh;PWM (%);modo manual\n"
        "2026-11-02 07:00:00;46,5;1450;1.234,5;100;off\n"
        "2026-11-02 07:01:00;unavailable;0;1234.6;0;on\n"
        "not a date;40;0;1;0;off\n"
        "2026-11-02 07:01:00;40;0;1;0;off\n"
        "\n"
    )
    parsed = parse_history_csv(text, TZ)
    assert parsed.skipped_rows == 2  # bad timestamp and duplicate
    first, second = parsed.rows
    assert first.sample.timestamp == datetime(2026, 11, 2, 7, tzinfo=TZ)
    assert first.sample.temperature == 46.5
    assert first.sample.energy == 1234.5
    assert first.sample.regulator == 100
    assert first.manual_mode is False
    assert second.sample.temperature is None
    assert second.sample.energy == 1234.6
    assert second.manual_mode is True
    assert not second.sample.valid


def test_parse_comma_iso_with_offset_and_optional_columns() -> None:
    text = (
        "time,temp,power,energy\n"
        "2026-11-02T06:00:00Z,45.0,0,10.0\n"
        "02/11/2026 07:05,44.5,,10.0\n"
    )
    parsed = parse_history_csv(text, TZ)
    first, second = parsed.rows
    assert first.sample.timestamp == datetime(2026, 11, 2, 6, tzinfo=UTC)
    assert first.sample.regulator is None
    assert first.manual_mode is None
    assert second.sample.timestamp == datetime(2026, 11, 2, 7, 5, tzinfo=TZ)
    assert second.sample.power is None


@pytest.mark.parametrize(
    "text",
    ["", "timestamp,temperature,power\n2026-11-02 07:00,45,0\n"],
)
def test_parse_rejects_unusable_files(text: str) -> None:
    with pytest.raises(HistoryFormatError):
        parse_history_csv(text, TZ)


def _history_csv() -> str:
    """Three days of minute readings from the simulator, as exported."""
    sim = TankSimulator(temperature=47.0)
    lines = ["timestamp;temperature (°C);power (W);energy (kWh);pwm (%);manual_mode"]
    manual = False

    def emit(
        minutes: int,
        *,
        regulator: float = 0,
        draw_kw: float = 0,
        temperature_unavailable: bool = False,
        skip: bool = False,
    ) -> None:
        for sample in sim.run(minutes, regulator=regulator, draw_kw=draw_kw):
            if skip:
                continue
            local = sample.timestamp.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
            temperature = (
                "unavailable"
                if temperature_unavailable
                else f"{sample.temperature:.1f}".replace(".", ",")
            )
            lines.append(
                f"{local};{temperature};{sample.power:.0f};{sample.energy:.4f}".replace(
                    ".", ","
                )
                + f";{sample.regulator:.0f};{'on' if manual else 'off'}"
            )

    # Day 1 (from 01:00 local): a strong shower at 08:00, then reheating.
    emit(7 * 60)
    emit(12, draw_kw=9.0)
    emit(3 * 60 - 12)
    emit(2 * 60, regulator=100)
    emit(12 * 60)
    # Day 2: manual mode switched on before the morning shower; a 2 h gap
    # and 30 minutes with the probe unavailable.
    emit(6 * 60 + 30)
    manual = True
    emit(30)
    emit(12, draw_kw=9.0)
    manual = False
    emit(2 * 60 - 12)
    emit(2 * 60, regulator=100)
    emit(3 * 60)
    emit(2 * 60, skip=True)
    emit(2 * 60)
    emit(30, temperature_unavailable=True)
    emit(5 * 60 + 30)
    # Day 3: two strong showers in a row; the second starts with an empty tank.
    emit(7 * 60)
    emit(12, draw_kw=9.0)
    emit(20)
    emit(10, draw_kw=7.2)
    emit(17 * 60 - 42)
    return "\n".join(lines) + "\n"


def _params_for(_: datetime):
    return PARAMS


def test_replay_detects_draws_failures_and_manual_mode() -> None:
    parsed = parse_history_csv(_history_csv(), TZ)
    assert parsed.skipped_rows == 0
    result = replay_history(parsed.rows, _params_for, TZ)

    assert len(result.draws) == 4
    energies = [draw.event.energy_kwh for draw in result.draws]
    assert energies[:3] == pytest.approx([1.8, 1.8, 1.8], rel=0.15)
    assert result.draws[0].useful_at_start_kwh > 1.5
    assert result.draws[3].useful_at_start_kwh < 0.9
    assert len(result.manual_activations) == 1
    assert result.observed_days == {
        date(2026, 11, 2),
        date(2026, 11, 3),
        date(2026, 11, 4),
    }


def test_import_into_demand_model_once() -> None:
    parsed = parse_history_csv(_history_csv(), TZ)
    result = replay_history(parsed.rows, _params_for, TZ)
    demand = DemandModel()

    summary = apply_to_demand(result, demand, day_type_for, 1.8, TZ)
    assert summary.days == 3
    assert summary.draws == 4
    # The manually anticipated shower and the shower with an empty tank.
    assert summary.failures == 2
    assert [draw.failure for draw in demand.draws] == [False, True, False, True]
    assert all(draw.imported for draw in demand.draws)

    again = apply_to_demand(result, demand, day_type_for, 1.8, TZ)
    assert again.days == 0
    assert again.draws == 0
    assert again.skipped_days == 3
    assert len(demand.draws) == 4


def test_import_ignoring_manual_mode() -> None:
    parsed = parse_history_csv(_history_csv(), TZ)
    result = replay_history(parsed.rows, _params_for, TZ)
    demand = DemandModel()
    summary = apply_to_demand(
        result, demand, day_type_for, 1.8, TZ, manual_mode_is_failure=False
    )
    assert summary.failures == 1


def test_imported_days_skip_cold_start() -> None:
    """Fourteen imported days make the profile ready."""
    sim = TankSimulator(temperature=47.0)
    lines = ["timestamp,temperature,power,energy,pwm,manual_mode"]
    for _ in range(15):
        for minutes, draw_kw, regulator in (
            (7 * 60, 0, 0),
            (12, 9.0, 0),
            (120, 0, 100),
        ):
            for sample in sim.run(minutes, regulator=regulator, draw_kw=draw_kw):
                lines.append(
                    f"{sample.timestamp.isoformat()},{sample.temperature},"
                    f"{sample.power},{sample.energy},{sample.regulator},off"
                )
        for sample in sim.run(24 * 60 - 7 * 60 - 132):
            lines.append(
                f"{sample.timestamp.isoformat()},{sample.temperature},"
                f"{sample.power},{sample.energy},{sample.regulator},off"
            )
    parsed = parse_history_csv("\n".join(lines), TZ)
    result = replay_history(parsed.rows, _params_for, TZ)
    demand = DemandModel()
    apply_to_demand(result, demand, day_type_for, 1.8, TZ)
    last = max(result.observed_days) + timedelta(days=1)
    assert demand.days_with_data(last) >= 14
    assert demand.status(last).value == "ready"
