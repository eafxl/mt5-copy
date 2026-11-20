"""Live settings - thread-safe configuration that can be updated at runtime.

Settings are grouped into:
- LIVE: Can be changed without restart (multiplier, slippage, etc.)
- STATIC: Require restart (terminal paths, credentials)

Usage:
    from .live_settings import get_live_settings

    settings = get_live_settings()
    slippage = settings.max_slippage  # Thread-safe read

    # Update (typically from config watcher)
    settings.update_from_config(config)
"""
import threading
from dataclasses import dataclass, field
from typing import Any

from .news_calendar import (
    normalize_currencies as _normalize_news_currencies,
    minutes_to_seconds as _news_minutes_to_seconds,
    normalize_windows as _normalize_news_windows,
)
from .utils.logging import get_logger


@dataclass
class SlaveSettings:
    """Live settings for a single slave."""
    name: str
    multiplier: float = 1.0
    adjust_by_leverage: bool = True
    enabled: bool = True
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    fixed_volume: float = 0.0
    fixed_risk_usd: float = 0.0
    risk_mode: str = "multiplier"  # "multiplier", "fixed_volume", "fixed_usd", or "pct_deposit"
    risk_pct: float = 1.0  # Percentage of deposit to risk (pct_deposit mode)
    risk_pct_base: str = "balance"  # "balance" or "equity" (pct_deposit mode)
    # Risk guard for this account: max loss-if-SL-hit as % of its own balance
    # (0 = off). Read on every open — see trade_copier._build_open_task.
    max_risk_pct_per_trade: float = 0.0
    initial_balance: float = 0.0  # Starting balance the ladder measures against
    dynamic_base: str = "balance"  # "balance" or "equity" (dynamic_pct mode)
    risk_tiers: list[dict] = field(default_factory=list)  # dynamic_pct ladder
    news_windows_s: dict[str, float] = field(default_factory=dict)  # impact -> SECONDS
    news_currencies: tuple = ()   # empty = every currency; see news_calendar

    def map_symbol(self, master_symbol: str) -> str:
        """Map master symbol to slave symbol."""
        return self.symbol_mapping.get(master_symbol, master_symbol)


