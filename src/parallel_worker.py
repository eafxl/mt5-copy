"""Parallel worker for slave operations with high reliability.

Each slave runs in its own process with its own MT5 connection.
Designed for reliability with retries, timeouts, and graceful degradation.

Supports both sync and async interfaces:
- execute_tasks() - synchronous, blocks until all complete
- execute_tasks_async() - async, yields results as they complete
"""
import asyncio
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable
from concurrent.futures import ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from enum import Enum

import MetaTrader5 as mt5


worker_logger = logging.getLogger("metact.worker")

# Use shared calculation functions (avoid duplication with utils/pip_calculator)
from .utils.pip_calculator import (
    calculate_slave_volume as _calculate_slave_volume,
    calculate_slave_sl_tp as _calculate_slave_sl_tp,
    check_entry_deviation as _check_entry_deviation,
    calculate_risk_based_volume as _calculate_risk_based_volume,
    projected_sl_loss as _projected_sl_loss,
)
from .utils.risk_ladder import resolve_risk_pct as _resolve_risk_pct
from .utils.tracer import Tracer

# Worker-process tracer (lazy singleton, writes to same JSONL file as main process)
_worker_tracer: Tracer | None = None


def _get_worker_tracer() -> Tracer:
    """Get or create tracer for this worker process.

    Each worker process gets its own Tracer instance that appends to the
    same traces JSONL file. JSONL line appends < 4KB are atomic on POSIX,
    so concurrent writes from multiple worker processes are safe.
    """
    global _worker_tracer
    if _worker_tracer is None:
        _worker_tracer = Tracer()
    return _worker_tracer


#: Actions that repeat on a timer and say the same thing every time. Two
#: slaves polled once a second produced four INFO lines a second — 345,600 a
#: day — which is not a log, it is a denial of service against the person
#: reading it, and it buries the twenty lines that matter.
#:
#: Everything NOT in this set is a money-moving action (open, close, modify,
#: reduce) and is still logged individually, every time. A summary is only
#: honest for events whose individual identity carries no information.
POLLING_ACTIONS = frozenset({"fetch_positions", "terminal_state"})

#: How often the summary of those actions is written.
SUMMARY_INTERVAL_S = 20.0


class _PollSummary:
    """Counts repetitive successes; reports failures at once.

    THE RULE
        A failure is news the first time it happens, so it is logged
        immediately and in full. A success that looks exactly like the last
        thousand is not news at all, so it is counted and reported on an
        interval:

            [poll] 20.0s: fetch_positions 40 ok (avg 1.2ms, max 6ms) —
                          5ers_7k 20, Bybit 20

        The count IS the information: "40 ok" over twenty seconds tells you
        the poll is alive and how fast, in one line instead of forty.

    Failures also reset nothing — the summary still reports the successes
    around them, so a link that fails one poll in fifty is visible as exactly
    that rather than as an isolated error with no denominator.
    """

    def __init__(self, logger, interval_s: float = SUMMARY_INTERVAL_S,
                 clock=time.time):
        self._logger = logger
        self._interval_s = interval_s
        self._clock = clock
        self._since = clock()
        self._counts: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._by_slave: dict[str, int] = {}
        self._ms_total: dict[str, float] = {}
        self._ms_max: dict[str, float] = {}

    @property
    def pending(self) -> int:
        return sum(self._counts.values()) + sum(self._failures.values())

    def record(self, action: str, slave: str, success: bool, duration_ms: float):
        if success:
            self._counts[action] = self._counts.get(action, 0) + 1
            self._by_slave[slave] = self._by_slave.get(slave, 0) + 1
            self._ms_total[action] = self._ms_total.get(action, 0.0) + duration_ms
            self._ms_max[action] = max(self._ms_max.get(action, 0.0), duration_ms)
        else:
            self._failures[action] = self._failures.get(action, 0) + 1

    def flush_if_due(self, force: bool = False):
        elapsed = self._clock() - self._since
        if not force and elapsed < self._interval_s:
            return
        if self.pending:
            self._logger.info("[poll] %s", self._describe(elapsed))
        self._since = self._clock()
        self._counts.clear()
        self._failures.clear()
        self._by_slave.clear()
        self._ms_total.clear()
        self._ms_max.clear()

    def _describe(self, elapsed: float) -> str:
        parts = []
        for action in sorted(set(self._counts) | set(self._failures)):
            ok = self._counts.get(action, 0)
            bad = self._failures.get(action, 0)
            piece = f"{action} {ok} ok"
            if ok:
                piece += (
                    f" (avg {self._ms_total[action] / ok:.1f}ms,"
                    f" max {self._ms_max[action]:.0f}ms)"
                )
            if bad:
                piece += f", {bad} FAILED"
            parts.append(piece)
        slaves = ", ".join(
            f"{name} {n}" for name, n in sorted(self._by_slave.items())
        )
        return f"{elapsed:.1f}s: " + "; ".join(parts) + (f" — {slaves}" if slaves else "")


_worker_logging_ready = False


def _ensure_worker_logging():
    """Give this worker process a log file of its own. Idempotent.

    WHY IT WAS MISSING ANYTHING AT ALL
        Workers are separate processes: ``logging.getLogger`` there returns a
        fresh, handler-less logger, so everything ``worker_logger`` said — the
        OPEN_FILL line with the realized fill and risk skew, every connect
        retry, every task exception — went nowhere. The trace file recorded the
        spans, so timings survived; the prose did not, and the prose is where
        the broker's own words are.

    Workers append to ONE file per SESSION rather than one per process. A pool
    recycles processes, so per-process files would litter the directory with
    dozens of short logs; the shared file follows the same appending pattern
    the tracer already uses across processes, and the PID is in every line.
    The session stamp arrives through the environment, so a worker's log sits
    next to the run that spawned it. Retention sweeps these like any other log.
    """
    global _worker_logging_ready
    if _worker_logging_ready:
        return
    _worker_logging_ready = True          # set first: a failure must not retry
    try:
        from .paths import logs_dir
        from .utils.logging import (
            AsyncFileHandler, TraceContextFilter, session_stamp,
        )

        path = logs_dir() / f"worker_{session_stamp()}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = AsyncFileHandler(file_path=path)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(trace)s | pid%(process)d | %(message)s"
        ))
        handler.addFilter(TraceContextFilter())
        worker_logger.addHandler(handler)
        worker_logger.setLevel(logging.INFO)
        worker_logger.propagate = False   # the parent's handlers are not ours
    except Exception:
        pass                              # logging must never break a copy


def _send_order(request: dict, kind: str, attempt: int = 0):
    """``mt5.order_send`` with one trace record per ATTEMPT.

    WHY EVERY ATTEMPT AND NOT JUST THE OUTCOME
        This is the only moment the copier hands control to the broker, and it
        is where the time and the surprises are: a requote retried twice, a
        rejection with a retcode nobody can read from a log line that only says
        "failed". The enclosing span records what the operation ended up doing;
        this records what the broker was asked, and what it said, each time.

        The record carries no credentials — a trade request has none — but it
        does carry price, volume and ticket, which is the point.

    Exceptions are recorded and re-raised: the caller's retry logic is
    unchanged by being observed.
    """
    tracer = _get_worker_tracer()
    started = time.perf_counter()
    common = {
        "kind": kind,
        "attempt": attempt + 1,
        "symbol": request.get("symbol"),
        "volume": request.get("volume"),
        "price": request.get("price"),
        "sl": request.get("sl"),
        "tp": request.get("tp"),
        "position": request.get("position") or request.get("magic"),
    }
    try:
        result = mt5.order_send(request)
    except Exception as e:
        tracer.event(
            "broker_order_send", outcome="exception", error=str(e),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1), **common,
        )
        raise

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if result is None:
        tracer.event(
            "broker_order_send", outcome="none",
            error=str(mt5.last_error()), elapsed_ms=elapsed_ms, **common,
        )
        return None

    retcode = getattr(result, "retcode", None)
    tracer.event(
        "broker_order_send",
        outcome="done" if retcode == mt5.TRADE_RETCODE_DONE else "rejected",
        retcode=retcode,
        comment=getattr(result, "comment", ""),
        ticket=getattr(result, "order", 0),
        deal=getattr(result, "deal", 0),
        fill_price=getattr(result, "price", 0.0),
        elapsed_ms=elapsed_ms,
        **common,
    )
    return result


class TaskAction(str, Enum):
    """Task action types."""
    OPEN = "open"
    CLOSE = "close"
    REDUCE = "reduce"  # partial close (master scaled out / took partial profit)
    MODIFY = "modify"
    WARMUP = "warmup"
    FETCH_POSITIONS = "fetch_positions"
    TERMINAL_STATE = "terminal_state"


