"""Event system for UI notifications.

ARCHITECTURE:
- Copy Path: Direct execution, no queues, maximum speed
- UI Path: Fire-and-forget events, separate queue, drops if slow

The copier emits events as a SIDE EFFECT after operations complete.
Events NEVER block or slow down the copy path.
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

_logger = logging.getLogger(__name__)


class EventType(Enum):
    """Event types."""
    TRADE_OPENED = auto()
    TRADE_CLOSED = auto()
    TRADE_MODIFIED = auto()  # SL/TP moved on a copied position
    TRADE_REDUCED = auto()   # master scaled out — slave partially closed
    PROFIT_UPDATE = auto()
    ERROR = auto()
    SLAVE_DISCONNECTED = auto()
    EA_UNAVAILABLE = auto()  # EA push port in use — user must Continue/Close
    EA_STATUS = auto()       # EA (TCP) connection up/down (edge-triggered)
    PRICE_DIFF = auto()      # TradingView ↔ master price offset (real price calculator)
    NEWS_BLOCK = auto()      # a copy was skipped inside a news window
    NEWS_UPDATE = auto()     # today's armed calendar, for the main-tab table
    DAILY_STOP_LIMIT = auto()  # 4 losing closes today — no new copies, UI frozen
    MASTER_STATUS = auto()   # master terminal reachable / Algo Trading (edge)


@dataclass(frozen=True)
class TradeEvent:
    """Immutable trade event for UI."""
    event_type: EventType
    trade_id: str = ""
    master_ticket: int = 0
    slave_name: str = ""
    slave_ticket: int = 0
    symbol: str = ""
    order_type: int = 0
    volume: float = 0.0
    master_volume: float = 0.0  # Master volume (for open toast: master→slave)
    open_price: float = 0.0
    open_time: float = 0.0
    profit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    error: str = ""
    symbol_mapped: str = ""  # Slave symbol after mapping (for error toast)
    reduced_volume: float = 0.0  # lots closed on a partial scale-out (TRADE_REDUCED)
    transport: str = ""      # "TCP" (EA push) or "Polling" — how the signal arrived
    delay_ms: float = 0.0    # copy latency: master event -> slave action, ms (total)
    # Latency waterfall legs (sum ≈ delay_ms):
    local_ms: float = 0.0    # our processing: dispatch + connect + calc
    ping_ms: float = 0.0     # transport: master fill -> signal ingested
    broker_ms: float = 0.0   # broker: order send -> fill
    ea_connected: bool = False  # EA_STATUS payload
    # PRICE_DIFF payload (real price calculator). `symbol` carries the master
    # symbol; `error` carries the scanner failure text for the strip's tooltip.
    tv_ticker: str = ""
    tv_price: float = 0.0
    master_price: float = 0.0    # master price at the TV quote's own moment
    master_bid: float = 0.0
    master_ask: float = 0.0
    price_diff: float = 0.0      # master_price - tv_price, median-smoothed
    price_diff_raw: float = 0.0  # this cycle's unsmoothed measurement
    price_diff_spread: float = 0.0  # max-min across the smoothing window
    price_diff_samples: int = 0
    # How the master side was sampled: "tick_mid" (live TV feed) or "m1_close"
    # (delayed feed — TV's value is already an interval aggregate).
    price_basis: str = ""
    spread_now: float = 0.0      # broker ask-bid, reported never applied
    digits: int = 5
    delay_s: float = 0.0         # TradingView feed delay, seconds
    # The shared instant and what each side answered for it — see PriceDiff.
    # Two ages, so a clock shift is a disagreement between numbers rather than
    # an offset that merely looks wrong.
    common_at: float = 0.0       # the instant both prices are for (host clock)
    lookback_s: float = 0.0
    tv_age_s: float = 0.0
    tv_published_age_s: float = 0.0
    master_age_s: float = 0.0
    tv_missing: bool = False
    tv_warming: bool = False     # missing only because this run just started
    master_missing: bool = False
    master_stale: bool = False   # master market shut at that instant
    master_quote_age_s: float = 0.0
    quote_age_s: float = 0.0     # age of the offset (>0 means cached)
    stale: bool = False
    description: str = ""        # TradingView's name for the ticker
    # News payload. NEWS_BLOCK uses `error` for the reason and `symbol` for the
    # master symbol that was not copied; NEWS_UPDATE carries the table rows as
    # tuples so this dataclass stays frozen and hashable.
    news_rows: tuple = ()
    news_window_s: float = 0.0   # the blocking window, in SECONDS
    news_stale: bool = False
    # Whether news blocking is switched on, and whether any slave actually arms
    # a window. Both are needed separately from `news_rows`: a quiet day, a
    # weekend, or a calendar whose impacts nobody arms all produce zero rows
    # while the feature is on and working — reporting that as "off" was wrong.
    news_enabled: bool = False
    news_armed: bool = False
    # DAILY_STOP_LIMIT payload. `error` carries the sentence shown to the user;
    # these carry the numbers behind it so the GUI does not have to parse prose.
    stop_count: int = 0
    stop_limit: int = 0
    stop_details: tuple = ()      # ("14:03 NAS100 -122.50", ...)
    open_positions: int = 0       # slave positions still open at shutdown
    # MASTER_STATUS payload. The master terminal as the SERVICE sees it —
    # measured, not inferred from whether the service is running.
    master_connected: bool = False
    master_algo: bool | None = None   # None = could not ask (not the same as off)
    timestamp: float = field(default_factory=time.time)


class UIEventQueue:
    """
    Dedicated queue for UI events.

    - Non-blocking puts (drops if full)
    - UI polls at its own pace
    - Never affects copy performance
    """

    _instance: "UIEventQueue | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "UIEventQueue":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, maxsize: int = 1000):
        if self._initialized:
            return
        self._initialized = True
        self._queue: queue.Queue[TradeEvent] = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._total_emitted = 0
        self._callbacks: list[Callable[[TradeEvent], None]] = []
        self._counter_lock = threading.Lock()

    def emit(self, event: TradeEvent) -> None:
        """
        Emit event to UI queue. NEVER BLOCKS.
        Drops event if queue is full - UI can catch up later.
        """
        with self._counter_lock:
            self._total_emitted += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._counter_lock:
                self._dropped += 1

        # Snapshot callbacks under lock, then call outside
        with self._counter_lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                _logger.debug("UIEventQueue callback error", exc_info=True)

    def poll(self, timeout: float = 0.05) -> TradeEvent | None:
        """Poll for next event. Returns None if empty."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def poll_all(self) -> list[TradeEvent]:
        """Poll all available events (non-blocking)."""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def register_callback(self, callback: Callable[[TradeEvent], None]) -> None:
        """Register a callback for immediate notification (must be fast!)."""
        with self._counter_lock:
            self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[TradeEvent], None]) -> None:
        """Unregister a callback."""
        with self._counter_lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    @property
    def dropped_count(self) -> int:
        with self._counter_lock:
            return self._dropped

    @property
    def total_emitted(self) -> int:
        with self._counter_lock:
            return self._total_emitted

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        with cls._lock:
            cls._instance = None


