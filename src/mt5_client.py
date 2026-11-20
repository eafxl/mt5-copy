"""MT5 API wrapper with error handling and retries."""
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import MetaTrader5 as mt5

from .utils.logging import get_logger
from .utils.tracer import get_tracer


class OrderType(IntEnum):
    """Order type enumeration."""
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5


@dataclass
class Position:
    """Position data container."""
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    magic: int
    comment: str
    time: int
    time_msc: int = 0  # master open time in ms (broker server clock)

    @property
    def is_buy(self) -> bool:
        return self.type == OrderType.BUY


@dataclass
class AccountInfo:
    """Account information."""
    login: int
    server: str
    leverage: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str


@dataclass
class ClosedTrade:
    """One position that is closed, as the broker's own history records it.

    ``net`` is realized profit including swap and commission — the number the
    account balance actually moved by, not the raw price difference.

    ``closed_at`` is the broker's stamp exactly as MT5 gives it, and
    ``server_day`` is the calendar date it falls on FOR THE BROKER. No clock
    conversion happens anywhere: an MT5 timestamp is the server's wall clock
    stored as an epoch that reads as UTC, so its date is already the server's
    date. Everything that tried to translate that into the host's clock was
    machinery serving a question nobody needed to ask.
    """
    position_id: int
    symbol: str
    net: float
    closed_at: float          # broker's stamp, unconverted
    server_day: str = ""      # "2026-08-28", the broker's own calendar

    @property
    def is_loss(self) -> bool:
        return self.net < 0

    @property
    def server_time(self) -> str:
        """``14:03`` on the broker's clock — what the History tab shows."""
        return datetime.fromtimestamp(self.closed_at, timezone.utc).strftime("%H:%M")


