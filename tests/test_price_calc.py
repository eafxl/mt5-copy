"""Real price calculator: delay parsing, scanner parsing, offset resolution.

No network and no MT5 — the scanner call and the master price reads are both
injected, so these tests pin the maths and the failure behaviour.
"""
import time

import pytest

from src import price_calc as pc
from src.config import Config, PriceCalcConfig, PriceCalcPair
from src.price_calc import (
    PriceCalculator,
    TVQuote,
    fetch_tv_quotes,
    parse_delay_seconds,
    resolve_ticker,
)


class _TracerSpy:
    """Stand-in for the global tracer: records events, writes nothing."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, operation, **tags):
        self.events.append((operation, tags))

    def of(self, operation: str) -> list[dict]:
        return [tags for op, tags in self.events if op == operation]


@pytest.fixture(autouse=True)
def tracer_spy(monkeypatch):
    """Keep every test in this module off the real trace file.

    _resolve_pair traces each measurement, and the default get_tracer() builds
    a live Tracer that mkdirs traces/ and writes to it — tens of KB per run.
    """
    spy = _TracerSpy()
    monkeypatch.setattr(pc, "get_tracer", lambda: spy)
    return spy


# ==================== update_mode parsing ====================

@pytest.mark.parametrize("mode,expected", [
    ("streaming", 0.0),
    ("delayed_streaming_600", 600.0),
    ("delayed_streaming_900", 900.0),
    ("end_of_day", 0.0),
    ("", 0.0),
    (None, 0.0),
])
def test_parse_delay_seconds(mode, expected):
    assert parse_delay_seconds(mode) == expected


# ==================== scanner response parsing ====================

_SCAN_BODY = {
    "totalCount": 3,
    "data": [
        {"s": "FX:EURUSD", "d": [1.16689, "streaming", 1787691600, 100000, "Euro vs. US Dollar"]},
        {"s": "COMEX:GC1!", "d": [4684.5, "delayed_streaming_600", 1787695200, 10, "Gold Futures"]},
        {"s": "X:CLOSED", "d": [None, "streaming", 0, 100, "Never Traded"]},
    ],
}


def _fake_urlopen(body):
    """Context-manager stand-in for urllib.request.urlopen."""
    import json

    class _Resp:
        def read(self):
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None: _Resp()


def test_fetch_parses_rows(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(_SCAN_BODY))
    quotes, error = fetch_tv_quotes(["FX:EURUSD", "COMEX:GC1!"])

    assert error == ""
    assert quotes["FX:EURUSD"].price == 1.16689
    assert quotes["FX:EURUSD"].delay_s == 0.0
    assert quotes["COMEX:GC1!"].delay_s == 600.0
    assert quotes["COMEX:GC1!"].description == "Gold Futures"


def test_fetch_reports_unknown_tickers(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(_SCAN_BODY))
    quotes, error = fetch_tv_quotes(["FX:EURUSD", "NOPE:NOPE"])

    assert "NOPE:NOPE" in error
    assert "FX:EURUSD" in quotes  # partial success still yields usable data


def test_fetch_survives_network_failure(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    quotes, error = fetch_tv_quotes(["FX:EURUSD"])

    assert quotes == {}
    assert error  # message, not an exception


def test_fetch_no_tickers_is_not_an_error():
    assert fetch_tv_quotes([]) == ({}, "")


# ==================== ticker qualification ====================

# Trimmed shape of the real symbol_search/v3 response for "NQ1!": the
# continuous contract hangs off the root symbol and carries its own prefix.
_SEARCH_BODY = {
    "symbols_remaining": 0,
    "symbols": [
        {
            "symbol": "NQ",
            "description": "E-mini Nasdaq-100 Futures",
            "exchange": "CME",
            "source_id": "CME_MINI",
            "contracts": [{"symbol": "NQ1!", "prefix": "CME_MINI"}],
        },
        {
            "symbol": "NQ",
            "description": "QLD Evening Peak Load Electricity Futures",
            "exchange": "ASX24",
            "source_id": "ASX24",
            "contracts": [{"symbol": "NQ1!"}],
        },
    ],
}


def test_bare_futures_ticker_gets_its_exchange(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(_SEARCH_BODY))
    ticker, description, error = resolve_ticker("NQ1!")

    assert ticker == "CME_MINI:NQ1!"
    assert description == "E-mini Nasdaq-100 Futures"
    assert error == ""


def test_qualified_ticker_is_passed_through_untouched(monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("must not hit the network")

    monkeypatch.setattr("urllib.request.urlopen", _never)
    assert resolve_ticker("COMEX:GC1!") == ("COMEX:GC1!", "", "")


def test_plain_symbol_row_resolves_via_source_id(monkeypatch):
    body = {"symbols": [{
        "symbol": "AAPL", "description": "Apple Inc.",
        "exchange": "NASDAQ", "source_id": "NASDAQ", "contracts": [],
    }]}
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))

    assert resolve_ticker("aapl")[0] == "NASDAQ:AAPL"


def test_unresolvable_ticker_explains_the_prefix_rule(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen({"symbols": []}))
    ticker, _desc, error = resolve_ticker("ZZZZ9!")

    assert ticker == "ZZZZ9!"          # left as typed
    assert "exchange prefix" in error


def test_resolve_survives_search_outage(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("dns failure")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    ticker, _desc, error = resolve_ticker("NQ1!")

    assert ticker == "NQ1!"
    assert error


# ==================== harness ====================
#
# Every measurement now needs THREE things lined up: a quote, a TradingView
# value timestamped at the common instant, and a master bar closing at it. The
# helpers below build that state explicitly, because each of the three is a
# separate way for the reading to come out red and the tests have to be able to
# aim at one at a time.

DELAY = 600.0
SRV_OFFSET = 7200.0          # a broker two hours ahead of UTC
TV = "COMEX:GC1!"
MASTER = "XAUUSD"


def _pairs(tv=TV, master=MASTER):
    return [PriceCalcPair(tv=tv, master=master)]


def _calc(quotes, error="", refresh_sec=5.0, pairs=None, window=7):
    return PriceCalculator(
        pairs or _pairs(), refresh_sec=refresh_sec, window=window,
        fetch=lambda tickers: (quotes, error),
    )


def _quote(price, delay=DELAY, ticker=TV, fetched_at=None):
    return TVQuote(
        ticker, price, delay_s=delay, bar_time=0,
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def _providers(at_price=4696.94, bid=4700.0, offset=SRV_OFFSET):
    """price_at / current_price / server_offset stand-ins.

    ``price_at`` is no longer part of the measurement — the offset needs an
    exact bar or nothing — but it is still injected, so it is still stubbed.
    """
    seen = {}

    def price_at(symbol, server_ts):
        seen["symbol"] = symbol
        seen["server_ts"] = server_ts
        return at_price

    return (
        price_at,
        lambda symbol: (bid, bid + 0.5),
        lambda symbol: offset,
        seen,
    )


def _bar_close_at(close_price, o=None, h=None, l=None, only_at=None):
    """A terminal that has every M1 bar — or only the one at ``only_at``.

    Records what it was asked for, and answers in the shape the real
    ``MT5Client.bar_close_at`` does: OHLC plus the bar's OPEN in broker
    seconds, one minute before the close asked for.
    """
    seen = {}

    def bar_close_at(symbol, server_close_ts):
        seen["symbol"] = symbol
        seen["server_close_ts"] = server_close_ts
        if only_at is not None and abs(server_close_ts - only_at) > 0.5:
            return None
        return (
            o if o is not None else close_price,
            h if h is not None else close_price,
            l if l is not None else close_price,
            close_price,
            float(server_close_ts) - pc.BAR_S,
        )

    return bar_close_at, seen


def _seed_tv(calc, price, at=None, ticker=TV, delay=DELAY):
    """Timestamp ``price`` as TradingView's value for the common instant.

    The log timestamps a value by when it FIRST appeared, so on a cold start
    nothing in it is old enough to answer for an instant 11 minutes back. That
    is the truth about a delayed feed and is tested for its own sake below; the
    tests that are about something else start from a log that has run a while.
    """
    instant = pc.common_instant(time.time(), delay) if at is None else at
    calc._tv_log.observe(ticker, price, instant + delay, delay)
    return instant


def _resolve(calc, tv_price, master_price, delay=DELAY, offset=SRV_OFFSET,
             quote_age=None):
    """One full cycle with both sides present. Returns the PriceDiff."""
    _seed_tv(calc, tv_price, delay=delay)
    common = pc.common_instant(time.time(), delay)
    bar, seen = _bar_close_at(master_price, only_at=common + offset)
    price_at, current, srv, _ = _providers(offset=offset)
    (diff,) = calc.refresh(
        price_at, current, srv, bar_at=bar, quote_age=quote_age
    )
    return diff, seen


# ==================== ticker qualification, through the calculator ====================

def test_calculator_qualifies_bare_tickers_once():
    calls = []

    def _resolve_fn(text):
        calls.append(text)
        return "CME_MINI:NQ1!", "E-mini Nasdaq-100 Futures", ""

    scanned = []
    quote = _quote(29172.5, ticker="CME_MINI:NQ1!")

    def _fetch(tickers):
        scanned.append(list(tickers))
        return {"CME_MINI:NQ1!": quote}, ""

    calc = PriceCalculator(
        [PriceCalcPair(tv="NQ1!", master="NAS100")],
        refresh_sec=0.0, fetch=_fetch, resolve=_resolve_fn,
    )
    # Keyed by what the CONFIG holds, not by the qualified name.
    _seed_tv(calc, 29172.5, ticker="NQ1!")
    common = pc.common_instant(time.time(), DELAY)
    bar, _ = _bar_close_at(29180.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (first,) = calc.refresh(price_at, current, srv, bar_at=bar)
    (second,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert scanned == [["CME_MINI:NQ1!"], ["CME_MINI:NQ1!"]]
    assert calls == ["NQ1!"]                  # looked up once, then remembered
    assert first.tv_price == 29172.5
    assert first.stale is False
    assert second.diff == first.diff
    assert first.tv_ticker == "NQ1!"


def test_calculator_reports_unresolvable_ticker():
    calc = PriceCalculator(
        [PriceCalcPair(tv="ZZZZ9!", master="X")],
        fetch=lambda t: ({}, ""),
        resolve=lambda t: ("ZZZZ9!", "", "needs an exchange prefix"),
    )
    price_at, current, offset, _ = _providers()

    (diff,) = calc.refresh(price_at, current, offset)

    assert diff.stale is True
    assert "exchange prefix" in diff.error


# ==================== the common instant ====================
#
# ONE timestamp, fixed before either side is read, a whole minute further back
# than the feed's delay. "Now minus the delay" is unsettled on both sides at
# once: the master's bar covering it is still open and TradingView has not
# published its value for that minute yet.

def test_the_common_instant_is_a_whole_minute():
    """A bar is addressed by its boundary — 12:34:17 has no bar."""
    assert pc.common_instant(1_700_000_437.0, 600.0) % 60 == 0


def test_the_common_instant_adds_a_settle_margin_to_the_delay():
    now = 1_700_000_400.0                  # already on a boundary
    assert now - pc.common_instant(now, 600.0) == 600.0 + 60.0


def test_a_live_feed_is_settled_the_same_way():
    """No separate live path: a 0s delay still reads a closed bar."""
    now = 1_700_000_400.0
    assert now - pc.common_instant(now, 0.0) == 60.0


@pytest.mark.parametrize("delay", [0.0, 600.0, 900.0])
def test_the_instant_is_never_in_the_unsettled_minute(delay):
    now = 1_700_000_437.0
    assert now - pc.common_instant(now, delay) >= delay + 60.0


def test_both_sides_are_read_at_that_one_instant():
    calc = _calc({TV: _quote(4684.5)})
    diff, seen = _resolve(calc, 4684.5, 4696.0)

    # The master was asked for it in the BROKER's clock...
    assert seen["server_close_ts"] == pytest.approx(diff.common_at + SRV_OFFSET)
    # ...and both readings report the same age back in ours.
    assert diff.tv_age_s == pytest.approx(diff.master_age_s, abs=2.0)
    # Quantized to the minute, so the lookback lands inside the minute that
    # starts at delay + margin — never before it.
    assert DELAY + pc.SETTLE_MARGIN_S <= diff.lookback_s < DELAY + 120.0


def test_the_offset_is_the_two_prices_at_that_instant():
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0)

    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)
    assert diff.master_price == 4696.0
    assert diff.basis == "m1_close"
    assert diff.stale is False


def test_the_measurement_never_touches_bid_ask_or_the_mid():
    """One price series on each side or the difference is not a venue offset.

    TradingView publishes a single close with no spread; the master side reads
    the M1 bar close, also a single price. An absurd spread changes nothing.
    """
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    common = pc.common_instant(time.time(), DELAY)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)

    (diff,) = calc.refresh(
        lambda s, t: 1.0,                       # tick path must not be used
        lambda s: (1.0, 9999.0),                # absurd spread
        lambda s: SRV_OFFSET,
        bar_at=bar,
    )

    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)
    assert diff.spread_now == pytest.approx(9998.0)   # reported, not applied


def test_timezone_offset_moves_only_the_master_request():
    """The instant is ours; only the request into MT5 is the broker's."""
    calc = _calc({TV: _quote(4684.5)})
    diff, seen = _resolve(calc, 4684.5, 4696.0, offset=-18000.0)  # UTC-5

    assert seen["server_close_ts"] == pytest.approx(diff.common_at - 18000.0)
    assert diff.master_age_s == pytest.approx(diff.tv_age_s, abs=2.0)


