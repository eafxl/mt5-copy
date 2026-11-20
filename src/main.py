"""Main entry point for MetaCT Service (Async).

All I/O operations (logging, tracing, debug) are async - never blocks copy path.
LiveSettings enables real-time config updates without restart.

Can run standalone (CLI) or embedded inside the GUI process (ServiceThread).
"""
import asyncio
import signal
import sys
import threading
from typing import Optional

from .config import Config
from .config_watcher import start_config_watcher, stop_config_watcher
from .live_settings import get_live_settings
from .master import MasterMonitor
from .mt5_client import MT5Client
from .position_tracker import PositionTracker
from .events import emit_master_status
from .retention import RetentionSweeper
from .trade_copier import TradeCopier
from .utils.logging import setup_logging, get_logger
from .utils.tracer import setup_tracer, get_tracer
from .utils.debug import setup_debug, dump_state, get_debug_stats, enable_tracemalloc


#: How often the master terminal is measured (and revived if it went away).
#: A local struct read, so the cost is nil; the interval exists so a terminal
#: that is genuinely down is not hammered with reconnect attempts.
MASTER_CHECK_INTERVAL_S = 5.0


def _warm_master_terminal(terminal_path: str, timeout_ms: int = 60000) -> bool:
    """Start master terminal in a subprocess (separate GIL).

    MetaTrader5's mt5.initialize() holds the GIL while waiting for the
    terminal process to start (up to 60s). Running it in a subprocess
    avoids blocking the GUI's event loop. Once the terminal is running,
    the in-process mt5.initialize() call returns instantly.
    """
    import MetaTrader5 as mt5
    try:
        result = mt5.initialize(path=terminal_path, timeout=timeout_ms)
        mt5.shutdown()
        return bool(result)
    except Exception:
        return False