def server_day_of(server_ts: float) -> str:
    """The broker's calendar date for one of its own timestamps."""
    return datetime.fromtimestamp(server_ts, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class SymbolInfo:
    """Symbol information."""
    name: str
    point: float
    digits: int
    volume_min: float
    volume_max: float
    volume_step: float
    bid: float
    ask: float
    trade_mode: int
    trade_tick_value: float = 1.0  # Value of 1 tick for 1 lot (deposit currency)
    trade_tick_size: float = 0.00001  # Price change per tick


#: Broker server clocks are timezones: a whole number of hours from UTC, or a
#: half hour in a few places. Nothing legitimate sits eight minutes off the
#: hour — that shape means the "server time" being measured is a STALE TICK
#: from a closed market, not the server's clock.
#:
#: The tolerance is deliberately tight. A tick stale by exactly a whole number
#: of hours still passes this, which is why it is only one of the defences.
OFFSET_QUANTUM_S = 1800.0
OFFSET_TOLERANCE_S = 300.0
OFFSET_MAX_S = 14 * 3600.0


def _plausible_offset(offset: float) -> bool:
    """Could this be a broker's timezone, rather than a stale quote?"""
    if offset is None or abs(offset) > OFFSET_MAX_S:
        return False
    nearest = round(offset / OFFSET_QUANTUM_S) * OFFSET_QUANTUM_S
    return abs(offset - nearest) <= OFFSET_TOLERANCE_S


class MT5Error(Exception):
    """MT5 API error."""
    def __init__(self, message: str, error_code: int = 0):
        self.error_code = error_code
        super().__init__(f"{message} (error: {error_code})")


class MT5Client:
    """MT5 API client with error handling."""

    def __init__(self, retry_attempts: int = 3, retry_delay_ms: int = 1000):
        self.retry_attempts = retry_attempts
        self.retry_delay_ms = retry_delay_ms
        self.logger = get_logger()
        self._current_terminal: str | None = None
        # Rolling (host_ts, raw_offset) samples for server_time_offset().
        self._offset_samples: "deque[tuple[float, float]]" = deque()
        # The last tick timestamp seen, so a frozen feed stops contributing
        # samples, and the last answer, so an empty window keeps returning it.
        self._last_tick_ts: float = 0.0
        self._server_offset: float | None = None
        # Separate from _last_tick_ts, which the offset sampler consumes:
        # server_today() must not steal the "this tick advanced" edge from it.
        self._last_seen_tick: float = 0.0

    def initialize(self, terminal_path: str = "", timeout_ms: int = 60000) -> bool:
        """Initialize MT5 connection to specific terminal."""
        # Always shutdown first to ensure clean state
        self.logger.info(f"Connecting to terminal: {terminal_path or 'default'}")
        mt5.shutdown()
        time.sleep(0.1)

        for attempt in range(self.retry_attempts):
            try:
                if terminal_path:
                    result = mt5.initialize(path=terminal_path, timeout=timeout_ms)
                else:
                    result = mt5.initialize(timeout=timeout_ms)

                if result:
                    self._current_terminal = terminal_path
                    self.logger.info(f"Terminal connected successfully")
                    return True

                error = mt5.last_error()
                self.logger.warning(
                    f"MT5 init attempt {attempt + 1}/{self.retry_attempts}: {error}"
                )

            except Exception as e:
                self.logger.warning(f"MT5 init exception: {e}")

            time.sleep(self.retry_delay_ms / 1000)

        self.logger.error(f"MT5 initialize failed after {self.retry_attempts} attempts")
        return False

    def shutdown(self):
        """Shutdown MT5 connection."""
        mt5.shutdown()
        self._current_terminal = None

    def login(self, login: int, password: str, server: str) -> bool:
        """Login to MT5 account."""
        for attempt in range(self.retry_attempts):
            if mt5.login(login, password=password, server=server):
                self.logger.info(f"Logged in to account {login}")
                return True

            error = mt5.last_error()
            self.logger.warning(
                f"Login attempt {attempt + 1}/{self.retry_attempts} failed: {error}"
            )
            if attempt < self.retry_attempts - 1:
                time.sleep(self.retry_delay_ms / 1000)

        self.logger.error(f"Failed to login to account {login}")
        return False

    def get_account_info(self) -> AccountInfo | None:
        """Get current account information."""
        info = mt5.account_info()
        if info is None:
            self.logger.error(f"Failed to get account info: {mt5.last_error()}")
            return None

        return AccountInfo(
            login=info.login,
            server=info.server,
            leverage=info.leverage,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            currency=info.currency,
        )

    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        positions = mt5.positions_get()
        if positions is None:
            error = mt5.last_error()
            if error[0] != 0:  # 0 means no positions
                self.logger.error(f"Failed to get positions: {error}")
            return []

        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                type=p.type,
                volume=p.volume,
                price_open=p.price_open,
                price_current=p.price_current,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                magic=p.magic,
                comment=p.comment,
                time=p.time,
                time_msc=getattr(p, "time_msc", 0),
            )
            for p in positions
        ]

    def get_position_by_ticket(self, ticket: int) -> Position | None:
        """Get position by ticket."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return None
        p = positions[0]
        return Position(
            ticket=p.ticket,
            symbol=p.symbol,
            type=p.type,
            volume=p.volume,
            price_open=p.price_open,
            price_current=p.price_current,
            sl=p.sl,
            tp=p.tp,
            profit=p.profit,
            magic=p.magic,
            comment=p.comment,
            time=p.time,
            time_msc=getattr(p, "time_msc", 0),
        )

    def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        """Get symbol information."""
        tracer = get_tracer()
        with tracer.span("mt5_get_symbol_info") as span:
            span.set_tag("symbol", symbol)

            info = mt5.symbol_info(symbol)
            if info is None:
                self.logger.error(f"Symbol '{symbol}' not found")
                span.set_tag("result", "not_found")
                return None

            # Ensure symbol is visible in Market Watch
            if not info.visible:
                if not mt5.symbol_select(symbol, True):
                    self.logger.warning(f"Could not enable symbol '{symbol}'")

            tick = mt5.symbol_info_tick(symbol)
            bid = tick.bid if tick else 0
            ask = tick.ask if tick else 0

            span.set_tags(bid=bid, ask=ask, digits=info.digits, result="ok")

            return SymbolInfo(
                name=info.name,
                point=info.point,
                digits=info.digits,
                volume_min=info.volume_min,
                volume_max=info.volume_max,
                volume_step=info.volume_step,
                bid=bid,
                ask=ask,
                trade_mode=info.trade_mode,
                trade_tick_value=info.trade_tick_value,
                trade_tick_size=info.trade_tick_size,
            )

    def server_time_offset(self, symbol: str) -> float | None:
        """Broker server-clock offset from real UTC: ``server_now - host_utc``.

        MT5 timestamps (position/tick ``time``) are the broker server's local
        seconds, NOT UTC. This measures the offset live off a fresh tick — no
        timezone tables, no DST logic — so any server timestamp can be
        normalized to true UTC (and re-projected into another broker's clock).
        Returns None only before the first tick has ever been seen.
        """
        # The offset is ~constant (broker timezone); the jitter comes from tick
        # STALENESS — the last tick can be older than "now", which UNDER-states
        # server_now → under-states the offset. A stale tick can only
        # under-estimate, so the MAX over a recent window is the least-stale.
        #
        # TWO RULES KEEP THAT FROM COLLAPSING WHEN THE FEED GOES QUIET, and
        # both were learned the hard way (traces 2026-08-27): across the 21:00
        # CME break this returned values from 6898 to 10803 — a 65-MINUTE
        # spread on a broker sitting at a constant UTC+3. The real-price
        # sampler adds the offset to reach the broker's clock, so it was
        # reading bars from an hour away and the offset it computed swung to
        # -129 against a true -64. Nothing about the market was wrong; the
        # clock estimate was.
        #
        #   1. Sample only when the tick has ACTUALLY ADVANCED. Re-reading one
        #      frozen tick every 500ms otherwise floods the window with
        #      samples whose implied offset shrinks by 0.5s each time, and the
        #      max walks down with them.
        #   2. Never expire the last answer. Samples still age out so a real
        #      shift (DST) is adopted within the window once ticks resume, but
        #      an empty window means "no news", not "no offset" — and a
        #      weekend is 48h of no news.
        #   3. A CLOSED MARKET has no fresh tick at all, and the rules above
        #      cannot save that: with one stale sample, the max IS the stale
        #      one. Measured on a Saturday (capture 2026-08-29) the last
        #      EURUSD tick was 16.5h old and this returned -9h08m for a broker
        #      sitting at UTC+3 — and the daily stop counter, which turns
        #      local midnight into broker time with it, then counted Friday's
        #      ten losing trades as today's and locked trading out.
        #
        #      Two defences. A sample must LOOK like a broker timezone (see
        #      _plausible_offset) — -9h08m does not, because no broker sits
        #      eight minutes off the hour. And the last good answer is
        #      remembered ACROSS RESTARTS, so a weekend or a cold start on a
        #      closed market reuses the value measured while it was open.
        host_now = time.time()
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and tick.time:
            tmsc = getattr(tick, "time_msc", 0)
            server_now = (tmsc / 1000.0) if tmsc else float(tick.time)
            if server_now != self._last_tick_ts:
                self._last_tick_ts = server_now
                candidate = server_now - host_now
                if _plausible_offset(candidate):
                    self._offset_samples.append((host_now, candidate))
        cutoff = host_now - 600.0
        while self._offset_samples and self._offset_samples[0][0] < cutoff:
            self._offset_samples.popleft()
        if self._offset_samples:
            # Live samples win, so a real shift (DST) is adopted as soon as the
            # stale ones age out of the window.
            self._server_offset = max(o for _, o in self._offset_samples)
        return self._server_offset

    def _any_symbol(self) -> str:
        """Some symbol this terminal can quote, for measuring the clock.

        ``server_time_offset`` needs a symbol to read a tick from. History has
        no symbol of its own, and at startup there may be no open position to
        borrow one from, so fall back to whatever Market Watch holds.
        """
        try:
            positions = mt5.positions_get()
            if positions:
                return str(positions[0].symbol)
        except Exception:
            pass
        try:
            symbols = mt5.symbols_get()
        except Exception:
            return ""
        for sym in symbols or ():
            if getattr(sym, "visible", True):
                return str(sym.name)
        return ""

    def server_today(self) -> str:
        """The BROKER's current calendar date, ``YYYY-MM-DD``.

        The limit resets on the broker's day, so this is the day it must ask
        about — not the host machine's, which is a different calendar whenever
        the two clocks straddle midnight.

        HOW IT KNOWS, WITHOUT A CLOCK OFFSET
            A quote carries the broker's wall clock. While the market is open
            that quote is the broker's "now", so its date is the answer
            outright. Liveness is read from the quote ADVANCING between calls,
            which needs no comparison between the two clocks — the thing that
            went wrong before.

            With a stale quote (market closed) the broker's date is at least
            the quote's, and at least the host's if the host has already rolled
            over. Taking the later of the two is the safe direction: it can
            only ever look at a day the broker has not reached yet, which
            counts zero trades. The opposite mistake — reusing the last
            TRADING day — is the one that stops trading on a day that has not
            traded, and it is what the Saturday capture caught.
        """
        host_today = time.strftime("%Y-%m-%d", time.localtime())
        symbol = self._any_symbol()
        tick = mt5.symbol_info_tick(symbol) if symbol else None
        stamp = float(getattr(tick, "time", 0) or 0) if tick is not None else 0.0
        if not stamp:
            return host_today

        quote_day = server_day_of(stamp)
        previous, self._last_seen_tick = self._last_seen_tick, stamp
        if previous and stamp != previous:
            # Advancing: the market is open and this IS the broker's now.
            return quote_day
        # A FIRST reading proves nothing — every quote looks new when there is
        # nothing to compare it against, and taking it as live is how a
        # Saturday reports Friday. Until a second reading confirms movement,
        # fall back to the later of the two dates.
        return max(quote_day, host_today)

    def closed_trades(self, days_back: int = 1) -> "list[ClosedTrade] | None":
        """Positions closed recently, dated by the BROKER's own calendar.

    WHY THERE IS NO CLOCK ARITHMETIC HERE ANY MORE
        There used to be. The day boundary was the host machine's midnight, so
        every timestamp had to be carried between two clocks, and the offset
        between them was measured off the last tick. On a closed market that
        tick is hours old and the offset comes out nonsense: measured on a
        Saturday (capture 2026-08-29) it read -9h08m for a broker at UTC+3,
        which moved Friday's ten losing trades into Saturday and would have
        stopped trading on a day that had not traded.

        An MT5 timestamp is the server's wall clock stored as an epoch that
        reads as UTC. Its date is therefore the SERVER's date, already, for
        free. Counting by the broker's day removes the conversion, the tick,
        the offset and every defence they needed — and it is the boundary the
        prop firm's own daily reset uses, so it is also the more correct rule.

        ``days_back`` is generous on purpose: the caller picks the day it
        wants out of the result, and having the neighbouring days present is
        what makes a boundary mistake visible instead of silent.

        Returns None — never a number — when the terminal cannot answer. A
        caller must not read "cannot tell" as "none happened".
        """
        span = max(1, int(days_back)) + 1
        try:
            deals = mt5.history_deals_get(
                datetime.now(timezone.utc) - timedelta(days=span),
                datetime.now(timezone.utc) + timedelta(days=1),
            )
        except Exception as e:
            self.logger.warning("Trade history unavailable: %s", e)
            return None
        if deals is None:
            self.logger.warning(
                "Trade history unavailable: %s", mt5.last_error()
            )
            return None

        out_entries = (
            getattr(mt5, "DEAL_ENTRY_OUT", 1),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
        )
        # position_id -> [net, symbol, latest closing stamp]
        closed: dict[int, list] = {}
        for deal in deals:
            try:
                if int(getattr(deal, "entry", -1)) not in out_entries:
                    continue
                position_id = int(getattr(deal, "position_id", 0) or 0)
                if not position_id:
                    continue
                # A scale-out closes one position with several deals; they net
                # into one trade, or a good day trips the limit by itself.
                net = (float(getattr(deal, "profit", 0.0) or 0.0)
                       + float(getattr(deal, "swap", 0.0) or 0.0)
                       + float(getattr(deal, "commission", 0.0) or 0.0))
                closed_at = float(getattr(deal, "time", 0) or 0)
            except (TypeError, ValueError):
                continue
            row = closed.get(position_id)
            if row is None:
                closed[position_id] = [net, str(getattr(deal, "symbol", "")), closed_at]
            else:
                row[0] += net
                row[2] = max(row[2], closed_at)

        trades = [
            ClosedTrade(
                position_id=pid, symbol=sym, net=net, closed_at=when,
                server_day=server_day_of(when),
            )
            for pid, (net, sym, when) in closed.items()
        ]
        trades.sort(key=lambda t: t.closed_at)
        return trades

    def terminal_state(self) -> dict:
        """What the terminal itself reports about its own readiness.

        ``algo_trading`` is MT5's ``trade_allowed`` — the "Algo Trading"
        toggle in the toolbar. It is the single switch that silently turns a
        working copier into one that places no orders at all, and nothing in
        the app could see it, so it was only ever discovered by a trade not
        happening.

        Returns ``{}`` when the terminal cannot be reached; the caller shows
        that as unknown rather than as "off", because those are different
        facts and only one of them is the operator's to fix.
        """
        try:
            info = mt5.terminal_info()
        except Exception:
            return {}
        if info is None:
            return {}
        return {
            "algo_trading": bool(getattr(info, "trade_allowed", False)),
            "connected": bool(getattr(info, "connected", False)),
            "build": int(getattr(info, "build", 0) or 0),
            "path": str(getattr(info, "path", "") or ""),
        }

    def symbol_digits(self, symbol: str) -> int:
        """Price decimals for a symbol, 0 if unknown. Cheap, non-logging."""
        try:
            info = mt5.symbol_info(symbol)
            return int(info.digits) if info else 0
        except Exception:
            return 0

    def quote_age(self, symbol: str) -> float | None:
        """Seconds since the master's latest tick for ``symbol``, or None.

        The counterpart to the TradingView feed's own delay: the two ages are
        what makes a timing mismatch visible instead of showing up as a wild
        offset. A frozen feed (market closed, symbol dropped out of Market
        Watch) reads as a growing number here rather than as a price that
        merely looks wrong.
        """
        try:
            tick = mt5.symbol_info_tick(symbol)
        except Exception:
            return None
        if tick is None or not tick.time:
            return None
        offset = self.server_time_offset(symbol)
        if offset is None:
            return None
        tmsc = getattr(tick, "time_msc", 0)
        server_ts = (tmsc / 1000.0) if tmsc else float(tick.time)
        return max(0.0, time.time() - (server_ts - offset))

    # M1 bars either side of the target. Back far enough that a thin session or
    # a weekend gap still yields a bar; forward far enough that the bar whose
    # CLOSE lands nearest the target is inside the window.
    _BAR_WINDOW_BACK_S = 600
    _BAR_WINDOW_FWD_S = 120
    _M1_S = 60

    def _m1_window(self, symbol: str, around: float):
        """M1 bars either side of ``around`` (broker clock). Empty on failure.

        ``copy_rates_range``, NOT ``copy_rates_from``: the latter returns
        ``count`` bars ENDING at its date, so asking it for bars "from
        target - 600" hands back a window whose NEWEST bar is 600s BEFORE the
        target. That aged every delayed-feed measurement by exactly the feed's
        delay a second time — a 600s-delayed TradingView quote was compared to
        a master price 1200s old — and the offset then swung with whatever the
        market did across that gap (measured: ±50 points on NAS100 against a
        true offset of -64). A range is unambiguous in both directions.
        """
        from datetime import datetime, timezone

        try:
            frm = datetime.fromtimestamp(
                int(around) - self._BAR_WINDOW_BACK_S, tz=timezone.utc
            )
            to = datetime.fromtimestamp(
                int(around) + self._BAR_WINDOW_FWD_S, tz=timezone.utc
            )
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, frm, to)
        except Exception as e:
            self.logger.debug("_m1_window(%s) bar read failed: %s", symbol, e)
            return ()
        return rates if rates is not None else ()

    def _m1_bar_at(self, symbol: str, target: float):
        """The M1 bar whose CLOSE instant sits nearest ``target``, or None.

        Nearest CLOSE, not "last bar opening at or before target": an M1 bar's
        close is its last price, true at ``bar_time + 60``. Picking the bar that
        merely contains the target returns a close up to 60s NEWER than the
        moment asked for — a one-sided bias, always in the same direction.

        Nearest is right for ``price_at``, which answers "roughly what was it
        then". It is NOT right for the real-price offset — see ``bar_close_at``.
        """
        rates = self._m1_window(symbol, target)
        if len(rates) == 0:
            return None
        return min(
            rates, key=lambda r: abs((int(r["time"]) + self._M1_S) - target)
        )

    def bar_close_at(
        self, symbol: str, server_close_ts: float
    ) -> tuple[float, float, float, float, float] | None:
        """The M1 candle that CLOSED exactly at ``server_close_ts``, or None.

        Exact, because the real-price offset is a comparison between two feeds
        at one instant: a bar from the neighbouring minute answers a different
        question with the same confidence, and that is precisely how a
        10-minute-stale reading passed for a live one. ``None`` is the useful
        answer — the caller reports the reading as missing.

        TIMEZONE
            ``server_close_ts`` is in the BROKER's clock, because MT5 bar
            stamps are server-local seconds and nothing here converts them.
            The caller builds it by adding a live-measured server offset to a
            host-clock instant, so it carries that measurement's sub-second
            jitter. A broker timezone is a whole number of minutes, so the
            required bar is rounded to the minute — that removes the jitter
            without hiding a genuine shift, which would move the result by a
            whole minute or more and show up as the caller's two age counters
            disagreeing.

        Returns ``(open, high, low, close, bar_time)``; ``bar_time`` is the
        bar's OPEN, one ``M1`` before its close.
        """
        if not symbol or not server_close_ts:
            return None

        wanted_open = int(round((float(server_close_ts) - self._M1_S) / self._M1_S)) * self._M1_S
        for bar in self._m1_window(symbol, float(wanted_open)):
            if int(bar["time"]) == wanted_open:
                return (
                    float(bar["open"]), float(bar["high"]),
                    float(bar["low"]), float(bar["close"]),
                    float(wanted_open),
                )
        return None

    def price_at(self, symbol: str, server_ts: float) -> float | None:
        """Master mid price at ``server_ts`` (broker server clock).

        Best-effort point-in-time sampler: ticks first (millisecond alignment),
        the nearest M1 close as the fallback. Answers "roughly what was it
        then", which is what the diagnostics and the copier's own checks want.

        NOT used by the real-price offset. That comparison needs a specific
        instant on both feeds or nothing — see `bar_close_at`. A sampler that
        always answers cannot report "no data for that minute", and the offset
        then quotes market drift as if it were a venue difference.

        Returns None when the terminal has no history at that point (symbol not
        in Market Watch, market closed then, history not downloaded yet).
        """
        if not symbol or not server_ts:
            return None

        from datetime import datetime, timezone

        target = float(server_ts)
        target_ms = int(target * 1000)

        # --- Preferred: the last tick at or before the target instant.
        try:
            frm = datetime.fromtimestamp(int(target) - 30, tz=timezone.utc)
            ticks = mt5.copy_ticks_from(symbol, frm, 5000, mt5.COPY_TICKS_INFO)
        except Exception as e:
            ticks = None
            self.logger.debug("price_at(%s) tick read failed: %s", symbol, e)

        if ticks is not None and len(ticks) > 0:
            best = None
            for t in ticks:
                if int(t["time_msc"]) <= target_ms:
                    best = t
                else:
                    break  # ticks come ordered; past the target, stop
            if best is not None:
                bid, ask = float(best["bid"]), float(best["ask"])
                if bid and ask:
                    return (bid + ask) / 2.0
                if bid or ask:
                    return bid or ask

        # --- Fallback: M1 bar closing nearest the target. Coarser, but there
        # whenever tick history is not.
        best = self._m1_bar_at(symbol, target)
        return None if best is None else float(best["close"])

    def get_current_price(self, symbol: str) -> tuple[float, float]:
        """Get current bid/ask price."""
        tracer = get_tracer()
        with tracer.span("mt5_get_price") as span:
            span.set_tag("symbol", symbol)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                span.set_tag("result", "no_tick")
                return 0.0, 0.0
            span.set_tags(bid=tick.bid, ask=tick.ask, result="ok")
            return tick.bid, tick.ask

    def open_position(
        self,
        symbol: str,
        order_type: int,
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
        deviation: int = 0,
        magic: int = 0,
        comment: str = "",
        filling_mode: int | None = None,
    ) -> int | None:
        """
        Open a new position.

        Returns ticket number on success, None on failure.
        """
        tracer = get_tracer()
        with tracer.span("mt5_open_position") as span:
            side = "BUY" if order_type == OrderType.BUY else "SELL"
            span.set_tags(symbol=symbol, side=side, volume=volume, sl=sl, tp=tp)

            # Get current price
            bid, ask = self.get_current_price(symbol)
            if bid == 0:
                self.logger.error(f"Cannot get price for {symbol}")
                span.set_tag("result", "no_price")
                return None

            price = ask if order_type == OrderType.BUY else bid
            span.set_tag("entry_price", price)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "magic": magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode if filling_mode is not None else mt5.ORDER_FILLING_IOC,
            }
            if deviation:
                request["deviation"] = deviation

            for attempt in range(self.retry_attempts):
                span.set_tag("attempt", attempt + 1)

                with tracer.span("mt5_order_send") as send_span:
                    result = mt5.order_send(request)
                    if result is None:
                        error = mt5.last_error()
                        self.logger.error(f"Order send failed: {error}")
                        send_span.set_tags(result="error", error=str(error))
                        continue

                    send_span.set_tag("retcode", result.retcode)

                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        self.logger.info(
                            f"Opened {symbol} {side} "
                            f"{volume} @ {result.price}, ticket={result.order}"
                        )
                        span.set_tags(
                            result="success",
                            ticket=result.order,
                            fill_price=result.price
                        )
                        send_span.set_tags(result="success", ticket=result.order)
                        return result.order

                    # Handle requote
                    if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
                        self.logger.warning(f"Requote, retrying...")
                        send_span.set_tag("result", "requote")
                        # Update price
                        bid, ask = self.get_current_price(symbol)
                        request["price"] = ask if order_type == OrderType.BUY else bid
                        continue

                    self.logger.error(
                        f"Order failed: {result.comment} (retcode={result.retcode})"
                    )
                    send_span.set_tags(result="failed", error=result.comment)
                    break

            span.set_tag("result", "failed")
            return None

    def close_position(self, ticket: int, deviation: int = 0, filling_mode: int | None = None) -> bool:
        """Close position by ticket."""
        tracer = get_tracer()
        with tracer.span("mt5_close_position") as span:
            span.set_tag("ticket", ticket)

            position = self.get_position_by_ticket(ticket)
            if position is None:
                self.logger.warning(f"Position {ticket} not found")
                span.set_tag("result", "not_found")
                return False

            span.set_tags(symbol=position.symbol, volume=position.volume)

            # Opposite order type
            close_type = OrderType.SELL if position.is_buy else OrderType.BUY

            # Get fresh tick price instead of stale position.price_current
            bid, ask = self.get_current_price(position.symbol)
            if bid == 0:
                self.logger.error(f"Cannot get close price for {position.symbol}")
                span.set_tag("result", "no_price")
                return False
            price = bid if position.is_buy else ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": ticket,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": close_type,
                "price": price,
                "magic": 0,
                "comment": "Close copied",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode if filling_mode is not None else mt5.ORDER_FILLING_IOC,
            }
            if deviation:
                request["deviation"] = deviation

            for attempt in range(self.retry_attempts):
                span.set_tag("attempt", attempt + 1)
                result = mt5.order_send(request)
                if result is None:
                    continue

                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.info(f"Closed position {ticket}")
                    span.set_tags(result="success", close_price=result.price)
                    return True

                # Handle requote
                if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
                    bid, ask = self.get_current_price(position.symbol)
                    request["price"] = bid if position.is_buy else ask
                    continue

                self.logger.error(
                f"Close failed: {result.comment} (retcode={result.retcode})"
            )

        return False

    def modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        """Modify position SL/TP."""
        position = self.get_position_by_ticket(ticket)
        if position is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": position.symbol,
            "sl": sl,
            "tp": tp,
        }

        result = mt5.order_send(request)
        if result is None:
            return False

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.logger.debug(f"Modified position {ticket}: SL={sl}, TP={tp}")
            return True

        self.logger.error(
            f"Modify failed: {result.comment} (retcode={result.retcode})"
        )
        return False
