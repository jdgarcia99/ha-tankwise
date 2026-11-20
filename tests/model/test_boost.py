"""Boost state: end conditions, energy and persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.boost import Boost, BoostEnd

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def test_end_reasons() -> None:
    boost = Boost(T0, 60.0, T0 + timedelta(minutes=90), 40.0)
    assert boost.end_reason(T0 + timedelta(minutes=10), 50.0) is None
    assert boost.end_reason(T0 + timedelta(minutes=10), 60.0) is BoostEnd.TARGET
    assert boost.end_reason(T0 + timedelta(minutes=90), 55.0) is BoostEnd.DURATION
    unlimited = Boost(T0, 60.0, None, 40.0)
    assert unlimited.end_reason(T0 + timedelta(hours=5), 55.0) is None
    assert unlimited.end_reason(T0, None) is None


def test_energy_and_cost() -> None:
    boost = Boost(T0, 60.0, None, 40.0)
    boost = boost.account(T0 + timedelta(minutes=1), 1500.0, 0.2)
    boost = boost.account(T0 + timedelta(minutes=2), 1500.0, 0.2)
    assert boost.energy_kwh == pytest.approx(0.05)
    assert boost.cost == pytest.approx(0.01)
    # A long gap (restart) counts at most five minutes.
    boost = boost.account(T0 + timedelta(hours=1), 1500.0, 0.2)
    assert boost.energy_kwh == pytest.approx(0.05 + 0.125)
    assert boost.account(T0 + timedelta(hours=1), None, 0.2) == boost


def test_round_trip() -> None:
    boost = Boost(T0, 61.0, T0 + timedelta(minutes=30), None, 0.4, 0.1, T0)
    assert Boost.from_dict(boost.as_dict()) == boost
    with pytest.raises(ValueError):
        Boost.from_dict({"target": 60})
