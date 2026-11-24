"""Cut-off samples after a history import: the union of both, by time and value.

24/11: after installing 0.12.2 the list stayed [69.1, 68.5, 67.9] and the
69.4 °C cut-off of 13:49 was missing. The import of the last 10 days had fewer
samples than the running model (older ones fall outside the window), and the
list with more samples won.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tankwise_model.tank import MAX_CUTOFF_SAMPLES, TankModel


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def _model(samples: list[tuple[datetime | None, float]]) -> TankModel:
    data = TankModel().as_dict()
    data["cutoff_samples"] = [value for _, value in samples]
    data["cutoff_times"] = [
        None if moment is None else moment.isoformat() for moment, _ in samples
    ]
    return TankModel.from_dict(data)


def test_reimport_with_one_new_sample_keeps_the_old_ones() -> None:
    """Saved: three old samples (no time, 0.12.2). Imported: one new one."""
    running = _model([(None, 69.1), (None, 68.5), (None, 67.9)])
    imported = _model([(_at(24, 12, 53), 69.4)])
    imported.merge_learning(running)
    assert imported.cutoff_samples == (69.1, 68.5, 67.9, 69.4)
    assert imported.as_dict()["cutoff_times"][-1] == _at(24, 12, 53).isoformat()


def test_the_same_sample_in_both_is_kept_once() -> None:
    old = (_at(20, 15, 4), 68.5)
    running = _model([(None, 69.1), old, (_at(24, 12, 53), 69.4)])
    # The import finds the same cut-offs again, at the same poll.
    imported = _model([(old[0] + timedelta(seconds=30), 68.5), (_at(24, 12, 53), 69.4)])
    imported.merge_learning(running)
    assert imported.cutoff_samples == (69.1, 68.5, 69.4)
    # A sample saved without a time matches one with the same value.
    imported = _model([(_at(19, 10, 0), 69.1)])
    imported.merge_learning(_model([(None, 69.1)]))
    assert imported.cutoff_samples == (69.1,)
    assert imported.as_dict()["cutoff_times"] == [_at(19, 10, 0).isoformat()]


def test_the_merged_list_keeps_the_most_recent_samples() -> None:
    start = _at(1, 0)
    running = _model(
        [
            (start + timedelta(days=day), 68.0 + day / 100)
            for day in range(MAX_CUTOFF_SAMPLES)
        ]
    )
    imported = _model([(_at(24, 12, 53), 69.4)])
    imported.merge_learning(running)
    samples = imported.cutoff_samples
    assert len(samples) == MAX_CUTOFF_SAMPLES
    assert samples[-1] == 69.4
    assert samples[0] == 68.01  # the oldest one is dropped


def test_a_discarded_sample_is_not_brought_back_by_an_import() -> None:
    running = _model([(None, 68.5), (_at(23, 10, 51), 60.9)])
    running.discard_cutoff_sample(value=60.9)
    imported = _model([(_at(23, 10, 51), 60.9), (_at(24, 12, 53), 69.4)])
    imported.merge_learning(running)
    assert imported.cutoff_samples == (68.5, 69.4)
    # Remembered across a restart and the next import.
    restored = TankModel.from_dict(imported.as_dict())
    again = _model([(_at(23, 10, 51), 60.9)])
    again.merge_learning(restored)
    assert 60.9 not in again.cutoff_samples


def test_times_are_saved_and_old_data_loads_without_them() -> None:
    model = _model([(None, 68.5), (_at(24, 12, 53), 69.4)])
    data = model.as_dict()
    assert data["cutoff_times"] == [None, _at(24, 12, 53).isoformat()]
    assert TankModel.from_dict(data).as_dict()["cutoff_times"] == data["cutoff_times"]
    del data["cutoff_times"], data["discarded_cutoffs"]
    old = TankModel.from_dict(data)
    assert old.cutoff_samples == (68.5, 69.4)
    assert old.as_dict()["cutoff_times"] == [None, None]
