"""Draws measured by a hot water meter at the outlet of the tank.

The reference case is the evening of 23/11: the probe (fed by the diverter)
was unavailable from 17:26 to 18:44 while 34 L left the tank at about 67 °C
(5 L at 17:44, 28 L from 17:56 to 18:10 and a single litre at 18:33). Without
the meter TankWise estimated 0.98 kWh from the probe drop; a mixed tank
would have dropped about 17 °C, the stratified probe only dropped 8.5 °C.
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
from tankwise_model.tank import (
    METER_DRAW_END,
    METERED_PROBE_DEADBAND,
    MIN_STRATIFICATION_SAMPLES,
    WATER_HEAT_CAPACITY_KWH_PER_L_K,
    ConsumptionEvent,
    TankModel,
    TankParams,
    TankSample,
)

from .layered import LayeredTank
from .simulator import PARAMS as SIM_PARAMS, TankSimulator, drive

TZ = ZoneInfo("Europe/Madrid")
MAINS = 13.4
PARAMS = TankParams(mains_temperature=MAINS, cutoff_temperature=68.0)
TOTAL_AT_START = 351890.0

# Probe readings of 23/11 (None: unavailable).
PROBE_2311 = [
    ("17:00:03", 67.25),
    ("17:20:03.8", 67.75),
    ("17:21:03.8", 68.0),
    ("17:22:03.8", 67.5),
    ("17:23:03.8", 68.0),
    ("17:24:03.8", 67.5),
    ("17:26:18.3", None),
    ("18:44:15.1", 59.0),
    ("18:45:14.2", 59.25),
    ("18:46:14.2", 59.5),
    ("18:47:14.2", 60.0),
    ("18:48:14.2", 60.25),
    ("18:49:14.6", 60.0),
]
# Meter total of 23/11 (litres): one entry per state change.
METER_2311 = [
    ("17:43:52.5", 351891),
    ("17:44:07.5", 351892),
    ("17:44:22.5", 351894),
    ("17:44:37.5", 351895),
    *(
        (clock, 351896 + index)
        for index, clock in enumerate(
            [
                "17:56:37.5",
                "17:57:07.5",
                "17:57:22.5",
                "17:57:37.5",
                "17:58:07.5",
                "17:58:22.5",
                "17:58:37.5",
                "17:59:07.5",
                "17:59:22.5",
                "18:00:52.5",
                "18:01:22.5",
                "18:01:37.5",
                "18:01:52.5",
                "18:02:07.5",
                "18:02:22.5",
                "18:04:22.5",
                "18:04:52.5",
                "18:07:22.4",
                "18:07:52.5",
                "18:08:22.5",
                "18:08:37.6",
                "18:08:52.5",
                "18:09:07.5",
                "18:09:22.5",
                "18:09:37.6",
                "18:09:52.5",
                "18:10:07.6",
                "18:10:22.5",
            ]
        )
    ),
    ("18:33:22.6", 351924),
]


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-23T{clock}").replace(tzinfo=TZ)


def _replay_2311(*, meter: bool) -> tuple[TankModel, object]:
    series = {
        ROLE_TEMPERATURE: changes((_at(clock), value) for clock, value in PROBE_2311),
        # The Shelly reported 0 W but for a 1.9 W blip that added 1 mWh.
        ROLE_POWER: changes(
            [(_at("17:00:00"), 0.0), (_at("18:40:36.6"), 1.9), (_at("18:40:38.5"), 0.0)]
        ),
        ROLE_ENERGY: changes(
            [(_at("17:00:00"), 3395.267884), (_at("18:41:00.3"), 3395.267885)]
        ),
    }
    if meter:
        series[ROLE_HOT_WATER_VOLUME] = changes(
            [(_at("17:00:00"), TOTAL_AT_START)]
            + [(_at(clock), float(total)) for clock, total in METER_2311]
        )
    model = TankModel()
    result = replay(
        series, PARAMS, model=model, start=_at("17:00:30"), end=_at("19:00:30")
    )
    return model, result.last_state


def _energy(event: ConsumptionEvent) -> float:
    assert event.liters is not None
    span = event.start_temperature - MAINS
    return event.liters * WATER_HEAT_CAPACITY_KWH_PER_L_K * span


# ---------------------------------------------------------------------------
# The real case
# ---------------------------------------------------------------------------


def test_real_case_with_the_meter() -> None:
    """The litres are recorded although the probe was missing all along."""
    model, _state = _replay_2311(meter=True)
    draws = model.consumptions
    assert [draw.liters for draw in draws] == [5.0, 28.0, 1.0]
    first, shower, _single = draws
    # From the reading before the first litre to the last litre.
    assert first.start == _at("17:43:30")
    assert first.end == _at("17:45:30")
    assert shower.start == _at("17:56:30")  # 12 minutes later: another draw
    assert shower.end == _at("18:10:30")
    # T_out: the last probe reading (filtered), not lowered by the drop.
    assert first.start_temperature == pytest.approx(67.5, abs=0.3)
    assert shower.start_temperature == first.start_temperature
    for draw in draws:
        assert draw.energy_kwh == pytest.approx(_energy(draw))
        assert draw.metered
        assert not draw.while_heating
        assert not draw.estimated
        assert not draw.slow
    assert shower.energy_kwh == pytest.approx(1.76, abs=0.03)
    # 33 L until 18:10: twice what the probe drop gave without the meter.
    assert first.energy_kwh + shower.energy_kwh == pytest.approx(2.07, abs=0.03)


def test_real_case_learns_the_stratification() -> None:
    """The probe fell 8.5 °C where the metered energy drops a mixed tank 17."""
    model, state = _replay_2311(meter=True)
    [(start, ratio)] = model.stratification_samples
    assert start == pytest.approx(67.5, abs=0.3)
    assert ratio == pytest.approx(0.48, abs=0.04)
    assert state.stratification_samples == 1
    # One sample is not enough to use it: the probe still corrects the model.
    assert state.stratification_ratio == 1.0
    assert MIN_STRATIFICATION_SAMPLES > 1


def test_real_case_without_the_meter() -> None:
    """v0.11.1 behaviour: one estimated draw over the gap of the probe."""
    model, _state = _replay_2311(meter=False)
    [draw] = model.consumptions
    assert draw.estimated
    assert not draw.metered
    assert draw.start == _at("17:25:30")
    assert draw.end == _at("18:44:30")
    assert draw.energy_kwh == pytest.approx(0.982, abs=0.005)
    # The 1 mWh of the 1.9 W blip at 18:40 is not a heating.
    assert not draw.while_heating


# ---------------------------------------------------------------------------
# Without a meter nothing changes
# ---------------------------------------------------------------------------

# Draws (start, end UTC; kWh; flags: S shortage, E estimated) and final
# state of the scenario below, computed with TankWise 0.11.1.
GOLDEN_0_11_1 = {
    "draws": [
        ("01:53", "02:13", 1.883242, ""),
        ("04:17", "04:31", 0.177249, "S"),
        ("04:36", "04:45", 0.196745, "S"),
        ("04:53", "05:05", 0.128218, "S"),
        ("06:28", "07:19", 0.54513, "SE"),
    ],
    "equivalent": 46.654633,
    "calibrations": 2,
    "reason": "few_loss_windows",
}


def test_without_a_meter_the_model_is_the_same_as_0_11_1() -> None:
    """A day with a shower, a slow tap, a gap with a draw and two heatings."""
    sim = TankSimulator(temperature=40.0)
    model = TankModel()
    drive(model, sim, 10)
    while sim.thermostat_closed:
        drive(model, sim, 1, regulator=100)
    drive(model, sim, 10, regulator=100)
    drive(model, sim, 60)
    drive(model, sim, 12, draw_kw=9.0)
    drive(model, sim, 120)
    drive(model, sim, 50, draw_kw=0.6)
    drive(model, sim, 90)
    sim.skip(45)
    sim.run(5, draw_kw=6.0)
    drive(model, sim, 30)
    while sim.thermostat_closed:
        drive(model, sim, 1, regulator=100)
    drive(model, sim, 10, regulator=100)
    state = drive(model, sim, 60)

    draws = [
        (
            f"{event.start:%H:%M}",
            f"{event.end:%H:%M}",
            round(event.energy_kwh, 6),
            "S" * event.shortage + "E" * event.estimated,
        )
        for event in model.consumptions
    ]
    assert not any(event.while_heating or event.slow for event in model.consumptions)
    assert draws == GOLDEN_0_11_1["draws"]
    assert all(event.liters is None for event in model.consumptions)
    assert state.equivalent_temperature is not None
    assert round(state.equivalent_temperature, 6) == GOLDEN_0_11_1["equivalent"]
    assert state.calibration_count == GOLDEN_0_11_1["calibrations"]
    assert state.status_reason == GOLDEN_0_11_1["reason"]
    assert state.stratification_samples == 0


# ---------------------------------------------------------------------------
# Grouping the litres into draws
# ---------------------------------------------------------------------------

START = datetime(2026, 11, 2, 7, 0, tzinfo=TZ)


class _Feed:
    """Minute samples with a steady probe, no heating and a meter."""

    def __init__(self, *, temperature: float = 60.0, params: TankParams = PARAMS):
        self.model = TankModel()
        self.params = params
        self.time = START
        self.volume = 1000.0
        self.temperature: float | None = temperature
        self.power = 0.0
        self.step(0.0)

    def step(self, liters: float = 0.0, *, flow: float | None = None) -> None:
        self.volume += liters
        self.model.update(
            TankSample(
                timestamp=self.time,
                temperature=self.temperature,
                power=self.power,
                energy=10.0,
                hot_water_volume=self.volume,
                hot_water_flow=flow,
                meter_configured=True,
            ),
            self.params,
        )
        self.time += timedelta(minutes=1)

    def run(self, minutes: int, liters: float = 0.0) -> None:
        for _ in range(minutes):
            self.step(liters)


def test_single_litres_are_grouped_into_one_draw() -> None:
    """A litre every two minutes is one slow draw, not five."""
    feed = _Feed()
    for _ in range(5):
        feed.step(1.0)
        feed.step()
    feed.run(5)
    [draw] = feed.model.consumptions
    assert draw.liters == 5


def test_three_minutes_without_litres_end_the_draw() -> None:
    feed = _Feed()
    feed.run(3, liters=4.0)
    minutes = int(METER_DRAW_END / timedelta(minutes=1))
    feed.run(minutes)
    feed.run(2, liters=4.0)
    feed.run(minutes)
    assert [draw.liters for draw in feed.model.consumptions] == [12.0, 8.0]


def test_flow_keeps_the_draw_open() -> None:
    """While the meter reports flow, a pause in the litres is not the end."""
    feed = _Feed()
    feed.step(3.0, flow=3.0)
    for _ in range(5):
        feed.step(0.0, flow=0.4)  # a trickle below one litre per minute
    feed.step(2.0, flow=2.0)
    feed.run(5)
    [draw] = feed.model.consumptions
    assert draw.liters == 5


def test_energy_uses_the_temperature_when_the_draw_started() -> None:
    """The probe falls during the draw; the water still left hot."""
    feed = _Feed(temperature=62.0)
    for minute in range(10):
        feed.temperature = 62.0 - minute  # the probe drops 9 °C
        feed.step(4.0)
    feed.run(5)
    [draw] = feed.model.consumptions
    assert draw.start_temperature == pytest.approx(62.0, abs=0.05)
    assert draw.energy_kwh == pytest.approx(
        40 * WATER_HEAT_CAPACITY_KWH_PER_L_K * (62.0 - MAINS), rel=1e-3
    )
    assert draw.min_temperature == pytest.approx(53.0)


def test_a_reset_or_a_glitch_of_the_meter_is_not_a_draw() -> None:
    feed = _Feed()
    feed.volume = 0.0  # the counter restarted from zero
    feed.step()
    feed.run(5)
    feed.step(500.0)  # 500 L in a minute: impossible
    feed.run(5)
    assert feed.model.consumptions == []


def test_litres_during_a_gap_of_the_probe_are_recorded() -> None:
    """A shower while the probe is unavailable: the litres are not lost."""
    feed = _Feed(temperature=60.0)
    feed.run(10)
    feed.temperature = None
    feed.run(10, liters=3.0)
    feed.run(10)
    feed.temperature = 52.0
    feed.run(10)
    [draw] = feed.model.consumptions
    assert draw.liters == 30
    assert not draw.estimated  # the meter saw every litre
    assert draw.start_temperature == pytest.approx(60.0, abs=0.1)
    assert draw.energy_kwh == pytest.approx(_energy(draw))


def test_litres_during_a_gap_of_the_meter_are_one_estimated_draw() -> None:
    feed = _Feed()
    feed.run(5)
    feed.time += timedelta(minutes=30)  # nothing read for 30 minutes
    feed.step(20.0)
    feed.run(5)
    [draw] = feed.model.consumptions
    assert draw.liters == 20
    assert draw.estimated
    assert draw.end - draw.start > timedelta(minutes=30)


def test_heating_during_the_draw_is_recorded() -> None:
    feed = _Feed()
    feed.power = 1500.0
    feed.run(3, liters=5.0)
    feed.power = 0.0
    feed.run(5)
    [draw] = feed.model.consumptions
    assert draw.while_heating


def test_probe_noise_below_a_degree_does_not_move_the_model() -> None:
    """With a meter the energy balance is trusted over the probe resolution."""
    steady = _Feed(temperature=60.0)
    noisy = _Feed(temperature=60.0)
    noisy.temperature = 60.0 + METERED_PROBE_DEADBAND - 0.25
    # 30 minutes: the losses lower the model 0.2 °C below the steady probe.
    steady.run(30)
    noisy.run(30)
    # A probe 0.75 °C higher does not pull the model: only the losses act.
    assert noisy.model.as_dict()["equivalent_temperature"] == pytest.approx(
        steady.model.as_dict()["equivalent_temperature"]
    )
    # Beyond the deadband it does.
    far = _Feed(temperature=60.0)
    far.temperature = 63.0
    far.run(30)
    assert (
        far.model.as_dict()["equivalent_temperature"]
        > steady.model.as_dict()["equivalent_temperature"] + 0.5
    )


# ---------------------------------------------------------------------------
# Stratification
# ---------------------------------------------------------------------------

LAYERED_PARAMS = TankParams(
    mains_temperature=14.0, cutoff_temperature=68.0, capacity_factor=1.0
)


class _Layered:
    """A stratified tank read by the model, with a meter at the outlet."""

    def __init__(self) -> None:
        self.tank = LayeredTank(temperature=66.0)
        self.model = TankModel()
        self.volume = 1000.0

    def run(self, minutes: int, *, liters: float = 0.0, heat: bool = False):
        state = None
        for _ in range(minutes):
            sample = self.tank.step(heat=heat, liters=liters)
            self.volume += liters
            state = self.model.update(
                replace(sample, hot_water_volume=self.volume, meter_configured=True),
                LAYERED_PARAMS,
            )
        assert state is not None
        return state

    def heat_to_cutoff(self) -> None:
        count = self.model.as_dict()["calibration_count"]
        for _ in range(300):
            self.run(1, heat=True)
            if self.model.as_dict()["calibration_count"] > count:
                break
        self.run(30)


def test_stratification_is_learned_and_keeps_the_energy_drawn() -> None:
    """Showers of 30 L: the probe at the top hardly falls.

    Before the ratio is learned the probe pulls the model back up (a mixed
    tank is assumed); once learned, the model keeps the energy the meter saw
    leave.
    """
    sim = _Layered()
    sim.run(30)
    rises = []
    for _ in range(4):
        after_draw = sim.run(10, liters=3.0)
        rested = sim.run(40)
        assert after_draw.equivalent_temperature is not None
        assert rested.equivalent_temperature is not None
        rises.append(rested.equivalent_temperature - after_draw.equivalent_temperature)
        sim.heat_to_cutoff()
    samples = sim.model.stratification_samples
    assert len(samples) == 4
    assert all(0.2 <= ratio <= 0.4 for _, ratio in samples)
    assert sim.model.stratification_ratio(66.0) < 0.4
    # First shower: pulled up by 3 °C towards the probe; once learned: kept.
    assert rises[0] > 2.0
    assert abs(rises[-1]) < 0.5
    # The energy of each shower is what really left the tank.
    total = sum(draw.energy_kwh for draw in sim.model.consumptions)
    assert total == pytest.approx(sim.tank.drawn_kwh, rel=0.05)


def test_small_draws_do_not_teach_the_stratification() -> None:
    """Below 4 °C in a mixed tank the probe (±1 °C) cannot tell."""
    # 6 L at 60 °C lower a mixed 100 L tank by 2.5 °C (k_cap 0.9).
    expected = 6 * (60.0 - MAINS) / PARAMS.volume_l * PARAMS.capacity_factor
    assert expected < 4.0
    feed = _Feed(temperature=60.0)
    feed.run(10)
    feed.run(1, liters=6.0)
    feed.run(20)
    assert len(feed.model.consumptions) == 1
    assert feed.model.stratification_samples == []


def test_heating_during_the_draws_does_not_teach_the_stratification() -> None:
    feed = _Feed(temperature=60.0)
    feed.run(10)
    feed.power = 1500.0
    feed.run(5, liters=6.0)
    feed.power = 0.0
    feed.run(30)
    assert feed.model.stratification_samples == []


# ---------------------------------------------------------------------------
# Persistence and energy balance
# ---------------------------------------------------------------------------


def test_meter_state_is_saved_and_restored() -> None:
    feed = _Feed(temperature=60.0)
    feed.run(10)
    feed.run(10, liters=3.0)
    feed.run(10)
    data = feed.model.as_dict()
    assert data["consumptions"][-1]["liters"] == 30
    restored = TankModel.from_dict(data)
    assert restored.as_dict()["consumptions"] == data["consumptions"]
    assert restored.as_dict()["meter_volume"] == feed.volume
    assert restored.stratification_samples == feed.model.stratification_samples


def test_data_saved_by_0_11_loads_without_meter_fields() -> None:
    feed = _Feed(temperature=60.0)
    data = feed.model.as_dict()
    for key in ("meter_volume", "meter_time", "stratification"):
        data.pop(key)
    data.pop("stratification_offset")
    model = TankModel.from_dict(data)
    assert model.stratification_samples == []
    assert model.stratification_ratio() == 1.0
    event = ConsumptionEvent.from_dict(
        {
            "start": START.isoformat(),
            "end": START.isoformat(),
            "energy_kwh": 1.0,
            "start_temperature": 60,
            "min_temperature": 50,
            "while_heating": False,
        }
    )
    assert event.liters is None
    assert not event.metered


def test_reconciliation_never_scales_metered_draws() -> None:
    """The energy balance after a heating does not touch measured draws."""
    sim = TankSimulator(temperature=40.0, cutoff=60.0)
    model = TankModel()
    volume = 1000.0

    def run(minutes: int, *, regulator: float = 0.0, liters: float = 0.0) -> None:
        nonlocal volume
        for _ in range(minutes):
            kwh_per_min = (
                liters
                * WATER_HEAT_CAPACITY_KWH_PER_L_K
                * (sim.temperature - SIM_PARAMS.mains_temperature)
            )
            sample = sim.step(regulator=regulator, draw_kw=kwh_per_min * 60)
            volume += liters
            model.update(
                replace(sample, hot_water_volume=volume, meter_configured=True),
                SIM_PARAMS,
            )

    def fill() -> None:
        while sim.thermostat_closed:
            run(1, regulator=100)
        run(10, regulator=100)
        run(30)

    run(10)
    fill()
    run(8, liters=5.0)
    run(30)
    fill()
    assert model.reconciliations  # a balance was made
    for draw in model.consumptions:
        assert draw.metered
        assert draw.reconciled is None
