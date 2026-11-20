"""Real price calculator — TradingView quote vs the master terminal's price.

WHY
    A TradingView chart and the master broker's feed disagree on the same
    instrument: different venue, different contract, different spread. The gap
    is nearly constant through a session, so once measured it converts prices
    between the two worlds ("TV 4700 = MT 4712.44").

THE DELAY PROBLEM
    Non-realtime (unpaid) TradingView symbols quote on a delay — 10 or 15
    minutes typically. Comparing a delayed TV price to the master's *current*
    price would fold that delay straight into the offset. So the delay is read
    from upstream (never assumed) and both sides are read at ONE instant.

    That instant is a whole minute, and one minute FURTHER back than the feed's
    delay: a 600s feed is read at 660s. "Now minus the delay" is unsettled on
    both sides at once — the master's M1 bar covering it is still open, and
    TradingView has not published its value for that minute yet. A margin makes
    the instant final on both sides, so the two readings are comparable rather
    than merely simultaneous.

    Neither side is allowed to substitute a nearby value. The master must have a
    bar closing exactly then; TradingView must have published a value for it
    (see TVSampleLog — the scanner reports a price but never its timestamp, so
    values are timestamped by when they FIRST appear). If either has nothing,
    the reading is reported missing and the UI shows it red. Every measurement
    carries the age each side answered with, so a clock shift is a number that
    disagrees rather than an offset that looks strange.

SOURCE
    ``POST https://scanner.tradingview.com/global/scan`` — the same
    unauthenticated endpoint the TradingView screener uses. It answers for
    forex, futures, equities and crypto in one call, and its ``update_mode``
    column carries the delay: ``"streaming"`` (live) or
    ``"delayed_streaming_600"`` (600 s behind). Undocumented, so every failure
    degrades to the cached offset rather than raising.

This module holds NO MetaTrader5 import — master prices arrive through injected
callables, so the maths is testable off-Windows.
"""
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable

from .config import PriceCalcPair
from .utils.logging import get_logger
from .utils.tracer import get_tracer

SCAN_URL = "https://scanner.tradingview.com/global/scan"
SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/v3/"
SCAN_COLUMNS = ["close", "update_mode", "time", "pricescale", "description"]
_USER_AGENT = "Mozilla/5.0 (compatible; MetaCT)"

# --- THE COMMON INSTANT ---------------------------------------------------
#
# Both sides are read at ONE timestamp, and that timestamp is a whole minute in
# the past. Not "now minus the delay": that instant is unsettled on both sides
# at once — the master's current M1 bar is still open, and TradingView has not
# published its value for it yet.
#
# The margin is what makes the instant settled. A 600s feed is therefore read at
# 660s: the M1 bar that closed then is final, and TradingView has certainly
# published its own value for that minute.
SETTLE_MARGIN_S = 60.0

#: M1. Both sides are quantized to it — the master has nothing finer that is
#: final, and asking for a non-boundary instant means asking for a bar that
#: does not exist.
BAR_S = 60.0

# How long a TradingView value may be the newest one on record and still count
# as the value at the common instant.
#
# A delayed feed is a STEP function, not a series of points: between
# publications the price IS the last published value, so the value at any
# instant is the last publication at or before it — an exact answer, not an
# approximation. This limit is only about a feed that has STOPPED: after a
# session closes or a connection dies, the last value stays "current" forever,
# and pairing it with a live master bar would report the market drifting away
# from a frozen number as a venue offset.
#
# Measured on CME_MINI:NQ1! (traces 2026-08-27): republishes every 30.1s median
# during the session, up to 141s in a quiet stretch. Five minutes is well clear
# of a quiet market and nowhere near a closed one.
TV_STALE_AFTER_S = 300.0


def common_instant(now: float, delay_s: float) -> float:
    """The settled instant both sides are read at, as a whole-minute boundary.

    Whole minutes because that is the finest thing the master has that is
    final, and because a bar is addressed by its boundary — ask for
    12:34:17 and there is no bar to return.
    """
    return math.floor((now - delay_s - SETTLE_MARGIN_S) / BAR_S) * BAR_S