def test_the_master_age_comes_from_the_bar_that_answered():
    """Not from what was asked for — that is the whole point of two counters.

    A terminal that answers with a bar from a different minute must show up as
    two ages that disagree, which is what a clock shift looks like.
    """
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    common = pc.common_instant(time.time(), DELAY)

    def lying_bar(symbol, server_close_ts):
        # Answers, but with the bar from ten minutes earlier.
        return (1.0, 2.0, 0.5, 4696.0, server_close_ts - 600.0 - pc.BAR_S)

    price_at, current, srv, _ = _providers()
    (diff,) = calc.refresh(price_at, current, srv, bar_at=lying_bar)

    assert diff.master_age_s - diff.tv_age_s == pytest.approx(600.0, abs=2.0)


# ==================== TradingView's own timestamp ====================
#
# The scanner returns a value, never the instant it was true. Measured on
# CME_MINI:NQ1! (traces 2026-08-27) it republishes a delayed feed every 30.1s
# median over 128 changes — so a value re-read between publications was already
# true up to 30s earlier, and `scan_time - delay` overstates its freshness by
# that much. The log timestamps a value by when it FIRST appeared.

STALE = pc.TV_STALE_AFTER_S


def test_a_value_is_timestamped_when_it_first_appears():
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=600.0)
    log.observe(TV, 100.0, seen_at=1005.0, delay_s=600.0)   # same publication
    log.observe(TV, 100.0, seen_at=1025.0, delay_s=600.0)

    at, price = log.at(TV, 400.0, STALE)
    assert (at, price) == (400.0, 100.0)      # 1000 - 600, not 1025 - 600


