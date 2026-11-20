"""Master history sampling: which M1 bar answers a point-in-time question.

WHY THIS FILE EXISTS
    The real-price offset is master-price-then minus TradingView-price-then. A
    sampler that returns the wrong bar still returns a plausible price, so the
    bug shows up only as an offset that "looks crazy": measured on NAS100
    (traces 2026-08-27) the offset swung between -17 and -129 against a true
    value of -64.

    Three failures are pinned here:

    1. ``copy_rates_from(symbol, tf, date, count)`` returns ``count`` bars
       ENDING at ``date``. Opening the window at ``target - 600`` therefore
       handed back a window whose NEWEST bar was 600s BEFORE the target — a
       600s-delayed TradingView quote was compared against a master price
       1200s old. ``copy_rates_range`` is used instead: unambiguous in both
       directions.

    2. Substituting a nearby bar. ``bar_close_at`` answers for one instant or
       not at all; a neighbouring bar would answer a different question with
       the same confidence, which is exactly how the stale reading passed for a
       live one.

    3. The broker clock. Bar stamps are server-local seconds and the offset to
       them is measured live off a tick, so the requested instant carries
       sub-second jitter. Rounding to the minute removes the jitter; a real
       shift moves things by a whole minute and stays visible.

MetaTrader5 is stubbed by conftest; only the rate readers are replaced here.
The real module answers with a numpy record array, which is not installed here
and is not what is under test — a list of subscriptable rows exercises the same
code, since the sampler only ever indexes rows by field name.
"""
import pytest

import MetaTrader5 as mt5

from src.mt5_client import MT5Client

M1 = 60


def _bars(start: int, count: int, closes=None):
    """``count`` consecutive M1 bars opening at ``start``.

    Close defaults to the bar's own open time, so a test can read straight off
    a returned close which bar answered.
    """
    rows = []
    for i in range(count):
        t = start + i * M1
        c = float(t) if closes is None else float(closes[i])
        rows.append({"time": t, "open": c, "high": c, "low": c, "close": c})
    return rows


class _NullLogger:
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def info(self, *a, **k): pass


@pytest.fixture
def client():
    c = MT5Client.__new__(MT5Client)          # no terminal, no connect
    c.logger = _NullLogger()
    return c


def _install(monkeypatch, reader):
    monkeypatch.setattr(mt5, "copy_rates_range", reader, raising=False)
    monkeypatch.setattr(mt5, "TIMEFRAME_M1", 1, raising=False)