@dataclass
class SlaveTask:
    """Task to execute on a slave.

    For OPEN action, params should contain:
        - symbol: str - trading symbol (slave symbol after mapping)
        - order_type: int - 0=BUY, 1=SELL
        - master_volume: float - master position volume
        - master_leverage: int - master account leverage
        - multiplier: float - slave multiplier from config
        - adjust_by_leverage: bool - whether to adjust volume by leverage ratio
        - master_entry: float - master position entry price
        - master_current_price: float - master's current market price (for deviation check)
        - master_sl: float - master SL (0 if none)
        - master_tp: float - master TP (0 if none)
        - is_buy: bool - true for buy, false for sell
        - deviation: int - max slippage in points
        - magic: int - magic number (master ticket)
        - comment: str - order comment
        - max_entry_deviation_ratio: float - skip if price moved too far (0 to disable)
        - max_risk_pct_per_trade: float - THIS slave's risk guard: block if the
          SL-hit loss exceeds this % of its balance (0 to disable)
        - fixed_risk_usd: float - if > 0, size volume so SL loss = this amount (0 to disable)

    For CLOSE action, params should contain:
        - ticket: int - slave position ticket
        - deviation: int - max slippage in points

    For MODIFY action, params should contain:
        - ticket: int - slave position ticket
        - master_entry: float - master entry price (for distance calc)
        - master_sl: float - new master SL
        - master_tp: float - new master TP
        - is_buy: bool - position direction
    """
    slave_id: str  # Stable key: login@server
    slave_name: str
    terminal_path: str
    login: int
    password: str
    server: str
    action: TaskAction
    params: dict[str, Any]
    retry_attempts: int = 3
    retry_delay_ms: int = 1000
    timeout_ms: int = 60000  # bumped from 30s — mid-trade re-init on slow hosts (ARM64 Parallels) can need >30s
    trace_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlaveResult:
    """Result from a slave task."""
    slave_name: str
    success: bool
    action: str
    slave_id: str = ""  # Stable key: login@server
    ticket: int | None = None
    volume: float = 0.0
    error: str | None = None
    duration_ms: float = 0.0
    retries_used: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class MT5Connection:
    """Manages MT5 connection with reliability features."""

    def __init__(self, terminal_path: str, login: int, password: str, server: str):
        self.terminal_path = terminal_path
        self.login = login
        self.password = password
        self.server = server
        self._connected = False

    def connect(self, timeout_ms: int = 60000) -> tuple[bool, str | None]:
        """Establish connection to MT5 terminal and login."""
        # Shutdown any existing connection
        try:
            mt5.shutdown()
        except Exception:
            pass

        time.sleep(0.1)  # Brief pause for clean state

        # Initialize terminal in NON-portable mode: each slave terminal at a
        # unique exe path gets its own %APPDATA% data dir keyed by that path.
        # Matches manual-launch behavior (broker config + saved creds cached
        # in AppData). Portable mode previously bound terminals to a config/
        # folder copied from master, which carried the wrong broker entries
        # for slaves that used a different broker (e.g. Bybit) and caused
        # Company Wizard prompts + IPC timeouts.
        try:
            if not mt5.initialize(path=self.terminal_path, timeout=timeout_ms):
                error = mt5.last_error()
                return False, f"Initialize failed: {error}"
        except Exception as e:
            return False, f"Initialize exception: {e}"

        # Login to account
        try:
            if not mt5.login(self.login, self.password, server=self.server):
                error = mt5.last_error()
                mt5.shutdown()
                return False, f"Login failed: {error}"
        except Exception as e:
            mt5.shutdown()
            return False, f"Login exception: {e}"

        # Verify connection
        account_info = mt5.account_info()
        if account_info is None:
            mt5.shutdown()
            return False, "Could not verify account info"

        if account_info.login != self.login:
            mt5.shutdown()
            return False, f"Login mismatch: expected {self.login}, got {account_info.login}"

        self._connected = True
        return True, None

    def disconnect(self):
        """Safely disconnect from MT5."""
        try:
            mt5.shutdown()
        except Exception:
            pass
        self._connected = False

    def is_connected(self) -> bool:
        """Check if still connected."""
        if not self._connected:
            return False
        try:
            info = mt5.account_info()
            return info is not None and info.login == self.login
        except Exception:
            return False

    def ensure_symbol_ready(self, symbol: str) -> tuple[bool, str | None]:
        """Ensure symbol is available and visible."""
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                return False, f"Symbol '{symbol}' not found"

            if not info.visible:
                if not mt5.symbol_select(symbol, True):
                    return False, f"Could not enable symbol '{symbol}'"
                time.sleep(0.1)  # Wait for symbol to be ready

            # Verify we can get a tick
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return False, f"No tick data for '{symbol}'"

            return True, None
        except Exception as e:
            return False, f"Symbol check failed: {e}"


def _broker_loss_per_lot(
    symbol: str,
    order_type: int,
    entry_price: float,
    sl_price: float,
    symbol_info,
) -> float:
    """Money lost on 1.0 lot if price moves entry -> SL. Broker-accurate.

    Do NOT trust ``trade_tick_value`` for this — some brokers report it wrongly
    for metals/CFDs (XAUUSD comes back like an index at 0.01 instead of ~$1),
    which makes risk-based sizing ~100x too big and triggers "No money".

    Priority:
      1. mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl) — exact:
         handles contract size + deposit-currency conversion for any instrument.
      2. trade_contract_size * price distance (quote≈deposit currency).
      3. 0.0 -> caller falls back to the legacy tick_value estimate.
    """
    if sl_price <= 0 or entry_price <= 0:
        return 0.0
    # 1. Broker profit calc for exactly 1 lot (loss = abs of the P/L).
    try:
        profit = mt5.order_calc_profit(order_type, symbol, 1.0, entry_price, sl_price)
        if profit is not None and abs(profit) > 0:
            return abs(profit)
    except Exception as e:
        worker_logger.debug("LOSS_PER_LOT: order_calc_profit failed for %s: %s", symbol, e)
    # 2. Contract-size fallback.
    contract_size = getattr(symbol_info, "trade_contract_size", 0.0) or 0.0
    if contract_size > 0:
        return abs(entry_price - sl_price) * contract_size
    # 3. Give up -> legacy tick math in calculate_risk_based_volume.
    return 0.0