# Global access
def get_ui_queue() -> UIEventQueue:
    """Get the UI event queue."""
    return UIEventQueue()


# Convenience emit functions - fire and forget, never block
def emit_trade_opened(
    trade_id: str,
    master_ticket: int,
    slave_name: str,
    slave_ticket: int,
    symbol: str,
    order_type: int,
    volume: float,
    open_price: float,
    open_time: float,
    sl: float = 0.0,
    tp: float = 0.0,
    master_volume: float = 0.0,
    transport: str = "",
    delay_ms: float = 0.0,
    local_ms: float = 0.0,
    ping_ms: float = 0.0,
    broker_ms: float = 0.0,
) -> None:
    """Emit trade opened event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.TRADE_OPENED,
        trade_id=trade_id,
        master_ticket=master_ticket,
        slave_name=slave_name,
        slave_ticket=slave_ticket,
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        master_volume=master_volume,
        open_price=open_price,
        open_time=open_time,
        sl=sl,
        tp=tp,
        transport=transport,
        delay_ms=delay_ms,
        local_ms=local_ms,
        ping_ms=ping_ms,
        broker_ms=broker_ms,
    ))


def emit_trade_closed(
    trade_id: str,
    master_ticket: int,
    slave_name: str,
    slave_ticket: int,
    profit: float = 0.0,
    symbol: str = "",
    order_type: int = 0,
    volume: float = 0.0,
    transport: str = "",
    delay_ms: float = 0.0,
    local_ms: float = 0.0,
    ping_ms: float = 0.0,
    broker_ms: float = 0.0,
) -> None:
    """Emit trade closed event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.TRADE_CLOSED,
        trade_id=trade_id,
        master_ticket=master_ticket,
        slave_name=slave_name,
        slave_ticket=slave_ticket,
        profit=profit,
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        transport=transport,
        delay_ms=delay_ms,
        local_ms=local_ms,
        ping_ms=ping_ms,
        broker_ms=broker_ms,
    ))


