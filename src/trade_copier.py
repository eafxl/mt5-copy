"""Core trade copying logic with parallel async execution.

Uses LiveSettings for real-time setting updates without restart.
Static config (credentials, terminal paths) loaded at init.
"""
import asyncio
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .config import Config, SlaveConfig, get_filling_mode
from .daily_stops import DailyStopGuard
from .events import (
    emit_trade_opened,
    emit_trade_closed,
    emit_trade_modified,
    emit_trade_reduced,
    emit_profit_update,
    emit_error,
    emit_news_block,
    emit_daily_stop_limit,
)
from .live_settings import get_live_settings
from .news_calendar import NewsCalendar
from .slave_state import SlaveStatus, get_slave_state
from .master import MasterMonitor, PositionChange
from .mt5_client import MT5Client, Position
from .position_tracker import PositionTracker
from .parallel_worker import ParallelExecutor, SlaveTask, SlaveResult, TaskAction, _manage_terminal_windows
from .sync_approval import PositionInfo, show_sync_approval
from .utils.logging import get_logger
from .utils.tracer import get_tracer

# The daily loss limit lives in daily_stops.MAX_DAILY_STOPS now, and is counted
# from the master's own trade history rather than from a session variable — the
# old counter reset on restart, which is the one thing a person does after a
# run of stops. Closes, partial closes and SL/TP updates are still never gated:
# open risk has to stay manageable even once new copies have stopped.


