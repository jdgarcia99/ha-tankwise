"""Simulation bench on the exported history (opt-in: TANKWISE_BENCH=1).

Takes about 20 seconds. Prints the day by day report with ``-s``.
"""

from __future__ import annotations

from datetime import datetime
import os

import pytest

from .bench import report, run_bench
from .history import find_export, load

EXPORT = find_export()
pytestmark = pytest.mark.skipif(
    EXPORT is None or not os.environ.get("TANKWISE_BENCH"),
    reason="needs a history export and TANKWISE_BENCH=1",
)


def test_plan_against_reality() -> None:
    assert EXPORT is not None
    history = load(EXPORT)
    results = run_bench(history, start=datetime(2026, 11, 13, tzinfo=history.tz))
    print("\n" + report(results))
    full = [item for item in results if item.real_grid_kwh + item.real_solar_kwh > 0.5]
    assert full
    # No hot water shortage with the plan, no manual session over two hours.
    assert sum(len(item.plan_shortages) for item in full) == 0
    assert max(item.longest_heating_min for item in full) <= 120
    # The plan costs less than what really happened.
    assert sum(item.plan_cost for item in full) < sum(item.real_cost for item in full)