def test_a_new_value_gets_its_own_timestamp():
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=600.0)
    log.observe(TV, 101.0, seen_at=1030.0, delay_s=600.0)

    assert log.at(TV, 400.0, STALE)[1] == 100.0
    assert log.at(TV, 430.0, STALE)[1] == 101.0


def test_a_value_that_repeats_after_a_change_is_timestamped_again():
    """A price can legitimately return to where it was."""
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=600.0)
    log.observe(TV, 101.0, seen_at=1030.0, delay_s=600.0)
    log.observe(TV, 100.0, seen_at=1060.0, delay_s=600.0)

    assert log.at(TV, 460.0, STALE)[1] == 100.0


# --- the lookup is a step function, not a nearest-point search ---------------
#
# Between publications the price IS the last published value, so the value at an
# instant is the last publication at or before it. Taking the NEAREST entry
# within a tolerance instead threw away 16% of good measurements on a quiet
# market, where TradingView went up to 141s between publications (traces
# 2026-08-27), and could also answer with a value from the FUTURE of the instant.

def test_the_value_in_force_is_the_last_one_published_before_the_instant():
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)
    log.observe(TV, 101.0, seen_at=1140.0, delay_s=0.0)     # 140s later

    # 130s after the first publication and 10s before the second: nearest would
    # pick 101.0, which was not true yet.
    assert log.at(TV, 1130.0, STALE)[1] == 100.0