# Providers injected by the service (see main.py). Kept as plain callables so
# tests can drive the whole calculator without MT5.
PriceAtFn = Callable[[str, float], float | None]      # (symbol, server_ts) -> mid
CurrentPriceFn = Callable[[str], tuple[float, float]]  # (symbol) -> (bid, ask)
ServerOffsetFn = Callable[[str], float | None]         # (symbol) -> server_now - utc_now
QuoteAgeFn = Callable[[str], float | None]             # (symbol) -> tick age, seconds
# (symbol, server_close_ts) -> (open, high, low, close[, bar_time]) of the M1
# candle that CLOSED at that broker-clock instant, or None. Exact: see
# MT5Client.bar_close_at. The 5th element, when present, is the bar's OPEN, and
# the age is taken from it rather than from what was asked for.
BarAtFn = Callable[[str, float], tuple[float, ...] | None]


@dataclass(frozen=True)
class TVQuote:
    """One row of the scanner response."""
    ticker: str
    price: float
    delay_s: float           # 0 for realtime feeds
    bar_time: float          # exchange bar timestamp reported by TV (epoch s)
    description: str = ""
    pricescale: int = 0
    # Host clock when the scanner answered. Pinned at fetch, not read off
    # ``time.time()`` on every access: the sampling instant and the instant the
    # trace records must be the same number, or the record cannot be used to
    # check the sampling.
    fetched_at: float = 0.0

    @property
    def quote_time(self) -> float:
        """Host-clock epoch seconds at which this price was the true price.

        Only an UPPER bound, and that is why it is not what the measurement
        uses. TradingView republishes a delayed feed every ~30s, so a value
        re-read between publications was already true up to 30s before this
        scan — see TVSampleLog, which timestamps a value by when it FIRST
        appeared instead.
        """
        return (self.fetched_at or time.time()) - self.delay_s


class TVSampleLog:
    """When each TradingView value FIRST appeared, per ticker.

    WHY
        The scanner answers with a value, never with the instant it was true.
        Taking ``scan_time - delay`` treats a value that has been sitting there
        for 25 seconds as if it had just arrived, and TradingView republishes a
        delayed feed only every ~30s. Measured on CME_MINI:NQ1! that put the
        TV side 30s newer than it really was, and the offset carried whatever
        the market did in those 30s (traces 2026-08-27: best-fit lag 630s
        against a stated delay of 600s).

        A value's first appearance is the tightest bound available without
        paying TradingView for a real-time feed: seen first at ``t0``, it was
        true at ``t0 - delay``, give or take one poll interval.

    So the log keeps one entry per CHANGE, and the measurement asks it for the
    value at a specific instant rather than for "the latest".
    """

    def __init__(self, window_s: float = 3600.0):
        self.window_s = float(window_s)
        # ticker -> deque of (effective_instant, price), oldest first.
        self._seen: dict[str, deque[tuple[float, float]]] = {}

    def observe(self, ticker: str, price: float, seen_at: float, delay_s: float) -> None:
        """Record ``price`` if it differs from the last one seen for ``ticker``."""
        if not ticker or not price:
            return
        log = self._seen.setdefault(ticker, deque())
        if log and log[-1][1] == price:
            return                      # same publication, already timestamped
        log.append((seen_at - delay_s, price))
        cutoff = seen_at - self.window_s
        while log and log[0][0] < cutoff - delay_s:
            log.popleft()

    def lookup(
        self, ticker: str, instant: float, stale_after_s: float
    ) -> tuple[tuple[float, float] | None, str]:
        """``(hit, reason)``. ``hit`` is ``(published_at, price)`` or None.

        ``reason`` names WHY there is no hit, because the two causes are not the
        same news and were being reported as one:

        - ``"warming"`` — every value on record is NEWER than the instant. The
          log has not been watching long enough to know what the feed said 11
          minutes ago, and nothing else does either. Measured on three restarts
          (traces 2026-08-27): 35-75s after start, then it resolves and stays
          resolved. Reporting that as "feed not publishing" was alarming and
          wrong — the feed was answering perfectly the whole time.
        - ``"stopped"`` — a value is in force but has been for longer than
          ``stale_after_s``. The feed really has gone quiet.
        - ``"unknown"`` — nothing for this ticker at all.
        """
        log = self._seen.get(ticker)
        if not log:
            return None, "unknown"
        best = None
        for published_at, price in log:
            if published_at <= instant:
                best = (published_at, price)
            else:
                break                       # appended in order
        if best is None:
            return None, "warming"
        if (instant - best[0]) > stale_after_s:
            return None, "stopped"
        return best, ""

    def at(
        self, ticker: str, instant: float, stale_after_s: float
    ) -> tuple[float, float] | None:
        """``(published_at, price)`` in force at ``instant``, or None.

        The LAST publication at or before ``instant`` — a delayed feed is a step
        function, so that is not an approximation, it is the value. An earlier
        version took the nearest entry within a tolerance and rejected anything
        further; on a quiet market, where TradingView went 141s between
        publications, that discarded 16% of otherwise perfect measurements
        (traces 2026-08-27) for no reason but the shape of the lookup.

        None in two cases, both of which the UI shows red:

        - nothing published at or before the instant. Cold start: the log has
          not been watching long enough to know what the feed said 11 minutes
          ago, and nothing else does either.
        - the value in force is older than ``stale_after_s``. The feed has
          stopped — market closed, connection gone — and its last price would
          otherwise stay "current" forever while the master moved on.
        """
        return self.lookup(ticker, instant, stale_after_s)[0]

    def forget(self, keep: set[str]) -> None:
        for ticker in list(self._seen):
            if ticker not in keep:
                del self._seen[ticker]


