"""Rolling history of raw TV↔master deviations, and the maths to plot it.

WHY THIS IS NOT IN src/gui/
    `src/gui/__init__.py` imports `main_window`, so anything under that package
    pulls in PyQt6 and cannot be exercised without it. Keeping the buffer and
    the scaling arithmetic here means the part that can actually be wrong is
    testable; the widget over in trades_tab.py only paints what this returns.

WHAT IS STORED
    One point per measurement: the RAW deviation, plotted as a plain line. Not
    the median-smoothed offset the converter uses — the question is whether the
    smoothing is discarding noise or hiding a real move, and a plot of the
    smoothed value cannot answer that.

THE AXIS
    `bounds` is percentile-based, not min/max. A delayed feed occasionally
    produces one badly-paired sample (a bar from the wrong minute, a quote
    caught mid-update), and a single such value at +98 against a true offset
    near -66 rescales the whole plot into a flat line with one spike. The
    outlier is still drawn — it just clips at the frame instead of dictating the
    scale.

    Only successful measurements belong here. A cycle that fell back to its
    cached offset has `raw_diff == 0.0` (see price_calc._resolve_pair), so
    plotting it would draw a spike to zero; the caller filters on `stale`, and
    the resulting gap in the series is the honest picture.
"""
import time
from collections import deque
from dataclasses import dataclass

#: How much history to keep per ticker. The chart's visible span is a subset.
DEFAULT_WINDOW_S = 1800.0

#: Smallest y-range to render. A dead-flat offset would otherwise scale to a
#: zero-height band and amplify float noise into a fake mountain range.
MIN_RANGE = 1e-6


@dataclass(frozen=True)
class Sample:
    """One measurement. ``ago`` is seconds into the past, so 0 is the right edge.

    ``at`` is the absolute timestamp. Plotting only needs ``ago``, but a reader
    hovering a point wants to know WHEN — and "142s ago" reads differently five
    minutes later, while a wall-clock time does not.
    """
    ago: float
    value: float
    at: float = 0.0


class DeviationHistory:
    """Per-ticker ring of ``(timestamp, deviation)`` points."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S):
        self.window_s = max(1.0, float(window_s))
        self._points: dict[str, deque[tuple[float, float]]] = {}

    def append(self, ticker: str, ts: float, value: float) -> None:
        """Record one measurement and drop anything past the window."""
        if not ticker:
            return
        points = self._points.setdefault(ticker, deque())
        points.append((ts, value))
        cutoff = ts - self.window_s
        while points and points[0][0] < cutoff:
            points.popleft()

    def series(
        self, ticker: str, now: float | None = None, span_s: float | None = None
    ) -> list[Sample]:
        """Visible samples, oldest first. Anything older than ``span_s`` drops."""
        points = self._points.get(ticker)
        if not points:
            return []
        now = time.time() if now is None else now
        span = self.window_s if span_s is None else max(1.0, float(span_s))
        out = []
        for ts, value in points:
            ago = now - ts
            if 0.0 <= ago <= span:
                out.append(Sample(ago=ago, value=value, at=ts))
        return out

    def latest(self, ticker: str) -> tuple[float, float] | None:
        """Most recent ``(timestamp, value)``, or None when nothing is held."""
        points = self._points.get(ticker)
        return points[-1] if points else None

    def count(self, ticker: str) -> int:
        return len(self._points.get(ticker) or ())

    def forget(self, keep: set[str]) -> None:
        """Drop tickers no longer configured."""
        for ticker in list(self._points):
            if ticker not in keep:
                del self._points[ticker]

    def clear(self) -> None:
        self._points.clear()


def bounds(
    samples: list[Sample], pad_frac: float = 0.12, trim: float = 0.05
) -> tuple[float, float]:
    """``(lo, hi)`` for the y axis, robust to a single bad sample.

    Uses the ``trim`` / ``1 - trim`` quantiles rather than min/max. One
    mis-paired measurement — an MT5 bar from the wrong minute, a TradingView
    quote caught mid-update — otherwise sets the scale for the whole plot: a
    lone +98 against a true offset near -66 turns everything else into a flat
    line. Values outside the range still get drawn, clipped at the frame.

    Padding keeps the line off the edges; MIN_RANGE stops a constant series from
    being magnified into float noise.
    """
    if not samples:
        return 0.0, 1.0

    values = sorted(s.value for s in samples)
    n = len(values)
    if n >= 8:
        # Only worth trimming once there is enough data for an outlier to be
        # distinguishable from the signal.
        k = max(1, int(n * trim))
        lo, hi = values[k], values[n - 1 - k]
    else:
        lo, hi = values[0], values[-1]

    span = hi - lo
    if span < MIN_RANGE:
        pad = max(abs(lo) * 0.001, MIN_RANGE)
        return lo - pad, hi + pad
    pad = span * pad_frac
    return lo - pad, hi + pad


def y_ticks(lo: float, hi: float, count: int = 3) -> list[float]:
    """`count` evenly spaced gridline values across [lo, hi], inclusive."""
    if count < 2 or hi <= lo:
        return [lo]
    step = (hi - lo) / (count - 1)
    return [lo + step * i for i in range(count)]


def x_ticks(span_s: float, count: int = 4) -> list[float]:
    """Seconds-ago positions for the time axis, right (0) to left (span_s)."""
    if count < 2 or span_s <= 0:
        return [0.0]
    step = span_s / (count - 1)
    return [step * i for i in range(count)]


def fmt_seconds_ago(seconds: float) -> str:
    """Axis label. Seconds as asked, so a 30 min span reads '-1800s'."""
    if seconds <= 0:
        return "0"
    return f"-{seconds:.0f}s"