@pytest.fixture
def rates_range(monkeypatch):
    """Capture the range asked for, answer with bars covering it."""
    seen = {}

    def copy_rates_range(symbol, timeframe, date_from, date_to):
        seen["symbol"] = symbol
        seen["from"] = date_from.timestamp()
        seen["to"] = date_to.timestamp()
        start = int(seen["from"] // M1) * M1
        count = int((seen["to"] - start) // M1) + 1
        return _bars(start, count)

    _install(monkeypatch, copy_rates_range)
    return seen


# ==================== the window ====================

def test_the_window_brackets_the_requested_close(client, rates_range):
    """Both sides of the instant, so the bar asked for is inside it.

    The regression was a window that ENDED before the target and still
    returned bars — asking for a range makes that impossible.
    """
    close_at = 1_700_000_400.0

    client.bar_close_at("NAS100", close_at)

    assert rates_range["from"] < close_at < rates_range["to"]


# ==================== exact match ====================

def test_returns_the_bar_that_closed_at_that_instant(client, rates_range):
    """The bar OPENING one minute before the requested close."""
    close_at = 1_700_000_400

    bar = client.bar_close_at("NAS100", float(close_at))

    assert bar[4] == close_at - M1


def test_a_neighbouring_bar_is_not_a_substitute(client, monkeypatch):
    """The whole point: no bar for that minute means no reading.

    The caller turns this None into a red row. A nearest-bar answer here is
    what let a 10-minute-stale price be reported as a live offset.
    """
    close_at = 1_700_000_400
    wanted_open = close_at - M1
    _install(
        monkeypatch,
        lambda *a: [b for b in _bars(wanted_open - 5 * M1, 11)
                    if b["time"] != wanted_open],
    )

    assert client.bar_close_at("NAS100", float(close_at)) is None


def test_a_gap_in_history_is_none_not_the_bar_before_it(client, monkeypatch):
    """A weekend, a halt, a symbol only just added to Market Watch."""
    close_at = 1_700_000_400
    _install(monkeypatch, lambda *a: _bars(close_at - 10 * M1, 5))  # all older

    assert client.bar_close_at("NAS100", float(close_at)) is None


# ==================== broker clock ====================

@pytest.mark.parametrize("jitter", [0.0, 0.4, -0.4, 0.49, -0.49])
def test_sub_second_clock_jitter_still_finds_the_bar(client, rates_range, jitter):
    """The server offset is measured off a live tick, so it is never exact.

    A broker timezone is a whole number of minutes, so rounding to the minute
    absorbs the measurement noise without absorbing a real shift.
    """
    close_at = 1_700_000_400

    bar = client.bar_close_at("NAS100", close_at + jitter)

    assert bar is not None
    assert bar[4] == close_at - M1


def test_a_whole_minute_shift_is_not_absorbed(client, rates_range):
    """Rounding must not hide a genuine clock shift — it must land elsewhere."""
    close_at = 1_700_000_400

    shifted = client.bar_close_at("NAS100", float(close_at + M1))

    assert shifted[4] == close_at          # a different bar, visibly


# ==================== shape and failure ====================

def test_returns_ohlc_plus_the_bars_own_timestamp(client, rates_range):
    """The timestamp is what lets the caller age the reading off the DATA."""
    bar = client.bar_close_at("NAS100", 1_700_000_460.0)

    assert len(bar) == 5
    o, h, l, c, t = bar
    assert t % M1 == 0
    assert (o, h, l, c) == (t, t, t, t)          # _bars encodes time as price


def test_no_history_is_none_not_a_guess(client, monkeypatch):
    _install(monkeypatch, lambda *a: [])

    assert client.bar_close_at("NAS100", 1_700_000_400.0) is None


def test_a_raising_terminal_is_none_not_an_exception(client, monkeypatch):
    def boom(*a):
        raise RuntimeError("terminal gone")

    _install(monkeypatch, boom)

    assert client.bar_close_at("NAS100", 1_700_000_400.0) is None


def test_missing_arguments_short_circuit(client, rates_range):
    assert client.bar_close_at("", 1_700_000_400.0) is None
    assert client.bar_close_at("NAS100", 0.0) is None
    assert not rates_range                      # no terminal call at all


# ==================== price_at: nearest, and only here ====================
#
# price_at answers "roughly what was it then" for diagnostics. Nearest is right
# there and wrong for the offset — hence two samplers rather than one flag.

@pytest.mark.parametrize("offset_into_minute", [0, 1, 29, 30, 31, 59])
def test_price_at_bar_fallback_picks_the_nearest_close(
    client, rates_range, monkeypatch, offset_into_minute
):
    monkeypatch.setattr(mt5, "copy_ticks_from", lambda *a: None, raising=False)
    monkeypatch.setattr(mt5, "COPY_TICKS_INFO", 1, raising=False)
    target = float(1_700_000_400 + offset_into_minute)

    price = client.price_at("NAS100", target)

    # _bars encodes the bar's open time as its close price.
    assert abs((price + M1) - target) <= M1 / 2


def test_price_at_prefers_ticks_when_it_has_them(client, rates_range, monkeypatch):
    ticks = [{"time_msc": 1_700_000_399_000, "bid": 100.0, "ask": 100.5}]
    monkeypatch.setattr(mt5, "copy_ticks_from", lambda *a: ticks, raising=False)
    monkeypatch.setattr(mt5, "COPY_TICKS_INFO", 1, raising=False)

    assert client.price_at("NAS100", 1_700_000_400.0) == pytest.approx(100.25)


# ==================== the broker clock ====================
#
# `bar_close_at` reaches the broker's clock by adding this offset, so an unstable
# offset silently reads a bar from somewhere else entirely. Measured across the
# 21:00 CME break (traces 2026-08-27) it ranged 6898 -> 10803: a 65-MINUTE
# spread on a broker sitting at a constant UTC+3, which is what drove the
# real-price offset to -129 against a true -64. The market was fine; the clock
# estimate collapsed.

class _Tick:
    def __init__(self, ts):
        self.time = int(ts)
        self.time_msc = int(ts * 1000)
        self.bid = 100.0
        self.ask = 100.5


def _clocked(monkeypatch, client, ticks):
    """Drive server_time_offset over a scripted (host_now, tick_ts) sequence."""
    import src.mt5_client as mod

    seen = []
    for host_now, tick_ts in ticks:
        monkeypatch.setattr(mod.time, "time", lambda v=host_now: v)
        monkeypatch.setattr(
            mt5, "symbol_info_tick",
            lambda s, v=tick_ts: (_Tick(v) if v else None), raising=False,
        )
        seen.append(client.server_time_offset("NAS100"))
    return seen


def _fresh_client():
    """A client with no terminal behind it, and no cache file either.

    """
    from collections import deque

    c = MT5Client.__new__(MT5Client)
    c.logger = _NullLogger()
    c._offset_samples = deque()
    c._last_tick_ts = 0.0
    c._server_offset = None
    c._current_terminal = ""
    return c


OFFSET = 10800.0        # a broker at UTC+3


def test_a_live_feed_measures_the_offset(monkeypatch):
    client = _fresh_client()
    got = _clocked(monkeypatch, client, [
        (1000.0, 1000.0 + OFFSET),
        (1000.5, 1000.5 + OFFSET),
        (1001.0, 1001.0 + OFFSET),
    ])
    assert got == [OFFSET, OFFSET, OFFSET]


def test_a_frozen_tick_does_not_drag_the_offset_down(monkeypatch):
    """The break that started all this.

    Re-reading one frozen tick every 500ms floods the window with samples whose
    implied offset shrinks by 0.5s each time, and the max walks down with them.
    """
    client = _fresh_client()
    frozen = 1000.0 + OFFSET
    # One good tick, then an hour of the same tick re-read every 30s.
    script = [(1000.0, frozen)]
    script += [(1000.0 + 30 * i, frozen) for i in range(1, 121)]

    got = _clocked(monkeypatch, client, script)

    assert set(got) == {OFFSET}, f"offset drifted to {sorted(set(got))}"


def test_the_last_offset_survives_an_empty_window(monkeypatch):
    """A weekend is 48h of no news, and no news is not the same as no offset."""
    client = _fresh_client()
    frozen = 1000.0 + OFFSET
    got = _clocked(monkeypatch, client, [
        (1000.0, frozen),
        (1000.0 + 601.0, frozen),      # past the 600s window; nothing new
        (1000.0 + 86400.0, frozen),    # a day later, still nothing new
    ])
    assert got == [OFFSET, OFFSET, OFFSET]


def test_no_tick_at_all_still_answers_once_it_has_seen_one(monkeypatch):
    client = _fresh_client()
    got = _clocked(monkeypatch, client, [
        (1000.0, 1000.0 + OFFSET),
        (1100.0, None),                # symbol_info_tick returns nothing
    ])
    assert got == [OFFSET, OFFSET]


def test_before_the_first_tick_there_is_no_answer(monkeypatch):
    client = _fresh_client()
    assert _clocked(monkeypatch, client, [(1000.0, None)]) == [None]


def test_a_real_shift_is_adopted_once_ticks_resume(monkeypatch):
    """DST must still be tracked — the samples age out, the ratchet does not."""
    client = _fresh_client()
    script = [(1000.0, 1000.0 + OFFSET)]
    # An hour later the broker has moved back one hour and is ticking again.
    new = OFFSET - 3600.0
    script += [(1000.0 + 700.0 + i, 1000.0 + 700.0 + i + new) for i in range(3)]

    got = _clocked(monkeypatch, client, script)

    assert got[0] == OFFSET
    assert got[-1] == new


# ==================== a closed market has no clock ====================
#
# MEASURED (capture history_20260829_120321.json, a Saturday):
#     probe symbol EURUSD, last tick 16.5 HOURS old
#     server_offset_s: -32902  (-9h08m)
# The broker sits at UTC+3. Nothing was wrong with the broker or the history —
# the offset was measured off a tick from Friday's close, and a stale tick can
# only UNDER-state the offset.
#
# It mattered because the daily stop counter converts local midnight into
# broker time with this number. Shifted by nine hours, Friday's ten losing
# trades landed inside Saturday, the count read 10 of 4, and the app would
# have locked trading out on a day that had not traded.


def test_a_stale_tick_is_refused_rather_than_believed(monkeypatch):
    """The Saturday case, with its real numbers."""
    client = _fresh_client()
    stale = 1000.0 - 32902.347764253616     # 16.5h-old tick, implying -9h08m

    got = _clocked(monkeypatch, client, [(1000.0, stale)])

    assert got == [None], "an implausible offset is not an offset"


def test_a_broker_timezone_is_accepted(monkeypatch):
    """Whole hours pass; so do the half-hour zones that exist."""
    for hours in (0, 2, 3, -5, 5.5, 12.5):
        client = _fresh_client()
        offset = hours * 3600.0

        got = _clocked(monkeypatch, client, [(1000.0, 1000.0 + offset)])

        assert got == [offset], f"{hours}h should be a plausible broker offset"


def test_an_absurd_offset_is_refused(monkeypatch):
    """No broker is fifteen hours from UTC; that is a stale tick or a bug."""
    client = _fresh_client()

    assert _clocked(monkeypatch, client, [(1000.0, 1000.0 + 15 * 3600)]) == [None]


def test_no_tick_and_no_samples_is_no_answer(monkeypatch):
    """Better than a wrong one. The daily stop limit no longer depends on this
    at all — it counts by the broker's own dating — but the real-price sampler
    still does, and a made-up offset there reads bars from the wrong hour.
    """
    client = _fresh_client()
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda s: None, raising=False)

    assert client.server_time_offset("EURUSD") is None