def test_a_long_quiet_stretch_is_not_a_missing_value():
    """141s between publications was observed live. It is a quiet market."""
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)

    assert log.at(TV, 1000.0 + 141.0, STALE)[1] == 100.0


def test_a_stopped_feed_is_missing_not_current():
    """A closed session leaves its last price 'current' forever otherwise, and
    the master would drift away from a frozen number as if it were an offset."""
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)

    assert log.at(TV, 1000.0 + STALE, STALE) is not None
    assert log.at(TV, 1000.0 + STALE + 1, STALE) is None


def test_nothing_published_yet_is_none():
    """Before the first entry there is no answer, and nothing can invent one."""
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)

    assert log.at(TV, 900.0, STALE) is None


def test_an_unknown_ticker_is_none():
    assert pc.TVSampleLog().at("NOPE", 400.0, STALE) is None


def test_the_log_drops_tickers_that_leave_the_config():
    log = pc.TVSampleLog()
    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)
    log.observe("OTHER", 100.0, seen_at=1000.0, delay_s=0.0)

    log.forget({TV})

    assert log.at(TV, 1000.0, STALE) is not None
    assert log.at("OTHER", 1000.0, STALE) is None


def test_the_stale_limit_clears_a_quiet_market_but_not_a_closed_one():
    assert pc.TV_STALE_AFTER_S == 300.0


def test_a_cold_start_is_warming_not_a_broken_feed():
    """Nothing tells us what TradingView said 11 minutes ago on the first scan.

    That absence is not a fault and must not read like one: measured over three
    restarts (traces 2026-08-27) it lasted 35-75s and then resolved for the rest
    of the run, while the feed answered perfectly throughout. Calling it "feed
    not publishing" sent the reader hunting for a problem that did not exist.
    """
    calc = _calc({TV: _quote(4684.5)})
    common = pc.common_instant(time.time(), DELAY)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.tv_missing is True
    assert diff.tv_warming is True
    assert diff.raw_diff == 0.0
    assert "Watching" in diff.error
    assert "not publishing" not in diff.error


def test_a_stopped_feed_is_not_called_warming():
    """The opposite news, and it must not be softened into the benign one."""
    calc = _calc({TV: _quote(4684.5)})
    common = pc.common_instant(time.time(), DELAY)
    _seed_tv(calc, 4684.5, at=common - pc.TV_STALE_AFTER_S - 1)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.tv_missing is True
    assert diff.tv_warming is False
    assert "feed stopped" in diff.error