def _execute_open_with_retry(conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int, tracer=None) -> SlaveResult:
    """Execute open position with retry logic.

    Performs full calculation of volume and SL/TP in worker process.
    """
    symbol = params["symbol"]
    order_type = params["order_type"]
    master_volume = params["master_volume"]
    master_leverage = params["master_leverage"]
    multiplier = params["multiplier"]
    adjust_by_leverage = params["adjust_by_leverage"]
    master_entry = params["master_entry"]
    master_current_price = params.get("master_current_price", 0.0)
    master_sl = params.get("master_sl", 0.0)
    master_tp = params.get("master_tp", 0.0)
    master_time = params.get("master_time", 0)
    master_open_utc = params.get("master_open_utc", 0.0)  # TRUE UTC of master open (delay calc)
    master_open_ms = master_open_utc * 1000.0 if master_open_utc else 0.0
    recv_ms = params.get("recv_ms", 0.0)  # host epoch ms when signal was ingested
    signal_source = params.get("signal_source", "Polling")  # "TCP" | "Polling"
    is_buy = params["is_buy"]
    deviation = params.get("deviation", 0)
    magic = params.get("magic", 0)
    comment = params.get("comment", "")
    max_entry_deviation_ratio = params.get("max_entry_deviation_ratio", 0.0)
    max_risk_pct_per_trade = params.get("max_risk_pct_per_trade", 0.0)
    debug_skip_execution = params.get("debug_skip_execution", False)
    filling_mode = params.get("filling_mode", mt5.ORDER_FILLING_IOC)

    last_error = None
    retries_used = 0
    slave_entry = 0.0  # Updated when tick is received
    volume = 0.0  # Set during calc; surfaced in failure details for diagnostics
    calc_debug: dict[str, Any] = {}  # Sizing inputs, surfaced on failure

    # Idempotency: if this slave already has an open position whose `magic`
    # field matches the master ticket we're about to copy, do NOT open a
    # duplicate. Return the existing ticket as success. This guards against:
    #   - Retries that succeeded broker-side but didn't get the response
    #   - Self-copy loops when a slave shares the master's broker account
    #     (the slave's own freshly opened position would otherwise be seen
    #     as a "new master position" on the next poll → infinite reopen).
    # MT5's positions_get doesn't take a magic= filter, so we scan all.
    if magic:
        try:
            existing = mt5.positions_get()
        except Exception:
            existing = None
        if existing:
            for p in existing:
                if p.magic == magic:
                    worker_logger.info(
                        "OPEN idempotent: slave already has ticket %d with "
                        "magic=%d — skipping duplicate open",
                        p.ticket, magic,
                    )
                    return SlaveResult(
                        slave_name="",
                        success=True,
                        action=TaskAction.OPEN.value,
                        ticket=p.ticket,
                        volume=p.volume,
                        retries_used=0,
                        details={
                            "idempotent": True,
                            "existing_ticket": p.ticket,
                            "magic": magic,
                        },
                    )

    # Get slave account leverage (needed for volume calculation)
    account_info = mt5.account_info()
    if account_info is None:
        return SlaveResult(
            slave_name="",
            success=False,
            action=TaskAction.OPEN.value,
            error="Could not get account info for leverage",
            retries_used=0,
        )
    slave_leverage = account_info.leverage
    slave_balance = account_info.balance

    for attempt in range(retries):
        retries_used = attempt

        # Ensure symbol is ready
        ready, err = conn.ensure_symbol_ready(symbol)
        if not ready:
            last_error = err
            # Permanent failure: symbol doesn't exist on this broker
            if err and "not found" in err:
                break
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Get symbol info
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            last_error = f"Cannot get symbol info for {symbol}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            last_error = f"No tick for {symbol}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        price = tick.ask if order_type == 0 else tick.bid  # 0=BUY, 1=SELL
        slave_entry = price

        # Validate price
        if price <= 0:
            last_error = f"Invalid price: {price}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Check entry deviation - skip if master price moved too far from entry
        # Uses master's current price (not slave), so inter-broker spread is irrelevant
        should_skip, price_deviation, max_deviation = _check_entry_deviation(
            master_entry=master_entry,
            master_sl=master_sl,
            master_current_price=master_current_price,
            max_entry_deviation_ratio=max_entry_deviation_ratio,
            is_buy=is_buy,
        )
        if should_skip:
            return SlaveResult(
                slave_name="",
                success=False,
                action=TaskAction.OPEN.value,
                error=f"Entry deviation {price_deviation:.5f} > max {max_deviation:.5f}",
                retries_used=retries_used,
                details={
                    "master_entry": master_entry,
                    "master_current_price": master_current_price,
                    "slave_price": slave_entry,
                    "deviation": price_deviation,
                    "max_allowed": max_deviation,
                    "skipped_by_deviation": True,
                }
            )

        # Calculate volume: priority = pct_deposit > risk-based > fixed_volume > multiplier/leverage
        risk_mode = params.get("risk_mode", "")
        risk_pct = params.get("risk_pct", 0.0)
        risk_pct_base = params.get("risk_pct_base", "balance")
        fixed_risk_usd = params.get("fixed_risk_usd", 0.0)
        fixed_volume = params.get("fixed_volume", 0.0)
        volume = 0.0
        volume_method = ""
        risk_loss_per_lot = 0.0  # Broker-accurate loss on 1 lot (for calc_debug)
        ladder_reason = ""

        # 0. Dynamic % ladder — risk_pct comes from the account's drift away
        #    from its configured initial balance (prop scale-up aware).
        if risk_mode == "dynamic_pct":
            dynamic_base = params.get("dynamic_base", "balance")
            base_value = account_info.equity if dynamic_base == "equity" else account_info.balance
            ladder_pct, ladder_reason = _resolve_risk_pct(
                params.get("initial_balance", 0.0),
                base_value,
                params.get("risk_tiers") or [],
                0.0,  # no silent fallback: an unusable ladder must block, not guess
            )
            # Both failures below are "we cannot size this correctly". Falling
            # through to multiplier/fixed sizing would quietly trade a size the
            # operator never chose, so refuse instead.
            if ladder_pct <= 0:
                return SlaveResult(
                    slave_name="",
                    success=False,
                    action=TaskAction.OPEN.value,
                    error=f"Dynamic risk unusable ({ladder_reason}) — set an "
                          f"initial balance and risk tiers for this slave",
                    retries_used=retries_used,
                    details={"risk_mode": risk_mode, "base_value": base_value},
                )
            if master_sl <= 0:
                return SlaveResult(
                    slave_name="",
                    success=False,
                    action=TaskAction.OPEN.value,
                    error="Dynamic risk needs a master SL to size against",
                    retries_used=retries_used,
                    details={"risk_mode": risk_mode, "risk_pct": ladder_pct},
                )
            computed_risk_usd = base_value * ladder_pct / 100.0
            if is_buy:
                slave_sl_est = slave_entry - (master_entry - master_sl)
            else:
                slave_sl_est = slave_entry + (master_sl - master_entry)
            risk_loss_per_lot = _broker_loss_per_lot(
                symbol, order_type, slave_entry, slave_sl_est, symbol_info
            )
            volume = _calculate_risk_based_volume(
                risk_usd=computed_risk_usd,
                entry_price=slave_entry,
                sl_price=slave_sl_est,
                trade_tick_value=symbol_info.trade_tick_value,
                trade_tick_size=symbol_info.trade_tick_size,
                min_volume=symbol_info.volume_min,
                max_volume=symbol_info.volume_max,
                volume_step=symbol_info.volume_step,
                loss_per_lot=risk_loss_per_lot,
            )
            if volume <= 0:
                return SlaveResult(
                    slave_name="",
                    success=False,
                    action=TaskAction.OPEN.value,
                    error=f"Dynamic risk sized 0 lots (risk ${computed_risk_usd:.2f} "
                          f"below broker min lot for {symbol})",
                    retries_used=retries_used,
                    details={"risk_mode": risk_mode, "risk_pct": ladder_pct,
                             "risk_usd": computed_risk_usd},
                )
            volume_method = "dynamic_pct"
            risk_pct = ladder_pct       # for logging below
            fixed_risk_usd = computed_risk_usd
            worker_logger.info(
                "DYNAMIC RISK %s: %s (base %.2f %s) -> risk $%.2f",
                symbol, ladder_reason, base_value, dynamic_base, computed_risk_usd,
            )

        # 1. % of Deposit mode (dynamic risk_usd from account balance/equity)
        if risk_mode == "pct_deposit" and risk_pct > 0 and master_sl > 0:
            base_value = account_info.equity if risk_pct_base == "equity" else account_info.balance
            computed_risk_usd = base_value * risk_pct / 100.0
            # Estimate slave SL from master SL distance
            if is_buy:
                slave_sl_est = slave_entry - (master_entry - master_sl)
            else:
                slave_sl_est = slave_entry + (master_sl - master_entry)
            # Broker-accurate loss per lot (do NOT trust trade_tick_value — wrong
            # for XAUUSD/metals on some brokers, causing ~100x oversized lots).
            risk_loss_per_lot = _broker_loss_per_lot(
                symbol, order_type, slave_entry, slave_sl_est, symbol_info
            )
            volume = _calculate_risk_based_volume(
                risk_usd=computed_risk_usd,
                entry_price=slave_entry,
                sl_price=slave_sl_est,
                trade_tick_value=symbol_info.trade_tick_value,
                trade_tick_size=symbol_info.trade_tick_size,
                min_volume=symbol_info.volume_min,
                max_volume=symbol_info.volume_max,
                volume_step=symbol_info.volume_step,
                loss_per_lot=risk_loss_per_lot,
            )
            volume_method = "pct_deposit"
            fixed_risk_usd = computed_risk_usd  # for logging below

        # 2. Fixed USD risk-based (needs SL + slave tick data)
        if volume <= 0 and fixed_risk_usd > 0 and master_sl > 0:
            # Estimate slave SL from master SL distance
            if is_buy:
                slave_sl_est = slave_entry - (master_entry - master_sl)
            else:
                slave_sl_est = slave_entry + (master_sl - master_entry)
            risk_loss_per_lot = _broker_loss_per_lot(
                symbol, order_type, slave_entry, slave_sl_est, symbol_info
            )
            volume = _calculate_risk_based_volume(
                risk_usd=fixed_risk_usd,
                entry_price=slave_entry,
                sl_price=slave_sl_est,
                trade_tick_value=symbol_info.trade_tick_value,
                trade_tick_size=symbol_info.trade_tick_size,
                min_volume=symbol_info.volume_min,
                max_volume=symbol_info.volume_max,
                volume_step=symbol_info.volume_step,
                loss_per_lot=risk_loss_per_lot,
            )
            volume_method = "risk_usd"

        # 3. Fixed volume override
        if volume <= 0 and fixed_volume > 0:
            volume = max(symbol_info.volume_min, min(symbol_info.volume_max, fixed_volume))
            volume = round(round(volume / symbol_info.volume_step) * symbol_info.volume_step, 2)
            volume_method = "fixed"

        # 4. Multiplier/leverage calculation
        if volume <= 0:
            volume = _calculate_slave_volume(
                master_volume=master_volume,
                master_leverage=master_leverage,
                slave_leverage=slave_leverage,
                multiplier=multiplier,
                adjust_by_leverage=adjust_by_leverage,
                min_volume=symbol_info.volume_min,
                max_volume=symbol_info.volume_max,
                volume_step=symbol_info.volume_step,
            )
            volume_method = "multiplier"

        # Calculate SL/TP: apply the master's SL/TP DISTANCE to the slave's
        # current entry price. Always on the correct side (distance is positive),
        # so it can't produce a wrong-side / invalid stop across brokers.
        sl, tp = _calculate_slave_sl_tp(
            master_entry=master_entry,
            master_sl=master_sl,
            master_tp=master_tp,
            slave_entry=slave_entry,
            is_buy=is_buy,
            symbol_digits=symbol_info.digits,
        )

        worker_logger.info(
            "OPEN_CALC: symbol=%s method=%s risk_usd=%.2f slave_lev=%d "
            "vol_min=%.2f vol_max=%.2f vol_step=%.2f calculated_vol=%.4f "
            "sl=%.5f tp=%.5f master_entry=%.5f slave_entry=%.5f",
            symbol, volume_method, fixed_risk_usd, slave_leverage,
            symbol_info.volume_min, symbol_info.volume_max, symbol_info.volume_step,
            volume, sl, tp, master_entry, slave_entry,
        )

        # Capture the sizing inputs so a "No money" failure is fully
        # diagnosable from traces.json (worker RISK_VOLUME logs never reach a
        # file — workers have no log handler). These flow into the failure
        # SlaveResult.details and the worker_calculate trace tags.
        calc_debug = {
            "volume_method": volume_method,
            "risk_ladder": ladder_reason,  # "" unless dynamic_pct sized this
            "calc_risk_usd": round(fixed_risk_usd, 2),
            "loss_per_lot": round(risk_loss_per_lot, 4),
            "tick_value": symbol_info.trade_tick_value,
            "tick_size": symbol_info.trade_tick_size,
            "contract_size": getattr(symbol_info, "trade_contract_size", 0.0),
            "sl_distance": round(abs(slave_entry - sl), 5) if sl > 0 else 0.0,
            "vol_min": symbol_info.volume_min,
            "vol_max": symbol_info.volume_max,
            "vol_step": symbol_info.volume_step,
            "requested_volume": volume,
            "slave_sl": sl,
            "slave_tp": tp,
            # Broker stop constraints — an Invalid stops (10016) reject usually
            # means SL/TP is inside stops_level or on the wrong side of price.
            "stops_level": getattr(symbol_info, "trade_stops_level", 0),
            "freeze_level": getattr(symbol_info, "trade_freeze_level", 0),
            "master_time": master_time,
            # Origin of the signal (every trace shows it) + latency waterfall.
            "signal_source": signal_source,
            "recv_ms": round(recv_ms, 1),
            # master open -> signal ingested (broker fill + detect/push)
            "master_to_recv_ms": round(recv_ms - master_open_ms, 1) if (recv_ms and master_open_ms) else 0.0,
            # ingested -> here (dispatch + connect + calc)
            "recv_to_calc_ms": round(time.time() * 1000.0 - recv_ms, 1) if recv_ms else 0.0,
            "copy_delay_ms": round((time.time() - master_open_utc) * 1000, 1) if master_open_utc else 0.0,
        }

        # This slave's risk guard: block trades whose SL-hit loss exceeds
        # max_risk_pct_per_trade % of the slave's current balance. Checked
        # right before order_send. A missing SL means risk is unbounded, so
        # those trades are blocked too. This is a hard, non-retryable stop.
        if max_risk_pct_per_trade > 0:
            max_risk_usd = slave_balance * max_risk_pct_per_trade / 100.0
            if sl <= 0:
                worker_logger.warning(
                    "RISK_GUARD: blocked %s vol=%.2f — no SL set, risk unbounded "
                    "(max=%.2f%% of $%.2f)",
                    symbol, volume, max_risk_pct_per_trade, slave_balance,
                )
                return SlaveResult(
                    slave_name="",
                    success=False,
                    action=TaskAction.OPEN.value,
                    volume=volume,
                    retries_used=retries_used,
                    error=(
                        f"Risk guard: no SL set, cannot verify {max_risk_pct_per_trade:.2f}% "
                        f"cap — blocked (would open {volume:.2f} lots)"
                    ),
                    details={
                        "skipped_by_risk_guard": True,
                        "max_risk_usd": max_risk_usd,
                        "balance": slave_balance,
                        "master_entry": master_entry,
                        "slave_price": slave_entry,
                        **calc_debug,
                    },
                )

            projected_loss = _projected_sl_loss(
                volume=volume,
                entry_price=slave_entry,
                sl_price=sl,
                trade_tick_value=symbol_info.trade_tick_value,
                trade_tick_size=symbol_info.trade_tick_size,
            )
            if projected_loss > max_risk_usd:
                worker_logger.warning(
                    "RISK_GUARD: blocked %s vol=%.2f — projected loss $%.2f > "
                    "max $%.2f (%.2f%% of $%.2f)",
                    symbol, volume, projected_loss, max_risk_usd,
                    max_risk_pct_per_trade, slave_balance,
                )
                return SlaveResult(
                    slave_name="",
                    success=False,
                    action=TaskAction.OPEN.value,
                    volume=volume,
                    retries_used=retries_used,
                    error=(
                        f"Risk guard: projected loss ${projected_loss:.2f} > "
                        f"max ${max_risk_usd:.2f} ({max_risk_pct_per_trade:.2f}% of "
                        f"${slave_balance:.2f}) — blocked {volume:.2f} lots"
                    ),
                    details={
                        "skipped_by_risk_guard": True,
                        "projected_loss": projected_loss,
                        "max_risk_usd": max_risk_usd,
                        "balance": slave_balance,
                        "master_entry": master_entry,
                        "slave_price": slave_entry,
                        **calc_debug,
                    },
                )

        # Debug/idle mode: everything computed + logged above, but skip the
        # actual order_send so trades can be simulated with zero risk.
        if debug_skip_execution:
            worker_logger.info(
                "DEBUG_SKIP: would open %s %.2f lots sl=%.5f tp=%.5f "
                "(execution skipped — debug mode)",
                symbol, volume, sl, tp,
            )
            return SlaveResult(
                slave_name="",
                success=False,
                action=TaskAction.OPEN.value,
                volume=volume,
                retries_used=retries_used,
                details={
                    "simulated": True,
                    "slave_sl": sl,
                    "slave_tp": tp,
                    "master_entry": master_entry,
                    "slave_price": slave_entry,
                    **calc_debug,
                },
            )

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
            "type_filling": filling_mode,
        }
        if deviation:
            request["deviation"] = deviation

        t_send_ms = time.time() * 1000.0  # latency waterfall: just before send
        try:
            result = _send_order(request, "open", attempt)
        except Exception as e:
            last_error = f"Order send exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if result is None:
            last_error = f"Order send returned None: {mt5.last_error()}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Success
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            # Actual fill price. Some brokers/fill modes leave result.price=0,
            # so fall back to the opened position's price_open. Needed to see
            # the drift between the QUOTE used for sizing and the real FILL —
            # that drift changes the realized SL risk vs the sized target.
            fill_price = result.price or 0.0
            if fill_price <= 0:
                try:
                    _pos = mt5.positions_get(ticket=result.order)
                    if _pos:
                        fill_price = float(_pos[0].price_open)
                except Exception:
                    pass

            # Entry drift (fill vs quote used in the calc) and how it skews the
            # realized SL risk: sizing assumed |quote - SL|; the market risks
            # |fill - SL|. risk_skew < 1 => filled toward SL => less risk than
            # sized (and vice versa). This is the per-trade cause of "risk was
            # $60 not $100" when others were fine.
            entry_drift = round(fill_price - slave_entry, symbol_info.digits) if fill_price else 0.0
            assumed_sl_dist = abs(slave_entry - sl) if sl > 0 else 0.0
            realized_sl_dist = abs(fill_price - sl) if (sl > 0 and fill_price) else 0.0
            risk_skew = round(realized_sl_dist / assumed_sl_dist, 4) if assumed_sl_dist > 0 else 0.0
            worker_logger.info(
                "OPEN_FILL: symbol=%s quote=%.5f fill=%.5f drift=%.5f sl=%.5f "
                "assumed_sl_dist=%.5f realized_sl_dist=%.5f risk_skew=%.4f vol=%.4f",
                symbol, slave_entry, fill_price, entry_drift, sl,
                assumed_sl_dist, realized_sl_dist, risk_skew, volume,
            )
            return SlaveResult(
                slave_name="",  # Filled by caller
                success=True,
                action=TaskAction.OPEN.value,
                ticket=result.order,
                volume=volume,
                retries_used=retries_used,
                details={
                    "fill_price": fill_price,
                    "requested_price": price,
                    "slippage": abs(fill_price - price) if fill_price else 0,
                    "entry_drift": entry_drift,
                    "assumed_sl_dist": round(assumed_sl_dist, symbol_info.digits),
                    "realized_sl_dist": round(realized_sl_dist, symbol_info.digits),
                    "risk_skew": risk_skew,
                    "slave_sl": sl,
                    "slave_tp": tp,
                    "slave_leverage": slave_leverage,
                    "master_entry": master_entry,
                    "slave_price": slave_entry,
                    # Latency waterfall (final legs): send + broker fill.
                    "send_to_fill_ms": round(time.time() * 1000.0 - t_send_ms, 1),
                    "total_copy_ms": round(time.time() * 1000.0 - master_open_ms, 1) if master_open_ms else 0.0,
                    **calc_debug,
                }
            )

        # Requote - retry with new price
        if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
            last_error = f"Requote on attempt {attempt + 1}"
            time.sleep(0.1)  # Brief pause before retry
            continue

        # Other failure
        last_error = f"{result.comment} (retcode={result.retcode})"
        if attempt < retries - 1:
            time.sleep(retry_delay_ms / 1000)

    return SlaveResult(
        slave_name="",
        success=False,
        action=TaskAction.OPEN.value,
        volume=volume,
        error=last_error,
        retries_used=retries_used,
        details={
            "master_entry": master_entry,
            "slave_price": slave_entry,
            "requested_volume": volume,
            **calc_debug,
        },
    )