def emit_trade_modified(
    trade_id: str,
    master_ticket: int,
    slave_name: str,
    slave_ticket: int,
    symbol: str,
    order_type: int = 0,
    sl: float = 0.0,
    tp: float = 0.0,
    transport: str = "",
    delay_ms: float = 0.0,
) -> None:
    """Emit SL/TP-moved event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.TRADE_MODIFIED,
        trade_id=trade_id,
        master_ticket=master_ticket,
        slave_name=slave_name,
        slave_ticket=slave_ticket,
        symbol=symbol,
        order_type=order_type,
        sl=sl,
        tp=tp,
        transport=transport,
        delay_ms=delay_ms,
    ))


def emit_trade_reduced(
    trade_id: str,
    master_ticket: int,
    slave_name: str,
    slave_ticket: int,
    symbol: str,
    order_type: int = 0,
    volume: float = 0.0,
    reduced_volume: float = 0.0,
    transport: str = "",
    delay_ms: float = 0.0,
) -> None:
    """Emit partial-close (scale-out) event (fire-and-forget).

    ``volume`` is the slave's remaining volume; ``reduced_volume`` is the amount
    just closed.
    """
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.TRADE_REDUCED,
        trade_id=trade_id,
        master_ticket=master_ticket,
        slave_name=slave_name,
        slave_ticket=slave_ticket,
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        reduced_volume=reduced_volume,
        transport=transport,
        delay_ms=delay_ms,
    ))


def emit_profit_update(
    trade_id: str,
    slave_name: str,
    slave_ticket: int,
    profit: float,
) -> None:
    """Emit profit update event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.PROFIT_UPDATE,
        trade_id=trade_id,
        slave_name=slave_name,
        slave_ticket=slave_ticket,
        profit=profit,
    ))


def emit_price_diff(diff) -> None:
    """Emit a resolved TV↔master offset (fire-and-forget).

    Takes a ``price_calc.PriceDiff``. Emitted every refresh cycle — including
    failed ones, where it carries the cached offset plus ``stale``/``error`` so
    the strip can gray itself and explain why in its tooltip.
    """
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.PRICE_DIFF,
        symbol=diff.master_symbol,
        tv_ticker=diff.tv_ticker,
        tv_price=diff.tv_price,
        master_price=diff.master_price,
        master_bid=diff.master_bid,
        master_ask=diff.master_ask,
        price_diff=diff.diff,
        price_diff_raw=diff.raw_diff,
        price_diff_spread=diff.spread,
        price_diff_samples=diff.samples,
        price_basis=diff.basis,
        spread_now=diff.spread_now,
        digits=diff.digits,
        delay_s=diff.delay_s,
        common_at=diff.common_at,
        lookback_s=diff.lookback_s,
        tv_age_s=diff.tv_age_s,
        tv_published_age_s=diff.tv_published_age_s,
        master_age_s=diff.master_age_s,
        tv_missing=diff.tv_missing,
        tv_warming=diff.tv_warming,
        master_missing=diff.master_missing,
        master_stale=diff.master_stale,
        master_quote_age_s=diff.master_quote_age_s,
        quote_age_s=diff.quote_age_s,
        stale=diff.stale,
        description=diff.description,
        error=diff.error,
    ))