def test_warming_clears_once_the_log_reaches_back_far_enough():
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    common = pc.common_instant(time.time(), DELAY)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (warming,) = calc.refresh(price_at, current, srv, bar_at=bar)
    assert warming.tv_warming is True

    # A minute later the value seen at startup predates the instant, because the
    # instant has advanced past it. Written into the log directly: `observe`
    # dedupes an unchanged price, and the point here is the passage of time,
    # not a new publication.
    from collections import deque
    calc._tv_log._seen[TV] = deque([(common - 30.0, 4684.5)])
    (resolved,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert resolved.tv_missing is False
    assert resolved.raw_diff == pytest.approx(4696.0 - 4684.5)


def test_the_lookup_names_why_there_is_no_value():
    log = pc.TVSampleLog()
    assert log.lookup(TV, 1000.0, STALE) == (None, "unknown")

    log.observe(TV, 100.0, seen_at=1000.0, delay_s=0.0)
    assert log.lookup(TV, 900.0, STALE) == (None, "warming")
    assert log.lookup(TV, 1000.0 + STALE + 1, STALE) == (None, "stopped")
    assert log.lookup(TV, 1000.0, STALE)[1] == ""


def test_the_stated_delay_is_still_what_is_reported():
    calc = _calc({TV: _quote(4684.5, delay=900.0)})
    diff, _ = _resolve(calc, 4684.5, 4696.0, delay=900.0)

    assert diff.delay_s == 900.0
    assert 900.0 + 60.0 <= diff.lookback_s < 900.0 + 120.0


# ==================== a missing side is red, never substituted ====================

def test_no_master_bar_at_that_minute_is_reported_missing():
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(
        price_at, current, srv, bar_at=lambda s, t: None
    )

    assert diff.master_missing is True
    assert diff.master_age_s == 0.0        # nothing was read; don't age a gap
    assert diff.raw_diff == 0.0
    assert "no M1 bar closing at" in diff.error


def test_neither_side_available_names_both():
    calc = _calc({TV: _quote(4684.5)})
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(
        price_at, current, srv, bar_at=lambda s, t: None
    )

    assert diff.tv_missing and diff.master_missing
    # Warming wins the message: it is the reason the reader can act on, and it
    # goes away by itself.
    assert "Watching" in diff.error


def test_the_error_names_the_instant_it_had_nothing_for():
    """"No data" is only actionable with the timestamp attached."""
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)

    when = time.strftime("%H:%M:%S", time.localtime(diff.common_at))
    assert when in diff.error


def test_a_missing_side_still_shows_the_price_it_does_have():
    """Readable, just not comparable — the row shows it red."""
    calc = _calc({TV: _quote(4684.5)})
    price_at, current, srv, _ = _providers()
    common = pc.common_instant(time.time(), DELAY)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.tv_missing is True
    assert diff.tv_price == 4684.5          # the live quote, unmeasured
    assert diff.master_price == 4696.0
    assert diff.diff == 0.0                 # no cache to fall back to


def test_a_missing_side_does_not_poison_the_median():
    """A failed cycle must not enter the smoothing window as a zero."""
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    for _ in range(3):
        _resolve(calc, 4684.5, 4696.0)
    good = calc._samples[TV][-1]

    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()
    calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)

    assert list(calc._samples[TV]) == [good] * 3


# ==================== smoothing ====================

def _nq_calc(window=7):
    return _calc({TV: _quote(4684.5)}, refresh_sec=0.0, window=window)


def test_offset_is_the_median_of_recent_samples():
    calc = _nq_calc()
    for master in (4696.0, 4697.0, 4698.0):
        diff, _ = _resolve(calc, 4684.5, master)

    assert diff.samples == 3
    assert diff.raw_diff == pytest.approx(4698.0 - 4684.5)
    assert diff.diff == pytest.approx(4697.0 - 4684.5)     # median, not last
    assert diff.spread == pytest.approx(2.0)


def test_single_outlier_cannot_move_the_offset():
    calc = _nq_calc()
    for master in (4696.0, 4696.0, 4696.0, 9999.0, 4696.0):
        diff, _ = _resolve(calc, 4684.5, master)

    assert diff.diff == pytest.approx(4696.0 - 4684.5)


def test_window_length_is_bounded():
    calc = _nq_calc(window=3)
    for master in (4690.0, 4691.0, 4692.0, 4693.0):
        diff, _ = _resolve(calc, 4684.5, master)

    assert diff.samples == 3


# ==================== falling back to the cache ====================

def test_missing_history_falls_back_to_cached_offset():
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    good, _ = _resolve(calc, 4684.5, 4696.0)

    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()
    (stale,) = calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)

    assert stale.stale is True
    assert stale.diff == pytest.approx(good.diff)
    assert stale.quote_age_s >= 0.0


def test_scanner_failure_keeps_last_offset():
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    good, _ = _resolve(calc, 4684.5, 4696.0)

    calc._fetch = lambda tickers: ({}, "TradingView scanner unreachable: x")
    price_at, current, srv, _ = _providers()
    (stale,) = calc.refresh(price_at, current, srv)

    assert stale.stale is True
    assert stale.diff == pytest.approx(good.diff)
    assert "unreachable" in stale.error


