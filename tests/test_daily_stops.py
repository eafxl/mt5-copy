"""Four losing closes a day, counted from the broker's own history.

WHY THIS FILE EXISTS
    The old limit (MAX_STOPS = 4) lived in a session variable. Four stops,
    close the app, reopen — zero again. Restarting the app is exactly what a
    person does after a run of stops, so the limit was weakest at the only
    moment it mattered. The count now comes from the master's deal history,
    which restarting cannot touch.

WHY THERE IS NO CLOCK ARITHMETIC IN HERE
    There was, and it was a mistake. Counting the HOST's day meant converting
    every broker timestamp into the host's clock, and that offset was measured
    off the last tick — which on a closed market is hours stale. Measured on a
    Saturday it read -9h08m for a broker at UTC+3, moved Friday's ten losing
    trades into Saturday, and would have stopped trading on a day that had not
    traded.

    An MT5 timestamp is the broker's wall clock stored as an epoch that reads
    as UTC, so every trade already knows its own broker date. Counting by the
    broker's day deletes the conversion, the tick, the offset and every defence
    they needed — and it is the boundary a prop firm's daily reset uses.
"""
import time

import pytest

from src.daily_stops import MAX_DAILY_STOPS, DailyStopGuard, host_today
from src.mt5_client import ClosedTrade, server_day_of

TODAY = host_today()
YESTERDAY = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


class _Client:
    """An MT5Client stub: a fixed history, and a broker date of its own."""

    def __init__(self, trades, today=None):
        self.trades = trades
        self.today = today or TODAY
        self.asked_days = None

    def server_today(self):
        return self.today

    def closed_trades(self, days_back=1):
        self.asked_days = days_back
        return self.trades


def trade(net, day=TODAY, symbol="NAS100", position_id=1, at="12:00"):
    """One closed position on a given BROKER day."""
    stamp = time.mktime(time.strptime(f"{day} {at}", "%Y-%m-%d %H:%M"))
    return ClosedTrade(
        position_id=position_id, symbol=symbol, net=net,
        closed_at=stamp, server_day=day,
    )


def losses(n, day=TODAY):
    return [trade(-50.0, day=day, position_id=1000 + i) for i in range(n)]


def guard(trades, today=None):
    return DailyStopGuard(_Client(trades, today))


# ==================== counting ====================

def test_three_losses_is_not_a_trip():
    g = guard(losses(3))

    assert g.refresh() == 3
    assert not g.tripped
    assert g.remaining == 1


def test_the_fourth_loss_trips_it():
    g = guard(losses(4))
    g.refresh()

    assert g.tripped
    assert g.remaining == 0


def test_a_winning_trade_does_not_count():
    g = guard(losses(3) + [trade(+400.0, position_id=2001)])

    assert g.refresh() == 3


def test_a_break_even_close_is_not_a_loss():
    assert guard([trade(0.0)]).refresh() == 0


def test_more_than_the_limit_is_still_reported_honestly():
    """Ten of four is a real answer; clamping it would hide how bad the day was."""
    g = guard(losses(10))

    assert g.refresh() == 10
    assert g.tripped


# ==================== the broker's day is the day ====================

def test_yesterdays_losses_do_not_count():
    g = guard(losses(4, day=YESTERDAY) + losses(1))

    assert g.refresh() == 1
    assert not g.tripped


def test_a_quiet_day_after_a_terrible_one_counts_zero():
    """The Saturday case that started this: Friday's ten losses are Friday's."""
    g = guard(losses(10, day=YESTERDAY))

    assert g.refresh() == 0
    assert not g.tripped


def test_the_day_comes_from_the_trade_not_from_arithmetic():
    """No offset, no tick, no conversion — the trade already knows its date."""
    g = guard([trade(-50.0, day=TODAY), trade(-50.0, day=YESTERDAY)])
    g.refresh()

    assert [t.server_day for t in g.losses] == [TODAY]


def test_neighbouring_days_are_fetched_so_a_boundary_error_is_visible():
    """Asking only for today would make a misfiled trade silently absent."""
    client = _Client(losses(1))
    DailyStopGuard(client).refresh()

    assert client.asked_days >= 2


def test_a_broker_stamp_dates_itself_without_conversion():
    """An MT5 timestamp is the server's wall clock stored as epoch-as-UTC."""
    # 2026-08-28 16:30 on the broker's clock.
    assert server_day_of(1787934600) == "2026-08-28"


def test_the_day_counted_is_the_one_the_broker_is_on():
    """The limit resets on the broker's day, so that is the day it asks about.

    Host and broker on different dates is the whole point of the distinction.
    """
    trades = [trade(-50.0, day="2026-08-28", position_id=i) for i in range(4)]
    g = DailyStopGuard(_Client(trades, today="2026-08-28"))

    g.refresh()

    assert g.day == "2026-08-28"
    assert g.tripped, "the broker's day has four losses on it"