def _get_history_profit(ticket: int) -> float | None:
    """Look up realized profit for a closed position from deal history."""
    try:
        deals = mt5.history_deals_get(position=ticket)
        if deals is None or len(deals) == 0:
            worker_logger.warning("HISTORY_PROFIT: no deals found for position %d", ticket)
            return None
        total = sum(d.profit + d.swap + d.commission for d in deals)
        worker_logger.info("HISTORY_PROFIT: ticket=%d deals=%d total=%.2f", ticket, len(deals), total)
        return total
    except Exception as e:
        worker_logger.warning("HISTORY_PROFIT: failed for ticket %d: %s", ticket, e)
        return None


def _execute_close_with_retry(conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int) -> SlaveResult:
    """Execute close position with retry logic."""
    ticket = params["ticket"]
    deviation = params.get("deviation", 0)
    filling_mode = params.get("filling_mode", mt5.ORDER_FILLING_IOC)

    last_error = None
    retries_used = 0
    pre_close_profit = 0.0

    for attempt in range(retries):
        retries_used = attempt

        # Get position
        try:
            positions = mt5.positions_get(ticket=ticket)
        except Exception as e:
            last_error = f"Get position exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if not positions:
            # Position might already be closed
            last_error = f"Position {ticket} not found (may be already closed)"
            history_profit = _get_history_profit(ticket)
            return SlaveResult(
                slave_name="",
                success=True,  # Consider this success
                action=TaskAction.CLOSE.value,
                ticket=ticket,
                retries_used=retries_used,
                details={"already_closed": True, "profit": history_profit if history_profit is not None else 0.0}
            )

        position = positions[0]
        symbol = position.symbol
        pre_close_profit = position.profit  # Capture PnL before closing

        # Ensure symbol ready
        ready, err = conn.ensure_symbol_ready(symbol)
        if not ready:
            last_error = err
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            last_error = f"No tick for {symbol}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Close with opposite order
        close_type = 1 if position.type == 0 else 0
        price = tick.bid if position.type == 0 else tick.ask  # BUY closes at bid, SELL at ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": symbol,
            "volume": position.volume,
            "type": close_type,
            "price": price,
            "magic": 0,
            "comment": "Close copied",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }
        if deviation:
            request["deviation"] = deviation

        try:
            result = _send_order(request, "close", attempt)
        except Exception as e:
            last_error = f"Close send exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if result is None:
            last_error = f"Close send returned None: {mt5.last_error()}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return SlaveResult(
                slave_name="",
                success=True,
                action=TaskAction.CLOSE.value,
                ticket=ticket,
                retries_used=retries_used,
                details={"close_price": result.price, "profit": pre_close_profit}
            )

        if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
            last_error = f"Requote on close attempt {attempt + 1}"
            time.sleep(0.1)
            continue

        last_error = f"{result.comment} (retcode={result.retcode})"
        if attempt < retries - 1:
            time.sleep(retry_delay_ms / 1000)

    # Re-check: position may have been closed by broker during our retries.
    # Use multiple attempts with delays — broker-side SL/TP fills can take
    # a few hundred milliseconds to clear from positions_get().
    for recheck in range(4):
        if recheck > 0:
            time.sleep(0.25)
        try:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                worker_logger.info(
                    "CLOSE_RECHECK: ticket=%d gone after retries (broker-closed, recheck=%d), treating as success",
                    ticket, recheck,
                )
                history_profit = _get_history_profit(ticket)
                return SlaveResult(
                    slave_name="",
                    success=True,
                    action=TaskAction.CLOSE.value,
                    ticket=ticket,
                    retries_used=retries_used,
                    details={
                        "already_closed": True,
                        "original_error": last_error,
                        "profit": history_profit if history_profit is not None else 0.0,
                    },
                )
        except Exception:
            pass  # If recheck fails, try again or fall through

    return SlaveResult(
        slave_name="",
        success=False,
        action=TaskAction.CLOSE.value,
        ticket=ticket,
        error=last_error,
        retries_used=retries_used,
        details={"profit": pre_close_profit},
    )


