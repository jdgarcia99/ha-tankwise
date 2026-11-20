"""Cost accounting."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.ledger import Ledger

TZ = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 11, 30, 22, 0, tzinfo=TZ)


def test_real_cost_split_between_grid_and_surplus() -> None:
    ledger = Ledger(TZ)
    ledger.add_real(T0, 1.0, from_surplus=False, buy_price=0.20, surplus_value=0.05)
    ledger.add_real(T0, 2.0, from_surplus=True, buy_price=0.20, surplus_value=0.05)
    today, month = ledger.current(T0)
    assert today.real_grid_kwh == 1.0 and today.real_solar_kwh == 2.0
    assert today.real_cost == pytest.approx(0.30)
    assert month.real_cost == pytest.approx(0.30)


def test_planned_slots_are_committed_once_with_rescues() -> None:
    ledger = Ledger(TZ)
    assert ledger.commit_slot(T0, grid_kwh=0.375, cost=0.06, rescue=False)
    assert not ledger.commit_slot(T0, grid_kwh=0.375, cost=0.06, rescue=False)
    assert ledger.commit_slot(
        T0 + timedelta(minutes=15), grid_kwh=0.2, cost=0.05, rescue=True
    )
    today, month = ledger.current(T0)
    assert today.planned_cost == pytest.approx(0.11)
    assert month.rescue_kwh == pytest.approx(0.2)
    assert month.rescue_cost == pytest.approx(0.05)


def test_day_and_month_reset() -> None:
    ledger = Ledger(TZ)
    ledger.add_real(T0, 1.0, from_surplus=False, buy_price=0.2, surplus_value=0)
    ledger.add_deviation(T0)
    later = T0 + timedelta(hours=3)  # 1 December, 01:00
    today, month = ledger.current(later)
    assert today.real_cost == 0 and month.real_cost == 0 and month.deviations == 0
    ledger.add_real(later, 1.0, from_surplus=False, buy_price=0.1, surplus_value=0)
    next_day = later + timedelta(days=1)
    today, month = ledger.current(next_day)
    assert today.real_cost == 0
    assert month.real_cost == pytest.approx(0.1)


def test_round_trip_and_invalid() -> None:
    ledger = Ledger(TZ)
    ledger.add_real(T0, 1.0, from_surplus=False, buy_price=0.2, surplus_value=0)
    ledger.commit_slot(T0, grid_kwh=0.3, cost=0.03, rescue=True)
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.as_dict())), TZ)
    assert restored.as_dict() == ledger.as_dict()
    with pytest.raises(ValueError):
        Ledger.from_dict({"format": 9}, TZ)