def test_cold_start_with_no_cache_reports_zero_not_a_crash():
    calc = _calc({}, error="TradingView knows none of: COMEX:GC1!")
    price_at, current, offset, _ = _providers()

    (diff,) = calc.refresh(price_at, current, offset)

    assert diff.diff == 0.0
    assert diff.stale is True
    assert diff.error


def test_provider_exceptions_are_contained():
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)

    def boom(*a):
        raise RuntimeError("terminal gone")

    (diff,) = calc.refresh(boom, boom, boom, bar_at=boom)

    assert diff.stale is True
    assert diff.error


def test_error_toast_only_fires_on_the_edge():
    """A scanner down for an hour must not toast every 5s for an hour."""
    calc = _calc({})
    assert calc.take_error_edge("boom") == "boom"    # the edge
    assert calc.take_error_edge("boom") == ""        # still down, stay quiet
    assert calc.take_error_edge("") == ""            # recovered
    assert calc.take_error_edge("boom") == "boom"    # broke again — new edge


def test_refresh_is_throttled():
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=5.0)
    assert calc.due() is True
    _resolve(calc, 4684.5, 4696.0)
    assert calc.due() is False


def test_no_pairs_is_never_due():
    assert PriceCalculator([]).due() is False


# ==================== the two freshness counters ====================
#
# Two ages, each taken from the data that side ANSWERED with. They are equal by
# construction, so any difference is a fault made visible — a clock shift, a
# bar from the wrong minute — rather than something inferred from an offset
# that merely looks strange.

def test_both_ages_are_the_lookback_in_use():
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0)

    expected = DELAY + pc.SETTLE_MARGIN_S
    assert diff.tv_age_s == pytest.approx(expected, abs=61.0)
    assert diff.master_age_s == pytest.approx(expected, abs=61.0)
    assert abs(diff.master_age_s - diff.tv_age_s) < 2.0


def test_the_master_age_is_measured_from_the_bars_close():
    """An M1 close is true at bar_time + 60 — that is when to age it from."""
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    common = pc.common_instant(time.time(), DELAY)
    # A bar whose OPEN is one minute before the requested close.
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.master_age_s == pytest.approx(time.time() - common, abs=2.0)


def test_master_tick_age_is_reported_when_available():
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 3.5)

    assert diff.master_quote_age_s == 3.5


def test_a_broken_quote_age_provider_cannot_break_the_measurement():
    def boom(symbol):
        raise RuntimeError("no tick")

    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0, quote_age=boom)

    assert diff.master_quote_age_s == 0.0
    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)


def test_the_quote_instant_is_pinned_at_fetch_not_recomputed():
    """Sampling and tracing must use the same instant, or the trace cannot
    check the sampling. ``fetched_at`` freezes it."""
    fetched = time.time() - 30.0
    quote = TVQuote(TV, 4684.5, delay_s=600.0, bar_time=0, fetched_at=fetched)

    assert quote.quote_time == fetched - 600.0
    assert quote.quote_time == quote.quote_time      # stable across reads


# ==================== config plumbing ====================

def test_config_parses_price_calc_section(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "master:\n"
        "  terminal_path: C:\\\\mt5\\\\terminal64.exe\n"
        "slaves: []\n"
        "symbol_mapping: {}\n"
        "price_calc:\n"
        "  enabled: true\n"
        "  refresh_sec: 5\n"
        "  pairs:\n"
        "    - tv: COMEX:GC1!\n"
        "      master: XAUUSD\n"
        "      last_diff: 12.44\n",
        encoding="utf-8",
    )
    loaded = Config.load(cfg)

    assert loaded.price_calc.enabled is True
    assert loaded.price_calc.pairs[0].tv == "COMEX:GC1!"
    assert loaded.price_calc.pairs[0].last_diff == 12.44


def test_config_without_price_calc_section_defaults_off(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "master:\n  terminal_path: x\nslaves: []\nsymbol_mapping: {}\n",
        encoding="utf-8",
    )
    loaded = Config.load(cfg)

    assert loaded.price_calc.enabled is False
    assert loaded.price_calc.pairs == []


def test_half_filled_pairs_are_dropped():
    pc = PriceCalcConfig(pairs=[{"tv": "FX:EURUSD"}, {"tv": "A", "master": "B"}])
    assert [p.tv for p in pc.pairs] == ["A"]


def test_refresh_sec_has_a_floor():
    assert PriceCalcConfig(refresh_sec=0.01).refresh_sec == 1.0


# ==================== tracing ====================
#
# Every measurement lands in the trace stream. Without it a wrong converted
# price is unattributable: a stale TV value, a bad server-clock shift, a bar
# from the wrong minute and a lagging median all look identical after the fact.
# The 600s sampling regression was diagnosed from this stream alone.