def _execute_modify_with_retry(conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int) -> SlaveResult:
    """Execute modify SL/TP with retry logic.

    Calculates slave SL/TP based on pip distance from master.
    """
    ticket = params["ticket"]
    master_entry = params["master_entry"]
    master_sl = params["master_sl"]
    master_tp = params["master_tp"]
    is_buy = params["is_buy"]

    last_error = None
    retries_used = 0

    for attempt in range(retries):
        retries_used = attempt

        # Verify position exists
        try:
            positions = mt5.positions_get(ticket=ticket)
        except Exception as e:
            last_error = f"Get position exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if not positions:
            last_error = f"Position {ticket} not found"
            return SlaveResult(
                slave_name="",
                success=False,
                action=TaskAction.MODIFY.value,
                ticket=ticket,
                error=last_error,
                retries_used=retries_used,
            )

        position = positions[0]
        slave_entry = position.price_open

        # Get symbol info for digits
        symbol_info = mt5.symbol_info(position.symbol)
        if symbol_info is None:
            last_error = f"Cannot get symbol info for {position.symbol}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        # Calculate SL/TP based on pip distance from master
        sl, tp = _calculate_slave_sl_tp(
            master_entry=master_entry,
            master_sl=master_sl,
            master_tp=master_tp,
            slave_entry=slave_entry,
            is_buy=is_buy,
            symbol_digits=symbol_info.digits,
        )

        worker_logger.info(
            "MODIFY_CALC: ticket=%d symbol=%s slave_entry=%.5f "
            "master_entry=%.5f master_sl=%.5f master_tp=%.5f "
            "new_sl=%.5f new_tp=%.5f old_sl=%.5f old_tp=%.5f",
            ticket, position.symbol, slave_entry,
            master_entry, master_sl, master_tp,
            sl, tp, position.sl, position.tp,
        )

        # Skip if already at target values
        if abs(position.sl - sl) < 0.00001 and abs(position.tp - tp) < 0.00001:
            return SlaveResult(
                slave_name="",
                success=True,
                action=TaskAction.MODIFY.value,
                ticket=ticket,
                retries_used=retries_used,
                details={"no_change": True, "sl": sl, "tp": tp}
            )

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": position.symbol,
            "sl": sl,
            "tp": tp,
        }

        try:
            result = _send_order(request, "modify", attempt)
        except Exception as e:
            last_error = f"Modify send exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if result is None:
            last_error = f"Modify send returned None: {mt5.last_error()}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return SlaveResult(
                slave_name="",
                success=True,
                action=TaskAction.MODIFY.value,
                ticket=ticket,
                retries_used=retries_used,
                details={"sl": sl, "tp": tp}
            )

        last_error = f"{result.comment} (retcode={result.retcode})"
        if attempt < retries - 1:
            time.sleep(retry_delay_ms / 1000)

    return SlaveResult(
        slave_name="",
        success=False,
        action=TaskAction.MODIFY.value,
        ticket=ticket,
        error=last_error,
        retries_used=retries_used,
    )


def _execute_warmup(conn: MT5Connection, slave_name: str = "") -> SlaveResult:
    """Execute warmup: verify connection, cache broker symbols.

    This pre-spawns the worker process and initializes MT5 terminal,
    so the first real trade doesn't incur a 5-25 second startup delay.
    Also caches available broker symbols for the symbol mapping editor.
    """
    try:
        account_info = mt5.account_info()
        if account_info is None:
            return SlaveResult(
                slave_name="",
                success=False,
                action=TaskAction.WARMUP.value,
                error="Could not get account info during warmup",
            )

        worker_logger.info(
            "WARMUP: login=%d server=%s leverage=%d balance=%.2f",
            account_info.login, account_info.server,
            account_info.leverage, account_info.balance,
        )

        # The Algo Trading toggle, captured while we are already connected. It
        # is the one switch that turns a working copier into one that places no
        # orders, so it belongs in the very first thing we learn about a
        # terminal rather than being discovered by a trade going missing.
        term = _read_terminal_state()

        # Fetch available broker symbols (returned to main process for in-memory cache)
        symbol_names = []
        try:
            symbols = mt5.symbols_get()
            if symbols:
                symbol_names = sorted(s.name for s in symbols)
                worker_logger.info(
                    "WARMUP: fetched %d broker symbols for %s",
                    len(symbol_names), slave_name,
                )
        except Exception as e:
            worker_logger.warning("WARMUP: failed to fetch symbols: %s", e)

        return SlaveResult(
            slave_name="",
            success=True,
            action=TaskAction.WARMUP.value,
            details={
                "login": account_info.login,
                "leverage": account_info.leverage,
                "balance": account_info.balance,
                "symbols": symbol_names,
                **term,
            },
        )
    except Exception as e:
        return SlaveResult(
            slave_name="",
            success=False,
            action=TaskAction.WARMUP.value,
            error=f"Warmup exception: {e}",
        )


def _read_terminal_state() -> dict:
    """``terminal_info`` fields worth showing, or ``{}`` when unreadable.

    ``{}`` rather than a default of False: "we could not ask" and "the
    operator has it switched off" are different facts, and only one of them is
    theirs to act on.
    """
    try:
        info = mt5.terminal_info()
    except Exception:
        return {}
    if info is None:
        return {}
    return {
        "algo_trading": bool(getattr(info, "trade_allowed", False)),
        "terminal_connected": bool(getattr(info, "connected", False)),
        "build": int(getattr(info, "build", 0) or 0),
    }


def _execute_terminal_state(conn: MT5Connection, slave_name: str = "") -> SlaveResult:
    """Read the terminal's own state — Algo Trading, link, build.

    Runs on the cached connection, so it costs about what fetch_positions
    costs (0.4ms p50 in production traces) and can be polled on the GUI's
    existing health-check cadence.
    """
    state = _read_terminal_state()
    return SlaveResult(
        slave_name=slave_name,
        success=bool(state),
        action=TaskAction.TERMINAL_STATE.value,
        error=None if state else "terminal_info unavailable",
        details=state,
    )


def _execute_fetch_positions(conn: MT5Connection, slave_name: str = "") -> SlaveResult:
    """Fetch all open positions from the slave account.

    Returns position data in result.details["positions"] as a list of dicts,
    each with keys: ticket, symbol, type, volume, magic, price_open, sl, tp, profit, time.
    """
    try:
        positions = mt5.positions_get()
        if positions is None:
            positions = ()

        pos_list = []
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "volume": p.volume,
                "magic": p.magic,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "time": p.time,
            })

        worker_logger.info(
            "FETCH_POSITIONS: %s has %d open positions",
            slave_name, len(pos_list),
        )

        return SlaveResult(
            slave_name="",
            success=True,
            action=TaskAction.FETCH_POSITIONS.value,
            details={"positions": pos_list},
        )
    except Exception as e:
        return SlaveResult(
            slave_name="",
            success=False,
            action=TaskAction.FETCH_POSITIONS.value,
            error=f"Fetch positions exception: {e}",
        )


def _execute_open_traced(
    tracer: Tracer, conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int,
) -> SlaveResult:
    """Open position with per-phase spans for delay identification."""
    symbol = params["symbol"]
    order_type = params["order_type"]
    is_buy = params["is_buy"]

    # Phase 1: Get account info
    with tracer.span("worker_account_info") as s:
        account_info = mt5.account_info()
        if account_info is None:
            s.set_tag("result", "failed")
            return SlaveResult(
                slave_name="", success=False, action=TaskAction.OPEN.value,
                error="Could not get account info for leverage",
            )
        s.set_tags(result="ok", leverage=account_info.leverage)

    # Phase 2: Symbol ready + get tick
    with tracer.span("worker_symbol_ready") as s:
        ready, err = conn.ensure_symbol_ready(symbol)
        s.set_tags(symbol=symbol, result="ok" if ready else "failed")
        if not ready:
            s.set_tag("error", err)

    # Phase 3: Calculate volume + SL/TP
    with tracer.span("worker_calculate") as s:
        result = _execute_open_with_retry(conn, params, retries, retry_delay_ms, tracer=tracer)
        s.set_tags(
            symbol=symbol, side="BUY" if is_buy else "SELL",
            success=result.success,
            volume=result.volume,
            ticket=result.ticket,
        )
        if result.details.get("skipped_by_deviation"):
            s.set_tag("skipped_by_deviation", True)
        if result.details.get("skipped_by_risk_guard"):
            s.set_tag("skipped_by_risk_guard", True)
        if result.details.get("simulated"):
            s.set_tag("simulated", True)
        if result.error:
            s.set_tag("error", result.error)
        # Add price details for trace-only debugging
        for key in ("master_entry", "slave_price", "deviation", "max_allowed",
                     "fill_price", "requested_price", "slippage",
                     "entry_drift", "assumed_sl_dist", "realized_sl_dist", "risk_skew",
                     "slave_sl", "slave_tp", "slave_leverage",
                     "requested_volume", "projected_loss", "max_risk_usd", "balance",
                     "volume_method", "risk_ladder", "calc_risk_usd", "loss_per_lot",
                     "tick_value", "tick_size",
                     "contract_size", "sl_distance", "vol_min", "vol_max", "vol_step",
                     "master_time", "stops_level", "freeze_level",
                     "signal_source", "recv_ms", "master_to_recv_ms",
                     "recv_to_calc_ms", "send_to_fill_ms", "total_copy_ms",
                     "copy_delay_ms"):
            if key in result.details:
                s.set_tag(key, result.details[key])

    return result


