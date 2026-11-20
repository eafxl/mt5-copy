"""Deviation history buffer and plot scaling.

No Qt and no MT5 — this is the whole reason the maths lives outside src/gui/.
The widget only paints what these functions return, so what can be wrong is
here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.deviation_history import (  # noqa: E402
    DEFAULT_WINDOW_S,
    MIN_RANGE,
    DeviationHistory,
    Sample,
    bounds,
    fmt_seconds_ago,
    x_ticks,
    y_ticks,
)

TV = "CME_MINI:NQ1!"


def _samples(*values) -> list[Sample]:
    return [Sample(ago=float(i), value=v) for i, v in enumerate(values)]


# ==================== buffer + pruning ====================

def test_default_window_is_thirty_minutes():
    assert DEFAULT_WINDOW_S == 1800.0


def test_samples_inside_the_window_are_kept():
    h = DeviationHistory()
    h.append(TV, 1000.0, 12.4)
    h.append(TV, 2799.0, 12.6)          # 1799s later — inside 1800
    assert h.count(TV) == 2


def test_samples_past_the_window_are_dropped():
    h = DeviationHistory()
    h.append(TV, 1000.0, 12.4)
    h.append(TV, 2801.0, 12.6)          # 1801s later — outside 1800
    assert h.count(TV) == 1
    assert h.latest(TV) == (2801.0, 12.6)


def test_pruning_is_relative_to_the_newest_sample_not_the_clock():
    """History from a paused service must not evaporate on the next append."""
    h = DeviationHistory(window_s=100.0)
    for ts in (1000.0, 1050.0, 1090.0):
        h.append(TV, ts, 1.0)
    h.append(TV, 1095.0, 1.0)
    assert h.count(TV) == 4             # oldest is 95s back, still in window


def test_a_custom_window_is_honoured():
    h = DeviationHistory(window_s=60.0)
    h.append(TV, 0.0, 1.0)
    h.append(TV, 61.0, 2.0)
    assert h.count(TV) == 1


def test_window_has_a_floor():
    assert DeviationHistory(window_s=0.0).window_s == 1.0
    assert DeviationHistory(window_s=-5.0).window_s == 1.0


def test_empty_ticker_is_ignored():
    h = DeviationHistory()
    h.append("", 1.0, 1.0)
    assert h.count("") == 0


# ==================== series extraction ====================

def test_series_reports_seconds_ago_oldest_first():
    h = DeviationHistory()
    h.append(TV, 900.0, 10.0)
    h.append(TV, 950.0, 11.0)
    h.append(TV, 1000.0, 12.0)

    got = h.series(TV, now=1000.0)
    assert [(c.ago, c.value) for c in got] == [
        (100.0, 10.0), (50.0, 11.0), (0.0, 12.0),
    ]


def test_series_clips_to_a_shorter_span_than_the_buffer():
    h = DeviationHistory()                      # 30 min buffer
    h.append(TV, 0.0, 1.0)
    h.append(TV, 1000.0, 2.0)
    h.append(TV, 1400.0, 3.0)

    visible = h.series(TV, now=1400.0, span_s=300.0)
    assert [(c.ago, c.value) for c in visible] == [(0.0, 3.0)]
    assert h.count(TV) == 3                     # buffer itself untouched


def test_series_ignores_samples_from_the_future():
    """A clock step backwards must not put points off the right edge."""
    h = DeviationHistory()
    h.append(TV, 1000.0, 1.0)
    h.append(TV, 1100.0, 2.0)
    got = h.series(TV, now=1050.0)
    assert [(c.ago, c.value) for c in got] == [(50.0, 1.0)]


def test_unknown_ticker_returns_empty_not_a_keyerror():
    h = DeviationHistory()
    assert h.series("NOPE", now=1.0) == []
    assert h.latest("NOPE") is None
    assert h.count("NOPE") == 0


def test_series_span_has_a_floor():
    h = DeviationHistory()
    h.append(TV, 1000.0, 1.0)
    got = h.series(TV, now=1000.0, span_s=0.0)
    assert [(c.ago, c.value) for c in got] == [(0.0, 1.0)]


def test_tickers_are_independent():
    h = DeviationHistory()
    h.append(TV, 1000.0, 1.0)
    h.append("FX:EURUSD", 1000.0, 2.0)
    assert h.latest(TV) == (1000.0, 1.0)
    assert h.latest("FX:EURUSD") == (1000.0, 2.0)


def test_forget_drops_unconfigured_tickers():
    h = DeviationHistory()
    h.append(TV, 1000.0, 1.0)
    h.append("FX:EURUSD", 1000.0, 2.0)

    h.forget({TV})

    assert h.count(TV) == 1
    assert h.count("FX:EURUSD") == 0


def test_clear_drops_everything():
    h = DeviationHistory()
    h.append(TV, 1000.0, 1.0)
    h.clear()
    assert h.count(TV) == 0


# ==================== y scaling ====================

def test_a_single_outlier_does_not_set_the_scale():
    """The reported symptom: one bad sample at +98 against an offset near -66.

    Min/max scaling turned everything else into a flat line. The trimmed range
    ignores it; the painter still draws it, clipped to the frame.
    """
    values = [-66.0, -67.0, -65.5, -66.5, -67.2, -66.1, -65.9, -66.8, 98.79]
    lo, hi = bounds(_samples(*values), pad_frac=0.0)

    assert hi < 0.0, "the +98 outlier must not reach the axis"
    assert lo >= -68.0
    assert (hi - lo) < 5.0      # the axis now shows the signal, not the spike


def test_outliers_at_both_ends_are_trimmed():
    values = [-200.0] + [-66.0] * 10 + [200.0]
    lo, hi = bounds(_samples(*values), pad_frac=0.0)
    assert -70.0 < lo <= -66.0
    assert -66.0 <= hi < -62.0


def test_a_short_series_is_not_trimmed():
    """Under 8 points there is no way to tell an outlier from the signal."""
    lo, hi = bounds(_samples(-66.0, -67.0, 10.0), pad_frac=0.0)
    assert lo == -67.0
    assert hi == 10.0


def test_bounds_pad_the_data_range():
    lo, hi = bounds(_samples(10.0, 20.0), pad_frac=0.1)
    assert lo == pytest.approx(9.0)
    assert hi == pytest.approx(21.0)


def test_bounds_never_collapse_on_a_flat_series():
    """A constant offset must render as a flat line, not amplified noise."""
    lo, hi = bounds(_samples(12.44, 12.44, 12.44))
    assert hi > lo
    assert lo < 12.44 < hi
    assert (hi - lo) >= MIN_RANGE


def test_bounds_handle_a_flat_series_at_zero():
    lo, hi = bounds(_samples(0.0, 0.0))
    assert hi > lo


def test_bounds_of_nothing_is_a_usable_range():
    lo, hi = bounds([])
    assert hi > lo


def test_bounds_handle_negative_offsets():
    lo, hi = bounds(_samples(-20.0, -10.0), pad_frac=0.1)
    assert lo == pytest.approx(-21.0)
    assert hi == pytest.approx(-9.0)


# ==================== axis ticks ====================

def test_y_ticks_span_the_range_inclusive():
    assert y_ticks(0.0, 10.0, count=3) == [0.0, 5.0, 10.0]


def test_y_ticks_degenerate_range_gives_one_line():
    assert y_ticks(5.0, 5.0, count=3) == [5.0]
    assert y_ticks(0.0, 10.0, count=1) == [0.0]


def test_x_ticks_run_from_now_into_the_past():
    assert x_ticks(1800.0, count=4) == [0.0, 600.0, 1200.0, 1800.0]


def test_x_ticks_degenerate_span():
    assert x_ticks(0.0) == [0.0]


def test_seconds_labels_are_in_seconds():
    """x-axis is seconds, as requested — a 30 min span reads -1800s."""
    assert fmt_seconds_ago(0.0) == "0"
    assert fmt_seconds_ago(450.0) == "-450s"
    assert fmt_seconds_ago(1800.0) == "-1800s"


# ==================== absolute timestamps ====================
#
# Plotting needs only "seconds ago", but a reader hovering a point wants to know
# WHEN — and "142s ago" reads differently five minutes later while a wall-clock
# time does not.

def test_series_carries_the_absolute_timestamp():
    h = DeviationHistory()
    h.append("NQ", 1000.0, -64.0)
    h.append("NQ", 1030.0, -63.0)

    got = h.series("NQ", now=1060.0, span_s=300.0)

    assert [s.at for s in got] == [1000.0, 1030.0]
    assert [s.ago for s in got] == [60.0, 30.0]