def test_every_measurement_is_traced(tracer_spy):
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0)

    (ev,) = tracer_spy.of("price_calc")
    assert ev["tv"] == TV
    assert ev["master"] == MASTER
    assert ev["tv_price"] == 4684.5
    assert ev["master_at"] == 4696.0
    assert ev["raw"] == pytest.approx(4696.0 - 4684.5)
    assert ev["samples"] == 1
    assert ev["delay_s"] == DELAY
    assert ev["stale"] is False
    assert ev["error"] == ""


def test_the_trace_carries_the_instant_and_both_ages(tracer_spy):
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 1.25)

    (ev,) = tracer_spy.of("price_calc")
    assert ev["common_at"] == diff.common_at
    assert ev["srv_offset"] == SRV_OFFSET
    assert ev["sampled_at"] == pytest.approx(diff.common_at + SRV_OFFSET)
    assert DELAY + pc.SETTLE_MARGIN_S <= ev["lookback_s"] < DELAY + 120.0
    assert ev["age_gap_s"] == pytest.approx(0.0, abs=2.0)
    assert ev["master_quote_age_s"] == 1.25
    assert ev["tv_missing"] is False
    assert ev["master_missing"] is False


def test_a_missing_side_is_traced_as_such(tracer_spy):
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()

    calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)

    (ev,) = tracer_spy.of("price_calc")
    assert ev["master_missing"] is True
    assert ev["tv_missing"] is False
    assert ev["raw"] == 0.0
    assert ev["stale"] is True


def test_one_trace_event_per_pair_per_refresh(tracer_spy):
    quotes = {
        TV: _quote(4684.5),
        "CME_MINI:NQ1!": _quote(29172.5, ticker="CME_MINI:NQ1!"),
    }
    calc = _calc(
        quotes, refresh_sec=0.0,
        pairs=[
            PriceCalcPair(tv=TV, master=MASTER),
            PriceCalcPair(tv="CME_MINI:NQ1!", master="NAS100"),
        ],
    )
    price_at, current, srv, _ = _providers()

    calc.refresh(price_at, current, srv)
    calc.refresh(price_at, current, srv)

    events = tracer_spy.of("price_calc")
    assert len(events) == 4
    assert [e["tv"] for e in events[:2]] == [TV, "CME_MINI:NQ1!"]


def test_provider_exception_still_produces_a_trace(tracer_spy):
    calc = _calc({TV: _quote(4684.5)})

    def boom(*a):
        raise RuntimeError("terminal gone")

    calc.refresh(boom, boom, boom, bar_at=boom)

    (ev,) = tracer_spy.of("price_calc")
    assert ev["stale"] is True
    assert ev["error"]


# ============ one toast per outage, and only for the scanner ============
#
# The per-pair message names the instant it is about ("no price for 20:04:00"),
# so it is a different string every minute. Fed to the edge-triggered toast it
# produced an endless stream of near-identical popups while the feed was in fact
# answering normally (traces 2026-08-27). Only the scanner's own failure is
# offered as a toast; the per-pair state belongs to the row and the trace.

def test_the_toast_source_is_the_scanner_error_alone():
    calc = _calc({TV: _quote(4684.5)})
    _seed_tv(calc, 4684.5)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)

    assert diff.error                      # the row still says what is wrong
    assert calc.last_scan_error == ""      # but nothing is offered as a toast


def test_a_missing_instant_never_reaches_the_toast_even_as_it_changes():
    """The message changes every minute; the toast must not fire every minute."""
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    price_at, current, srv, _ = _providers()
    seen = []
    for _ in range(3):
        (diff,) = calc.refresh(price_at, current, srv, bar_at=lambda s, t: None)
        seen.append(diff.error)
        edge = calc.take_error_edge(calc.last_scan_error)
        assert edge == ""

    assert all(seen)                       # every cycle reported it somewhere


def test_the_scanner_error_is_exposed_for_the_toast():
    calc = _calc({}, error="TradingView scanner unreachable: timed out")
    price_at, current, srv, _ = _providers()

    calc.refresh(price_at, current, srv)

    assert "unreachable" in calc.last_scan_error
    assert calc.take_error_edge(calc.last_scan_error)   # one toast
    assert calc.take_error_edge(calc.last_scan_error) == ""  # and only one


def test_the_scanner_error_clears_on_recovery():
    calc = _calc({}, error="boom", refresh_sec=0.0)
    price_at, current, srv, _ = _providers()
    calc.refresh(price_at, current, srv)
    assert calc.last_scan_error == "boom"

    calc._fetch = lambda tickers: ({TV: _quote(4684.5)}, "")
    calc.refresh(price_at, current, srv)

    assert calc.last_scan_error == ""


def test_the_measurement_reports_the_instant_it_used():
    """The UI prints it directly rather than deriving it from the lookback."""
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0)

    assert diff.common_at % 60 == 0
    assert diff.common_at == pytest.approx(time.time() - diff.lookback_s, abs=2.0)