def test_the_host_date_does_not_override_the_brokers():
    trades = [trade(-50.0, day="2026-08-28", position_id=i) for i in range(4)]
    g = DailyStopGuard(_Client(trades, today="2026-08-29"))

    g.refresh()

    assert g.count == 0, "yesterday on the broker's clock is not today"


def test_a_broker_that_cannot_be_asked_falls_back_to_the_host():
    """Never a crash and never a stale trading day — the fallback direction
    that cannot lock trading out wrongly."""
    class _Mute(_Client):
        def server_today(self):
            raise RuntimeError("terminal gone")

    g = DailyStopGuard(_Mute(losses(4)))
    g.refresh()

    assert g.day == host_today()


# ==================== the broker's current date ====================

class _Tick:
    def __init__(self, stamp):
        self.time = stamp


def _client_with_tick(monkeypatch, stamps):
    """An MT5Client whose quote returns each stamp in turn."""
    import MetaTrader5 as mt5

    from src.mt5_client import MT5Client

    client = MT5Client.__new__(MT5Client)
    client.logger = type("L", (), {"debug": lambda *a: None,
                                   "info": lambda *a: None,
                                   "warning": lambda *a: None,
                                   "error": lambda *a: None})()
    client._last_seen_tick = 0.0
    client._any_symbol = lambda: "EURUSD"
    seq = list(stamps)
    monkeypatch.setattr(
        mt5, "symbol_info_tick",
        lambda s: _Tick(seq.pop(0)) if seq else None, raising=False,
    )
    return client


def test_a_live_quote_gives_the_brokers_date_outright(monkeypatch):
    """Advancing quotes mean the market is open, so the quote IS 'now'."""
    monday = 1787934600            # 2026-08-28 16:30 broker time
    client = _client_with_tick(monkeypatch, [monday, monday + 60])

    client.server_today()          # first read establishes the baseline
    assert client.server_today() == "2026-08-28"


def test_a_stale_quote_never_reports_the_last_trading_day(monkeypatch):
    """The Saturday bug: Friday's quote must not make today Friday, or
    Friday's losses lock trading out on a day that never traded."""
    friday_close = 1787934600      # 2026-08-28 16:30
    client = _client_with_tick(monkeypatch, [friday_close, friday_close])

    client.server_today()
    day = client.server_today()

    assert day >= host_today(), "a frozen quote cannot drag the date backwards"


def test_no_quote_at_all_falls_back_to_the_host_date(monkeypatch):
    client = _client_with_tick(monkeypatch, [])

    assert client.server_today() == host_today()


# ==================== cannot tell is not zero ====================

def test_an_unreadable_history_reports_unknown():
    assert guard(None).refresh() is None


def test_unknown_is_not_a_trip():
    """Refusing to trade because the terminal blinked is our own outage."""
    g = guard(None)
    g.refresh()

    assert not g.tripped
    assert g.count is None
    assert g.remaining is None


def test_unknown_is_not_reported_as_zero_losses():
    g = guard(None)
    g.refresh()

    assert "unknown" in g.describe()


# ==================== restart is the point ====================

def test_a_restart_re_derives_the_same_count():
    """The actual bug: the old counter was a session variable."""
    first = guard(losses(4))
    first.refresh()

    fresh = guard(losses(4))        # a brand-new process would look like this
    fresh.refresh()

    assert first.count == fresh.count == 4
    assert fresh.tripped


# ==================== reporting ====================

def test_the_limit_is_four():
    assert MAX_DAILY_STOPS == 4
    assert DailyStopGuard(None).limit == 4


def test_the_trip_is_announced_once():
    """It is rediscovered on every blocked copy; the dialog must appear once."""
    g = guard(losses(4))
    g.refresh()

    assert g.announce_once() is True
    assert g.announce_once() is False


def test_nothing_is_announced_before_the_limit():
    g = guard(losses(3))
    g.refresh()

    assert g.announce_once() is False


def test_the_description_names_the_trades():
    g = guard([trade(-122.5, symbol="NAS100", at="09:14")])
    g.refresh()

    assert "1 of 4" in g.describe()
    assert "NAS100" in g.describe()
    assert "-122.50" in g.describe()


def test_the_times_shown_are_the_brokers_own():
    """They are compared against the terminal's History tab line by line, so
    they must be in the clock that tab uses."""
    g = guard([trade(-10.0, at="09:14")])
    g.refresh()

    from datetime import datetime, timezone

    expected = datetime.fromtimestamp(
        g.losses[0].closed_at, timezone.utc
    ).strftime("%H:%M")
    assert g.describe_losses()[0].startswith(expected)


def test_the_losses_are_listed_oldest_first():
    g = guard([
        trade(-10.0, symbol="GER40", position_id=2, at="11:00"),
        trade(-20.0, symbol="NAS100", position_id=1, at="09:00"),
    ])
    g.refresh()

    assert [line.split()[1] for line in g.describe_losses()] == ["NAS100", "GER40"]
