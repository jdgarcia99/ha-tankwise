"""Signal processing helpers for noisy temperature probes."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime
import math
from statistics import median


class ProbeFilter:
    """Median + exponential moving average filter for a noisy probe.

    The median over the last few samples removes isolated jumps; the
    exponential average (time constant ``tau_s``) smooths the remaining
    jitter. The filter restarts from scratch after a gap longer than
    ``max_gap_s``.
    """

    def __init__(
        self, window: int = 5, tau_s: float = 240.0, max_gap_s: float = 300.0
    ) -> None:
        """Initialise the filter."""
        self._window: deque[float] = deque(maxlen=window)
        self._tau_s = tau_s
        self._max_gap_s = max_gap_s
        self._value: float | None = None
        self._last_time: datetime | None = None

    @property
    def value(self) -> float | None:
        """Return the current filtered value."""
        return self._value

    @property
    def tau_s(self) -> float:
        """Return the time constant of the exponential average."""
        return self._tau_s

    @tau_s.setter
    def tau_s(self, value: float) -> None:
        """Change the time constant (e.g. to follow a fast transient)."""
        self._tau_s = value

    def reset(self) -> None:
        """Forget all previous samples."""
        self._window.clear()
        self._value = None
        self._last_time = None

    def update(self, timestamp: datetime, raw: float) -> float:
        """Add a raw sample and return the filtered value."""
        if (
            self._value is None
            or self._last_time is None
            or (timestamp - self._last_time).total_seconds() > self._max_gap_s
        ):
            self.reset()
            self._window.append(raw)
            self._value = raw
            self._last_time = timestamp
            return raw

        dt = max((timestamp - self._last_time).total_seconds(), 0.0)
        self._window.append(raw)
        alpha = 1.0 - math.exp(-dt / self._tau_s)
        self._value += alpha * (median(self._window) - self._value)
        self._last_time = timestamp
        return self._value


def theil_sen(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Return a robust (slope, intercept) fit of ``(x, y)`` points.

    The slope is the median of all pairwise slopes and the intercept the
    median of ``y - slope * x``. Requires at least two distinct x values.
    """
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1 :]
        if x2 != x1
    ]
    if not slopes:
        raise ValueError("at least two points with different x are required")
    slope = median(slopes)
    intercept = median(y - slope * x for x, y in points)
    return slope, intercept