def _execute_reduce_with_retry(conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int) -> SlaveResult:
    """Partially close a slave position to mirror a master scale-out.

    ``keep_ratio`` = new_master_volume / old_master_volume (< 1). The slave's
    new target = round(current_volume * keep_ratio) to the broker step; we close
    the difference. If the target would fall below the broker minimum, close the
    whole position instead. If the reduce amount is below the minimum lot, it's a
    no-op (can't partially close that little).
    """
    ticket = params["ticket"]
    keep_ratio = float(params.get("keep_ratio", 1.0))
    deviation = params.get("deviation", 0)
    filling_mode = params.get("filling_mode", mt5.ORDER_FILLING_IOC)
    last_error = None
    retries_used = 0

    for attempt in range(retries):
        retries_used = attempt
        try:
            positions = mt5.positions_get(ticket=ticket)
        except Exception as e:
            last_error = f"Get position exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue
        if not positions:
            return SlaveResult(
                slave_name="", success=True, action=TaskAction.REDUCE.value,
                ticket=ticket, retries_used=retries_used,
                details={"already_closed": True},
            )
        position = positions[0]
        symbol = position.symbol
        si = mt5.symbol_info(symbol)
        step = si.volume_step if si else 0.01
        vmin = si.volume_min if si else 0.01
        cur = position.volume

        target = round(round(cur * keep_ratio / step) * step, 2)
        if target < vmin:
            target = 0.0  # nothing worth keeping -> full close
        reduce_amount = round(cur - target, 2)
        if reduce_amount < vmin:
            # Too small to partially close — leave the position as-is.
            worker_logger.info(
                "REDUCE_NOOP: %s ticket=%d cur=%.2f keep_ratio=%.4f reduce=%.2f < min=%.2f",
                symbol, ticket, cur, keep_ratio, reduce_amount, vmin,
            )
            return SlaveResult(
                slave_name="", success=True, action=TaskAction.REDUCE.value,
                ticket=ticket, volume=cur, retries_used=retries_used,
                details={"reduced": 0.0, "new_volume": cur, "noop": True},
            )

        ready, err = conn.ensure_symbol_ready(symbol)
        if not ready:
            last_error = err
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            last_error = f"No tick for {symbol}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue

        close_type = 1 if position.type == 0 else 0
        price = tick.bid if position.type == 0 else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": symbol,
            "volume": reduce_amount,
            "type": close_type,
            "price": price,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }
        if deviation:
            request["deviation"] = deviation

        t_send_ms = time.time() * 1000.0  # latency waterfall: just before send
        try:
            result = _send_order(request, "reduce", attempt)
        except Exception as e:
            last_error = f"Order send exception: {e}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue
        if result is None:
            last_error = f"Order send returned None: {mt5.last_error()}"
            if attempt < retries - 1:
                time.sleep(retry_delay_ms / 1000)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            worker_logger.info(
                "REDUCE_OK: %s ticket=%d %.2f -> %.2f (closed %.2f, keep_ratio=%.4f)",
                symbol, ticket, cur, target, reduce_amount, keep_ratio,
            )
            return SlaveResult(
                slave_name="", success=True, action=TaskAction.REDUCE.value,
                ticket=ticket, volume=target, retries_used=retries_used,
                details={"reduced": reduce_amount, "new_volume": target,
                         "keep_ratio": keep_ratio},
            )
        last_error = f"{result.comment} (retcode={result.retcode})"
        if attempt < retries - 1:
            time.sleep(retry_delay_ms / 1000)

    return SlaveResult(
        slave_name="", success=False, action=TaskAction.REDUCE.value,
        ticket=ticket, error=last_error, retries_used=retries_used,
    )


def _execute_reduce_traced(
    tracer: Tracer, conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int,
) -> SlaveResult:
    """Partial close with a span."""
    with tracer.span("worker_reduce_order") as s:
        result = _execute_reduce_with_retry(conn, params, retries, retry_delay_ms)
        s.set_tags(ticket=params["ticket"], success=result.success,
                   retries=result.retries_used)
        for k in ("reduced", "new_volume", "keep_ratio", "noop"):
            if k in result.details:
                s.set_tag(k, result.details[k])
        if result.error:
            s.set_tag("error", result.error)
    return result


def _execute_close_traced(
    tracer: Tracer, conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int,
) -> SlaveResult:
    """Close position with spans."""
    ticket = params["ticket"]

    with tracer.span("worker_close_order") as s:
        result = _execute_close_with_retry(conn, params, retries, retry_delay_ms)
        s.set_tags(
            ticket=ticket, success=result.success,
            retries=result.retries_used,
        )
        if result.details.get("close_price"):
            s.set_tag("close_price", result.details["close_price"])
        if "profit" in result.details:
            s.set_tag("profit", result.details["profit"])
        if result.details.get("already_closed"):
            s.set_tag("already_closed", True)
        if result.error:
            s.set_tag("error", result.error)

    return result


def _execute_modify_traced(
    tracer: Tracer, conn: MT5Connection, params: dict, retries: int, retry_delay_ms: int,
) -> SlaveResult:
    """Modify SL/TP with spans."""
    ticket = params["ticket"]

    with tracer.span("worker_modify_sltp") as s:
        result = _execute_modify_with_retry(conn, params, retries, retry_delay_ms)
        s.set_tags(
            ticket=ticket, success=result.success,
            retries=result.retries_used,
        )
        if result.details.get("sl"):
            s.set_tags(sl=result.details["sl"], tp=result.details.get("tp", 0))
        if result.details.get("no_change"):
            s.set_tag("no_change", True)
        if result.error:
            s.set_tag("error", result.error)

    return result


_worker_atexit_registered = False

# Worker-process connection cache (reused across tasks in same process)
_worker_connection: MT5Connection | None = None
_worker_conn_key: tuple | None = None


def _worker_atexit():
    """Ensure MT5 terminal is shut down when worker process exits.

    Without this, terminal64.exe stays running as an orphan process
    after the Python worker exits.
    """
    global _worker_connection
    if _worker_connection is not None:
        _worker_connection.disconnect()
        _worker_connection = None
    try:
        mt5.shutdown()
    except Exception:
        pass


def _invalidate_connection():
    """Invalidate cached connection on errors."""
    global _worker_connection, _worker_conn_key
    if _worker_connection is not None:
        _worker_connection.disconnect()
        _worker_connection = None
        _worker_conn_key = None


def _get_or_create_connection(
    task: SlaveTask, tracer: Tracer,
) -> tuple[MT5Connection, float, bool]:
    """Get cached connection or create new one.

    Returns (connection, connect_ms, connected).
    Reuses existing MT5 connection if same terminal/login/server and still
    alive, saving ~100ms reconnect overhead per task.
    """
    global _worker_connection, _worker_conn_key

    key = (task.terminal_path, task.login, task.server)
    start = time.perf_counter()

    # Try cached connection
    if _worker_connection is not None and _worker_conn_key == key:
        if _worker_connection.is_connected():
            elapsed = (time.perf_counter() - start) * 1000
            tracer.event("worker_connect_cached", slave=task.slave_name)
            return _worker_connection, elapsed, True
        worker_logger.debug(
            "[%s] Cached connection stale, reconnecting", task.slave_name
        )

    # Disconnect old connection if switching slave
    if _worker_connection is not None:
        _worker_connection.disconnect()
        _worker_connection = None
        _worker_conn_key = None

    # Create and connect
    conn = MT5Connection(
        task.terminal_path, task.login, task.password, task.server
    )

    connected = False
    connect_error = None
    with tracer.span("worker_connect") as conn_span:
        for attempt in range(task.retry_attempts):
            success, err = conn.connect(timeout_ms=task.timeout_ms)
            if success:
                connected = True
                break
            connect_error = err
            worker_logger.warning(
                "[%s] Connect attempt %d failed: %s",
                task.slave_name, attempt + 1, err,
            )
            if attempt < task.retry_attempts - 1:
                time.sleep(task.retry_delay_ms / 1000)

        conn_span.set_tags(connected=connected, attempts=attempt + 1)
        if not connected:
            conn_span.set_tag("error", connect_error)

    connect_ms = (time.perf_counter() - start) * 1000

    if connected:
        _worker_connection = conn
        _worker_conn_key = key

    return conn, connect_ms, connected


def execute_slave_task(task: SlaveTask) -> SlaveResult:
    """
    Execute a task on a slave (runs in worker process).

    This is the main entry point called by ProcessPoolExecutor.
    Propagates trace context from the main process so spans are linked
    into the same trace tree (W3C Trace Context pattern).
    """
    global _worker_atexit_registered
    if not _worker_atexit_registered:
        import atexit
        atexit.register(_worker_atexit)
        _worker_atexit_registered = True
    _ensure_worker_logging()

    tracer = _get_worker_tracer()
    trace_ctx = task.trace_context or {}

    # Propagate trace context from main process into this worker
    with tracer.propagate(trace_ctx):
        return _execute_slave_task_inner(task, tracer)


