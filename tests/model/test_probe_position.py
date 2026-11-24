"""A probe in the thermostat's sheath, low in the tank, is not the outlet.

24/11, evening: the tank was full at 69.4 °C at 13:53. 66 L were measured
from 17:37 to 20:09. The probe hardly moved for the first 30 L (68.5 → 66 °C)
and then fell to 32 °C as the cold front reached the sheath, while some 55 L
of hot water at 40 °C were still available at the top. TankWise 0.12.2 took
it for a probe at the top: every late draw was a hot water shortage, the
useful litres factor was learned from it, and the energy of the draws used
the probe as the outlet temperature.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tankwise_model.replay import (
    ROLE_ENERGY,
    ROLE_HOT_WATER_VOLUME,
    ROLE_POWER,
    ROLE_TEMPERATURE,
    changes,
    replay,
)
from tankwise_model.tank import TankModel, TankParams, TankSample

TZ = ZoneInfo("Europe/Madrid")
TOP = TankParams(mains_temperature=13.4, cutoff_temperature=68.5)
SHEATH = replace(TOP, probe_at_top=False)

PROBE_2411 = [
    ("17:25:48", 68.33), ("17:30:48", 68.46), ("17:37:48", 67.96),
    ("17:40:18", 68.25), ("17:43:18", 67.75), ("17:50:18", 68.0), ("17:59:48", 67.5),
    ("18:00:18", 67.62), ("18:10:18", 67.46), ("18:20:18", 67.5),
    ("18:30:18", 67.29), ("18:40:18", 67.38), ("18:52:48", 67.21),
    ("19:00:48", 67.17), ("19:10:18", 66.96), ("19:18:18", 66.46),
    ("19:20:18", 66.17), ("19:26:18", 65.62), ("19:27:48", 65.08),
    ("19:28:48", 64.5), ("19:29:48", 63.88), ("19:30:18", 63.67),
    ("19:31:18", 63.12), ("19:32:18", 62.04), ("19:32:48", 61.29),
    ("19:33:18", 60.67), ("19:33:48", 60.12), ("19:34:18", 59.62),
    ("19:35:18", 58.96), ("19:37:48", 58.38), ("19:38:48", 57.75),
    ("19:39:18", 56.79), ("19:39:48", 55.67), ("19:40:18", 54.75),
    ("19:40:48", 54.12), ("19:41:48", 53.46), ("19:42:48", 52.92),
    ("19:44:18", 52.12), ("19:44:48", 51.0), ("19:45:18", 49.54),
    ("19:45:48", 47.96), ("19:46:18", 46.25), ("19:46:48", 44.83),
    ("19:47:18", 43.96), ("19:48:18", 42.71), ("19:48:48", 41.58),
    ("19:49:18", 40.92), ("19:50:18", 40.29), ("19:51:18", 39.33),
    ("19:51:48", 38.21), ("19:52:18", 36.88), ("19:52:48", 35.62),
    ("19:53:18", 34.33), ("19:53:48", 33.46), ("19:54:48", 32.5), ("19:56:48", 32.0),
    ("20:00:48", 31.71), ("20:10:48", 31.67), ("20:17:18", 32.17),
    ("20:21:18", 32.04),
]  # fmt: skip
METER_2411 = [
    ("17:00:00", 351969), ("17:37:38", 351970), ("17:37:53", 351971),
    ("17:38:08", 351972), ("17:38:23", 351973), ("17:38:53", 351974),
    ("17:39:08", 351975), ("17:39:23", 351976), ("17:39:38", 351977),
    ("17:41:08", 351978), ("17:41:38", 351979), ("17:41:53", 351980),
    ("17:42:08", 351981), ("19:07:23", 351982), ("19:07:53", 351983),
    ("19:14:38", 351985), ("19:14:53", 351986), ("19:17:08", 351987),
    ("19:24:08", 351988), ("19:24:23", 351989), ("19:24:38", 351991),
    ("19:25:08", 351992), ("19:25:23", 351993), ("19:25:38", 351994),
    ("19:27:08", 351995), ("19:27:23", 351996), ("19:27:38", 351997),
    ("19:27:53", 351998), ("19:28:08", 351999), ("19:30:38", 352001),
    ("19:30:53", 352002), ("19:31:23", 352003), ("19:31:38", 352004),
    ("19:31:53", 352005), ("19:32:38", 352006), ("19:37:38", 352007),
    ("19:37:53", 352008), ("19:38:08", 352009), ("19:38:23", 352010),
    ("19:38:38", 352011), ("19:38:53", 352012), ("19:43:23", 352013),
    ("19:43:38", 352014), ("19:43:53", 352015), ("19:44:23", 352016),
    ("19:44:38", 352017), ("19:44:53", 352018), ("19:45:08", 352019),
    ("19:45:23", 352020), ("19:47:08", 352021), ("19:47:38", 352022),
    ("19:47:53", 352023), ("19:50:23", 352024), ("19:50:38", 352025),
    ("19:50:53", 352027), ("19:51:08", 352028), ("19:51:23", 352029),
    ("19:51:38", 352030), ("19:51:53", 352031), ("19:52:08", 352032),
    ("19:52:23", 352033), ("19:52:53", 352034), ("20:09:23", 352035),
]  # fmt: skip


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-24T{clock}").replace(tzinfo=TZ)


def _evening(params: TankParams) -> tuple[TankModel, object]:
    """From a full tank: calibrated, stratified, k_use learned, then the draws."""
    data = TankModel().as_dict()
    data.update(
        equivalent_temperature=66.6,
        hot_temperature=67.0,
        uncertain=False,
        calibration_count=2,
        loss_windows=33,
        usable_samples=[0.892] * 10,
        cutoff_samples=[69.1, 68.5, 67.9],
        stratification=[[67.0, 0.45]] * 10,
        last_valid_time=_at("17:25:00").isoformat(),
        meter_volume=351969.0,
        meter_time=_at("17:25:00").isoformat(),
    )
    model = TankModel.from_dict(data)
    series = {
        ROLE_TEMPERATURE: changes((_at(t), v) for t, v in PROBE_2411),
        ROLE_POWER: changes([(_at("17:00:00"), 0.0)]),
        ROLE_ENERGY: changes([(_at("17:00:00"), 3400.0)]),
        ROLE_HOT_WATER_VOLUME: changes((_at(t), float(v)) for t, v in METER_2411),
    }
    result = replay(
        series, params, model=model, start=_at("17:26:00"), end=_at("21:00:00")
    )
    return model, result.last_state


def test_probe_at_top_takes_the_cold_front_for_a_shortage() -> None:
    """The 0.12.2 behaviour, kept for a probe that really is at the top."""
    model, _state = _evening(TOP)
    assert any(draw.shortage for draw in model.consumptions)


def test_probe_in_the_sheath_is_not_the_outlet() -> None:
    model, state = _evening(SHEATH)
    draws = model.consumptions
    assert sum(draw.liters or 0 for draw in draws) == 66
    # The cold front reached the probe, not the outlet: no shortage.
    assert not any(draw.shortage for draw in draws)
    # The hot water left at the temperature of the hot layer, not the probe's.
    late = [draw for draw in draws if draw.start >= _at("19:35:00")]
    assert late and all(draw.start_temperature > 60 for draw in late)
    # The probe reads 32 °C; the hot layer at the top is still there.
    assert state.probe_temperature == pytest.approx(32.0, abs=0.5)
    assert state.outlet_temperature is not None and state.outlet_temperature > 60
    # The model follows the metered energy, not the probe: some 55-70 L left.
    assert 50 <= state.liters_available <= 75
    # k_use is not learned from the probe crossing the comfort temperature.
    assert model.usable_factor == pytest.approx(0.892)


def test_energy_of_the_evening_draws_uses_the_hot_layer() -> None:
    sheath, _ = _evening(SHEATH)
    top, _ = _evening(TOP)
    energy_sheath = sum(draw.energy_kwh for draw in sheath.consumptions)
    energy_top = sum(draw.energy_kwh for draw in top.consumptions)
    # 66 L at about 66 °C, mains 13.4 °C: about 4 kWh.
    assert energy_sheath == pytest.approx(66 * 0.001163 * 52.5, rel=0.06)
    assert energy_top < energy_sheath  # the probe's 59, 51, 40 °C...


# ---------------------------------------------------------------------------
# Partial heating with the probe in the sheath
# ---------------------------------------------------------------------------

STRATIFIED = [[60.0, 0.45]] * 10


def _layered(hot: float, mixed: float, *, samples: list | None = None) -> TankModel:
    """A hot layer at ``hot`` over cold water: ``mixed`` is its mean."""
    data = TankModel().as_dict()
    data.update(
        equivalent_temperature=mixed,
        hot_temperature=hot,
        uncertain=False,
        calibration_count=2,
        loss_windows=33,
        stratification=STRATIFIED,
        usable_samples=samples or [],
        last_valid_time=_at("10:00:00").isoformat(),
        last_energy=100.0,
        meter_volume=1000.0,
        meter_time=_at("10:00:00").isoformat(),
    )
    return TankModel.from_dict(data)


def _heat(model: TankModel, kwh: float, params: TankParams = SHEATH):
    """Heat at 1 kW from the bottom (a sunny partial heating), no draws."""
    minutes = round(kwh * 60)
    energy, state = 100.0, None
    for minute in range(1, minutes + 1):
        energy += 1 / 60
        state = model.update(
            TankSample(
                timestamp=_at("10:00:00") + timedelta(minutes=minute),
                temperature=40.0,  # the probe next to the element: warm water
                power=1000.0,
                energy=energy,
                hot_water_volume=1000.0,
                meter_configured=True,
            ),
            params,
        )
    return state


def test_partial_heating_stays_below_a_hotter_layer() -> None:
    """Hot layer at 50 °C, cold bottom, +1 kWh without reaching the cut-off.

    The element heats the cold water at the bottom; it cannot rise through
    water hotter than itself, so the energy stays below the hot layer: the
    mean rises, the hot layer does not, and the litres follow the energy.
    """
    model = _layered(hot=50.0, mixed=35.0)
    start = model.update(
        TankSample(
            timestamp=_at("10:00:00"),
            temperature=40.0,
            power=0.0,
            energy=100.0,
            hot_water_volume=1000.0,
            meter_configured=True,
        ),
        SHEATH,
    )
    after = _heat(model, 1.0)
    assert after.calibration_count == 2  # no cut-off
    gained = after.equivalent_temperature - start.equivalent_temperature
    assert gained == pytest.approx(0.9 * 1.0 / SHEATH.heat_capacity, abs=0.5)
    assert after.outlet_temperature == pytest.approx(50.0, abs=0.5)  # only losses
    # The litres follow the energy: k_use (default 0.85) x k_cap x 1 kWh.
    added = after.liters_available - start.liters_available
    expected = 0.85 * 0.9 * 1.0 / (0.001163 * (40 - SHEATH.mains_temperature))
    assert added == pytest.approx(expected, rel=0.08)


def test_heating_past_the_hot_layer_raises_it() -> None:
    """+3 kWh: the mean passes 50 °C, the tank mixes and the layer follows."""
    model = _layered(hot=50.0, mixed=35.0)
    after = _heat(model, 3.0)
    assert after.equivalent_temperature > 55
    assert after.outlet_temperature == pytest.approx(after.equivalent_temperature)


def test_default_useful_litres_factor_with_the_probe_in_the_sheath() -> None:
    model = _layered(hot=60.0, mixed=40.0)
    assert model.usable_factor_for(SHEATH) == 0.85
    assert model.usable_factor_for(TOP) == 1.0  # as before
    assert model.usable_factor_for(replace(SHEATH, usable_factor_default=0.8)) == 0.8
    learned = _layered(hot=60.0, mixed=40.0, samples=[0.9] * 5)
    assert learned.usable_factor_for(SHEATH) == pytest.approx(0.9)
    assert learned.reset_usable_factor() == 5
    assert learned.usable_factor_for(SHEATH) == 0.85
    assert learned.as_dict()["usable_samples"] == []