class LiveSettings:
    """Thread-safe live settings container.

    All reads and writes are protected by a lock.
    Copy operations read these values on each trade.
    """

    _instance: "LiveSettings | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "LiveSettings":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._lock = threading.RLock()  # Reentrant lock for nested access
        self._logger = get_logger()

        # General settings (live-updatable)
        self._max_slippage_points: int = 0
        self._max_entry_deviation_ratio: float = 0.2
        self._retry_attempts: int = 3
        self._retry_delay_ms: int = 1000
        self._poll_interval_ms: int = 500

        # Pause state - when True, copy operations are skipped
        self._paused: bool = False

        # The risk guard lives on each slave (SlaveSettings.max_risk_pct_per_trade),
        # not here: one global cap is meaningless across accounts of different
        # sizes and min lots.
        # Debug/idle mode: compute + log trades but skip order_send
        self._debug_skip_execution: bool = False

        # Filling mode mapping (symbol -> mode string)
        self._filling_mode: dict[str, str] = {"default": "IOC"}

        # Real price calculator: raw {enabled, refresh_sec, pairs} section.
        # Held as the plain dict from config so the service can pick up edited
        # TV↔master pairs live, without a restart.
        self._price_calc: dict[str, Any] = {}

        # News calendar: raw {enabled, refresh_sec} section, so Settings edits
        # apply without a restart (same pattern as _price_calc).
        self._news: dict[str, Any] = {}

        # Per-slave settings
        self._slaves: dict[str, SlaveSettings] = {}

        # Per-slave broker symbol cache (populated at warmup, never persisted)
        self._symbol_cache: dict[str, list[str]] = {}

        # Version counter for change detection
        self._version: int = 0

        # Callbacks for change notifications
        self._callbacks: list[callable] = []

    # ==================== General Settings ====================

    @property
    def price_calc(self) -> dict[str, Any]:
        """Real-price-calculator section, as a copy (safe to read cross-thread)."""
        with self._lock:
            return dict(self._price_calc)

    @price_calc.setter
    def price_calc(self, value: dict[str, Any]):
        with self._lock:
            new = dict(value or {})
            if new != self._price_calc:
                self._price_calc = new
                self._version += 1


    @property
    def news(self) -> dict[str, Any]:
        """News-calendar section, as a copy (safe to read cross-thread)."""
        with self._lock:
            return dict(self._news)

    @news.setter
    def news(self, value: dict[str, Any]):
        with self._lock:
            new = dict(value or {})
            if new != self._news:
                self._news = new
                self._version += 1

    @property
    def max_slippage(self) -> int:
        with self._lock:
            return self._max_slippage_points

    @max_slippage.setter
    def max_slippage(self, value: int):
        with self._lock:
            if self._max_slippage_points != value:
                self._max_slippage_points = value
                self._version += 1
                self._logger.info(f"[LiveSettings] max_slippage -> {value}")

    @property
    def max_entry_deviation_ratio(self) -> float:
        with self._lock:
            return self._max_entry_deviation_ratio

    @max_entry_deviation_ratio.setter
    def max_entry_deviation_ratio(self, value: float):
        with self._lock:
            if self._max_entry_deviation_ratio != value:
                self._max_entry_deviation_ratio = value
                self._version += 1
                self._logger.info(f"[LiveSettings] max_entry_deviation_ratio -> {value}")

    @property
    def retry_attempts(self) -> int:
        with self._lock:
            return self._retry_attempts

    @retry_attempts.setter
    def retry_attempts(self, value: int):
        with self._lock:
            if self._retry_attempts != value:
                self._retry_attempts = value
                self._version += 1
                self._logger.info(f"[LiveSettings] retry_attempts -> {value}")

    @property
    def retry_delay_ms(self) -> int:
        with self._lock:
            return self._retry_delay_ms

    @retry_delay_ms.setter
    def retry_delay_ms(self, value: int):
        with self._lock:
            if self._retry_delay_ms != value:
                self._retry_delay_ms = value
                self._version += 1
                self._logger.info(f"[LiveSettings] retry_delay_ms -> {value}")

    @property
    def poll_interval_ms(self) -> int:
        with self._lock:
            return self._poll_interval_ms

    @poll_interval_ms.setter
    def poll_interval_ms(self, value: int):
        with self._lock:
            if self._poll_interval_ms != value:
                self._poll_interval_ms = value
                self._version += 1
                self._logger.info(f"[LiveSettings] poll_interval_ms -> {value}")

    @property
    def paused(self) -> bool:
        """Whether copy operations are paused. Read is lock-free for speed."""
        return self._paused

    @paused.setter
    def paused(self, value: bool):
        with self._lock:
            if self._paused != value:
                self._paused = value
                self._logger.info(f"[LiveSettings] paused -> {value}")

    @property
    def debug_skip_execution(self) -> bool:
        """Debug/idle mode: trades computed + logged but order_send skipped."""
        with self._lock:
            return self._debug_skip_execution

    @debug_skip_execution.setter
    def debug_skip_execution(self, value: bool):
        with self._lock:
            if self._debug_skip_execution != value:
                self._debug_skip_execution = value
                self._version += 1
                self._logger.info(f"[LiveSettings] debug_skip_execution -> {value}")

    @property
    def filling_mode(self) -> dict[str, str]:
        """Filling mode mapping (symbol -> mode string)."""
        with self._lock:
            return dict(self._filling_mode)

    @filling_mode.setter
    def filling_mode(self, value: dict[str, str]):
        with self._lock:
            if self._filling_mode != value:
                self._filling_mode = dict(value)
                self._version += 1
                self._logger.info(f"[LiveSettings] filling_mode -> {value}")

    @property
    def version(self) -> int:
        """Version counter - increments on any change."""
        with self._lock:
            return self._version

    # ==================== Slave Settings ====================

    def get_slave(self, slave_id: str) -> SlaveSettings | None:
        """Get slave settings by slave_id (thread-safe copy)."""
        with self._lock:
            slave = self._slaves.get(slave_id)
            if slave:
                # Return a copy to prevent external modification
                return SlaveSettings(
                    name=slave.name,
                    multiplier=slave.multiplier,
                    adjust_by_leverage=slave.adjust_by_leverage,
                    enabled=slave.enabled,
                    symbol_mapping=dict(slave.symbol_mapping),
                    fixed_volume=slave.fixed_volume,
                    fixed_risk_usd=slave.fixed_risk_usd,
                    risk_mode=slave.risk_mode,
                    risk_pct=slave.risk_pct,
                    risk_pct_base=slave.risk_pct_base,
                    max_risk_pct_per_trade=slave.max_risk_pct_per_trade,
                    initial_balance=slave.initial_balance,
                    dynamic_base=slave.dynamic_base,
                    risk_tiers=[dict(t) for t in slave.risk_tiers],
                    news_windows_s=dict(slave.news_windows_s),
                    news_currencies=_normalize_news_currencies(
                        getattr(slave, "news_currencies", ())
                    ),
                )
            return None

    def get_enabled_slaves(self) -> list[str]:
        """Get list of enabled slave_ids."""
        with self._lock:
            return [slave_id for slave_id, s in self._slaves.items() if s.enabled]

    def get_slave_multiplier(self, slave_id: str) -> float:
        """Get slave multiplier (default 1.0 if not found)."""
        with self._lock:
            slave = self._slaves.get(slave_id)
            return slave.multiplier if slave else 1.0

    def get_slave_adjust_by_leverage(self, slave_id: str) -> bool:
        """Get slave adjust_by_leverage (default True if not found)."""
        with self._lock:
            slave = self._slaves.get(slave_id)
            return slave.adjust_by_leverage if slave else True

    def is_slave_enabled(self, slave_id: str) -> bool:
        """Check if slave is enabled."""
        with self._lock:
            slave = self._slaves.get(slave_id)
            return slave.enabled if slave else False

    def map_symbol_for_slave(self, slave_id: str, master_symbol: str) -> str:
        """Map symbol for a specific slave."""
        with self._lock:
            slave = self._slaves.get(slave_id)
            if slave:
                return slave.map_symbol(master_symbol)
            return master_symbol

    def update_slave(self, slave_id: str, issuer: str = "", **kwargs):
        """Update slave settings."""
        with self._lock:
            if slave_id not in self._slaves:
                self._slaves[slave_id] = SlaveSettings(name=slave_id)

            slave = self._slaves[slave_id]
            changed = False

            for key, value in kwargs.items():
                if hasattr(slave, key) and getattr(slave, key) != value:
                    setattr(slave, key, value)
                    changed = True

            if changed:
                self._version += 1
                tag = f" [{issuer}]" if issuer else ""
                self._logger.info(f"[LiveSettings]{tag} Slave '{slave_id}' updated: {kwargs}")

    def remove_slave(self, slave_id: str):
        """Remove slave from live settings."""
        with self._lock:
            if slave_id in self._slaves:
                del self._slaves[slave_id]
                self._version += 1
                self._logger.info(f"[LiveSettings] Slave '{slave_id}' removed")

    # ==================== Broker Symbol Cache ====================

    def set_slave_symbols(self, slave_id: str, symbols: list[str]):
        """Cache broker symbols for a slave (called from warmup result)."""
        with self._lock:
            self._symbol_cache[slave_id] = symbols

    def get_slave_symbols(self, slave_id: str) -> list[str] | None:
        """Get cached broker symbols for a slave (None if not yet cached)."""
        with self._lock:
            syms = self._symbol_cache.get(slave_id)
            return list(syms) if syms else None

    # ==================== Bulk Updates ====================

    def update_from_config(self, config: "Config", issuer: str = "system"):
        """Update all live settings from a Config object.

        Called by ConfigWatcher when config.yaml changes.
        Note: ``_paused`` is deliberately NOT updated here — pause is a
        runtime-only state controlled by the GUI pause button.  Reloading
        the config file must never silently un-pause the service.
        """
        with self._lock:
            # Update general settings (paused intentionally excluded)
            self._max_slippage_points = config.settings.max_slippage_points
            self._max_entry_deviation_ratio = config.settings.max_entry_deviation_ratio
            self._retry_attempts = config.settings.retry_attempts
            self._retry_delay_ms = config.settings.retry_delay_ms
            self._poll_interval_ms = config.master.poll_interval_ms
            # The risk guard is per-slave — applied in the slave loop below.
            # NOTE: ``_debug_skip_execution`` is deliberately NOT updated here.
            # Like ``_paused`` it is a runtime safety switch — a config-file
            # reload must NEVER silently turn debug/simulate mode OFF and let
            # real orders through. It is controlled directly from the GUI.
            self._filling_mode = dict(config.filling_mode) if config.filling_mode else {"default": "IOC"}
            pc = config.price_calc
            self._price_calc = {
                "enabled": pc.enabled,
                "refresh_sec": pc.refresh_sec,
                "pairs": [
                    {"tv": p.tv, "master": p.master,
                     "last_diff": p.last_diff, "last_diff_at": p.last_diff_at}
                    for p in pc.pairs
                ],
            }
            self._news = {
                "enabled": config.news.enabled,
                "refresh_sec": config.news.refresh_sec,
            }

            # Update slave settings (keyed by slave_id = login@server)
            config_slave_ids = set()
            for slave in config.slaves:
                sid = slave.slave_id
                config_slave_ids.add(sid)
                if sid not in self._slaves:
                    self._slaves[sid] = SlaveSettings(name=slave.name)

                s = self._slaves[sid]
                s.name = slave.name  # Keep display name current
                s.multiplier = slave.multiplier
                s.adjust_by_leverage = slave.adjust_by_leverage
                s.enabled = slave.enabled
                s.symbol_mapping = dict(slave.symbol_mapping) if slave.symbol_mapping else {}
                s.fixed_volume = slave.fixed_volume
                s.fixed_risk_usd = slave.fixed_risk_usd
                s.risk_mode = slave.risk_mode
                s.risk_pct = slave.risk_pct
                s.risk_pct_base = slave.risk_pct_base
                s.max_risk_pct_per_trade = slave.max_risk_pct_per_trade
                s.initial_balance = slave.initial_balance
                s.dynamic_base = slave.dynamic_base
                s.risk_tiers = [dict(t) for t in slave.risk_tiers]
                s.news_windows_s = dict(slave.news_windows_s)
                s.news_currencies = _normalize_news_currencies(
                    getattr(slave, "news_currencies", ())
                )

            # Remove slaves that are no longer in config
            for sid in list(self._slaves.keys()):
                if sid not in config_slave_ids:
                    del self._slaves[sid]

            self._version += 1
            self._logger.info(
                f"[LiveSettings] [{issuer}] Bulk update from config (v{self._version})"
            )

            callbacks = list(self._callbacks)
            version = self._version

        # Notify OUTSIDE the lock to avoid blocking readers
        for cb in callbacks:
            try:
                cb(version, issuer)
            except Exception:
                pass

    def update_from_dict(self, data: dict[str, Any], issuer: str = "user"):
        """Update from raw config dict (used by GUI).

        Note: ``_paused`` is deliberately NOT updated here — pause is a
        runtime-only state controlled by the GUI pause button.
        """
        with self._lock:
            settings = data.get("settings", {})
            master = data.get("master", {})
            slaves = data.get("slaves", [])

            # General settings (paused intentionally excluded)
            if "max_slippage_points" in settings:
                self._max_slippage_points = settings["max_slippage_points"]
            if "max_entry_deviation_ratio" in settings:
                self._max_entry_deviation_ratio = settings["max_entry_deviation_ratio"]
            if "retry_attempts" in settings:
                self._retry_attempts = settings["retry_attempts"]
            if "retry_delay_ms" in settings:
                self._retry_delay_ms = settings["retry_delay_ms"]
            if "poll_interval_ms" in master:
                self._poll_interval_ms = master["poll_interval_ms"]
            # ``max_risk_pct_per_trade`` is per-slave now; a legacy global value
            # under ``settings`` is ignored here because Config.load has already
            # migrated it onto the slaves themselves.
            # ``debug_skip_execution`` intentionally excluded — see update_from_config.
            # Debug/simulate mode is toggled directly on the singleton by the GUI so
            # a bulk dict/config reload can never silently re-enable live execution.

            filling_mode_data = data.get("filling_mode", {})
            if filling_mode_data:
                self._filling_mode = dict(filling_mode_data)

            if "price_calc" in data:
                self._price_calc = dict(data.get("price_calc") or {})

            if "news" in data:
                self._news = dict(data.get("news") or {})

            # Slaves (keyed by slave_id = login@server)
            from .config import make_slave_id
            config_slave_ids = set()
            for slave_data in slaves:
                name = slave_data.get("name", "")
                login = slave_data.get("login", 0)
                server = slave_data.get("server", "")
                if not login or not server:
                    continue
                sid = make_slave_id(login, server)
                config_slave_ids.add(sid)

                if sid not in self._slaves:
                    self._slaves[sid] = SlaveSettings(name=name)

                s = self._slaves[sid]
                s.name = name  # Keep display name current
                s.multiplier = slave_data.get("multiplier", 1.0)
                s.adjust_by_leverage = slave_data.get("adjust_by_leverage", True)
                s.enabled = slave_data.get("enabled", False)
                s.symbol_mapping = dict(slave_data.get("symbol_mapping", {}))
                s.fixed_volume = slave_data.get("fixed_volume", 0.0)
                s.fixed_risk_usd = slave_data.get("fixed_risk_usd", 0.0)
                s.risk_mode = slave_data.get("risk_mode", "multiplier")
                s.risk_pct = slave_data.get("risk_pct", 1.0)
                s.risk_pct_base = slave_data.get("risk_pct_base", "balance")
                # Falls back to the legacy global cap when this slave carries
                # none of its own, so a pre-migration payload cannot arrive here
                # and quietly switch the guard off.
                s.max_risk_pct_per_trade = slave_data.get(
                    "max_risk_pct_per_trade",
                    settings.get("max_risk_pct_per_trade", 0.0),
                )
                s.initial_balance = slave_data.get("initial_balance", 0.0)
                s.dynamic_base = slave_data.get("dynamic_base", "balance")
                s.risk_tiers = [dict(t) for t in slave_data.get("risk_tiers", []) if isinstance(t, dict)]
                # Seconds if present; a legacy minutes-only payload is
                # converted so a live edit cannot quietly shrink the guard.
                if slave_data.get("news_windows_s") is not None:
                    s.news_windows_s = _normalize_news_windows(
                        slave_data.get("news_windows_s")
                    )
                elif slave_data.get("news_windows") is not None:
                    s.news_windows_s = _news_minutes_to_seconds(
                        slave_data.get("news_windows")
                    )
                s.news_currencies = _normalize_news_currencies(
                    slave_data.get("news_currencies")
                )

            # Remove old slaves
            for sid in list(self._slaves.keys()):
                if sid not in config_slave_ids:
                    del self._slaves[sid]

            self._version += 1
            self._logger.info(
                f"[LiveSettings] [{issuer}] Bulk update from dict (v{self._version})"
            )
            callbacks = list(self._callbacks)
            version = self._version

        # Notify OUTSIDE the lock to avoid blocking readers
        for cb in callbacks:
            try:
                cb(version, issuer)
            except Exception:
                pass

    # ==================== Callbacks ====================

    def register_callback(self, callback: callable):
        """Register a callback for settings changes."""
        with self._lock:
            self._callbacks.append(callback)

    def unregister_callback(self, callback: callable):
        """Unregister a callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    # ==================== Snapshot ====================

    def snapshot(self) -> dict[str, Any]:
        """Get a snapshot of all settings (for debugging/logging)."""
        with self._lock:
            return {
                "version": self._version,
                "paused": self._paused,
                "debug_skip_execution": self._debug_skip_execution,
                "max_slippage": self._max_slippage_points,
                "max_entry_deviation_ratio": self._max_entry_deviation_ratio,
                "retry_attempts": self._retry_attempts,
                "retry_delay_ms": self._retry_delay_ms,
                "poll_interval_ms": self._poll_interval_ms,
                "filling_mode": dict(self._filling_mode),
                "news": dict(self._news),
                "slaves": {
                    name: {
                        "name": s.name,
                        "multiplier": s.multiplier,
                        "adjust_by_leverage": s.adjust_by_leverage,
                        "enabled": s.enabled,
                        "symbol_mapping": dict(s.symbol_mapping),
                        "fixed_volume": s.fixed_volume,
                        "fixed_risk_usd": s.fixed_risk_usd,
                        "risk_mode": s.risk_mode,
                        "risk_pct": s.risk_pct,
                        "risk_pct_base": s.risk_pct_base,
                        "max_risk_pct_per_trade": s.max_risk_pct_per_trade,
                        "initial_balance": s.initial_balance,
                        "dynamic_base": s.dynamic_base,
                        "risk_tiers": [dict(t) for t in s.risk_tiers],
                        "news_windows_s": dict(s.news_windows_s),
                        "news_currencies": list(s.news_currencies),
                    }
                    for name, s in self._slaves.items()
                },
            }

    @classmethod
    def reset(cls):
        """Reset singleton (for testing)."""
        with cls._instance_lock:
            cls._instance = None


def get_live_settings() -> LiveSettings:
    """Get the global LiveSettings instance."""
    return LiveSettings()