def _execute_slave_task_inner(task: SlaveTask, tracer: Tracer) -> SlaveResult:
    """Inner implementation with full span instrumentation."""
    action = task.action if isinstance(task.action, TaskAction) else TaskAction(task.action)

    with tracer.span(f"worker_{action.value}") as root_span:
        # The master ticket and symbol are what a person searches by; without
        # them a worker span can only be reached from its parent, which is in
        # another process's file region.
        root_span.set_tags(
            slave=task.slave_name,
            action=action.value,
            slave_id=task.slave_id,
            symbol=task.params.get("symbol", ""),
            master_ticket=task.params.get("magic", 0),
            slave_ticket=task.params.get("ticket", 0),
        )
        start_time = time.perf_counter()

        # Get or reuse cached connection (avoids ~100ms reconnect per task)
        conn, connect_ms, connected = _get_or_create_connection(task, tracer)

        if not connected:
            root_span.set_tag("error", "Connection failed")
            return SlaveResult(
                slave_name=task.slave_name,
                success=False,
                action=action.value,
                slave_id=task.slave_id,
                error="Connection failed",
                duration_ms=connect_ms,
            )

        try:
            # Execute action (traced)
            action_start = time.perf_counter()

            if action == TaskAction.OPEN:
                result = _execute_open_traced(
                    tracer, conn, task.params, task.retry_attempts, task.retry_delay_ms
                )
            elif action == TaskAction.CLOSE:
                result = _execute_close_traced(
                    tracer, conn, task.params, task.retry_attempts, task.retry_delay_ms
                )
            elif action == TaskAction.REDUCE:
                result = _execute_reduce_traced(
                    tracer, conn, task.params, task.retry_attempts, task.retry_delay_ms
                )
            elif action == TaskAction.MODIFY:
                result = _execute_modify_traced(
                    tracer, conn, task.params, task.retry_attempts, task.retry_delay_ms
                )
            elif action == TaskAction.WARMUP:
                result = _execute_warmup(conn, slave_name=task.slave_name)
            elif action == TaskAction.TERMINAL_STATE:
                result = _execute_terminal_state(conn, slave_name=task.slave_name)
            elif action == TaskAction.FETCH_POSITIONS:
                result = _execute_fetch_positions(conn, slave_name=task.slave_name)
            else:
                result = SlaveResult(
                    slave_name=task.slave_name,
                    success=False,
                    action=str(task.action),
                    error=f"Unknown action: {task.action}",
                )

            # Fill in slave name/id and duration
            action_ms = (time.perf_counter() - action_start) * 1000
            total_ms = (time.perf_counter() - start_time) * 1000
            result.slave_name = task.slave_name
            result.slave_id = task.slave_id
            result.duration_ms = total_ms

            root_span.set_tags(
                connect_ms=round(connect_ms, 1),
                action_ms=round(action_ms, 1),
                success=result.success,
                ticket=result.ticket,
            )

            return result

        except Exception as e:
            _invalidate_connection()
            duration_ms = (time.perf_counter() - start_time) * 1000
            worker_logger.error(f"[{task.slave_name}] Task exception: {e}")
            root_span.set_tag("error", str(e))
            return SlaveResult(
                slave_name=task.slave_name,
                success=False,
                action=action.value,
                slave_id=task.slave_id,
                error=f"Exception: {e}",
                duration_ms=duration_ms,
            )