@dataclass
class PriceDiff:
    """A resolved TV↔master offset, ready for the UI."""
    tv_ticker: str
    master_symbol: str
    tv_price: float = 0.0        # TV price AT the common instant
    master_price: float = 0.0    # master price AT the common instant
    master_bid: float = 0.0      # master price right now (informational only)
    master_ask: float = 0.0
    # --- The common instant, and what each side answered for it.
    #
    # Two ages, not one, and each derived from the data actually used rather
    # than from what was asked for. They are equal by construction, so any
    # difference between them is a fault — a clock shift, a bar from the wrong
    # minute — visible as a number instead of inferred from a wild offset.
    common_at: float = 0.0        # host-clock instant both sides were read at
    lookback_s: float = 0.0       # now - common_at ("the delay in use")
    tv_age_s: float = 0.0         # age of the TV value used
    master_age_s: float = 0.0     # age of the master value used
    # How long ago TradingView last published anything for this ticker. Always
    # >= tv_age_s. A feed that has stopped shows up here as a growing number
    # while tv_age_s stays put, which is the difference between "quiet" and
    # "gone" — and nothing else distinguishes them.
    tv_published_age_s: float = 0.0
    tv_missing: bool = False      # TradingView had nothing for that instant
    # The master had not ticked since the instant being measured — its market
    # was already shut when that bar was supposedly forming. Distinct from
    # `master_missing` (no bar at all): here a bar exists and is a fiction.
    master_stale: bool = False
    # ...and whether that is simply the first minute of watching the feed, as
    # opposed to a feed that has stopped. Same absence of data, opposite news.
    tv_warming: bool = False
    master_missing: bool = False  # the master had no bar closing then
    diff: float = 0.0            # master_price - tv_price, SMOOTHED (what to use)
    raw_diff: float = 0.0        # this cycle's unsmoothed measurement
    spread: float = 0.0          # max-min across the smoothing window
    samples: int = 0             # measurements behind `diff`
    # Which master series was read. Only ever "m1_close" — the M1 bar close, a
    # single price with no spread, matching what TradingView publishes — or ""
    # when no bar answered. Reported so a future second basis cannot be
    # introduced silently.
    basis: str = ""
    # Broker spread at read time, reported never applied. It is not part of the
    # measurement at all — both sides use a single price series — so this exists
    # only so the trader can see it and decide.
    spread_now: float = 0.0
    digits: int = 5
    delay_s: float = 0.0
    master_quote_age_s: float = 0.0   # age of the master's latest tick (bid/ask)
    quote_age_s: float = 0.0     # how old the offset is (0 = fresh this cycle)
    stale: bool = False          # True when diff came from cache, not a fresh scan
    description: str = ""
    error: str = ""