class CopyTradingService:
    """Main service orchestrator with async support.

    Args:
        embedded: When True, skips logging/debug init (GUI already did it)
                  and skips signal handlers (not allowed outside main thread).
    """

    def __init__(self, embedded: bool = False):
        self.embedded = embedded
        self.running = False
        self.config: Config | None = None
        self.mt5: MT5Client | None = None
        self.master: MasterMonitor | None = None
        self.tracker: PositionTracker | None = None
        self.copier: TradeCopier | None = None
        self.logger = None
        self._shutdown_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ea_bridge = None  # EABridge | None — EA push listener
        self._retention: RetentionSweeper | None = None

    def setup(self) -> bool:
        """Initialize all components (sync - called before async loop)."""
        # Logger is available early in embedded mode (GUI set it up), so setup
        # failures are visible in the GUI/log, not just the console.
        _log = get_logger()
        try:
            # Load configuration
            self.config = Config.load()
            _log.info("Config loaded from: %s", self.config.config_path)

            # Validate terminal paths
            if not self.config.master.terminal_path:
                _log.error(
                    "SETUP FAILED: master.terminal_path is empty in %s — set the "
                    "master terminal64.exe path in Settings.", self.config.config_path,
                )
                return False

            if not self.config.slave_terminal.terminal_path:
                _log.error(
                    "SETUP FAILED: slave_terminal.terminal_path is empty in %s "
                    "(legacy 'directory' key is ignored) — set/Save the slave "
                    "terminal path in Settings.", self.config.config_path,
                )
                return False

            # Setup logging (skip in embedded mode - GUI already initialized it)
            if self.embedded:
                self.logger = get_logger()
            else:
                self.logger = setup_logging(self.config.settings.log_level)
            self.logger.info("=" * 50)
            self.logger.info("MetaCT Service Starting")
            self.logger.info("=" * 50)

            # Setup tracer for execution profiling. Subscription comes from
            # config: heartbeat operations are muted by default so the file
            # stays readable — see TracingConfig.
            tracing = self.config.tracing
            tracer = setup_tracer(
                enabled=tracing.enabled,
                include=tracing.operations,
                exclude=tracing.exclude,
            )
            if tracing.enabled:
                scope = (
                    f"only {', '.join(tracer.include)}" if tracer.include
                    else f"all except {', '.join(tracer.exclude) or '(nothing)'}"
                )
                self.logger.info(
                    "Tracer enabled (%s), writing to: %s", scope, tracer.trace_dir
                )
            else:
                self.logger.info("Tracer disabled by config")

            # Age out old traces/logs/dumps. Done here rather than on a timer
            # of its own: the sweep costs milliseconds and the only moment it
            # MUST have happened is before a long run starts appending more.
            self._retention = RetentionSweeper(
                self.config.retention.effective_days
            )
            if self._retention.days > 0:
                report = self._retention.run()
                self.logger.info(
                    "Retention (%d day(s)): %s",
                    self._retention.days, report.describe(),
                )
                for path, err in report.failed:
                    self.logger.warning("Could not remove %s: %s", path, err)
            else:
                self.logger.info("Retention disabled — output files kept forever")

            # Create MT5 client
            self.mt5 = MT5Client(
                retry_attempts=self.config.settings.retry_attempts,
                retry_delay_ms=self.config.settings.retry_delay_ms,
            )

            # Create master monitor
            self.master = MasterMonitor(
                self.mt5,
                terminal_path=self.config.master.terminal_path
            )

            # Pre-warm master terminal in subprocess (avoids GIL freeze in GUI)
            if self.embedded:
                from concurrent.futures import ProcessPoolExecutor
                self.logger.info(
                    f"Connecting to terminal: {self.config.master.terminal_path}"
                )
                try:
                    with ProcessPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(
                            _warm_master_terminal,
                            self.config.master.terminal_path,
                        )
                        warm_ok = future.result(timeout=120)
                        if not warm_ok:
                            self.logger.warning(
                                "Master pre-warm failed, will retry directly"
                            )
                except Exception as e:
                    self.logger.warning(f"Master pre-warm error: {e}")

            # Connect to master
            if not self.master.connect():
                self.logger.error("Failed to connect to master account")
                return False

            # Validate: no slave may share the master's (login, server). If a
            # slave logs into the same account as master, the slave's freshly
            # opened ticket appears in master's positions_get on the next
            # poll → self-copy loop. This check is the first line of defence;
            # process_changes also filters at runtime.
            master_info = self.master.get_account_info()
            if master_info is not None:
                m_login = master_info.login
                m_server = (master_info.server or "").strip().lower()
                conflicts = []
                for s in self.config.slaves:
                    if not s.enabled:
                        continue
                    if s.login == m_login and (s.server or "").strip().lower() == m_server:
                        conflicts.append(s.name)
                if conflicts:
                    self.logger.error(
                        "Refusing to start: slave(s) %s share master account "
                        "%s@%s. This would cause an infinite self-copy loop. "
                        "Fix Settings — give each slave a distinct broker login.",
                        conflicts, m_login, master_info.server,
                    )
                    return False

            # Create position tracker
            self.tracker = PositionTracker()

            # Cleanup tracker for removed slaves
            active_slaves = {s.name for s in self.config.slaves if s.enabled}
            self.tracker.cleanup_missing_slaves(active_slaves)

            # Create trade copier (async-enabled, uses LiveSettings)
            self.copier = TradeCopier(
                self.config, self.mt5, self.master, self.tracker,
                embedded=self.embedded,
            )

            # The day's loss limit, counted from the master's own history. This
            # is the check the old session counter could not make: four stops,
            # restart, and the counter was zero again. Refusing here means the
            # service never reaches its poll loop, so nothing can be copied.
            self.copier.stops.refresh()
            self.logger.info("Daily stops: %s", self.copier.stops.describe())
            if self.copier.stops.tripped:
                self.logger.error(
                    "Refusing to start: %s. Trading is finished for today.",
                    self.copier.stops.describe(),
                )
                self.copier.announce_stop_limit()
                return False

            # Start config watcher for live settings updates
            watcher = start_config_watcher(self.config.config_path)
            self.logger.info(f"Config watcher started (check interval: {watcher.check_interval}s)")
            self.logger.info("Settings source of truth: the app UI. config.yaml is read "
                             "at startup and written on Save; runtime file edits are NOT applied.")

            # Log slave configuration
            enabled_slaves = [s for s in self.config.slaves if s.enabled]
            parallel_slaves = [s for s in enabled_slaves if s.terminal_path]

            # Log active global guard / debug settings so the loaded values are
            # visible without guesswork (read from LiveSettings = what's live now).
            _ls = get_live_settings()
            self.logger.info(
                f"Global settings: debug_skip_execution={_ls.debug_skip_execution}, "
                f"max_slippage={_ls.max_slippage}pts"
            )

            self.logger.info(f"Configured slaves: {len(enabled_slaves)} enabled")
            if parallel_slaves:
                self.logger.info(f"Parallel mode: {len(parallel_slaves)} slaves with dedicated terminals")
            else:
                self.logger.info("Sequential mode: all slaves share default terminal")

            for slave in self.config.slaves:
                status = "enabled" if slave.enabled else "disabled"
                terminal_info = ""
                if slave.enabled:
                    if slave.terminal_path:
                        terminal_info = f", terminal=OWN"
                    else:
                        terminal_info = f", terminal=SHARED"
                # The risk guard is per-slave, so it is logged per slave —
                # a cap that blocks every trade on one account should be
                # readable in the startup lines, not only in the block message.
                guard = (
                    f"{slave.max_risk_pct_per_trade:.2f}% of balance"
                    if slave.max_risk_pct_per_trade > 0 else "OFF"
                )
                self.logger.info(
                    f"  - {slave.name}: {status}, "
                    f"multiplier={slave.multiplier}, "
                    f"adjust_by_leverage={slave.adjust_by_leverage}, "
                    f"max_risk/trade={guard}{terminal_info}"
                )

            return True

        except FileNotFoundError as e:
            print(f"Configuration error: {e}")
            return False
        except ValueError as e:
            print(f"Configuration error: {e}")
            return False
        except Exception as e:
            print(f"Setup error: {e}")
            return False

    async def _run_blocking(self, func, *args):
        """Run a blocking function in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args)

    async def warmup_only(self) -> tuple[int, int]:
        """Run warmup_slaves only (called by WarmupThread).

        Returns (success_count, fail_count).
        """
        if self.copier is None:
            return 0, 0
        return await self.copier.warmup_slaves()

    async def run_async(self, on_ready=None, skip_warmup=False):
        """Main async service loop.

        Args:
            on_ready: Optional callback invoked after warmup and initial sync
                      are complete (before the poll loop starts).
            skip_warmup: If True, skip warmup_slaves (already done by WarmupThread).
        """
        if self.logger is None or self.copier is None:
            return

        self.running = True
        self._shutdown_event = asyncio.Event()
        self._loop = asyncio.get_event_loop()

        # Setup signal handlers for graceful shutdown (Unix only, main thread only)
        # In embedded mode (GUI thread), the GUI handles shutdown.
        if (
            not self.embedded
            and sys.platform != "win32"
            and threading.current_thread() is threading.main_thread()
        ):
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._loop.add_signal_handler(sig, self._signal_shutdown)

        # Pre-connect slave terminals (eliminates 5-25s delay on first trade)
        if not skip_warmup:
            await self.copier.warmup_slaves()

        # Initial sync (async)
        await self.copier.sync_initial()

        # Emit existing positions to populate the UI trades table
        self.copier.emit_existing_positions()

        # Signal that warmup is done and service is ready
        if on_ready:
            on_ready()

        settings = get_live_settings()
        poll_interval = self.config.master.poll_interval_ms / 1000

        # EA push bridge — PRIMARY, event-driven (~ms latency). Polling runs
        # ALONGSIDE as a fallback: catches external actions (e.g. a mobile
        # terminal close) and recovers if the EA/socket drops. The tracker
        # dedups, so both feeding process_changes is safe.
        if self.config.settings.ea_push_enabled:
            from .ea_bridge import EABridge
            port = self.config.settings.ea_push_port
            bridge = EABridge(self._loop, port=port)
            try:
                bridge.start()
                self._ea_bridge = bridge
            except OSError as e:
                # Port already in use (another instance / stale listener). Don't
                # abort — fall back to polling and notify the user (Continue/Close
                # handled in the GUI via the EA_UNAVAILABLE event).
                self._ea_bridge = None
                self.logger.warning(
                    "EA push port %d in use (%s) — falling back to Polling only", port, e
                )
                from .events import emit_ea_unavailable
                emit_ea_unavailable(port, str(e))

        self.logger.info(
            "Monitoring started — EA push %s, poll fallback %.3fs",
            "ON" if self._ea_bridge else "OFF", poll_interval,
        )
        if not self.embedded:
            self.logger.info("Press Ctrl+C to stop")

        async def _process(change, origin: str):
            """Dispatch a PositionChange (from either source). Tracker dedups."""
            if settings.paused:
                return
            try:
                await self.copier.process_changes(change)
            except Exception as e:
                self.logger.warning("%s change handler error: %s", origin, e)

        # Real price calculator (TradingView ↔ master offset). Off by default;
        # driven from the poll loop at its own cadence.
        from .price_calc import PriceCalculator
        from .config import PriceCalcPair
        price_calc = PriceCalculator(
            list(self.config.price_calc.pairs),
            refresh_sec=self.config.price_calc.refresh_sec,
        )

        def _price_calc_cycle():
            """Blocking: one scanner call + master reads. Runs in the executor."""
            from .events import emit_error, emit_price_diff

            # Re-read the section each cycle so pairs edited in Settings apply
            # without restarting the service.
            live_pc = settings.price_calc or {}
            if not live_pc.get("enabled", self.config.price_calc.enabled):
                return
            raw_pairs = live_pc.get("pairs")
            if raw_pairs is not None:
                price_calc.set_pairs(
                    [PriceCalcPair(tv=p.get("tv", ""), master=p.get("master", ""))
                     for p in raw_pairs if p.get("tv") and p.get("master")],
                    refresh_sec=live_pc.get("refresh_sec", price_calc.refresh_sec),
                )
            if not price_calc.due():
                return

            mt5c = self.master.mt5
            # Span so the per-pair `price_calc` events _resolve_pair writes hang
            # off one parent instead of landing under trace_id="NO_TRACE" — one
            # cycle then reads as a single tree in the trace file.
            with get_tracer().span("price_calc_cycle") as trace:
                diffs = price_calc.refresh(
                    price_at=mt5c.price_at,
                    current_price=mt5c.get_current_price,
                    server_offset=mt5c.server_time_offset,
                    digits_of=mt5c.symbol_digits,
                    # EXACT-instant sampler: the bar that closed at the shared
                    # timestamp, or nothing. A nearest-bar sampler cannot say
                    # "no data for that minute", which is the one answer that
                    # keeps a stale reading from passing for a live one.
                    bar_at=mt5c.bar_close_at,
                    # Freshness counter for the master side, so the UI can show
                    # it against the TV feed's delay.
                    quote_age=mt5c.quote_age,
                )
                worst = next((d.error for d in diffs if d.error), "")
                trace.set_tags(
                    pairs=len(diffs),
                    stale=sum(1 for d in diffs if d.stale),
                    error=worst,
                )
            # ONE toast per TradingView outage. Two things make that work:
            #
            #  - only the SCANNER error is offered here, never a per-pair
            #    "no data for 12:34:00" message. Those carry the instant they
            #    are about, so they are a new string every minute — as toast
            #    input they produced an endless stream of near-identical
            #    popups. They belong in the row (which turns red) and the
            #    trace, and nowhere else.
            #  - the edge check then collapses a persistent outage to its
            #    first cycle.
            edge = price_calc.take_error_edge(price_calc.last_scan_error)
            if edge:
                emit_error("", "Price Calc", edge)
            for d in diffs:
                emit_price_diff(d)

        # ForexFactory calendar. Owned here, handed to the copier so the copy
        # path can consult it without any I/O of its own.
        from .news_calendar import NewsCalendar
        news = NewsCalendar(refresh_sec=self.config.news.refresh_sec)
        self.copier.news = news

        def _news_cycle():
            """Blocking: one feed fetch + cache write. Runs in the executor."""
            from .events import emit_error, emit_news_update

            live_news = settings.news or {}
            if not live_news.get("enabled", self.config.news.enabled):
                # Disabled means disabled: drop the calendar so a stale one
                # can't keep blocking trades after the switch is turned off.
                if self.copier.news is not None:
                    self.copier.news = None
                # Say so out loud. Returning silently left the UI showing
                # whatever it last knew, which after a restart is nothing.
                emit_news_update((), enabled=False, armed=False)
                return
            if self.copier.news is None:
                self.copier.news = news

            news.set_refresh(live_news.get("refresh_sec", news.refresh_sec))
            if news.due():
                error = news.refresh()
                edge = news.take_error_edge(error)
                if edge:
                    self.logger.warning("News calendar: %s", edge)
                    emit_error("", "News", edge)

            # Table rows: today's events armed by at least one slave's windows.
            snapshot = settings.snapshot().get("slaves", {})
            # Impact windows AND currencies, kept together per slave: the table
            # must list what will actually block, and that is a per-slave
            # conjunction of the two.
            rules = [
                (s.get("name") or slave_id,
                 s.get("news_windows_s", {}) or {},
                 s.get("news_currencies", ()) or ())
                for slave_id, s in snapshot.items()
            ]
            # Armed is about CONFIGURATION, not about today's rows: a quiet day
            # or a weekend produces no rows while blocking is fully armed, and
            # conflating the two reported a working feature as switched off.
            armed = any(
                any(float(v or 0) > 0 for v in windows.values())
                for _name, windows, _cur in rules
            )
            emit_news_update(
                news.armed_today(rules),
                stale=news.stale, enabled=True, armed=armed,
            )

        news_busy = False

        async def _refresh_news():
            """Never allowed to disrupt the copy loop."""
            nonlocal news_busy
            if news_busy:
                return
            news_busy = True
            try:
                await self._run_blocking(_news_cycle)
            except Exception as e:
                self.logger.debug("News calendar error: %s", e)
            finally:
                news_busy = False

        pc_busy = False

        async def _refresh_price_calc():
            """Never allowed to disrupt the copy loop."""
            nonlocal pc_busy
            if pc_busy:
                return  # a slow scan is still running — don't pile up
            pc_busy = True
            try:
                await self._run_blocking(_price_calc_cycle)
            except Exception as e:
                self.logger.debug("Price calc error: %s", e)
            finally:
                pc_busy = False

        async def _sweep_old_files():
            """Daily retention sweep, off the copy path like everything else."""
            if self._retention is None:
                return
            try:
                report = await self._run_blocking(self._retention.run_if_due)
                if report is not None and report.count:
                    self.logger.info(
                        "Retention (%d day(s)): %s",
                        self._retention.days, report.describe(),
                    )
            except Exception as e:
                self.logger.debug("Retention sweep error: %s", e)

        master_health = {"connected": None, "algo": None, "checked": 0.0}

        async def _check_master():
            """Measure the master terminal, and revive it if it went away.

            WHY THE SERVICE DOES THIS AND NOT THE GUI
                The master connection is process-wide MT5 state that the
                service owns. The Terminals tab used to read it from the GUI
                thread, and the relaunch button called a helper that ends in
                `mt5.shutdown()` IN THIS PROCESS — so "relaunch the master"
                tore down the service's own connection, and nothing put it
                back until the next copy happened to call `reconnect`.

                Now the service asks, on its own cycle: it is the only place
                that may touch that connection, and the answer is published as
                an event for anything that wants to display it.
            """
            import time as _t
            if _t.time() - master_health["checked"] < MASTER_CHECK_INTERVAL_S:
                return
            master_health["checked"] = _t.time()
            try:
                connected, algo = await self._run_blocking(self.master.health)
                if not connected:
                    # A killed or relaunched terminal is picked up here rather
                    # than at the next trade, which may be hours away.
                    if await self._run_blocking(self.master.ensure_connected):
                        connected, algo = await self._run_blocking(
                            self.master.health
                        )
            except Exception as e:
                self.logger.debug("Master health check error: %s", e)
                return
            if (connected, algo) == (master_health["connected"],
                                     master_health["algo"]):
                return              # edge-triggered: nothing new to say
            if master_health["connected"] and not connected:
                self.logger.error(
                    "Master terminal is DOWN — nothing can be copied until it "
                    "is back."
                )
            elif connected and master_health["connected"] is False:
                self.logger.info("Master terminal is back.")
            master_health["connected"] = connected
            master_health["algo"] = algo
            emit_master_status(connected, algo)

        async def _refresh_profits():
            """Pull real per-slave PnL. Never allowed to disrupt the copy loop."""
            try:
                await self.copier.refresh_slave_profits()
            except Exception as e:
                self.logger.debug("Profit refresh error: %s", e)

        async def _ea_loop():
            """Event-driven consumer of EA push events (primary path)."""
            if self._ea_bridge is None:
                return
            while self.running:
                try:
                    change = await asyncio.wait_for(
                        self._ea_bridge.next_change(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    if self._shutdown_event.is_set():
                        break
                    continue
                await _process(change, "EA")

        async def _poll_loop():
            """Polling fallback — external actions + disaster recovery."""
            nonlocal poll_interval
            was_paused = False
            consecutive_errors = 0
            last_profit_update = 0.0
            import time as _time
            while self.running:
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=poll_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass

                # Read-only price information — runs even while paused, and
                # self-throttles to price_calc.refresh_sec.
                asyncio.ensure_future(_refresh_price_calc())
                # Same deal: read-only, self-throttled, runs while paused so the
                # table is current the moment copying resumes.
                asyncio.ensure_future(_refresh_news())
                # Housekeeping. `run_if_due` is a clock comparison until a day
                # has passed, so this costs nothing per poll; it exists so a
                # machine left running for weeks still ages out its files.
                asyncio.ensure_future(_sweep_old_files())
                # The master is the one terminal nothing else watches; it is
                # also the one whose death stops everything.
                asyncio.ensure_future(_check_master())

                if settings.paused:
                    if not was_paused:
                        self.logger.info("Copy paused - existing trades remain open")
                        was_paused = True
                    continue
                if was_paused:
                    self.logger.info("Copy resumed")
                    was_paused = False

                poll_interval = settings.poll_interval_ms / 1000

                try:
                    loop_start = _time.perf_counter()
                    changes = await self._run_blocking(self.master.detect_changes)
                    changes.recv_ms = _time.time() * 1000.0  # ingest stamp (poll)
                    detect_ms = (_time.perf_counter() - loop_start) * 1000
                    if detect_ms > 100:
                        self.logger.warning("Slow poll cycle: %.0fms", detect_ms)
                    consecutive_errors = 0
                except Exception as poll_err:
                    consecutive_errors += 1
                    if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                        self.logger.warning(
                            "Poll error (%d consecutive): %s",
                            consecutive_errors, poll_err,
                        )
                    if consecutive_errors >= 10:
                        self.logger.warning(
                            "Too many consecutive errors (%d), attempting reconnect",
                            consecutive_errors,
                        )
                        await self._run_blocking(self.master.reconnect)
                        consecutive_errors = 0
                    continue

                has_changes = (
                    changes.new_positions
                    or changes.closed_tickets
                    or changes.modified_positions
                )
                if has_changes:
                    await _process(changes, "Poll")

                now = _time.perf_counter()
                if now - last_profit_update > 1.0:
                    # Fire-and-forget: reading each slave's real PnL must never
                    # add latency to close detection, so it runs as its own task
                    # rather than being awaited inside the poll cycle.
                    asyncio.ensure_future(_refresh_profits())
                    last_profit_update = now

        try:
            await asyncio.gather(_ea_loop(), _poll_loop())
        except asyncio.CancelledError:
            self.logger.info("Service cancelled")
        except Exception as e:
            self.logger.error(f"Service error: {e}")
            raise

    def _signal_shutdown(self):
        """Handle shutdown signal."""
        self.logger.info("Shutdown signal received")
        self.running = False
        if self._shutdown_event:
            self._shutdown_event.set()

    async def shutdown_async(self):
        """Async graceful shutdown."""
        self.running = False
        if self._shutdown_event:
            self._shutdown_event.set()

        if self.logger:
            self.logger.info("Shutting down...")

        # Stop config watcher
        stop_config_watcher()

        # Stop EA push bridge
        if self._ea_bridge:
            try:
                self._ea_bridge.stop()
            except Exception:
                pass
            self._ea_bridge = None

        # Dump debug stats on shutdown
        dump_state(get_debug_stats(), "shutdown_stats")

        # Shutdown copier (closes executor + kills slave terminals)
        if self.copier:
            self.copier.shutdown()

        # Shutdown MT5 connection and kill master terminal
        if self.mt5:
            await self._run_blocking(self.mt5.shutdown)
        if self.config and self.config.master.terminal_path:
            from .parallel_worker import _kill_mt5_terminals
            _kill_mt5_terminals({self.config.master.terminal_path})

        # Close tracer
        tracer = get_tracer()
        tracer.close()

        if self.logger:
            self.logger.info("Service stopped")

    def shutdown(self):
        """Sync shutdown (for non-async contexts)."""
        self.running = False
        if self.logger:
            self.logger.info("Shutting down...")

        # Stop config watcher
        stop_config_watcher()

        # Stop EA push bridge
        if self._ea_bridge:
            try:
                self._ea_bridge.stop()
            except Exception:
                pass
            self._ea_bridge = None

        if self.copier:
            self.copier.shutdown()

        if self.mt5:
            self.mt5.shutdown()
        if self.config and self.config.master.terminal_path:
            from .parallel_worker import _kill_mt5_terminals
            _kill_mt5_terminals({self.config.master.terminal_path})

        tracer = get_tracer()
        tracer.close()
        if self.logger:
            self.logger.info("Service stopped")


async def async_main():
    """Async entry point."""
    service = CopyTradingService()

    # Setup (sync)
    if not service.setup():
        sys.exit(1)

    # Run async
    try:
        await service.run_async()
    finally:
        await service.shutdown_async()


def main():
    """Entry point."""
    # Frozen builds re-execute this binary to spawn pool workers; without
    # freeze_support() the re-entry would start a second copy service.
    import multiprocessing
    multiprocessing.freeze_support()

    # Initialize debug system FIRST (before any other imports might fail)
    from .paths import dumps_dir, ensure_dirs

    ensure_dirs()
    setup_debug(dumps_dir())
    enable_tracemalloc()

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass  # Already handled by signal handler
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()