def _get_running_terminal_paths() -> "set[str] | None":
    """Normalized paths of every running terminal64.exe, or None if unknown.

    Used by the Terminals tab to detect a manually closed terminal.

    NONE IS NOT AN EMPTY SET
        This used to answer "nothing is running" whenever the query itself
        failed — on a PowerShell error, a timeout, or simply on a non-Windows
        machine. The caller then marked every terminal dead and started
        reconnecting them, off a failure that said nothing about the
        terminals at all.

        A further limit worth knowing: Get-Process can list a process whose
        Path it cannot read, when that process belongs to another user or runs
        at a different elevation. The master is the terminal most likely to
        have been started by hand, so it is the one most likely to be missing
        from an otherwise healthy answer — which is why the service's own
        connection, not this, is the authority on the master.
    """
    import os
    import subprocess
    import sys

    if sys.platform != "win32":
        return None

    try:
        ps_cmd = (
            "Get-Process -Name terminal64 -ErrorAction SilentlyContinue"
            " | Select-Object Path"
            " | ForEach-Object { $_.Path }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return None

        paths = set()
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line:
                paths.add(os.path.normpath(line).lower())
        return paths
    except Exception:
        return None


def _manage_terminal_windows(master_path: str, slave_paths: set[str], maximize_master: bool):
    """Manage MT5 terminal windows after warmup.

    maximize_master=True:  maximize master, minimize slaves.
    maximize_master=False: minimize everything.

    Windows-only. Uses PowerShell to map PIDs to exe paths, then
    ctypes user32 ShowWindow to change window state.
    """
    import os
    import subprocess
    import sys

    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    logger = logging.getLogger("metact.parallel")

    SW_MINIMIZE = 6
    SW_MAXIMIZE = 3

    # Normalize paths for comparison
    master_norm = os.path.normpath(master_path).lower() if master_path else ""
    slave_norm = {os.path.normpath(p).lower() for p in slave_paths if p}

    if not master_norm and not slave_norm:
        return

    try:
        # Get PID|Path pairs for all terminal64 processes
        ps_cmd = (
            "Get-Process -Name terminal64 -ErrorAction SilentlyContinue"
            " | Select-Object Id, Path"
            " | ForEach-Object { $_.Id.ToString() + '|' + $_.Path }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return

        # Build pid -> normalized_path dict
        pid_to_path: dict[int, str] = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            pid_str, exe_path = line.split("|", 1)
            if pid_str.isdigit() and exe_path:
                pid_to_path[int(pid_str)] = os.path.normpath(exe_path).lower()

        if not pid_to_path:
            return

        # Enumerate top-level visible windows and match to PIDs
        user32 = ctypes.windll.user32

        EnumWindows = user32.EnumWindows
        GetWindowThreadProcessId = user32.GetWindowThreadProcessId
        IsWindowVisible = user32.IsWindowVisible
        ShowWindow = user32.ShowWindow

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_callback(hwnd, _lparam):
            if not IsWindowVisible(hwnd):
                return True  # continue enumeration

            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_pid = pid.value

            norm_path = pid_to_path.get(proc_pid)
            if norm_path is None:
                return True

            if norm_path == master_norm:
                if maximize_master:
                    ShowWindow(hwnd, SW_MAXIMIZE)
                    logger.info("Maximized master terminal window (PID %d)", proc_pid)
                else:
                    ShowWindow(hwnd, SW_MINIMIZE)
                    logger.info("Minimized master terminal window (PID %d)", proc_pid)
            elif norm_path in slave_norm:
                ShowWindow(hwnd, SW_MINIMIZE)
                logger.info("Minimized slave terminal window (PID %d)", proc_pid)

            return True  # continue enumeration

        EnumWindows(WNDENUMPROC(enum_callback), 0)

    except Exception as e:
        logger.warning("Failed to manage terminal windows: %s", e)


def _kill_mt5_terminals(terminal_paths: set[str], grace_s: float | None = None,
                        on_wait=None):
    """Close terminal64.exe processes matching the given executable paths.

    mt5.initialize() launches terminal64.exe but mt5.shutdown() only
    disconnects — it does NOT close the terminal. The terminal also detaches
    from the Python process tree, so taskkill /T doesn't reach it.

    We list running processes via PowerShell (with tasklist+ctypes fallback)
    and stop each PID whose image path matches anything in `terminal_paths`.

    "Stop" means WM_CLOSE first, taskkill /F only for what won't go: MT5
    writes chart templates (EA inputs), terminal.ini and the experts config on
    clean exit only, so a force-kill loses every setting made that session.
    Pass grace_s=0 to skip straight to the force kill.
    """
    import os
    import sys

    if sys.platform != "win32" or not terminal_paths:
        return

    from .template_manager import (
        GRACEFUL_CLOSE_TIMEOUT_S,
        list_terminal64_pids_with_paths,
        stop_pids,
    )

    logger = logging.getLogger("metact.parallel")
    grace = GRACEFUL_CLOSE_TIMEOUT_S if grace_s is None else grace_s

    normalized = {os.path.normpath(p).lower() for p in terminal_paths}
    logger.info("Stopping MT5 terminals matching %d path(s): %s",
                len(normalized), sorted(normalized))

    pairs = list_terminal64_pids_with_paths()
    logger.info("Discovered %d running terminal64.exe process(es)", len(pairs))
    for pid, exe_path in pairs:
        logger.debug("  PID %d → %s", pid, exe_path)

    targets = [pid for pid, exe_path in pairs if exe_path in normalized]
    clean, forced = stop_pids(targets, grace_s=grace, on_wait=on_wait)

    unmatched = [(pid, p) for pid, p in pairs if p not in normalized]
    if unmatched:
        logger.info("Skipped %d unrelated terminal64 process(es):", len(unmatched))
        for pid, p in unmatched:
            logger.info("  PID %d → %s (not in our managed set)", pid, p)
    logger.info("Stopped %d MT5 terminal(s): %d clean, %d forced",
                clean + forced, clean, forced)


class ParallelExecutor:
    """Executes slave tasks in parallel using process pool with reliability features.

    Provides both sync and async interfaces:
    - execute_tasks() - sync, returns all results when complete
    - execute_tasks_async() - async, yields results as they complete
    """

    def __init__(
        self,
        max_workers: int | None = None,
        task_timeout_seconds: float = 60.0,
        terminal_paths: set[str] | None = None,
    ):
        """
        Initialize parallel executor.

        Args:
            max_workers: Max parallel processes. None = auto (number of tasks).
            task_timeout_seconds: Timeout for each task.
            terminal_paths: Set of terminal64.exe paths to kill on shutdown.
        """
        self.max_workers = max_workers
        self.task_timeout_seconds = task_timeout_seconds
        self.logger = logging.getLogger("metact.parallel")
        # One DEDICATED single-worker pool per slave_id. MT5 is one-terminal-
        # per-process, so pinning each slave to its own process keeps that
        # slave's connection warm across tasks (cache hit) instead of paying a
        # ~100ms reconnect whenever a shared pool routes another slave onto it.
        self._pools: dict[str, ProcessPoolExecutor] = {}
        self._terminal_paths: set[str] = terminal_paths or set()
        self._summary = _PollSummary(self.logger)

    def _get_pool(self, key: str) -> ProcessPoolExecutor:
        """Get (or create) the dedicated single-worker pool for a slave."""
        pool = self._pools.get(key)
        if pool is None:
            pool = ProcessPoolExecutor(max_workers=1)
            self._pools[key] = pool
        return pool

    def shutdown(self):
        """Shutdown the executor and kill managed MT5 terminals.

        Uses shutdown(wait=True) so workers exit cleanly and run atexit
        handlers. Wrapped in a thread with timeout to avoid blocking forever.
        After workers exit, explicitly kills terminal64.exe processes by path
        since mt5.shutdown() only disconnects — it doesn't close the terminal.
        """
        # Whatever the poll counters are holding belongs in the log, not in a
        # dead object: a stop right after a burst of failures is exactly when
        # the numbers are worth having.
        self._summary.flush_if_due(force=True)
        if self._pools:
            pools = list(self._pools.values())
            # Snapshot processes across all per-slave pools BEFORE shutdown.
            processes = []
            for p in pools:
                processes.extend(getattr(p, '_processes', {}).values())
            self.logger.info(
                "Shutting down %d slave pool(s), %d worker process(es)",
                len(pools), len(processes),
            )

            import threading
            shutdown_done = threading.Event()

            def _do_shutdown():
                for p in pools:
                    try:
                        p.shutdown(wait=True, cancel_futures=True)
                    except Exception:
                        pass
                shutdown_done.set()

            t = threading.Thread(target=_do_shutdown, daemon=True)
            t.start()
            shutdown_done.wait(timeout=10)

            # Force-kill any workers that survived the graceful shutdown
            for proc in processes:
                if proc.is_alive():
                    self.logger.warning(
                        "Worker process %d did not exit in time, killing", proc.pid
                    )
                    proc.kill()

            self._pools = {}

        # Kill terminal64.exe processes that match our configured paths.
        # mt5.shutdown() only disconnects; terminals stay open otherwise.
        if self._terminal_paths:
            self.logger.info("Killing MT5 terminals: %s", self._terminal_paths)
            _kill_mt5_terminals(self._terminal_paths)

    def _handle_result(self, result: SlaveResult) -> SlaveResult:
        """Log and return a result.

        A poll that succeeded is counted, not logged — see _PollSummary.
        Everything else, and every failure, is logged the moment it happens.
        """
        if result.action in POLLING_ACTIONS:
            self._summary.record(
                result.action, result.slave_name, result.success, result.duration_ms
            )
            if not result.success:
                self.logger.warning(
                    f"[{result.slave_name}] {result.action} failed: {result.error}"
                )
            return result

        if result.success:
            self.logger.info(
                f"[{result.slave_name}] {result.action} success "
                f"(ticket={result.ticket}, {result.duration_ms:.0f}ms)"
            )
        else:
            self.logger.warning(
                f"[{result.slave_name}] {result.action} failed: {result.error}"
            )
        return result

    def _log_batch(self, tasks: list[SlaveTask], stage: str, **fmt):
        """The 'Executing N / Completed N' pair, unless it is all polling.

        A batch of polls is one line every twenty seconds in the summary; the
        batch banners around it would put the count back three times over.
        """
        if all(
            (t.action.value if isinstance(t.action, TaskAction) else str(t.action))
            in POLLING_ACTIONS
            for t in tasks
        ):
            return
        self.logger.info(stage.format(**fmt))

    def _make_error_result(self, task: SlaveTask, error: str, duration_ms: float = 0) -> SlaveResult:
        """Create an error result for a task."""
        return SlaveResult(
            slave_name=task.slave_name,
            success=False,
            action=task.action.value if isinstance(task.action, TaskAction) else str(task.action),
            slave_id=task.slave_id,
            error=error,
            duration_ms=duration_ms,
        )

    def execute_tasks(self, tasks: list[SlaveTask]) -> list[SlaveResult]:
        """
        Execute multiple tasks in parallel (synchronous).

        Uses as_completed() to process results as they finish.
        Returns results in same order as tasks.
        """
        if not tasks:
            return []

        max_workers = self.max_workers or min(len(tasks), 8)
        results: dict[str, SlaveResult] = {}

        self._log_batch(tasks, "Executing {n} tasks with {w} workers",
                        n=len(tasks), w=max_workers)

        try:
            # Each slave -> its own dedicated pool (warm, pinned connection).
            future_to_task = {
                self._get_pool(task.slave_id).submit(execute_slave_task, task): task
                for task in tasks
            }

            # Process results as they complete (faster response)
            for future in as_completed(future_to_task, timeout=self.task_timeout_seconds * len(tasks)):
                task = future_to_task[future]
                try:
                    result = future.result(timeout=0.1)  # Already complete
                    results[task.slave_id] = self._handle_result(result)

                except FuturesTimeoutError:
                    self.logger.error(f"[{task.slave_name}] Task timed out")
                    results[task.slave_id] = self._make_error_result(
                        task, f"Task timed out after {self.task_timeout_seconds}s",
                        self.task_timeout_seconds * 1000
                    )

                except Exception as e:
                    self.logger.error(f"[{task.slave_name}] Task exception: {e}")
                    results[task.slave_id] = self._make_error_result(task, str(e))

        except FuturesTimeoutError:
            self.logger.error("Overall execution timed out")
            # Add error results for tasks that didn't complete
            for task in tasks:
                if task.slave_id not in results:
                    results[task.slave_id] = self._make_error_result(
                        task, "Overall execution timed out"
                    )

        except Exception as e:
            self.logger.error(f"Executor error: {e}")
            for task in tasks:
                if task.slave_id not in results:
                    results[task.slave_id] = self._make_error_result(
                        task, f"Executor error: {e}"
                    )

        # Return results in original task order
        ordered_results = [results.get(task.slave_id, self._make_error_result(task, "No result")) for task in tasks]

        success_count = sum(1 for r in ordered_results if r.success)
        self._log_batch(tasks, "Completed: {ok}/{n} successful",
                        ok=success_count, n=len(tasks))
        # Time-based, so a poll that stops arriving still gets its last
        # summary out rather than sitting in the counters forever.
        self._summary.flush_if_due()

        return ordered_results

    async def execute_tasks_async(
        self,
        tasks: list[SlaveTask],
        on_result: Callable[[SlaveTask, SlaveResult], None] | None = None,
    ) -> list[SlaveResult]:
        """
        Execute multiple tasks in parallel (async).

        Runs the process pool in a thread to avoid blocking the event loop.
        Optionally calls on_result callback as each result arrives.

        Args:
            tasks: List of tasks to execute
            on_result: Optional callback(task, result) called as results arrive

        Returns:
            Results in same order as tasks
        """
        if not tasks:
            return []

        loop = asyncio.get_event_loop()
        max_workers = self.max_workers or min(len(tasks), 8)
        results: dict[str, SlaveResult] = {}

        self._log_batch(tasks, "Executing {n} tasks async with {w} workers",
                        n=len(tasks), w=max_workers)

        def run_in_executor():
            """Run the process pool (called in thread)."""
            task_results = {}
            try:
                # Each slave -> its own dedicated pool (warm, pinned connection).
                future_to_task = {
                    self._get_pool(task.slave_id).submit(execute_slave_task, task): task
                    for task in tasks
                }

                for future in as_completed(future_to_task, timeout=self.task_timeout_seconds * len(tasks)):
                    task = future_to_task[future]
                    try:
                        result = future.result(timeout=0.1)
                        task_results[task.slave_id] = (task, result)
                    except Exception as e:
                        task_results[task.slave_id] = (task, self._make_error_result(task, str(e)))

            except Exception as e:
                for task in tasks:
                    if task.slave_id not in task_results:
                        task_results[task.slave_id] = (task, self._make_error_result(task, f"Executor error: {e}"))

            return task_results

        # Run executor in thread pool to not block event loop
        task_results = await loop.run_in_executor(None, run_in_executor)

        # Process results and call callbacks
        for slave_id, (task, result) in task_results.items():
            result = self._handle_result(result)
            results[slave_id] = result
            if on_result:
                try:
                    if asyncio.iscoroutinefunction(on_result):
                        await on_result(task, result)
                    else:
                        on_result(task, result)
                except Exception as e:
                    self.logger.error(f"Callback error: {e}")

        # Return in original order
        ordered_results = [results.get(task.slave_id, self._make_error_result(task, "No result")) for task in tasks]

        success_count = sum(1 for r in ordered_results if r.success)
        self._log_batch(tasks, "Completed: {ok}/{n} successful",
                        ok=success_count, n=len(tasks))
        # Time-based, so a poll that stops arriving still gets its last
        # summary out rather than sitting in the counters forever.
        self._summary.flush_if_due()

        return ordered_results

    def execute_single(self, task: SlaveTask) -> SlaveResult:
        """Execute a single task (sync convenience method)."""
        results = self.execute_tasks([task])
        return results[0] if results else self._make_error_result(task, "No result returned")

    async def execute_single_async(self, task: SlaveTask) -> SlaveResult:
        """Execute a single task (async convenience method)."""
        results = await self.execute_tasks_async([task])
        return results[0] if results else self._make_error_result(task, "No result returned")