class TradeCopier:
    """Orchestrates trade copying between master and slaves with parallel execution.

    Settings are read from LiveSettings on each operation, enabling live updates.
    Static config (credentials, terminals) is loaded once at init.
    """

    def __init__(
        self,
        config: Config,
        mt5_client: MT5Client,
        master: MasterMonitor,
        tracker: PositionTracker,
        embedded: bool = False,
    ):
        self.config = config
        self.mt5 = mt5_client
        self.master = master
        self.tracker = tracker
        self.embedded = embedded
        self.logger = get_logger()

        # Daily loss limit, counted from the master's own trade history so a
        # restart cannot clear it. Refreshed at startup and after every close.
        self.stops = DailyStopGuard(mt5_client)

        # Guard so only one profit refresh is in flight at a time
        self._profit_refresh_busy = False

        # Master tickets whose close has already moved the daily count. EA push
        # and polling both report every close, and re-reading the history twice
        # per close buys nothing. Bounded: a ticket closes once, and duplicate
        # reports arrive milliseconds apart, so only the recent ones matter.
        self._counted_closes: deque[int] = deque(maxlen=64)

        # News calendar. Set by the service (main.py) once the poll loop owns
        # one; None means the feature is off and the gate is a no-op.
        self.news: "NewsCalendar | None" = None

        # Default slave terminal (fallback if slave doesn't have own terminal)
        self.default_slave_terminal = config.slave_terminal.terminal_path

        # Initial cleanup set seeded from constructor-time config. The
        # authoritative set is rebuilt in shutdown() so slaves added after
        # startup AND the master are also killed.
        terminal_paths = self._collect_terminal_paths()

        # Parallel executor for slave operations
        enabled_count = sum(1 for s in config.slaves if s.enabled)
        self.executor = ParallelExecutor(
            max_workers=enabled_count or 4,
            task_timeout_seconds=60.0,
            terminal_paths=terminal_paths,
        )

        # Initialize LiveSettings from config
        settings = get_live_settings()
        settings.update_from_config(config)

    async def _run_blocking(self, func: Callable, *args):
        """Run a blocking function in executor to not block event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    async def refresh_slave_profits(self):
        """Emit each slave's REAL floating PnL, read from its own terminal.

        Replaces the old estimate (master_profit × volume ratio), which was
        only ever correct when a slave filled at the master's price with the
        same contract size and deposit currency — none of which holds across
        brokers. It also emitted nothing at all when the master ticket was
        missing from the last snapshot, leaving the row pinned at the 0.00 it
        was created with.

        One FETCH_POSITIONS round-trip per slave (positions_get is a local
        terminal cache — ~1ms, not an order round-trip). Runs off the copy
        path with at most one refresh in flight; slaves whose fetch fails fall
        back to the estimate so a single bad terminal doesn't blank the table.
        """
        if self._profit_refresh_busy:
            return  # previous refresh still running — skip this tick

        tracked_all = self.tracker.get_all_positions()
        if not tracked_all:
            return

        # Only ask slaves that actually hold a tracked position.
        holding: set[str] = set()
        for t in tracked_all.values():
            holding.update(t.slaves.keys())
        if not holding:
            return

        state = get_slave_state()
        slave_cfgs = [
            sc for sc in self.config.slaves
            if sc.slave_id in holding and state.is_copy_active(sc.slave_id)
        ]
        if not slave_cfgs:
            return

        self._profit_refresh_busy = True
        try:
            tasks = [self._build_fetch_positions_task(sc) for sc in slave_cfgs]
            results = await self._dispatch(tasks)

            # slave_id -> {slave_ticket: position dict}
            live: dict[str, dict[int, dict]] = {}
            for r in results:
                if not r.success:
                    self.logger.debug(
                        "[%s] Profit refresh failed: %s", r.slave_name, r.error
                    )
                    continue
                live[r.slave_id] = {
                    p["ticket"]: p for p in r.details.get("positions", [])
                }

            if not live:
                self.emit_profit_updates()  # every slave failed — fall back
                return

            for tracked in tracked_all.values():
                for slave_id, slave_pos in tracked.slaves.items():
                    by_ticket = live.get(slave_id)
                    if by_ticket is None:
                        continue  # this slave's fetch failed
                    real = by_ticket.get(slave_pos.ticket)
                    if real is None:
                        # Gone from the slave (its own SL/TP hit, or closed
                        # externally). The close path reports realized PnL.
                        continue

                    # Keep tracked volume honest — a scale-out done directly on
                    # the slave would otherwise skew nothing here, but does skew
                    # the estimate fallback and the close-toast volume.
                    vol = real.get("volume", slave_pos.volume)
                    if vol and vol != slave_pos.volume:
                        self.tracker.update_slave_volume(
                            tracked.master_ticket, slave_id, vol
                        )

                    sc = self._get_slave_config_by_id(slave_id)
                    emit_profit_update(
                        trade_id=tracked.trade_id,
                        slave_name=sc.name if sc else slave_id,
                        slave_ticket=slave_pos.ticket,
                        profit=round(real.get("profit", 0.0), 2),
                    )
        finally:
            self._profit_refresh_busy = False

    def emit_profit_updates(self):
        """Estimate slave PnL from the master's snapshot (fallback only).

        Slave PnL ≈ master_profit * (slave_volume / master_volume). Wrong
        whenever the slave's fill price, contract size or deposit currency
        differ from the master's — use ``refresh_slave_profits`` for the real
        number. Kept as the degraded path for when a slave terminal can't be
        reached.
        """
        master_positions = self.master.last_positions
        all_tracked = self.tracker.get_all_positions()

        for ticket, tracked in all_tracked.items():
            master_pos = master_positions.get(ticket)
            if master_pos is None:
                continue  # Position no longer on master

            for slave_id, slave_pos in tracked.slaves.items():
                if tracked.master_volume > 0:
                    ratio = slave_pos.volume / tracked.master_volume
                    estimated_profit = master_pos.profit * ratio
                else:
                    estimated_profit = 0.0

                # Resolve display name from config
                sc = self._get_slave_config_by_id(slave_id)
                display_name = sc.name if sc else slave_id

                emit_profit_update(
                    trade_id=tracked.trade_id,
                    slave_name=display_name,
                    slave_ticket=slave_pos.ticket,
                    profit=round(estimated_profit, 2),
                )

    def _collect_terminal_paths(self) -> set[str]:
        """Collect every terminal64.exe path under our control.

        Sources (union):
          - master.terminal_path (so master is killed on shutdown too)
          - Every slave.terminal_path in config (enabled OR not — slaves can
            be toggled at runtime, the binary on disk is the same either way)
          - Every `MetaTrader 5 *` subfolder under the locked slaves_dir
            (catches orphaned/legacy slave terminals not in current config)
        """
        from .template_manager import collect_slave_terminals_on_disk
        paths: set[str] = set()
        if self.config.master.terminal_path:
            paths.add(self.config.master.terminal_path)
        for s in self.config.slaves:
            tp = s.get_terminal_path(self.default_slave_terminal)
            if tp:
                paths.add(tp)
        paths |= collect_slave_terminals_on_disk()
        self.logger.info(
            "_collect_terminal_paths: %d path(s): %s", len(paths), sorted(paths)
        )
        return paths

    def shutdown(self):
        """Shutdown executor and cleanup resources.

        Rebuilds the kill set with the CURRENT config + disk scan so that
        master + post-startup slaves are also taken down.
        """
        self.logger.info("TradeCopier.shutdown: rebuilding kill set")
        self.executor._terminal_paths = self._collect_terminal_paths()
        self.executor.shutdown()
        self.logger.info("TradeCopier.shutdown: done")

    async def _dispatch(
        self,
        tasks: list[SlaveTask],
        on_result: Callable[[SlaveTask, SlaveResult], None] | None = None,
    ) -> list[SlaveResult]:
        """Dispatch slave tasks — concurrently by default (kills the per-slave
        queue latency where e.g. Bybit waited behind 5ers), or serially if
        ``settings.parallel_dispatch`` is off (ARM64/Parallels IPC-timeout
        safety). Warmup still uses _execute_sequential directly (cold spacing)."""
        if not tasks:
            return []
        if self.config.settings.parallel_dispatch:
            return await self.executor.execute_tasks_async(tasks, on_result=on_result)
        return await self._execute_sequential(tasks, on_result=on_result)

    async def _execute_sequential(
        self,
        tasks: list[SlaveTask],
        on_result: Callable[[SlaveTask, SlaveResult], None] | None = None,
        delay_between_s: float = 0.0,
    ) -> list[SlaveResult]:
        """Run slave tasks one at a time.

        Serializing slave operations avoids IPC / network / GIL contention
        on slow hosts (notably ARM64 Windows under Parallels, where two
        terminals talking to MT5 brokers in parallel reliably triggers
        (-10005, IPC timeout) on cold reconnects). After warmup the per-
        worker connection cache keeps each call's overhead in the low-100ms
        range, so the latency penalty for typical 2-4 slaves is small.

        `delay_between_s` adds a pause between tasks. Use ~2s for warmup
        (cold-start spacing) and 0 for steady-state ops (already cached).
        """
        if not tasks:
            self.logger.info("_execute_sequential: no tasks")
            return []
        self.logger.info(
            "_execute_sequential: %d task(s), delay=%ss, actions=%s",
            len(tasks), delay_between_s,
            [t.action.value if hasattr(t.action, "value") else str(t.action) for t in tasks],
        )
        import time as _time
        out: list[SlaveResult] = []
        for i, t in enumerate(tasks):
            if i > 0 and delay_between_s > 0:
                self.logger.info(
                    "_execute_sequential: sleeping %ss before next slave", delay_between_s
                )
                await asyncio.sleep(delay_between_s)
            t0 = _time.perf_counter()
            self.logger.info(
                "_execute_sequential[%d/%d]: dispatching %s on %s",
                i + 1, len(tasks),
                t.action.value if hasattr(t.action, "value") else t.action,
                t.slave_name,
            )
            batch = await self.executor.execute_tasks_async([t], on_result=on_result)
            elapsed = (_time.perf_counter() - t0) * 1000
            for r in batch:
                self.logger.info(
                    "_execute_sequential[%d/%d]: result slave=%s success=%s "
                    "ticket=%s duration=%.0fms wall=%.0fms err=%r",
                    i + 1, len(tasks), r.slave_name, r.success, r.ticket,
                    r.duration_ms, elapsed, r.error,
                )
            out.extend(batch)
        self.logger.info("_execute_sequential: done, %d result(s)", len(out))
        return out

    def _get_slave_config(self, slave_name: str) -> SlaveConfig | None:
        """Get slave static config by name."""
        for slave in self.config.slaves:
            if slave.name == slave_name:
                return slave
        return None

    def _get_slave_config_by_id(self, slave_id: str) -> SlaveConfig | None:
        """Get slave static config by slave_id (login@server)."""
        for slave in self.config.slaves:
            if slave.slave_id == slave_id:
                return slave
        return None

    @staticmethod
    def _elapsed_ms(recv_ms: float) -> float:
        """Copy latency in ms: signal-ingest (recv_ms, host clock) → now.

        Available for every op (open/close/modify/reduce) since recv_ms is
        stamped on ingest by both the EA bridge and the poll loop. 0 if unknown.
        """
        return round(time.time() * 1000.0 - recv_ms, 1) if recv_ms else 0.0

    def announce_stop_limit(self):
        """Report the trip once, so the UI can freeze its controls.

        Emitted once per trip (the guard remembers), because the limit is
        rediscovered on every blocked copy and every subsequent close, and a
        modal dialog per discovery would be unusable.

        The open-position count travels with it. Those positions carry their
        SL and TP at the broker already, so they close themselves; what stops
        is the copier mirroring any further change the master makes to them.
        Reported either way — it is the difference between a position that
        will be managed and one that is now on its own stops.
        """
        if not self.stops.announce_once():
            return
        open_positions = sum(
            len(tracked.slaves)
            for tracked in self.tracker.get_all_positions().values()
        )
        self.logger.error(
            "Daily stop limit: %s. No new copies for the rest of today. "
            "%d slave position(s) still open and still managed.",
            self.stops.describe(), open_positions,
        )
        emit_daily_stop_limit(
            count=self.stops.count or 0,
            limit=self.stops.limit,
            details=tuple(self.stops.describe_losses()),
            open_positions=open_positions,
        )

    async def _recount_daily_stops(self, master_ticket: int) -> bool:
        """Re-read the day's losses off the master and announce a trip.

        WHY EVERY CLOSE, NOT EVERY COPIED CLOSE
            This used to run only where slave positions were being closed —
            inside the tracked branch. A master position nobody mirrored (every
            slave skipped it, or it predates this session) returns early before
            reaching it, so its loss went uncounted until the next tracked close
            or the next restart. The limit exists to stop the day at the fourth
            loss; discovering the fourth loss an hour later, or after a trade it
            should have blocked, is the one failure mode that matters.

            The rule counts the MASTER's losses, and a master loss is a master
            loss whether or not anything mirrored it.

        Deduplicated by ticket because EA push and polling both report the same
        close, and each report would otherwise cost a history read.

        Returns whether the count was actually re-read.
        """
        if master_ticket in self._counted_closes:
            return False
        self._counted_closes.append(master_ticket)

        await self._run_blocking(self.stops.refresh)
        if self.stops.tripped:
            self.logger.error(
                "DAILY STOP LIMIT — %s. No further copies today.",
                self.stops.describe(),
            )
            self.announce_stop_limit()
        return True

    @staticmethod
    def _record_action(action: str, slave: str, outcome: str, **tags):
        """One trace record per slave per action — including the ones we DIDN'T do.

        WHY SKIPS ARE RECORDED AT ALL
            The worker writes a span for every task it executes, so the trace
            already explains what happened to the trades that were sent. It
            said nothing about the trades that were never sent: a slave paused
            in the state machine, a news window, a duplicate suppressed by the
            tracker, a slave that was Off at close time. Those are the cases
            the user actually asks about — "why did only two of three slaves
            take this trade" — and they were answerable only from log prose.

            Now every decision leaves the same shaped record under the trade's
            own trace_id, so the full fan-out of one master action is one
            filter: ``operation == "trade_action"``.

        ``outcome`` is a short verb: ``sent``, ``filled``, ``failed``,
        ``skipped``, ``simulated``, ``blocked``. ``reason`` says which flavour
        of skip.
        """
        get_tracer().event(
            "trade_action", action=action, slave=slave, outcome=outcome, **tags
        )

    def _build_open_task(
        self,
        slave_config: SlaveConfig,
        master_pos: Position,
        master_leverage: int,
        trade_id: str,
        source: str,
        recv_ms: float = 0.0,
    ) -> SlaveTask:
        """Build a SlaveTask for opening a position."""
        settings = get_live_settings()

        # Get LIVE settings for this slave (keyed by slave_id)
        sid = slave_config.slave_id
        slave_settings = settings.get_slave(sid)
        multiplier = slave_settings.multiplier if slave_settings else slave_config.multiplier
        adjust_by_leverage = slave_settings.adjust_by_leverage if slave_settings else slave_config.adjust_by_leverage
        fixed_volume = slave_settings.fixed_volume if slave_settings else slave_config.fixed_volume
        fixed_risk_usd = slave_settings.fixed_risk_usd if slave_settings else slave_config.fixed_risk_usd
        risk_mode = slave_settings.risk_mode if slave_settings else slave_config.risk_mode
        risk_pct = slave_settings.risk_pct if slave_settings else slave_config.risk_pct
        risk_pct_base = slave_settings.risk_pct_base if slave_settings else slave_config.risk_pct_base
        max_risk_pct = (
            slave_settings.max_risk_pct_per_trade if slave_settings
            else slave_config.max_risk_pct_per_trade
        )
        src = slave_settings or slave_config
        initial_balance = src.initial_balance
        dynamic_base = src.dynamic_base
        risk_tiers = [dict(t) for t in src.risk_tiers]

        # Normalize the master's open time to TRUE UTC using the master
        # terminal's measured server offset. MT5 times are server-local, so
        # each broker's epoch differs; the worker re-projects this UTC value
        # into the slave broker's clock via ITS own offset. 0 => unavailable
        # (worker then falls back to current price for SL anchoring).
        master_offset = self.mt5.server_time_offset(master_pos.symbol)
        if master_offset is not None and (master_pos.time_msc or master_pos.time):
            # Prefer ms precision (time_msc) for the latency waterfall.
            master_open_srv = (master_pos.time_msc / 1000.0) if master_pos.time_msc else float(master_pos.time)
            master_open_utc = master_open_srv - master_offset
        else:
            master_open_utc = 0.0

        # Map symbol using LIVE symbol mapping
        slave_symbol = settings.map_symbol_for_slave(sid, master_pos.symbol)

        # Get filling mode for this symbol
        filling = get_filling_mode(slave_symbol, settings.filling_mode)

        tracer = get_tracer()
        return SlaveTask(
            slave_id=sid,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.OPEN,
            params={
                "symbol": slave_symbol,
                "order_type": master_pos.type,
                "master_volume": master_pos.volume,
                "master_leverage": master_leverage,
                "multiplier": multiplier,  # LIVE
                "adjust_by_leverage": adjust_by_leverage,  # LIVE
                "fixed_volume": fixed_volume,  # LIVE
                "fixed_risk_usd": fixed_risk_usd,  # LIVE
                "risk_mode": risk_mode,  # LIVE
                "risk_pct": risk_pct,  # LIVE
                "risk_pct_base": risk_pct_base,  # LIVE
                "initial_balance": initial_balance,  # LIVE (dynamic_pct)
                "dynamic_base": dynamic_base,  # LIVE (dynamic_pct)
                "risk_tiers": risk_tiers,  # LIVE (dynamic_pct)
                "master_entry": master_pos.price_open,
                "master_current_price": master_pos.price_current,
                "master_sl": master_pos.sl,
                "master_tp": master_pos.tp,
                "master_time": master_pos.time,  # raw master-server epoch (diagnostics)
                "master_open_utc": master_open_utc,  # TRUE UTC of master open (copy-delay calc)
                "signal_source": source,  # "TCP" (EA push) or "Polling" — origin
                "recv_ms": recv_ms,  # host epoch ms when signal was ingested (latency waterfall)
                "is_buy": master_pos.is_buy,
                "deviation": settings.max_slippage,  # LIVE
                "magic": master_pos.ticket,
                "comment": f"CP:{trade_id}" if trade_id else f"Copy:{master_pos.ticket}",
                "max_entry_deviation_ratio": settings.max_entry_deviation_ratio,  # LIVE
                "max_risk_pct_per_trade": max_risk_pct,  # LIVE (per-slave)
                "debug_skip_execution": settings.debug_skip_execution,  # LIVE
                "filling_mode": filling,  # LIVE
            },
            retry_attempts=settings.retry_attempts,  # LIVE
            retry_delay_ms=settings.retry_delay_ms,  # LIVE
            trace_context=tracer.get_trace_context(),
        )

    def _build_close_task(
        self,
        slave_config: SlaveConfig,
        slave_ticket: int,
        symbol: str = "",
    ) -> SlaveTask:
        """Build a SlaveTask for closing a position."""
        settings = get_live_settings()
        tracer = get_tracer()
        sid = slave_config.slave_id

        # Get filling mode for this symbol
        filling_mode = None
        if symbol:
            slave_symbol = settings.map_symbol_for_slave(sid, symbol)
            filling_mode = get_filling_mode(slave_symbol, settings.filling_mode)

        return SlaveTask(
            slave_id=sid,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.CLOSE,
            params={
                "ticket": slave_ticket,
                "deviation": settings.max_slippage,  # LIVE
                "filling_mode": filling_mode,
            },
            retry_attempts=settings.retry_attempts,  # LIVE
            retry_delay_ms=settings.retry_delay_ms,  # LIVE
            trace_context=tracer.get_trace_context(),
        )

    def _build_reduce_task(
        self,
        slave_config: SlaveConfig,
        slave_ticket: int,
        keep_ratio: float,
        symbol: str = "",
    ) -> SlaveTask:
        """Build a SlaveTask for a partial close (scale-out) to keep_ratio."""
        settings = get_live_settings()
        tracer = get_tracer()
        sid = slave_config.slave_id
        filling_mode = None
        if symbol:
            slave_symbol = settings.map_symbol_for_slave(sid, symbol)
            filling_mode = get_filling_mode(slave_symbol, settings.filling_mode)
        return SlaveTask(
            slave_id=sid,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.REDUCE,
            params={
                "ticket": slave_ticket,
                "keep_ratio": keep_ratio,
                "deviation": settings.max_slippage,  # LIVE
                "filling_mode": filling_mode,
            },
            retry_attempts=settings.retry_attempts,  # LIVE
            retry_delay_ms=settings.retry_delay_ms,  # LIVE
            trace_context=tracer.get_trace_context(),
        )

    def _build_modify_task(
        self,
        slave_config: SlaveConfig,
        slave_ticket: int,
        master_pos: Position,
    ) -> SlaveTask:
        """Build a SlaveTask for modifying SL/TP."""
        settings = get_live_settings()
        tracer = get_tracer()

        return SlaveTask(
            slave_id=slave_config.slave_id,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.MODIFY,
            params={
                "ticket": slave_ticket,
                "master_entry": master_pos.price_open,
                "master_sl": master_pos.sl,
                "master_tp": master_pos.tp,
                "is_buy": master_pos.is_buy,
            },
            retry_attempts=settings.retry_attempts,  # LIVE
            retry_delay_ms=settings.retry_delay_ms,  # LIVE
            trace_context=tracer.get_trace_context(),
        )

    def _get_enabled_slave_configs(self) -> list[SlaveConfig]:
        """Get list of currently enabled slave configs (checks LiveSettings).

        Used by warmup, sync, and copy paths. Per-slave runtime state
        (On/Off/Paused) is checked separately in copy operations only,
        so that warmup can always start all config-enabled terminals.
        """
        settings = get_live_settings()
        enabled = []
        for slave in self.config.slaves:
            if settings.is_slave_enabled(slave.slave_id):
                enabled.append(slave)
        return enabled

    def build_terminal_state_task(self, slave_config: SlaveConfig) -> SlaveTask:
        """A cheap poll of one terminal's own state (Algo Trading, link).

        ``retry_attempts=1`` and no reconnect ambition: this is a status read
        for the Terminals tab, so a terminal that is down should report that
        immediately rather than spend the poll interval trying to raise it.
        Reviving it is the Play button's job.
        """
        return SlaveTask(
            slave_id=slave_config.slave_id,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.TERMINAL_STATE,
            params={},
            retry_attempts=1,
        )

    def _build_warmup_task(self, slave_config: SlaveConfig) -> SlaveTask:
        """Build a warmup task for pre-connecting a slave terminal.

        Uses retry_attempts=1 so the worker won't keep relaunching
        terminal64.exe if the user manually closes it during warmup.
        timeout_ms is bumped to 120s — cold-start of MT5 terminal on slow
        hardware (e.g. ARM64 Windows under Parallels) can exceed the 60s
        default, especially for unfamiliar brokers that need a config fetch.
        """
        return SlaveTask(
            slave_id=slave_config.slave_id,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.WARMUP,
            params={},
            retry_attempts=1,
            retry_delay_ms=1000,
            timeout_ms=120000,
            trace_context=get_tracer().get_trace_context(),
        )

    def _build_fetch_positions_task(self, slave_config: SlaveConfig) -> SlaveTask:
        """Build a task to fetch open positions from a slave terminal."""
        return SlaveTask(
            slave_id=slave_config.slave_id,
            slave_name=slave_config.name,
            terminal_path=slave_config.get_terminal_path(self.default_slave_terminal),
            login=slave_config.login,
            password=slave_config.password,
            server=slave_config.server,
            action=TaskAction.FETCH_POSITIONS,
            params={},
            retry_attempts=2,
            retry_delay_ms=1000,
            trace_context=get_tracer().get_trace_context(),
        )

    def emit_existing_positions(self):
        """Emit TRADE_OPENED events for all tracked positions so the UI can populate."""
        all_tracked = self.tracker.get_all_positions()
        for ticket, tracked in all_tracked.items():
            for slave_id, slave_pos in tracked.slaves.items():
                sc = self._get_slave_config_by_id(slave_id)
                display_name = sc.name if sc else slave_id
                emit_trade_opened(
                    trade_id=tracked.trade_id,
                    master_ticket=tracked.master_ticket,
                    slave_name=display_name,
                    slave_ticket=slave_pos.ticket,
                    symbol=tracked.symbol,
                    order_type=tracked.order_type,
                    volume=slave_pos.volume,
                    open_price=0.0,
                    open_time=0.0,
                    sl=slave_pos.sl,
                    tp=slave_pos.tp,
                    master_volume=tracked.master_volume,
                )

    def _ensure_slave_terminals(self):
        """Re-create slave terminal folders from the template if missing on disk.

        The source is `slave_terminal.template_path` — a virgin MT5 install
        seeded once via Settings. We never copy from the master folder
        because the master is logged-in / dirty.

        If the template is unconfigured / missing on disk, we both log and
        emit a UI error event so the user sees a banner — silent failures
        previously surfaced as cryptic IPC timeouts during warmup.
        """
        self.logger.info("_ensure_slave_terminals: starting")
        template_path = self.config.slave_terminal.template_path
        self.logger.info("_ensure_slave_terminals: template_path=%r", template_path)
        if not template_path:
            self.logger.warning(
                "Slave template not configured. Seed it from Settings → "
                "Slave Template. Missing slave folders will NOT be created."
            )
            emit_error(
                trade_id="",
                slave_name="(template)",
                error="Slave template not configured — open Settings → Slave Template → Seed Template.",
            )
            return
        template_folder = Path(template_path)
        if not template_folder.is_dir():
            self.logger.error(
                f"Template folder missing on disk: {template_folder}. "
                "Re-seed from Settings → Slave Template."
            )
            emit_error(
                trade_id="",
                slave_name="(template)",
                error=f"Slave template folder missing: {template_folder}. Re-seed from Settings.",
            )
            return

        # Slaves directory is locked (see template_manager.slaves_dir).
        from .template_manager import slaves_dir as _slaves_dir_fn
        slaves_dir = _slaves_dir_fn()
        self.logger.info(
            "_ensure_slave_terminals: template_folder=%s slaves_dir=%s",
            template_folder, slaves_dir,
        )

        for slave in self._get_enabled_slave_configs():
            tp = slave.get_terminal_path(self.default_slave_terminal)
            if tp and Path(tp).exists():
                self.logger.info(
                    "[%s] terminal already at %s — skipping create", slave.name, tp
                )
                continue

            slave_folder = slaves_dir / f"MetaTrader 5 {slave.name}"
            self.logger.info(
                "[%s] need terminal folder; candidate=%s", slave.name, slave_folder
            )
            if slave_folder.exists():
                terminal_exe = slave_folder / "terminal64.exe"
                if not terminal_exe.exists():
                    terminal_exe = slave_folder / "terminal.exe"
                if terminal_exe.exists():
                    slave.terminal_path = str(terminal_exe)
                    self.logger.info(f"[{slave.name}] Found existing terminal: {terminal_exe}")
                else:
                    self.logger.warning(
                        f"[{slave.name}] Folder exists but no terminal exe: {slave_folder}"
                    )
                continue

            try:
                slaves_dir.mkdir(parents=True, exist_ok=True)
                self.logger.info(
                    "[%s] copytree %s → %s", slave.name, template_folder, slave_folder
                )
                shutil.copytree(template_folder, slave_folder)
                terminal_exe = slave_folder / "terminal64.exe"
                if not terminal_exe.exists():
                    terminal_exe = slave_folder / "terminal.exe"
                if terminal_exe.exists():
                    slave.terminal_path = str(terminal_exe)
                    self.logger.info(
                        f"[{slave.name}] Created terminal folder from template: {slave_folder}"
                    )
                else:
                    self.logger.warning(
                        f"[{slave.name}] Copied template but terminal exe not found"
                    )
            except PermissionError:
                self.logger.error(
                    f"[{slave.name}] Permission denied copying to: {slave_folder}. "
                    f"Change Slaves Directory in Settings to a writable folder, "
                    f"or relaunch as Administrator."
                )
            except Exception as e:
                self.logger.exception(f"[{slave.name}] Failed to create terminal folder: {e}")
        self.logger.info("_ensure_slave_terminals: done")

    async def warmup_slaves(self) -> tuple[int, int]:
        """Pre-connect all enabled slave terminals to eliminate first-trade delay.

        Returns (success_count, fail_count).
        """
        self.logger.info("warmup_slaves: invoking _ensure_slave_terminals")
        await self._run_blocking(self._ensure_slave_terminals)
        enabled_slaves = self._get_enabled_slave_configs()
        self.logger.info(
            "warmup_slaves: %d enabled slave(s): %s",
            len(enabled_slaves), [s.name for s in enabled_slaves],
        )
        if not enabled_slaves:
            self.logger.info("No enabled slaves to warm up")
            return 0, 0

        self.logger.info(f"Warming up {len(enabled_slaves)} slave terminal(s)...")

        # Warm up all terminals. Parallel by default: each slave has its own
        # dedicated worker process + terminal, so cold connects overlap and
        # total time collapses from N×(connect+gap) to ~one connect. Falls back
        # to serial with a 2s cold-start gap when parallel_warmup is off (ARM64/
        # Parallels IPC-timeout safety).
        tasks = [self._build_warmup_task(sc) for sc in enabled_slaves]
        if self.config.settings.parallel_warmup:
            self.logger.info("warmup_slaves: parallel warmup of %d terminal(s)", len(tasks))
            results = await self.executor.execute_tasks_async(tasks)
        else:
            results = await self._execute_sequential(tasks, delay_between_s=2.0)

        settings = get_live_settings()
        success_count = 0
        fail_count = 0
        for result in results:
            if result.success:
                success_count += 1
                self.logger.info(
                    f"[{result.slave_name}] Warmup OK "
                    f"(leverage={result.details.get('leverage')}, {result.duration_ms:.0f}ms)"
                )
                # Cache broker symbols in memory for mapping editor (keyed by slave_id)
                symbols = result.details.get("symbols", [])
                if symbols:
                    settings.set_slave_symbols(result.slave_id, symbols)
                    self.logger.info(
                        f"[{result.slave_name}] Cached {len(symbols)} broker symbols"
                    )
            else:
                fail_count += 1
                self.logger.warning(
                    f"[{result.slave_name}] Warmup failed: {result.error}"
                )

        # Reconnect master after warmup (in case MT5 singleton was affected)
        await self._run_blocking(self.master.reconnect)

        # Manage terminal windows (maximize master, minimize slaves)
        _manage_terminal_windows(
            master_path=self.config.master.terminal_path,
            slave_paths={s.get_terminal_path(self.default_slave_terminal)
                         for s in enabled_slaves if s.get_terminal_path(self.default_slave_terminal)},
            maximize_master=self.config.settings.maximize_master_on_start,
        )

        return success_count, fail_count

    async def _discover_existing_positions(
        self,
        master_positions: list[Position],
        enabled_slaves: list[SlaveConfig],
    ) -> set[int]:
        """Discover existing slave positions that match master positions.

        Fetches open positions from each slave via FETCH_POSITIONS tasks,
        matches them to master positions by magic == master_ticket,
        and adds matched pairs to the in-memory tracker.

        Returns set of master tickets that are fully synced (no copy needed).
        """
        if not enabled_slaves:
            return set()

        # Build master ticket lookup
        master_by_ticket = {pos.ticket: pos for pos in master_positions}

        # Fetch positions from each slave sequentially (see _execute_sequential).
        tasks = [self._build_fetch_positions_task(sc) for sc in enabled_slaves]
        results = await self._execute_sequential(tasks)

        # slave_id -> list of position dicts
        slave_positions: dict[str, list[dict]] = {}
        for result in results:
            if result.success:
                slave_positions[result.slave_id] = result.details.get("positions", [])
                self.logger.info(
                    f"[{result.slave_name}] Discovered {len(slave_positions[result.slave_id])} open positions"
                )
            else:
                self.logger.warning(
                    f"[{result.slave_name}] Failed to fetch positions: {result.error}"
                )

        # Match slave positions to master positions by magic == master_ticket
        discovered_count = 0
        for slave_config in enabled_slaves:
            sid = slave_config.slave_id
            positions = slave_positions.get(sid, [])
            for sp in positions:
                magic = sp.get("magic", 0)
                if magic in master_by_ticket:
                    master_pos = master_by_ticket[magic]

                    # Ensure master is tracked
                    if not self.tracker.is_tracked(master_pos.ticket):
                        self.tracker.add_position(
                            master_ticket=master_pos.ticket,
                            symbol=master_pos.symbol,
                            order_type=master_pos.type,
                            master_volume=master_pos.volume,
                            master_sl=master_pos.sl,
                            master_tp=master_pos.tp,
                        )

                    # Add slave position to tracker (keyed by slave_id)
                    self.tracker.add_slave_position(
                        master_ticket=master_pos.ticket,
                        slave_id=sid,
                        slave_ticket=sp["ticket"],
                        volume=sp["volume"],
                        sl=sp.get("sl", 0.0),
                        tp=sp.get("tp", 0.0),
                    )
                    discovered_count += 1
                    self.logger.info(
                        f"[{slave_config.name}] Matched slave #{sp['ticket']} "
                        f"to master #{magic} ({sp['symbol']})"
                    )

        self.logger.info(f"Discovery complete: {discovered_count} existing position pair(s) found")

        # Return master tickets that are fully synced to all enabled slaves
        fully_synced = set()
        for pos in master_positions:
            all_synced = all(
                self.tracker.has_slave_position(pos.ticket, sc.slave_id)
                for sc in enabled_slaves
            )
            if all_synced:
                fully_synced.add(pos.ticket)

        return fully_synced

    async def sync_initial(self):
        """
        Perform initial sync of existing master positions to all slaves.

        Discovers existing slave positions from the API first (tracker starts empty),
        then shows approval dialog for unsynced positions before copying.
        """
        settings = get_live_settings()

        # Check sync_existing_on_start from original config (not live-updatable)
        if not self.config.settings.sync_existing_on_start:
            self.logger.info("Initial sync disabled in config")
            return

        self.logger.info("Starting initial position sync...")

        # Run blocking MT5 calls in executor
        master_positions = await self._run_blocking(self.master.get_all_positions)
        master_leverage = await self._run_blocking(self.master.get_leverage)

        self.logger.info(f"Master has {len(master_positions)} open positions")

        if not master_positions:
            self.logger.info("No positions to sync")
            await self._run_blocking(self.master.snapshot_positions)
            return

        # Get enabled slaves from LiveSettings
        enabled_slaves = self._get_enabled_slave_configs()

        # Discover existing slave positions from API (tracker starts empty)
        fully_synced = await self._discover_existing_positions(
            master_positions, enabled_slaves
        )
        self.logger.info(f"{len(fully_synced)} master position(s) already fully synced")

        # Reconnect master after discovery (slave connections may have disrupted it)
        await self._run_blocking(self.master.reconnect)

        # Identify positions that still need syncing
        positions_needing_sync = [
            pos for pos in master_positions if pos.ticket not in fully_synced
        ]

        if not positions_needing_sync:
            self.logger.info("All positions already synced")
            await self._run_blocking(self.master.snapshot_positions)
            return

        # Build position info for approval dialog (MT5 calls in executor)
        position_infos = []
        for pos in positions_needing_sync:
            bid, ask = await self._run_blocking(self.mt5.get_current_price, pos.symbol)
            current_price = ask if pos.is_buy else bid

            position_infos.append(PositionInfo(
                ticket=pos.ticket,
                symbol=pos.symbol,
                side="BUY" if pos.is_buy else "SELL",
                volume=pos.volume,
                entry_price=pos.price_open,
                current_price=current_price,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
            ))

        # In embedded mode (GUI), auto-approve all positions
        # (the sync approval dialog cannot run from a background thread)
        if self.embedded:
            approved_tickets = {p.ticket for p in position_infos}
            self.logger.info(f"Embedded mode: auto-approved {len(approved_tickets)} positions")
        else:
            # Show approval dialog (blocking PyQt - run in executor)
            self.logger.info(f"Showing approval dialog for {len(position_infos)} positions...")
            approved_tickets = await self._run_blocking(show_sync_approval, position_infos)

        if not approved_tickets:
            self.logger.info("No positions approved for sync")
            await self._run_blocking(self.master.snapshot_positions)
            return

        self.logger.info(f"User approved {len(approved_tickets)} positions for sync")

        # Filter to only approved positions
        approved_positions = [p for p in positions_needing_sync if p.ticket in approved_tickets]

        # Update tracker with approved master positions
        for pos in approved_positions:
            if not self.tracker.is_tracked(pos.ticket):
                self.tracker.add_position(
                    master_ticket=pos.ticket,
                    symbol=pos.symbol,
                    order_type=pos.type,
                    master_volume=pos.volume,
                    master_sl=pos.sl,
                    master_tp=pos.tp,
                )

        # Sync approved positions to all slaves in parallel (async)
        await self._sync_positions_parallel(approved_positions, master_leverage)

        # Reconnect to master FIRST, then take snapshot
        await self._run_blocking(self.master.reconnect)
        await self._run_blocking(self.master.snapshot_positions)

        self.logger.info("Initial sync completed")

    async def _sync_positions_parallel(
        self,
        master_positions: list[Position],
        master_leverage: int,
    ):
        """Sync positions to all slaves in parallel (async)."""
        tracer = get_tracer()

        # Get enabled slaves from LiveSettings
        enabled_slaves = self._get_enabled_slave_configs()
        state = get_slave_state()

        # Build tasks for all slave+position combinations
        tasks: list[SlaveTask] = []
        task_metadata: dict[tuple[str, int], tuple[int, str, str]] = {}

        for master_pos in master_positions:
            trade_id = self.tracker.get_trade_id(master_pos.ticket) or ""

            for slave_config in enabled_slaves:
                sid = slave_config.slave_id
                # Skip if slave is paused/off in runtime state machine
                if not state.is_copy_active(sid):
                    continue

                # Skip if already copied to this slave
                if self.tracker.has_slave_position(master_pos.ticket, sid):
                    self.logger.debug(
                        f"Position {master_pos.ticket} already synced to {slave_config.name}"
                    )
                    continue

                task = self._build_open_task(slave_config, master_pos, master_leverage, trade_id, "Polling")
                tasks.append(task)
                task_metadata[(sid, master_pos.ticket)] = (master_pos.ticket, sid, trade_id)

        if not tasks:
            self.logger.info("No positions to sync")
            return

        self.logger.info(f"Syncing {len(tasks)} position(s) to slaves in parallel...")

        with tracer.trace("sync_positions_parallel") as trace:
            trace.set_tag("task_count", len(tasks))

            # Callback to process results as they arrive
            success_count = 0

            def on_result(task: SlaveTask, result: SlaveResult):
                nonlocal success_count
                master_ticket = task.params["magic"]
                _, sid, trade_id = task_metadata[(task.slave_id, master_ticket)]

                if result.success and result.ticket:
                    self.tracker.add_slave_position(
                        master_ticket=master_ticket,
                        slave_id=sid,
                        slave_ticket=result.ticket,
                        volume=result.volume,
                        sl=result.details.get("slave_sl", 0.0),
                        tp=result.details.get("slave_tp", 0.0),
                    )
                    self.logger.info(
                        f"[{task.slave_name}] Synced position {master_ticket} -> {result.ticket} "
                        f"({result.volume} lots, {result.duration_ms:.0f}ms)"
                    )
                    success_count += 1
                elif result.details.get("simulated"):
                    self.logger.info(
                        f"[{task.slave_name}] DEBUG: would sync {master_ticket} "
                        f"({result.details.get('requested_volume', 0.0)} lots) — execution skipped"
                    )
                elif result.details.get("skipped_by_risk_guard"):
                    self.logger.warning(
                        f"[{task.slave_name}] Risk guard blocked {master_ticket}: {result.error}"
                    )
                elif result.details.get("skipped_by_deviation"):
                    self.logger.warning(
                        f"[{task.slave_name}] Skipped {master_ticket}: {result.error}"
                    )
                else:
                    self.logger.error(
                        f"[{task.slave_name}] Failed to sync {master_ticket}: {result.error}"
                    )

            # Dispatch to slaves (parallel by default — see _dispatch / config.parallel_dispatch).
            await self._dispatch(tasks, on_result=on_result)

            trace.set_tags(success_count=success_count, failed_count=len(tasks) - success_count)

    async def process_changes(self, changes: PositionChange):
        """Process detected position changes (async).

        Filters out self-copy positions: when a slave shares the same MT5
        account as the master (configuration mistake / same broker login),
        the slave's freshly opened ticket shows up in master's positions_get
        on the very next poll. Without this filter we'd copy our own copies
        in an infinite loop. The slave's magic is the master_ticket we set,
        so any "new" master position whose magic matches a tracked master
        ticket is unambiguously our own copy reflected back.
        """
        if changes.new_positions:
            tracked = self.tracker.get_master_tickets()
            filtered = []
            self_copies = []
            for p in changes.new_positions:
                if p.magic and p.magic in tracked:
                    self_copies.append(p)
                else:
                    filtered.append(p)
            if self_copies:
                self.logger.warning(
                    "Skipping %d self-copy position(s) reflected from a slave "
                    "that shares the master account: %s. Tracked master "
                    "tickets they map to: %s. Check Settings — at least one "
                    "slave appears to use the same broker login as master.",
                    len(self_copies),
                    [p.ticket for p in self_copies],
                    [p.magic for p in self_copies],
                )
                emit_error(
                    trade_id="",
                    slave_name="(self-copy)",
                    error=(
                        f"Detected {len(self_copies)} self-copy position(s). "
                        "A slave shares the master's account. Fix Settings."
                    ),
                )
            changes.new_positions = filtered

        master_leverage = await self._run_blocking(self.master.get_leverage)

        # Process all change types concurrently where possible
        tasks = []

        # Handle new positions
        for pos in changes.new_positions:
            tasks.append(self._copy_new_position(
                pos, master_leverage, changes.source, changes.recv_ms,
                changes.signal_id,
            ))

        # Handle closed positions
        for ticket in changes.closed_tickets:
            tasks.append(self._close_copied_positions(
                ticket, changes.source, changes.recv_ms, changes.signal_id,
            ))

        # Handle modified positions
        for pos in changes.modified_positions:
            tasks.append(self._update_sl_tp(
                pos, changes.source, changes.recv_ms, changes.signal_id,
            ))

        # Handle reduced positions (master scaled out / partial take-profit)
        for pos in changes.reduced_positions:
            tasks.append(self._reduce_copied_positions(
                pos, changes.source, changes.recv_ms, changes.signal_id,
            ))

        # Execute all change handlers concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _news_block(self, slave_id: str):
        """``(event, window_seconds)`` if a news window is open for this slave.

        Reads LIVE windows so a change in the Rules tab applies to the very next
        trade. No I/O — the calendar is an in-memory list refreshed elsewhere.
        """
        if self.news is None:
            return None
        settings = get_live_settings()
        slave_settings = settings.get_slave(slave_id)
        if slave_settings is None:
            return None
        windows = dict(slave_settings.news_windows_s)
        if not windows:
            return None
        # Returns the window that applied, so the toast can quote it. The
        # currency filter is per slave: one trading NAS100 has no reason to
        # stop for Australian retail sales.
        return self.news.blocked_for(
            windows, currencies=getattr(slave_settings, "news_currencies", ())
        )

    async def _copy_new_position(
        self, master_pos: Position, master_leverage: int, source: str,
        recv_ms: float = 0.0, signal_id: str = "",
    ):
        """Copy a new position to all slaves in parallel (async)."""
        # Daily loss limit. Checked before claiming the ticket so the tracker
        # stays clean, and read from the broker's history rather than a session
        # counter — see daily_stops. A trip also asks the app to shut down.
        if self.stops.tripped:
            self.logger.warning(
                "Daily stop limit reached (%s) — NOT copying %s ticket %s",
                self.stops.describe(), master_pos.symbol, master_pos.ticket,
            )
            self._record_action(
                "open", "(all slaves)", "blocked", reason="daily_stop_limit",
                master_ticket=master_pos.ticket, symbol=master_pos.symbol,
                stops=self.stops.count, limit=self.stops.limit,
            )
            self.announce_stop_limit()
            return

        # Atomically claim this master ticket. If it was already claimed —
        # by initial sync OR by the OTHER signal source (EA push vs polling) —
        # we do NOT copy again. This is the single guard that prevents the
        # hybrid EA+poll paths from ever copying the same trade twice.
        tracked, is_new = self.tracker.claim_position(
            master_ticket=master_pos.ticket,
            symbol=master_pos.symbol,
            order_type=master_pos.type,
            master_volume=master_pos.volume,
            master_sl=master_pos.sl,
            master_tp=master_pos.tp,
        )
        if not is_new:
            self.logger.debug(
                f"Position {master_pos.ticket} already claimed ({source} dedup), skipping"
            )
            return

        tracer = get_tracer()
        side = "BUY" if master_pos.is_buy else "SELL"

        with tracer.trace("copy_new_position", trade_id=tracked.trade_id) as trace:
            trace.set_tags(
                master_ticket=master_pos.ticket,
                symbol=master_pos.symbol,
                side=side,
                volume=master_pos.volume,
                sl=master_pos.sl,
                tp=master_pos.tp,
                source=source,
                # The wire line this came from. With it, one grep of the trace
                # file walks a filled slave order back to the exact EA message
                # or poll cycle that triggered it.
                signal_id=signal_id,
                recv_ms=round(recv_ms, 1) if recv_ms else 0.0,
            )

            self.logger.info(
                f"New position detected: {master_pos.symbol} {side} "
                f"{master_pos.volume} lots (ticket: {master_pos.ticket}, trade_id: {tracked.trade_id})"
            )

            # Get enabled slaves from LiveSettings (checked at copy time)
            enabled_slaves = self._get_enabled_slave_configs()
            state = get_slave_state()

            # Build tasks for all enabled slaves
            tasks: list[SlaveTask] = []

            for slave_config in enabled_slaves:
                sid = slave_config.slave_id
                # Skip if slave is paused/off in runtime state machine
                if not state.is_copy_active(sid):
                    self.logger.debug(
                        f"[{slave_config.name}] Skipped (paused/off)"
                    )
                    self._record_action(
                        "open", slave_config.name, "skipped",
                        reason="paused_or_off",
                        state=state.get_status(sid).value,
                        master_ticket=master_pos.ticket,
                    )
                    continue

                # News window: a scheduled print is inside this slave's block
                # window, so the copy is skipped — permanently, not queued. By
                # the time the window closes the master's entry is stale and
                # copying it would just chase price. Exits are never gated;
                # see _close_copied_positions.
                blocking = self._news_block(sid)
                if blocking is not None:
                    event, window_s = blocking
                    reason = event.describe()
                    self.logger.warning(
                        "[%s] Skipped %s — news window: %s (±%gs)",
                        slave_config.name, master_pos.symbol, reason, window_s,
                    )
                    trace.set_tag("news_blocked", reason)
                    self._record_action(
                        "open", slave_config.name, "blocked",
                        reason="news", event=reason, window_s=window_s,
                        master_ticket=master_pos.ticket,
                    )
                    emit_news_block(
                        slave_name=slave_config.name,
                        symbol=master_pos.symbol,
                        reason=reason,
                        window_s=window_s,
                        master_ticket=master_pos.ticket,
                    )
                    continue

                # Skip if already copied to this slave (prevents double execution)
                if self.tracker.has_slave_position(master_pos.ticket, sid):
                    self.logger.debug(
                        f"Position {master_pos.ticket} already copied to {slave_config.name}, skipping"
                    )
                    self._record_action(
                        "open", slave_config.name, "skipped",
                        reason="already_copied", master_ticket=master_pos.ticket,
                    )
                    continue

                task = self._build_open_task(
                    slave_config, master_pos, master_leverage, tracked.trade_id, source, recv_ms
                )
                tasks.append(task)

            if not tasks:
                state_snapshot = {n: s.value for n, s in state.get_all().items()}
                enabled_names = [s.name for s in enabled_slaves]
                self.logger.warning(
                    f"No eligible slaves for position {master_pos.ticket} "
                    f"(enabled={enabled_names}, states={state_snapshot})"
                )
                trace.set_tag("slaves_copied", 0)
                return

            trace.set_tag("slave_tasks", len(tasks))

            # Callback to process results as they arrive
            slaves_copied = 0

            def on_result(task: SlaveTask, result: SlaveResult):
                nonlocal slaves_copied

                if result.success and result.ticket:
                    self.tracker.add_slave_position(
                        master_ticket=master_pos.ticket,
                        slave_id=task.slave_id,
                        slave_ticket=result.ticket,
                        volume=result.volume,
                        sl=result.details.get("slave_sl", 0.0),
                        tp=result.details.get("slave_tp", 0.0),
                    )
                    self.logger.info(
                        f"[LIFECYCLE] OPEN master={master_pos.ticket} -> "
                        f"slave={result.ticket} on {task.slave_name} "
                        f"(trade_id={tracked.trade_id})"
                    )
                    slaves_copied += 1
                    self._record_action(
                        "open", task.slave_name, "filled",
                        master_ticket=master_pos.ticket,
                        slave_ticket=result.ticket,
                        symbol=task.params.get("symbol"),
                        volume=result.volume,
                        duration_ms=round(result.duration_ms, 1),
                        retries=result.retries_used,
                    )

                    open_price = result.details.get("open_price", master_pos.price_open)
                    open_time = result.details.get("open_time", 0.0)
                    slave_sl = result.details.get("slave_sl", 0.0)
                    slave_tp = result.details.get("slave_tp", 0.0)

                    # Waterfall legs (clamp ≥0 — the ping leg goes negative when
                    # the master server-time offset over-corrects). Head total is
                    # the leg sum, NOT copy_delay_ms, which is offset-dependent
                    # and can be negative. Falls back to ingest→now if no legs.
                    _local = max(0.0, result.details.get("recv_to_calc_ms", 0.0))
                    _ping = max(0.0, result.details.get("master_to_recv_ms", 0.0))
                    _broker = max(0.0, result.details.get("send_to_fill_ms", 0.0))
                    _total = (_local + _ping + _broker) or self._elapsed_ms(recv_ms)

                    # Fire-and-forget UI event (never blocks) — uses display name
                    emit_trade_opened(
                        trade_id=tracked.trade_id,
                        master_ticket=master_pos.ticket,
                        slave_name=task.slave_name,
                        slave_ticket=result.ticket,
                        symbol=master_pos.symbol,
                        order_type=master_pos.type,
                        volume=result.volume,
                        open_price=open_price,
                        open_time=open_time,
                        sl=slave_sl,
                        tp=slave_tp,
                        master_volume=master_pos.volume,
                        transport=source,
                        delay_ms=_total,
                        local_ms=_local,
                        ping_ms=_ping,
                        broker_ms=_broker,
                    )

                elif result.details.get("simulated"):
                    self.logger.info(
                        f"[{task.slave_name}] DEBUG: would copy {master_pos.ticket} "
                        f"({result.details.get('requested_volume', 0.0)} lots) — execution skipped"
                    )
                    self._record_action(
                        "open", task.slave_name, "simulated",
                        master_ticket=master_pos.ticket,
                        volume=result.details.get("requested_volume", 0.0),
                    )
                elif result.details.get("skipped_by_risk_guard"):
                    self.logger.warning(
                        f"[{task.slave_name}] Risk guard blocked: {result.error}"
                    )
                    self._record_action(
                        "open", task.slave_name, "blocked", reason="risk_guard",
                        master_ticket=master_pos.ticket, error=result.error,
                    )
                elif result.details.get("skipped_by_deviation"):
                    self.logger.warning(
                        f"[{task.slave_name}] Skipped due to entry deviation: {result.error}"
                    )
                    self._record_action(
                        "open", task.slave_name, "blocked", reason="entry_deviation",
                        master_ticket=master_pos.ticket, error=result.error,
                    )
                else:
                    self.logger.error(
                        f"[{task.slave_name}] Failed to copy: {result.error}"
                    )
                    self._record_action(
                        "open", task.slave_name, "failed",
                        master_ticket=master_pos.ticket, error=result.error,
                        retries=result.retries_used,
                        duration_ms=round(result.duration_ms, 1),
                    )
                    # Fire-and-forget error event — uses display name
                    slave_symbol = task.params.get("symbol", master_pos.symbol)
                    emit_error(
                        trade_id=tracked.trade_id,
                        slave_name=task.slave_name,
                        error=result.error,
                        master_ticket=master_pos.ticket,
                        symbol=master_pos.symbol,
                        symbol_mapped=slave_symbol,
                    )

            # Dispatch to slaves (parallel by default — see _dispatch / config.parallel_dispatch).
            await self._dispatch(tasks, on_result=on_result)

            trace.set_tag("slaves_copied", slaves_copied)

            # Reconnect to master
            await self._run_blocking(self.master.reconnect)

    async def _close_copied_positions(
        self, master_ticket: int, source: str, recv_ms: float = 0.0,
        signal_id: str = "",
    ):
        """Close all slave positions for a closed master position in parallel (async).

        The ticket is claimed atomically up front, so exactly one of the EA-push
        and polling paths runs the close. Both used to get through, emitting two
        TRADE_CLOSED events whose profits disagreed (the second read realized PnL
        from deal history after the first had already closed the position) and
        counting one stopped-out trade twice.
        """
        tracked = self.tracker.claim_close(master_ticket)
        if tracked is None:
            # Nothing of ours to close — but the master still closed something,
            # and if it closed at a loss the day's count just moved. See
            # _recount_daily_stops for why this cannot wait for a copied close.
            await self._recount_daily_stops(master_ticket)
            return

        tracer = get_tracer()
        with tracer.trace("close_position", trade_id=tracked.trade_id) as trace:
            trace.set_tags(
                master_ticket=master_ticket,
                symbol=tracked.symbol,
                source=source,
                signal_id=signal_id,
                recv_ms=round(recv_ms, 1) if recv_ms else 0.0,
                slaves_holding=len(tracked.slaves),
            )

            self.logger.info(
                f"[LIFECYCLE] CLOSE master={master_ticket} -> "
                f"{len(tracked.slaves)} slaves (trade_id={tracked.trade_id})"
            )

            # Build close tasks for all slaves that have this position
            # Use original config for credentials, but check if slave still exists
            tasks: list[SlaveTask] = []
            state = get_slave_state()

            # Slaves we deliberately won't send a close to. claim_close has
            # already dropped this ticket, and the master ticket is gone from
            # its terminal, so nothing will ever revisit them — they must be
            # reported, not silently forgotten.
            unreachable: list[SlaveConfig] = []

            for slave_config in self.config.slaves:
                sid = slave_config.slave_id
                if sid not in tracked.slaves:
                    continue

                # PAUSED stops NEW copies; it must never block an EXIT. A paused
                # slave holding a live position while the master is flat is
                # unhedged risk the user did not ask for. Only OFF (terminal not
                # running) genuinely cannot be closed.
                if state.get_status(sid) == SlaveStatus.OFF:
                    self.logger.error(
                        f"[{slave_config.name}] Cannot close ticket "
                        f"{tracked.slaves[sid].ticket} — slave is Off. "
                        f"Position is left OPEN and is no longer tracked."
                    )
                    unreachable.append(slave_config)
                    self._record_action(
                        "close", slave_config.name, "skipped",
                        reason="slave_off", master_ticket=master_ticket,
                        slave_ticket=tracked.slaves[sid].ticket,
                    )
                    continue

                slave_ticket = tracked.slaves[sid].ticket
                task = self._build_close_task(slave_config, slave_ticket, tracked.symbol)
                tasks.append(task)

            for slave_config in unreachable:
                sid = slave_config.slave_id
                slave_vol = tracked.slaves.get(sid)
                emit_error(
                    trade_id=tracked.trade_id,
                    slave_name=slave_config.name,
                    error=(
                        "Slave is Off — its position was NOT closed and is no "
                        "longer tracked. Close it manually in the terminal."
                    ),
                    master_ticket=master_ticket,
                    symbol=tracked.symbol,
                )
                # Move the row out of Active Trades regardless: the copier has
                # stopped managing it, so showing it as an active copy is a lie.
                # The paired error above says what actually happened.
                emit_trade_closed(
                    trade_id=tracked.trade_id,
                    master_ticket=master_ticket,
                    slave_name=slave_config.name,
                    slave_ticket=slave_vol.ticket if slave_vol else 0,
                    profit=0.0,
                    symbol=tracked.symbol,
                    order_type=tracked.order_type,
                    volume=slave_vol.volume if slave_vol else 0.0,
                    transport=source,
                    delay_ms=self._elapsed_ms(recv_ms),
                )

            if not tasks:
                trace.set_tags(slaves_closed=0, unreachable=len(unreachable))
                return  # already untracked by claim_close

            trace.set_tags(slave_tasks=len(tasks), unreachable=len(unreachable))

            # Callback to process results as they arrive
            slaves_closed = 0
            worst_profit = 0.0

            def on_result(task: SlaveTask, result: SlaveResult):
                nonlocal slaves_closed, worst_profit
                slave_vol = tracked.slaves.get(task.slave_id)
                already_closed = result.details.get("already_closed", False)

                if result.success:
                    self.logger.info(
                        f"[LIFECYCLE] CLOSE_RESULT slave={result.ticket} on "
                        f"{task.slave_name}: success (already_closed={already_closed})"
                    )
                    slaves_closed += 1

                    trade_profit = result.details.get("profit", 0.0)
                    self._record_action(
                        "close", task.slave_name,
                        "already_closed" if already_closed else "closed",
                        master_ticket=master_ticket,
                        slave_ticket=result.ticket,
                        symbol=tracked.symbol,
                        profit=trade_profit,
                        duration_ms=round(result.duration_ms, 1),
                    )
                    worst_profit = min(worst_profit, trade_profit)

                    slave_sl = slave_vol.sl if slave_vol else 0.0
                    if trade_profit == 0.0 and slave_sl != 0.0:
                        self.logger.error(
                            f"[{task.slave_name}] PnL=0.00 but SL was set "
                            f"(sl={slave_sl:.5f}, ticket={result.ticket}) — "
                            f"history lookup may have failed"
                        )

                    # Fire-and-forget UI event (never blocks) — uses display name
                    emit_trade_closed(
                        trade_id=tracked.trade_id,
                        master_ticket=master_ticket,
                        slave_name=task.slave_name,
                        slave_ticket=result.ticket,
                        profit=trade_profit,
                        symbol=tracked.symbol,
                        order_type=tracked.order_type,
                        volume=slave_vol.volume if slave_vol else 0.0,
                        transport=source,
                        delay_ms=self._elapsed_ms(recv_ms),
                    )

                else:
                    self.logger.error(
                        f"[LIFECYCLE] CLOSE_RESULT slave={result.ticket} on "
                        f"{task.slave_name}: FAILED ({result.error})"
                    )
                    self._record_action(
                        "close", task.slave_name, "failed",
                        master_ticket=master_ticket,
                        slave_ticket=result.ticket or 0,
                        error=result.error,
                        duration_ms=round(result.duration_ms, 1),
                    )
                    emit_error(
                        trade_id=tracked.trade_id,
                        slave_name=task.slave_name,
                        error=result.error,
                        master_ticket=master_ticket,
                    )
                    # Use pre-close profit captured by the worker if available
                    fail_profit = result.details.get("profit", 0.0) if result.details else 0.0
                    # Defensive: also emit TRADE_CLOSED so UI cleans up
                    # the trade row even when the close request failed
                    emit_trade_closed(
                        trade_id=tracked.trade_id,
                        master_ticket=master_ticket,
                        slave_name=task.slave_name,
                        slave_ticket=result.ticket or 0,
                        profit=fail_profit,
                        symbol=tracked.symbol,
                        order_type=tracked.order_type,
                        volume=slave_vol.volume if slave_vol else 0.0,
                        transport=source,
                        delay_ms=self._elapsed_ms(recv_ms),
                    )

            # Dispatch to slaves (parallel by default — see _dispatch / config.parallel_dispatch).
            await self._dispatch(tasks, on_result=on_result)

            trace.set_tag("slaves_closed", slaves_closed)

            # A close is the only thing that can move the daily count, so a
            # close is what re-reads it. The master's history is the authority
            # — not `worst_profit`, which is the SLAVES' realized PnL and can
            # disagree with the master's own result (different broker,
            # different fill, a slave that was Off). The rule is about the
            # master account, so the master account is what gets counted.
            if worst_profit < 0.0:
                self.logger.warning(
                    "Master=%s %s closed at a loss on a slave (%.2f)",
                    master_ticket, tracked.symbol, worst_profit,
                )
            await self._recount_daily_stops(master_ticket)
            trace.set_tags(
                daily_stops=self.stops.count, daily_stop_limit=self.stops.limit,
            )

            # Already removed from the tracker by claim_close at entry.
            self.logger.info(
                f"[LIFECYCLE] REMOVED master={master_ticket} from tracker"
            )

            # Reconnect to master
            await self._run_blocking(self.master.reconnect)

    async def _reduce_copied_positions(
        self, master_pos: Position, source: str, recv_ms: float = 0.0,
        signal_id: str = "",
    ):
        """Mirror a master scale-out (partial close) onto all slaves.

        keep_ratio = new_master_volume / old_master_volume. Each slave is
        partially closed to keep that same fraction, so slaves stay
        proportional to the master after a partial take-profit.
        """
        tracked = self.tracker.get_position(master_pos.ticket)
        if tracked is None:
            return
        old_vol = tracked.master_volume
        new_vol = master_pos.volume
        if old_vol <= 0 or new_vol >= old_vol - 1e-9:
            return  # not a reduction
        keep_ratio = new_vol / old_vol

        tracer = get_tracer()
        with tracer.trace("reduce_position", trade_id=tracked.trade_id) as trace:
            trace.set_tags(
                master_ticket=master_pos.ticket, symbol=tracked.symbol,
                old_volume=old_vol, new_volume=new_vol,
                keep_ratio=round(keep_ratio, 4), source=source,
                signal_id=signal_id,
                recv_ms=round(recv_ms, 1) if recv_ms else 0.0,
            )
            self.logger.info(
                f"[LIFECYCLE] REDUCE master={master_pos.ticket} "
                f"{old_vol:.2f}->{new_vol:.2f} (keep {keep_ratio:.3f}) -> "
                f"{len(tracked.slaves)} slaves (trade_id={tracked.trade_id})"
            )

            tasks: list[SlaveTask] = []
            state = get_slave_state()
            for slave_config in self.config.slaves:
                sid = slave_config.slave_id
                if sid not in tracked.slaves:
                    continue
                if not state.is_copy_active(sid):
                    self._record_action(
                        "reduce", slave_config.name, "skipped",
                        reason="paused_or_off", master_ticket=master_pos.ticket,
                    )
                    continue
                slave_ticket = tracked.slaves[sid].ticket
                tasks.append(
                    self._build_reduce_task(slave_config, slave_ticket, keep_ratio, tracked.symbol)
                )

            if not tasks:
                trace.set_tag("slaves_reduced", 0)
                # Still record the new master volume so future ratios are correct.
                self.tracker.update_master_volume(master_pos.ticket, new_vol)
                return

            reduced = 0

            def on_result(task: SlaveTask, result: SlaveResult):
                nonlocal reduced
                if result.success:
                    reduced += 1
                    new_slave_vol = result.details.get("new_volume")
                    if new_slave_vol is not None:
                        self.tracker.update_slave_volume(
                            master_pos.ticket, task.slave_id, new_slave_vol
                        )
                    self.logger.info(
                        f"[{task.slave_name}] Reduced -> {result.details.get('new_volume')} "
                        f"lots (closed {result.details.get('reduced')})"
                    )
                    self._record_action(
                        "reduce", task.slave_name,
                        "noop" if result.details.get("noop") else "reduced",
                        master_ticket=master_pos.ticket,
                        slave_ticket=result.ticket or 0,
                        new_volume=result.details.get("new_volume", 0.0),
                        closed_volume=result.details.get("reduced", 0.0),
                        duration_ms=round(result.duration_ms, 1),
                    )
                    if not result.details.get("noop"):
                        emit_trade_reduced(
                            trade_id=tracked.trade_id,
                            master_ticket=master_pos.ticket,
                            slave_name=task.slave_name,
                            slave_ticket=result.ticket or 0,
                            symbol=tracked.symbol,
                            order_type=tracked.order_type,
                            volume=result.details.get("new_volume", 0.0),
                            reduced_volume=result.details.get("reduced", 0.0),
                            transport=source,
                            delay_ms=self._elapsed_ms(recv_ms),
                        )
                else:
                    self.logger.error(
                        f"[{task.slave_name}] Reduce failed: {result.error}"
                    )
                    self._record_action(
                        "reduce", task.slave_name, "failed",
                        master_ticket=master_pos.ticket,
                        slave_ticket=result.ticket or 0,
                        error=result.error,
                    )

            await self._dispatch(tasks, on_result=on_result)

            trace.set_tag("slaves_reduced", reduced)
            # Record the master's new volume for subsequent scale-outs.
            self.tracker.update_master_volume(master_pos.ticket, new_vol)
            await self._run_blocking(self.master.reconnect)

    async def _update_sl_tp(
        self, master_pos: Position, source: str, recv_ms: float = 0.0,
        signal_id: str = "",
    ):
        """Update SL/TP on slave positions in parallel (async)."""
        tracked = self.tracker.get_position(master_pos.ticket)
        if tracked is None:
            return

        tracer = get_tracer()
        with tracer.trace("update_sl_tp", trade_id=tracked.trade_id) as trace:
            trace.set_tags(
                master_ticket=master_pos.ticket,
                symbol=master_pos.symbol,
                # Both sides of the change: "SL moved to 1.0840" is only a
                # finding next to what it moved FROM.
                old_sl=tracked.master_sl,
                old_tp=tracked.master_tp,
                new_sl=master_pos.sl,
                new_tp=master_pos.tp,
                source=source,
                signal_id=signal_id,
                recv_ms=round(recv_ms, 1) if recv_ms else 0.0,
            )

            self.logger.info(
                f"Master position {master_pos.ticket} SL/TP changed: "
                f"SL={master_pos.sl}, TP={master_pos.tp} (trade_id: {tracked.trade_id})"
            )

            # Update tracker for master
            self.tracker.update_master_sl_tp(
                master_pos.ticket, master_pos.sl, master_pos.tp
            )

            # Build modify tasks for all slaves that have this position
            tasks: list[SlaveTask] = []
            state = get_slave_state()

            for slave_config in self.config.slaves:
                sid = slave_config.slave_id
                if sid not in tracked.slaves:
                    continue

                # Skip if slave is paused/off in runtime state machine
                if not state.is_copy_active(sid):
                    self.logger.debug(
                        f"[{slave_config.name}] Modify skipped (paused/off)"
                    )
                    self._record_action(
                        "modify", slave_config.name, "skipped",
                        reason="paused_or_off", master_ticket=master_pos.ticket,
                    )
                    continue

                slave_ticket = tracked.slaves[sid].ticket
                task = self._build_modify_task(
                    slave_config,
                    slave_ticket,
                    master_pos,
                )
                tasks.append(task)

            if not tasks:
                trace.set_tag("slaves_updated", 0)
                return

            trace.set_tag("slave_tasks", len(tasks))

            # Callback to process results as they arrive
            slaves_updated = 0

            def on_result(task: SlaveTask, result: SlaveResult):
                nonlocal slaves_updated
                if result.success:
                    # Update tracker with new SL/TP values from result
                    new_sl = result.details.get("sl", 0.0)
                    new_tp = result.details.get("tp", 0.0)
                    if new_sl or new_tp:
                        self.tracker.update_slave_sl_tp(
                            master_pos.ticket, task.slave_id, new_sl, new_tp
                        )
                    self.logger.info(
                        f"[{task.slave_name}] Updated SL/TP ({result.duration_ms:.0f}ms)"
                    )
                    slaves_updated += 1
                    self._record_action(
                        "modify", task.slave_name,
                        "unchanged" if result.details.get("no_change") else "modified",
                        master_ticket=master_pos.ticket,
                        slave_ticket=result.ticket or 0,
                        sl=new_sl, tp=new_tp,
                        duration_ms=round(result.duration_ms, 1),
                    )
                    if not result.details.get("no_change"):
                        emit_trade_modified(
                            trade_id=tracked.trade_id,
                            master_ticket=master_pos.ticket,
                            slave_name=task.slave_name,
                            slave_ticket=result.ticket or 0,
                            symbol=tracked.symbol,
                            order_type=tracked.order_type,
                            sl=new_sl,
                            tp=new_tp,
                            transport=source,
                            delay_ms=self._elapsed_ms(recv_ms),
                        )
                else:
                    self.logger.error(
                        f"[{task.slave_name}] Failed to update SL/TP: {result.error}"
                    )
                    self._record_action(
                        "modify", task.slave_name, "failed",
                        master_ticket=master_pos.ticket,
                        slave_ticket=result.ticket or 0,
                        error=result.error,
                    )

            # Dispatch to slaves (parallel by default — see _dispatch / config.parallel_dispatch).
            await self._dispatch(tasks, on_result=on_result)

            trace.set_tag("slaves_updated", slaves_updated)

        # Reconnect to master
        await self._run_blocking(self.master.reconnect)
