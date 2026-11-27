"""Automatic compensation cap: the past hours come from the recorder statistics."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.tankwise.compensation_history import (
    async_import_compensation,
)
from custom_components.tankwise.model.compensation import CompensationTracker

IMPORT = "sensor.grid_import_energy"
EXPORT = "sensor.grid_export_energy"
BUY = "sensor.pvpc"
INJECTION = "sensor.injection_price"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Any, enable_custom_integrations: None
) -> None:
    """The recorder must be set up before Home Assistant in these tests."""


def _meta(statistic_id: str, unit: str, *, has_sum: bool) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=has_sum,
        mean_type=StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement=unit,
    )


async def test_past_hours_are_imported_from_the_statistics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    now = dt_util.parse_datetime("2026-11-18T12:30:00+00:00")
    freezer.move_to(now)
    first = now.replace(minute=0) - timedelta(hours=6)
    hours = [first + timedelta(hours=index) for index in range(6)]
    imported, exported = 1000.0, 500.0
    import_rows, export_rows, buy_rows, injection_rows = [], [], [], []
    for index, hour in enumerate(hours):
        imported += 0.5
        exported += 2.0 if index >= 3 else 0.0
        import_rows.append(StatisticData(start=hour, state=imported, sum=imported))
        export_rows.append(StatisticData(start=hour, state=exported, sum=exported))
        buy_rows.append(StatisticData(start=hour, mean=0.20, min=0.20, max=0.20))
        injection_rows.append(StatisticData(start=hour, mean=0.10, min=0.10, max=0.10))
    async_import_statistics(hass, _meta(IMPORT, "kWh", has_sum=True), import_rows)
    async_import_statistics(hass, _meta(EXPORT, "kWh", has_sum=True), export_rows)
    async_import_statistics(hass, _meta(BUY, "€/kWh", has_sum=False), buy_rows)
    async_import_statistics(
        hass, _meta(INJECTION, "€/kWh", has_sum=False), injection_rows
    )
    await async_wait_recording_done(hass)

    tracker = CompensationTracker()
    # An hour the tracker already accounted live is kept as it is.
    tracker.add_hour(hours[4], 9.0, 9.0, 1.0, 1.0)
    added = await async_import_compensation(
        hass,
        tracker,
        import_sensor=IMPORT,
        export_sensor=EXPORT,
        buy_sensor=BUY,
        injection_sensor=INJECTION,
    )
    # The first hour has no hour before it; the 5th was known.
    assert added == 4
    assert hours[0] not in tracker.hours
    assert tracker.hours[hours[4]].import_kwh == 9.0
    assert tracker.hours[hours[3]].export_kwh == pytest.approx(2.0)
    assert tracker.hours[hours[3]].compensation_eur == pytest.approx(0.2)
    assert tracker.hours[hours[1]].energy_eur == pytest.approx(0.1)
    # Without the recorder statistics of a sensor nothing is added.
    assert (
        await async_import_compensation(
            hass,
            CompensationTracker(),
            import_sensor="sensor.missing",
            export_sensor=EXPORT,
            buy_sensor=BUY,
            injection_sensor=INJECTION,
        )
        == 0
    )