def emit_error(
    trade_id: str,
    slave_name: str,
    error: str,
    master_ticket: int = 0,
    symbol: str = "",
    symbol_mapped: str = "",
) -> None:
    """Emit error event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.ERROR,
        trade_id=trade_id,
        slave_name=slave_name,
        master_ticket=master_ticket,
        error=error,
        symbol=symbol,
        symbol_mapped=symbol_mapped,
    ))


def emit_news_block(
    slave_name: str,
    symbol: str,
    reason: str,
    window_s: float = 0.0,
    master_ticket: int = 0,
) -> None:
    """A new copy was skipped because a news window is open on this slave."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.NEWS_BLOCK,
        slave_name=slave_name,
        symbol=symbol,
        error=reason,
        news_window_s=window_s,
        master_ticket=master_ticket,
    ))


def emit_news_update(
    rows, stale: bool = False, enabled: bool = True, armed: bool = True
) -> None:
    """Today's armed calendar rows, plus whether blocking is on at all.

    ``rows`` is an iterable of ``(ts, impact, currency, title, minutes)``.

    ``enabled``/``armed`` are carried even when ``rows`` is empty — an empty
    table is the normal state on a quiet day and says nothing about whether the
    feature is working.
    """
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.NEWS_UPDATE,
        news_rows=tuple(tuple(r) for r in rows),
        news_stale=stale,
        news_enabled=enabled,
        news_armed=armed,
    ))


def emit_slave_disconnected(slave_name: str, error: str = "") -> None:
    """Emit slave disconnected event (fire-and-forget)."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.SLAVE_DISCONNECTED,
        slave_name=slave_name,
        error=error,
    ))


def emit_ea_status(connected: bool) -> None:
    """EA (TCP) link up/down. Emit only on edge (change) to avoid spam."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.EA_STATUS,
        ea_connected=connected,
        transport="TCP",
    ))


def emit_ea_unavailable(port: int, detail: str = "") -> None:
    """EA push port is taken — copies run on polling fallback. The GUI shows a
    Continue/Close prompt so the user can decide to proceed or stop."""
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.EA_UNAVAILABLE,
        error=(f"EA push port {port} is already in use. "
               f"Fast copy (TCP) is unavailable — running on Polling fallback. {detail}").strip(),
    ))


def emit_daily_stop_limit(
    count: int,
    limit: int,
    details: "tuple[str, ...]" = (),
    open_positions: int = 0,
) -> None:
    """The day's loss limit is reached: no more copies today.

    Emitted once per trip (DailyStopGuard.announce_once decides). The GUI
    freezes every control that could start or resume copying and then raises
    the full-screen wall whose only button closes the app; the headless service
    logs it and refuses to start. Emitted on a launch that finds the day
    already spent, too — that is the same event and the same wall.
    """
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.DAILY_STOP_LIMIT,
        stop_count=count,
        stop_limit=limit,
        stop_details=tuple(details),
        open_positions=open_positions,
        error=(
            f"Daily limit reached: {count} losing trade(s) today "
            f"(limit {limit}). No further copies will be made."
        ),
    ))


def emit_master_status(connected: bool, algo: bool | None) -> None:
    """The master terminal as the service actually finds it.

    Edge-triggered (the caller only emits on change), because this is polled
    every cycle and nothing downstream benefits from being told twice.

    ``algo=None`` means the terminal could not be asked, which is NOT the same
    as Algo Trading being off: one is our blindness, the other is a switch the
    operator can fix.
    """
    get_ui_queue().emit(TradeEvent(
        event_type=EventType.MASTER_STATUS,
        master_connected=connected,
        master_algo=algo,
    ))
