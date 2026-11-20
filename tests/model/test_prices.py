"""Prices per slot (PVPC-like hourly attributes)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.prices import parse_hourly_prices, slot_prices

TZ = ZoneInfo("Europe/Madrid")
TODAY = date(2026, 11, 20)


def _attributes(base: float, *, tomorrow: float | None = None) -> dict:
    data = {f"price_{hour:02d}h": base + hour / 100 for hour in range(24)}
    data["friendly_name"] = "PVPC"
    if tomorrow is not None:
        data |= {
            f"price_next_day_{hour:02d}h": tomorrow + hour / 100 for hour in range(24)
        }
    return data


def test_parse_today_and_tomorrow() -> None:
    prices = parse_hourly_prices(_attributes(0.10, tomorrow=0.20), TODAY)
    assert prices.at(datetime(2026, 11, 20, 6, 45, tzinfo=TZ)) == pytest.approx(0.16)
    assert prices.at(datetime(2026, 11, 21, 0, 0, tzinfo=TZ)) == pytest.approx(0.20)
    assert prices.at(datetime(2026, 11, 22, 0, 0, tzinfo=TZ)) is None


def test_bad_values_are_ignored() -> None:
    prices = parse_hourly_prices(
        {"price_03h": "n/a", "price_99h": 1, "price_04h": "0.2"}, TODAY
    )
    assert prices.days[TODAY][3] is None
    assert prices.days[TODAY][4] == 0.2


def test_slots_without_tomorrow_repeat_today() -> None:
    start = datetime(2026, 11, 20, 20, 0, tzinfo=TZ)
    result = slot_prices(
        start,
        36 * 4,
        TZ,
        buy=parse_hourly_prices(_attributes(0.10), TODAY),
        injection=None,
        fixed_surplus_value=0.05,
        compensation_cap_reached=False,
    )
    assert result is not None
    assert not result.tomorrow_known
    assert len(result.buy) == 144
    assert result.buy[0] == pytest.approx(0.30)  # 20:00 today
    assert result.buy[4 * 10] == pytest.approx(0.16)  # 06:00 tomorrow -> today 06:00
    assert set(result.surplus) == {0.05}
    # Night median used as terminal value.
    assert result.terminal_value() == pytest.approx(0.125, abs=0.01)


def test_tomorrow_prices_and_injection() -> None:
    start = datetime(2026, 11, 20, 21, 0, tzinfo=TZ)
    result = slot_prices(
        start,
        16,
        TZ,
        buy=parse_hourly_prices(_attributes(0.10, tomorrow=0.30), TODAY),
        injection=parse_hourly_prices(_attributes(0.05), TODAY),
        fixed_surplus_value=None,
        compensation_cap_reached=False,
    )
    assert result is not None and result.tomorrow_known
    assert result.buy[12] == pytest.approx(0.30)  # 00:00 tomorrow
    assert result.surplus[12] == pytest.approx(0.05)  # injection: today's 00h


def test_compensation_cap_makes_the_surplus_free() -> None:
    result = slot_prices(
        datetime(2026, 11, 20, 12, tzinfo=TZ),
        4,
        TZ,
        buy=parse_hourly_prices(_attributes(0.10), TODAY),
        injection=parse_hourly_prices(_attributes(0.05), TODAY),
        fixed_surplus_value=None,
        compensation_cap_reached=True,
    )
    assert result is not None and set(result.surplus) == {0.0}


def test_no_prices() -> None:
    empty = parse_hourly_prices({}, TODAY)
    start = datetime(2026, 11, 20, tzinfo=TZ)
    assert (
        slot_prices(
            start,
            4,
            TZ,
            buy=empty,
            injection=None,
            fixed_surplus_value=None,
            compensation_cap_reached=False,
        )
        is None
    )
    fallback = slot_prices(
        start,
        4,
        TZ,
        buy=empty,
        injection=None,
        fixed_surplus_value=None,
        compensation_cap_reached=False,
        current_buy=0.2,
    )
    assert fallback is not None and set(fallback.buy) == {0.2}
    assert start + timedelta(hours=1) > start