def _fmt_secs(seconds: float) -> str:
    """'45s' / '11m00s' — for error text that has to name a lookback."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def parse_delay_seconds(update_mode: str | None) -> float:
    """Seconds of quote delay encoded in TradingView's ``update_mode``.

    ``"streaming"`` -> 0.0, ``"delayed_streaming_600"`` -> 600.0,
    ``"end_of_day"`` / unknown -> 0.0 (treated as realtime; the resulting offset
    is then only as good as the feed, which the UI flags via ``delay_s``).
    """
    if not update_mode:
        return 0.0
    tail = str(update_mode).rsplit("_", 1)[-1]
    try:
        return float(int(tail))
    except ValueError:
        return 0.0


def _http_json(req: urllib.request.Request, timeout: float):
    """GET/POST returning parsed JSON, or raises. Shared plumbing."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def resolve_ticker(text: str, timeout: float = 5.0) -> tuple[str, str, str]:
    """Expand a bare ticker into TradingView's ``EXCHANGE:TICKER`` form.

    The scanner matches on the fully qualified name only: ``NQ1!`` returns
    nothing, ``CME_MINI:NQ1!`` returns the E-mini. Nobody remembers that
    ``NQ1!`` lives on ``CME_MINI`` while ``GC1!`` lives on ``COMEX``, so the
    prefix is looked up through the same symbol-search endpoint the website's
    search box uses.

    Returns ``(ticker, description, error)``. Text that already carries a colon
    is passed through untouched — an explicit choice always wins.
    """
    text = (text or "").strip()
    if not text:
        return "", "", ""
    if ":" in text:
        return text.upper(), "", ""

    query = urllib.parse.urlencode({
        "text": text, "hl": "0", "lang": "en", "domain": "production",
    })
    req = urllib.request.Request(
        SEARCH_URL + "?" + query,
        headers={
            "User-Agent": _USER_AGENT,
            # The endpoint is origin-checked; without these it answers 403.
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
        },
    )
    try:
        data = _http_json(req, timeout)
    except Exception as e:
        return text.upper(), "", f"Could not look up '{text}' on TradingView: {e}"

    # v3 wraps the list; v1 returns it bare. Accept both.
    rows = data.get("symbols", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []

    wanted = text.upper()
    for row in rows:
        description = str(row.get("description") or "")
        # Continuous-contract rows ("NQ1!") hang off the root symbol ("NQ"),
        # each carrying its own exchange prefix — check those first.
        for contract in row.get("contracts") or []:
            if str(contract.get("symbol") or "").upper() != wanted:
                continue
            prefix = contract.get("prefix") or row.get("source_id") or row.get("exchange")
            if prefix:
                return f"{prefix}:{wanted}", description, ""
        if str(row.get("symbol") or "").upper() == wanted:
            prefix = row.get("source_id") or row.get("exchange")
            if prefix:
                return f"{prefix}:{wanted}", description, ""

    return text.upper(), "", (
        f"'{text}' needs an exchange prefix (e.g. CME_MINI:NQ1!) — "
        f"TradingView search found no match for it"
    )


def fetch_tv_quotes(
    tickers: Iterable[str], timeout: float = 5.0
) -> tuple[dict[str, TVQuote], str]:
    """Fetch last prices for ``tickers`` in one scanner call.

    Returns ``(quotes_by_ticker, error)``. ``error`` is "" on success; on
    failure the dict is empty and the message is human-readable (it lands in a
    toast and the strip's tooltip). Never raises.
    """
    tickers = [t for t in tickers if t]
    if not tickers:
        return {}, ""

    payload = json.dumps({
        "symbols": {"tickers": tickers},
        "columns": SCAN_COLUMNS,
    }).encode("utf-8")
    req = urllib.request.Request(
        SCAN_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {}, f"TradingView scanner HTTP {e.code} ({e.reason})"
    except urllib.error.URLError as e:
        return {}, f"TradingView scanner unreachable: {e.reason}"
    except TimeoutError:
        return {}, f"TradingView scanner timed out after {timeout:.0f}s"
    except Exception as e:  # malformed response, socket reset, ...
        return {}, f"TradingView scanner error: {e}"

    try:
        data = json.loads(body)
        rows = data.get("data") or []
    except Exception:
        return {}, "TradingView scanner returned a non-JSON response"

    fetched_at = time.time()
    quotes: dict[str, TVQuote] = {}
    for row in rows:
        ticker = row.get("s") or ""
        cols = row.get("d") or []
        if not ticker or len(cols) < len(SCAN_COLUMNS):
            continue
        close, update_mode, bar_time, pricescale, description = cols[:5]
        if close is None:
            continue  # market never opened / no quote yet — not an error
        quotes[ticker] = TVQuote(
            ticker=ticker,
            price=float(close),
            delay_s=parse_delay_seconds(update_mode),
            bar_time=float(bar_time or 0.0),
            description=str(description or ""),
            pricescale=int(pricescale or 0),
            fetched_at=fetched_at,
        )

    missing = [t for t in tickers if t not in quotes]
    err = ""
    if missing and not quotes:
        err = f"TradingView knows none of: {', '.join(missing)}"
    elif missing:
        err = f"No TradingView quote for: {', '.join(missing)}"
    return quotes, err


class PriceCalculator:
    """Keeps the TV↔master offset per configured pair.

    ``refresh()`` is blocking (one HTTP call + a few MT5 reads) and is meant to
    be driven from the service's executor, never from the Qt thread.
    """

    def __init__(
        self,
        pairs: list[PriceCalcPair],
        refresh_sec: float = 5.0,
        fetch: Callable[..., tuple[dict[str, TVQuote], str]] = fetch_tv_quotes,
        resolve: Callable[[str], tuple[str, str, str]] = resolve_ticker,
        window: int = 7,
    ):
        self.pairs = list(pairs)
        # Each measurement pairs a TradingView last-trade with a master price
        # sampled at the same instant — both carry timing noise, and on an index
        # that noise is worth tens of points while the true offset barely moves.
        # The reported offset is therefore the MEDIAN of a short window: unlike a
        # mean, one bad sample cannot drag it at all.
        self.window = max(1, int(window))
        self._samples: dict[str, deque[float]] = {}
        # When each TradingView value first appeared — the TV side's own
        # history, so a measurement can ask it for a specific instant.
        self._tv_log = TVSampleLog()
        self.refresh_sec = max(1.0, float(refresh_sec))
        self._fetch = fetch
        self._resolve = resolve
        # Bare ticker -> qualified "EXCHANGE:TICKER". Looked up once per ticker
        # and kept for the process; a saved config predating this can carry a
        # bare ticker and must not re-query the search endpoint every 5s.
        self._qualified: dict[str, str] = {}
        self._resolve_errors: dict[str, str] = {}
        self.logger = get_logger()
        self._last_run = 0.0
        self._last_error = ""       # for edge-triggered toasts
        self._last_logged = ""      # same idea for the log file
        # The SCANNER's own failure this cycle, "" when it answered. Kept apart
        # from the per-pair `PriceDiff.error` on purpose: a per-pair message
        # names the instant it is about, so it is a different string every
        # minute and is useless as toast input. Only this one is offered to the
        # user as a popup — see main.py.
        self.last_scan_error = ""
        # Cached last-good offsets, seeded from config so a cold start with a
        # dead network still converts prices (flagged stale in the UI).
        self._cache: dict[str, tuple[float, float]] = {
            p.tv: (p.last_diff, p.last_diff_at)
            for p in pairs if p.last_diff_at
        }

    def set_pairs(self, pairs: list[PriceCalcPair], refresh_sec: float | None = None):
        """Adopt an edited pair list without dropping cached offsets."""
        self.pairs = list(pairs)
        if refresh_sec is not None:
            self.refresh_sec = max(1.0, float(refresh_sec))
        self._tv_log.forget({p.tv for p in self.pairs})

    def due(self, now: float | None = None) -> bool:
        """True when the next scan is owed."""
        now = time.time() if now is None else now
        return bool(self.pairs) and (now - self._last_run) >= self.refresh_sec

    def take_error_edge(self, error: str) -> str:
        """Return ``error`` only when it differs from the last reported one.

        Keeps a 5-second poll from firing the same toast forever: one toast when
        the scanner breaks, one when it recovers (empty string), nothing while
        it stays broken.
        """
        if error == self._last_error:
            return ""
        self._last_error = error
        return error

    def refresh(
        self,
        price_at: PriceAtFn,
        current_price: CurrentPriceFn,
        server_offset: ServerOffsetFn,
        digits_of: Callable[[str], int] | None = None,
        bar_at: BarAtFn | None = None,
        quote_age: QuoteAgeFn | None = None,
    ) -> list[PriceDiff]:
        """Scan TradingView and resolve the offset for every configured pair.

        The master price is read at the TV quote's own moment: the quote time is
        shifted into the broker's clock (MT5 timestamps are server-local, not
        UTC) before sampling. A pair that can't be resolved this cycle falls
        back to its cached offset and is marked stale.
        """
        self._last_run = time.time()
        qualified = {p.tv: self._qualify(p.tv) for p in self.pairs}
        quotes, error = self._fetch(sorted(set(qualified.values())))
        self.last_scan_error = error
        # Log on the edge only — a scanner that stays down would otherwise write
        # a warning every refresh_sec for as long as the service runs.
        if error != self._last_logged:
            if error:
                self.logger.warning("[PriceCalc] %s", error)
            elif self._last_logged:
                self.logger.info("[PriceCalc] TradingView scanner recovered")
            self._last_logged = error

        results: list[PriceDiff] = []
        for pair in self.pairs:
            tv = qualified[pair.tv]
            results.append(self._resolve_pair(
                pair, quotes.get(tv),
                self._resolve_errors.get(pair.tv) or error,
                price_at, current_price, server_offset, digits_of, bar_at,
                quote_age,
            ))
        return results

    def _smooth(self, tv: str, raw: float) -> float:
        """Median of the last ``window`` measurements for this pair."""
        window = self._samples.setdefault(tv, deque(maxlen=self.window))
        window.append(raw)
        return statistics.median(window)

    def _qualify(self, ticker: str) -> str:
        """Fully qualified form of ``ticker``, looked up once and remembered."""
        if ":" in ticker:
            return ticker
        if ticker in self._qualified:
            return self._qualified[ticker]

        resolved, description, err = self._resolve(ticker)
        self._qualified[ticker] = resolved
        self._resolve_errors[ticker] = err
        if err:
            self.logger.warning("[PriceCalc] %s", err)
        else:
            self.logger.info(
                "[PriceCalc] '%s' resolved to %s%s",
                ticker, resolved, f" ({description})" if description else "",
            )
        return resolved

    def _resolve_pair(
        self,
        pair: PriceCalcPair,
        quote: TVQuote | None,
        scan_error: str,
        price_at: PriceAtFn,
        current_price: CurrentPriceFn,
        server_offset: ServerOffsetFn,
        digits_of: Callable[[str], int] | None,
        bar_at: BarAtFn | None = None,
        quote_age: QuoteAgeFn | None = None,
    ) -> PriceDiff:
        out = PriceDiff(
            tv_ticker=pair.tv,
            master_symbol=pair.master,
            error=scan_error,
            description=quote.description if quote else "",
            delay_s=quote.delay_s if quote else 0.0,
            tv_price=quote.price if quote else 0.0,
        )

        # Hoisted out of the sampling block so the trace can report them on the
        # paths that never get that far.
        srv_offset = 0.0
        sampled_at = 0.0

        if digits_of is not None:
            try:
                out.digits = digits_of(pair.master) or out.digits
            except Exception:
                pass

        try:
            bid, ask = current_price(pair.master)
            out.master_bid, out.master_ask = bid, ask
            if bid and ask:
                out.spread_now = ask - bid
        except Exception as e:
            out.error = out.error or f"Master price for {pair.master}: {e}"

        if quote_age is not None:
            try:
                out.master_quote_age_s = quote_age(pair.master) or 0.0
            except Exception:
                pass

        resolved = False
        if quote is not None:
            now = quote.fetched_at or time.time()
            self._tv_log.observe(pair.tv, quote.price, now, quote.delay_s)

            # ONE instant, chosen before either side is read, so neither can
            # drag the comparison toward its own convenience.
            out.common_at = common_instant(now, quote.delay_s)
            out.lookback_s = max(0.0, time.time() - out.common_at)

            # --- TradingView side: its value FOR that instant, or nothing.
            tv_hit, why = self._tv_log.lookup(
                pair.tv, out.common_at, TV_STALE_AFTER_S
            )
            if tv_hit is None:
                out.tv_missing = True
                # Warming is not a fault: it is the first minute of watching a
                # delayed feed, and it clears on its own.
                out.tv_warming = why in ("warming", "unknown")
                out.tv_price = quote.price          # shown, but not measured with
            else:
                tv_published_at, out.tv_price = tv_hit
                # Aged from the INSTANT, not from the publication: the value is
                # the feed's price for that instant, so that is what the counter
                # is reporting the age of, and it is then directly comparable
                # with the master's. How long the feed itself has been silent is
                # a separate fact, reported separately.
                out.tv_age_s = max(0.0, time.time() - out.common_at)
                out.tv_published_age_s = max(0.0, time.time() - tv_published_at)

            # --- Master side: the bar that CLOSED at that same instant.
            at = None
            try:
                srv_offset = server_offset(pair.master) or 0.0
                # The broker's clock, not ours. MT5 bar stamps are server-local
                # seconds; the offset is measured live off a tick rather than
                # assumed from a timezone table, and it is a whole number of
                # minutes, so `bar_close_at` can round the jitter out of it.
                sampled_at = out.common_at + srv_offset
                at = self._sample_master(pair, sampled_at, out, bar_at, srv_offset)
            except Exception as e:
                out.error = out.error or f"Master history for {pair.master}: {e}"

            # A bar can exist and still be worthless. When the master's last
            # tick is OLDER than the instant we are measuring, its market had
            # already closed by then: MT5 keeps handing back the final bars of
            # the session, flat and unchanging, while TradingView's own feed
            # runs on. Measured at the 21:00 CME break (traces 2026-08-27):
            # 921 cycles produced 3 distinct master prices against 16 from TV,
            # and the offset ran to -129 against a true -64. Every bad reading
            # in that log came from this — sd 30.3 with a stale tick against
            # 4.3 with a fresh one.
            #
            # The comparison is against the LOOKBACK, not a fixed timeout, so
            # it needs no threshold to tune: a market that stopped after the
            # instant is fine (the bar is real), one that stopped before it is
            # not (the bar is a leftover).
            if at is not None and out.master_quote_age_s > out.lookback_s:
                out.master_stale = True
                at = None

            if at is None:
                out.master_missing = True
                out.master_age_s = 0.0      # nothing was read; don't age a gap

            if at is not None and not out.tv_missing:
                out.master_price = at
                out.raw_diff = at - out.tv_price
                out.diff = self._smooth(pair.tv, out.raw_diff)
                window = self._samples[pair.tv]
                out.samples = len(window)
                out.spread = max(window) - min(window)
                out.quote_age_s = 0.0
                self._cache[pair.tv] = (out.diff, time.time())
                pair.last_diff, pair.last_diff_at = out.diff, time.time()
                resolved = True
            elif at is not None:
                out.master_price = at       # readable, just not comparable yet

            if not resolved and not out.error:
                out.error = self._missing_message(pair, out)

        if not resolved:
            # Fresh resolution failed — fall back to the last known offset.
            cached = self._cache.get(pair.tv)
            if cached:
                out.diff, cached_at = cached
                out.quote_age_s = max(0.0, time.time() - cached_at)
                # Without a fresh TV price, show what the cached offset implies.
                if not out.tv_price and out.master_bid:
                    out.tv_price = out.master_bid - out.diff
            out.stale = True

        # Single exit, so every outcome is recorded — including the ones that
        # fail before a measurement exists.
        self._trace(pair, out, quote, srv_offset, sampled_at)
        return out

    @staticmethod
    def _missing_message(pair: PriceCalcPair, out: PriceDiff) -> str:
        """Name the side that had nothing, and the instant it had nothing for.

        The instant is in the message because "no data" is only actionable
        with it: a weekend, a symbol out of Market Watch and a clock that
        slipped all read the same without it.
        """
        when = time.strftime("%H:%M:%S", time.localtime(out.common_at))
        # Row + trace only; none of these is ever a toast (see main.py).
        if out.tv_warming:
            return (
                f"Watching {pair.tv} — the first reading needs the feed's own "
                f"value for {when}, and this run has not been listening that "
                f"long yet. Clears within a minute."
            )
        if out.master_stale:
            silent = time.strftime(
                "%H:%M:%S", time.localtime(time.time() - out.master_quote_age_s)
            )
            return (
                f"{pair.master} has not ticked since {silent} — its market is "
                f"shut, so there is nothing real to compare at {when}"
            )
        if out.tv_missing and out.master_missing:
            return (
                f"Neither TradingView nor {pair.master} has a price for "
                f"{when} ({_fmt_secs(out.lookback_s)} ago)"
            )
        if out.tv_missing:
            return (
                f"TradingView has published no {pair.tv} price since "
                f"{_fmt_secs(out.tv_published_age_s or out.lookback_s)} ago — "
                f"feed stopped"
            )
        return (
            f"{pair.master} has no M1 bar closing at {when} "
            f"({_fmt_secs(out.lookback_s)} ago)"
        )

    def _sample_master(
        self,
        pair: PriceCalcPair,
        sampled_at: float,
        out: PriceDiff,
        bar_at: BarAtFn | None,
        srv_offset: float = 0.0,
    ) -> float | None:
        """Master price for the bar CLOSING at ``sampled_at`` (broker clock).

        One price series on both sides or the difference is not a venue offset.
        TradingView publishes a single price — a last/close, no spread — so the
        master side is read the same way: the M1 bar's close, which on MT5 is a
        bid-series close and carries no spread either. Bid, ask and the mid
        between them are never part of the measurement.

        Nothing is substituted when that exact bar is absent. Returning the
        nearest bar instead would answer a different question with the same
        confidence, which is how a 10-minute-stale reading passed for a live
        one; ``None`` here becomes a red reading in the UI.

        The age is taken from the bar the terminal actually returned, converted
        back out of the broker's clock — not from what was asked for. That is
        what makes a server-clock shift show up as two counters disagreeing.
        """
        if bar_at is None:
            return None

        bar = bar_at(pair.master, sampled_at)
        if bar is None:
            return None

        out.basis = "m1_close"
        # An M1 close is true at the END of its minute; the 5th element is the
        # bar's OPEN, in broker seconds.
        if len(bar) > 4 and bar[4]:
            close_at = bar[4] + BAR_S - srv_offset
        else:
            close_at = sampled_at - srv_offset
        out.master_age_s = max(0.0, time.time() - close_at)
        return bar[3]                               # close

    def _trace(
        self,
        pair: PriceCalcPair,
        out: PriceDiff,
        quote: TVQuote | None,
        srv_offset: float,
        sampled_at: float,
    ) -> None:
        """Record one measurement in the trace stream.

        Without this the offset is unreconstructable after the fact: a wrong
        converted price could come from a stale TV quote, a bad server-clock
        shift, a missed history sample, or the median lagging a real move, and
        nothing on disk distinguished them.

        ``sampled_at`` is the broker-clock instant actually handed to
        ``price_at`` — the one value here that cannot be recovered from
        anywhere else, and the one that makes a clock-shift bug visible.
        """
        get_tracer().event(
            "price_calc",
            # One record per distinct measurement. The instant is quantized to
            # the minute while the cycle runs every ~5s, so without this the
            # same finding is restated a dozen times with a larger age counter
            # each time — 83% of the file, measured.
            dedupe_key=f"{pair.tv}->{pair.master}",
            tv=pair.tv,
            master=pair.master,
            tv_price=out.tv_price,
            master_at=out.master_price,
            master_bid=out.master_bid,
            master_ask=out.master_ask,
            raw=out.raw_diff,
            diff=out.diff,
            spread=out.spread,
            samples=out.samples,
            basis=out.basis,
            spread_now=out.spread_now,
            digits=out.digits,
            delay_s=out.delay_s,
            bar_time=quote.bar_time if quote else 0.0,
            quote_time=quote.quote_time if quote else 0.0,
            srv_offset=srv_offset,
            sampled_at=sampled_at,
            # The instant both sides were asked for, and the age each ANSWERED
            # with. They are equal by construction, so a non-zero gap is a
            # fault, not noise — and it is recorded whether or not anyone was
            # looking at the window when it happened.
            common_at=out.common_at,
            lookback_s=out.lookback_s,
            tv_age_s=out.tv_age_s,
            tv_published_age_s=out.tv_published_age_s,
            master_age_s=out.master_age_s,
            age_gap_s=out.master_age_s - out.tv_age_s,
            tv_missing=out.tv_missing,
            tv_warming=out.tv_warming,
            master_missing=out.master_missing,
            master_stale=out.master_stale,
            master_quote_age_s=out.master_quote_age_s,
            stale=out.stale,
            quote_age_s=out.quote_age_s,
            error=out.error,
        )

        # DEBUG, not INFO: one pair at the default 5s cadence is ~720 lines an
        # hour. The trace stream is the record; this is for a live tail.
        self.logger.debug(
            "[PriceCalc] %s→%s TV %.*f MT %.*f raw %+.*f diff %+.*f "
            "spr %.*f n%d delay %.0fs%s%s",
            pair.tv, pair.master,
            out.digits, out.tv_price,
            out.digits, out.master_price,
            out.digits, out.raw_diff,
            out.digits, out.diff,
            out.digits, out.spread,
            out.samples, out.delay_s,
            " STALE" if out.stale else "",
            f" err={out.error}" if out.error else "",
        )