def test_a_quiet_feed_still_measures():
    """A publication 141s before the instant is the value at that instant.

    The regression this pins: 16% of live cycles were reported missing on a
    quiet market purely because the lookup searched for the NEAREST entry
    within 30s instead of the last one in force.
    """
    calc = _calc({TV: _quote(4684.5)})
    common = pc.common_instant(time.time(), DELAY)
    _seed_tv(calc, 4684.5, at=common - 141.0)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.tv_missing is False
    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)
    # Aged from the instant, not from the publication — so it stays comparable
    # with the master's age. How long the feed has been silent is separate.
    assert diff.tv_age_s == pytest.approx(diff.master_age_s, abs=2.0)
    assert diff.tv_published_age_s > diff.tv_age_s


def test_a_dead_feed_is_reported_missing_not_measured():
    calc = _calc({TV: _quote(4684.5)})
    common = pc.common_instant(time.time(), DELAY)
    _seed_tv(calc, 4684.5, at=common - pc.TV_STALE_AFTER_S - 1)
    bar, _ = _bar_close_at(4696.0, only_at=common + SRV_OFFSET)
    price_at, current, srv, _ = _providers()

    (diff,) = calc.refresh(price_at, current, srv, bar_at=bar)

    assert diff.tv_missing is True
    assert diff.raw_diff == 0.0


# ============ a shut market is not a measurement ============
#
# MT5 keeps returning the final bars of a session long after it ends — flat,
# unchanging, and perfectly well-formed. TradingView's feed runs on. Pairing the
# two reports the market drifting away from a frozen number as a venue offset.
#
# Measured at the 21:00 CME break (traces 2026-08-27): 921 cycles produced 3
# distinct master prices against 16 from TV, and the offset ran to -129 against
# a true -64. Split by the master's own tick age, every bad reading in an
# 11-hour log fell on one side of the line:
#
#     tick < 60s : n=7003  sd  4.26
#     tick >  5m : n= 203  sd 30.30   <- all of it

def test_a_master_that_stopped_ticking_before_the_instant_is_refused():
    calc = _calc({TV: _quote(4684.5)})
    # The last tick is older than the lookback: the market was already shut
    # when the bar we are about to read was supposedly forming.
    diff, _ = _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 3600.0)

    assert diff.master_stale is True
    assert diff.master_missing is True
    assert diff.raw_diff == 0.0
    assert "has not ticked since" in diff.error
    assert "market is shut" in diff.error


def test_a_market_that_stopped_after_the_instant_still_measures():
    """The bar is real — it formed while the market was open. Only a tick
    older than the instant itself invalidates it."""
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 120.0)

    assert diff.master_stale is False
    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)


def test_the_rule_needs_no_threshold_to_tune():
    """It is the lookback itself, so it tracks the feed's delay automatically.

    A 900s feed reads further back and tolerates a correspondingly older tick;
    a live feed tolerates almost none. No constant to pick, and nothing to
    revisit when the delay changes.
    """
    for delay in (0.0, 600.0, 900.0):
        calc = _calc({TV: _quote(4684.5, delay=delay)})
        lookback = time.time() - pc.common_instant(time.time(), delay)
        just_ok, _ = _resolve(calc, 4684.5, 4696.0, delay=delay,
                              quote_age=lambda s, v=lookback - 30: v)
        assert just_ok.master_stale is False, delay

        calc2 = _calc({TV: _quote(4684.5, delay=delay)})
        too_old, _ = _resolve(calc2, 4684.5, 4696.0, delay=delay,
                              quote_age=lambda s, v=lookback + 30: v)
        assert too_old.master_stale is True, delay


def test_without_a_tick_age_provider_nothing_changes():
    """The check is skipped rather than guessed when the age is unavailable."""
    calc = _calc({TV: _quote(4684.5)})
    diff, _ = _resolve(calc, 4684.5, 4696.0)          # no quote_age injected

    assert diff.master_quote_age_s == 0.0
    assert diff.master_stale is False
    assert diff.raw_diff == pytest.approx(4696.0 - 4684.5)


def test_a_stale_master_falls_back_to_the_cached_offset():
    """The converter keeps working off the last good measurement."""
    calc = _calc({TV: _quote(4684.5)}, refresh_sec=0.0)
    good, _ = _resolve(calc, 4684.5, 4696.0)

    shut, _ = _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 3600.0)

    assert shut.stale is True
    assert shut.diff == pytest.approx(good.diff)


def test_a_stale_master_is_traced():
    calc = _calc({TV: _quote(4684.5)})
    _resolve(calc, 4684.5, 4696.0, quote_age=lambda s: 3600.0)

    from src.price_calc import get_tracer
    (ev,) = get_tracer().of("price_calc")
    assert ev["master_stale"] is True
    assert ev["master_missing"] is True
