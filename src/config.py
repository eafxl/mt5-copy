"""Configuration management for MetaCT."""
import logging
import os
from dataclasses import dataclass, field, fields as _dc_fields
from pathlib import Path
from typing import Optional

import yaml

from .news_calendar import (
    minutes_to_seconds as _news_minutes_to_seconds,
    normalize_currencies as _normalize_news_currencies,
    normalize_windows as _normalize_news_windows,
)
from .utils.risk_ladder import default_tiers_as_dicts, normalize_tiers


# Risk mode options for slave volume calculation
RISK_MODE_MULTIPLIER = "multiplier"  # Use multiplier * master_volume (with leverage adjustment)
RISK_MODE_FIXED_VOLUME = "fixed_volume"  # Use fixed lot size directly
RISK_MODE_FIXED_USD = "fixed_usd"  # Calculate lots so SL loss = fixed USD amount
RISK_MODE_PCT_DEPOSIT = "pct_deposit"  # Risk a % of account balance/equity per trade
# Risk a % picked from a tier ladder keyed on drift from initial_balance
RISK_MODE_DYNAMIC_PCT = "dynamic_pct"

RISK_MODES = [
    RISK_MODE_MULTIPLIER,
    RISK_MODE_FIXED_VOLUME,
    RISK_MODE_FIXED_USD,
    RISK_MODE_PCT_DEPOSIT,
    RISK_MODE_DYNAMIC_PCT,
]


@dataclass
class SlaveConfig:
    """Slave account configuration."""
    name: str
    login: int
    password: str
    server: str
    multiplier: float = 1.0
    adjust_by_leverage: bool = True
    enabled: bool = True
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    terminal_path: str = ""  # Per-slave terminal (for parallel mode)
    fixed_volume: float = 0.0  # Lot size when risk_mode="fixed_volume"
    fixed_risk_usd: float = 0.0  # USD risk when risk_mode="fixed_usd"
    risk_mode: str = RISK_MODE_MULTIPLIER  # One of RISK_MODES
    risk_pct: float = 1.0  # Percentage of deposit to risk per trade (pct_deposit mode)
    # UI-only: upper limit of the risk_pct slider/spinbox in the Rules dialog.
    # Not read by the copy path — risk_pct is already bounded by it at entry.
    risk_pct_max: float = 1.0
    risk_pct_base: str = "balance"  # "balance" or "equity" (pct_deposit mode)
    # Risk guard for THIS account: max loss-if-SL-hit as % of its own balance.
    # 0 = no guard. Per-slave because the cap only means anything next to the
    # account it guards: a $100 account whose broker's min lot already risks
    # 1.9% cannot pass a 1% cap at all, while the 100k next to it needs one.
    # A single global number blocked every trade on the small account forever.
    max_risk_pct_per_trade: float = 0.0
    # --- dynamic_pct mode ---
    # Risk % is picked from risk_tiers by how far the account has drifted from
    # initial_balance, instead of being a flat number. See utils/risk_ladder.py.
    # Account's starting balance. A prop firm may RAISE this after a scale-up,
    # so it is configured, never inferred from the first balance we see.
    # 0 leaves the mode unusable — trades are blocked rather than mis-sized.
    initial_balance: float = 0.0
    dynamic_base: str = "balance"  # "balance" or "equity" — what the ladder measures
    risk_tiers: list[dict] = field(default_factory=default_tiers_as_dicts)
    # News blocking: {impact: SECONDS} — e.g. {"High": 124.0}. An impact left
    # out (or set to 0) never blocks, so {} means no news restrictions at all.
    # Applied literally: 124 means 124, with no padding added anywhere.
    news_windows_s: dict[str, float] = field(default_factory=dict)
    # Legacy field, in MINUTES. Only ever read to migrate — see __post_init__.
    # Kept so an old config.yaml loads without losing its guard.
    news_windows: dict[str, float] = field(default_factory=dict)
    # Which currencies those windows apply to — e.g. ["USD"] for a slave that
    # only trades US indices. EMPTY MEANS ALL, so an existing config keeps
    # blocking exactly as it did; narrowing is opt-in. Global entries ("All":
    # bank holidays, Jackson Hole) always match.
    news_currencies: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.multiplier <= 0:
            raise ValueError(f"Slave '{self.name}': multiplier must be positive")
        if self.login <= 0:
            raise ValueError(f"Slave '{self.name}': invalid login")
        if self.symbol_mapping is None:
            self.symbol_mapping = {}
        if self.risk_mode not in RISK_MODES:
            self.risk_mode = RISK_MODE_MULTIPLIER
        if self.initial_balance < 0:
            raise ValueError(f"Slave '{self.name}': initial_balance must be >= 0")
        if self.max_risk_pct_per_trade < 0:
            raise ValueError(
                f"Slave '{self.name}': max_risk_pct_per_trade must be >= 0"
            )
        if self.dynamic_base not in ("balance", "equity"):
            self.dynamic_base = "balance"
        # Keep the stored ladder in the sorted/validated shape the copy path
        # sees, so config.yaml and the running engine can never disagree.
        self.risk_tiers = [
            {"up_to_pct": up, "risk_pct": risk}
            for up, risk in normalize_tiers(self.risk_tiers)
        ]
        # Windows are seconds now. A file written before that holds minutes,
        # and reading those as seconds would turn a 2-minute guard into a
        # 2-second one — protection all but removed, silently. So the legacy
        # field is converted, and only when the new one is absent: once
        # news_windows_s exists it is the truth and the old key is ignored.
        if self.news_windows_s:
            self.news_windows_s = _normalize_news_windows(self.news_windows_s)
        elif self.news_windows:
            self.news_windows_s = _news_minutes_to_seconds(self.news_windows)
        else:
            self.news_windows_s = {}
        self.news_windows = {}          # never written back; migration is one-way
        self.news_currencies = list(_normalize_news_currencies(self.news_currencies))

    def map_symbol(self, master_symbol: str) -> str:
        """Map master symbol to this slave's symbol."""
        return self.symbol_mapping.get(master_symbol, master_symbol)

    @property
    def slave_id(self) -> str:
        """Stable identifier: login@server. Immutable within a session."""
        return f"{self.login}@{self.server}"

    def get_terminal_path(self, fallback: str = "") -> str:
        """Get terminal path for this slave (own or fallback)."""
        return self.terminal_path or fallback


def make_slave_id(login: int, server: str) -> str:
    """Build a slave_id from raw login + server (for GUI code working with dicts)."""
    return f"{login}@{server}"


@dataclass
class PriceCalcPair:
    """One TradingView ticker mapped to a master symbol.

    ``last_diff`` caches the most recent (master - TV) offset so the strip can
    show a usable number on a cold start, before the first scan lands — the
    offset is broker-structural and barely moves within a session.
    """
    tv: str                     # "COMEX:GC1!" — EXCHANGE:TICKER as TradingView spells it
    master: str                 # "XAUUSD" — symbol on the master terminal
    last_diff: float = 0.0      # master_price - tv_price, from the last good scan
    last_diff_at: float = 0.0   # host epoch seconds of that scan


@dataclass
class PriceCalcConfig:
    """Real price calculator: TradingView quote vs master price."""
    enabled: bool = False
    refresh_sec: float = 5.0    # how often to poll the TV scanner
    pairs: list[PriceCalcPair] = field(default_factory=list)

    def __post_init__(self):
        # Don't hammer the (undocumented, unauthenticated) scanner endpoint.
        if self.refresh_sec < 1.0:
            self.refresh_sec = 1.0
        allowed = {f.name for f in _dc_fields(PriceCalcPair)}
        parsed = []
        for p in self.pairs:
            if isinstance(p, dict):
                if not p.get("tv") or not p.get("master"):
                    continue  # half-filled row from the Settings table — drop it
                p = PriceCalcPair(**{k: v for k, v in p.items() if k in allowed})
            parsed.append(p)
        self.pairs = parsed


@dataclass
class TerminalLifecycleConfig:
    """What happens to the MT5 terminals when the service stops.

    MEASURED (traces 2026-08-28, 14 warmups over 13 hours)
        Every second of warmup is `mt5.initialize`, and all of it is cold
        launch:

            10:05:50  44143 ms  Bybit    connect_ms=43955  action_ms=187
            10:05:51  45115 ms  5ers_7k  connect_ms=45077  action_ms= 38

        The warmup WORK is 38-187ms. The 11-45s is Windows starting a
        terminal that the previous Stop had just closed. Against an already
        running terminal the same call returns instantly — visible in one log
        where the master was initialized twice in a row: 10s, then 0s.

    So stopping the service no longer closes the terminals by default: the next
    warmup attaches to them instead of relaunching them. Closing on APP EXIT is
    unchanged and unconditional — that is where a terminal's own settings (EA
    inputs, chart state) get written, and losing those is the reason the clean
    shutdown exists at all.

    Set ``close_on_stop: true`` for the old behaviour.
    """
    close_on_stop: bool = False


@dataclass
class TracingConfig:
    """Which operations reach the trace file.

    The trace is only useful if it can be opened. Left unfiltered, three
    heartbeat operations were 100% of an 11.8 MB run — so a subscription, with
    the heartbeats muted by default.

    ``operations`` is an allowlist and wins when set: name one thing and the
    file contains only that, which is what chasing a specific bug wants.
    ``exclude`` is the denylist used otherwise; set it to an empty list to
    record everything.
    """
    enabled: bool = True
    operations: list[str] = field(default_factory=list)
    exclude: list[str] | None = None      # None = the tracer's own default

    def __post_init__(self):
        self.operations = [str(o).strip() for o in (self.operations or []) if str(o).strip()]
        if self.exclude is not None:
            self.exclude = [str(o).strip() for o in self.exclude if str(o).strip()]


@dataclass
class RetentionConfig:
    """How long the app's own output files are kept.

    Traces, logs and debug dumps roll by day or by session and are never
    otherwise removed, so without this the install grows without limit. Seven
    days is the default because that is the window in which a user still
    remembers the incident they are looking for; older files have only ever
    been read to answer "when did this start", which the trace's own daily
    naming already answers.

    ``days: 0`` keeps everything, for a machine under active investigation.
    Only files the app writes are ever deleted — see retention.SWEPT.
    """
    enabled: bool = True
    days: int = 7

    def __post_init__(self):
        self.days = int(self.days)
        if self.days < 0:
            self.days = 0

    @property
    def effective_days(self) -> int:
        """Retention actually applied: 0 (keep forever) when disabled."""
        return self.days if self.enabled else 0


@dataclass
class NewsConfig:
    """ForexFactory calendar: news windows that block new copies."""
    enabled: bool = False
    refresh_sec: float = 900.0   # weekly feed — 15 min is plenty

    def __post_init__(self):
        if self.refresh_sec < 60.0:
            self.refresh_sec = 60.0


@dataclass
class MasterConfig:
    """Master account configuration."""
    terminal_path: str = ""  # Path to master MT5 terminal
    poll_interval_ms: int = 500

    def __post_init__(self):
        if self.poll_interval_ms < 10:
            raise ValueError("poll_interval_ms must be at least 10ms")


@dataclass
class SlaveTerminalConfig:
    """Slave terminal configuration.

    The slaves *directory* is no longer stored here — see
    template_manager.slaves_dir() for the locked location.
    """
    terminal_path: str = ""  # Path to slave MT5 terminal (separate installation)
    # Template: virgin MT5 install captured once, used as the source folder
    # for every new slave. Avoids inheriting master broker config / state.
    template_path: str = ""        # absolute path to <slaves_dir>/_template
    template_source: str = ""      # original install folder the template was copied from
    template_created_at: str = ""  # ISO timestamp of seeding


@dataclass
class SettingsConfig:
    """General settings."""
    sync_existing_on_start: bool = True
    max_slippage_points: int = 0
    retry_attempts: int = 3
    retry_delay_ms: int = 1000
    log_level: str = "INFO"
    # Max deviation from entry as ratio of SL distance (0.2 = 20% of SL distance)
    # If current price is further than this from master entry, skip the trade
    max_entry_deviation_ratio: float = 0.2
    # When True, copy operations are paused (existing trades remain open)
    paused: bool = False
    toast_position: str = "Bottom-Right"
    maximize_master_on_start: bool = True
    # LEGACY, read only to migrate. The risk guard is per-slave now
    # (SlaveConfig.max_risk_pct_per_trade); a file written before that carries
    # its cap here, and Config.load copies it onto every slave that has none of
    # its own. Never written back — see Config.load and settings_tab._save.
    max_risk_pct_per_trade: float = 0.0
    # Debug/idle mode: trades are fully computed and logged but order_send is
    # skipped — simulate copy behavior with zero risk.
    debug_skip_execution: bool = False
    # EA push bridge: master EA pushes trade events over a localhost socket for
    # ~ms copy latency. Polling still runs as a fallback (external/mobile actions
    # + disaster recovery).
    ea_push_enabled: bool = True
    ea_push_port: int = 47800
    # Dispatch slave open/close/modify concurrently (fast). Turn OFF only if a
    # host suffers MT5 IPC timeouts under parallel terminal load (ARM64/Parallels).
    parallel_dispatch: bool = True
    # Warm up (cold-connect) all slave terminals concurrently instead of one at a
    # time. Each slave uses its own worker process/terminal, so this cuts warmup
    # from N×(connect+gap) to ~one connect. Turn OFF for the same IPC-timeout
    # reason as parallel_dispatch.
    parallel_warmup: bool = True

    def __post_init__(self):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        if self.log_level.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        self.log_level = self.log_level.upper()
        if self.max_entry_deviation_ratio < 0:
            raise ValueError("max_entry_deviation_ratio must be >= 0")
        if self.max_risk_pct_per_trade < 0:
            raise ValueError("max_risk_pct_per_trade must be >= 0")


@dataclass
class Config:
    """Main configuration container."""
    master: MasterConfig
    slave_terminal: SlaveTerminalConfig
    slaves: list[SlaveConfig]
    symbol_mapping: dict[str, str]
    filling_mode: dict[str, str] = field(default_factory=lambda: {"default": "IOC"})
    settings: SettingsConfig = field(default_factory=SettingsConfig)
    price_calc: PriceCalcConfig = field(default_factory=PriceCalcConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    terminals: TerminalLifecycleConfig = field(
        default_factory=TerminalLifecycleConfig
    )
    config_path: Path = field(default_factory=lambda: Path("config/config.yaml"))

    @staticmethod
    def _known(dc_cls, d: dict) -> dict:
        """Keep only keys that are fields of ``dc_cls``, dropping unknown/legacy
        keys (e.g. a removed 'sl_mode') so an older config.yaml never crashes
        loading with 'unexpected keyword argument'."""
        allowed = {f.name for f in _dc_fields(dc_cls)}
        dropped = [k for k in d if k not in allowed]
        if dropped:
            logging.getLogger("metact").info(
                "Ignoring unknown config key(s) for %s: %s", dc_cls.__name__, dropped
            )
        return {k: v for k, v in d.items() if k in allowed}

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from YAML file."""
        if config_path is None:
            from .paths import config_path as default_config_path
            config_path = default_config_path()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                "Open the GUI and save settings once to create it, or run "
                "install.bat when running from source."
            )

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError("Configuration file is empty")

        # Parse master config
        master_data = data.get("master", {})
        master = MasterConfig(**cls._known(MasterConfig, master_data))

        # Parse slave terminal config. Drop legacy `directory` key — slaves
        # directory is now locked by template_manager.slaves_dir(). Old
        # configs may carry a stale value; ignoring it avoids surprising
        # the user with two competing notions of "slaves directory".
        slave_terminal_data = dict(data.get("slave_terminal", {}) or {})
        legacy_directory = slave_terminal_data.pop("directory", None)
        if legacy_directory:
            logging.getLogger("metact").info(
                "Ignoring legacy slave_terminal.directory=%r — using locked path "
                "from template_manager.slaves_dir() instead.",
                legacy_directory,
            )
        slave_terminal = SlaveTerminalConfig(**cls._known(SlaveTerminalConfig, slave_terminal_data))

        # Parse slave configs (load ALL, including disabled — needed for
        # closing positions on slaves that were disabled after opening)
        slaves_data = data.get("slaves", [])
        if not slaves_data:
            logging.getLogger("metact").warning(
                "No slave accounts configured in config.yaml"
            )

        slaves = [SlaveConfig(**cls._known(SlaveConfig, sd)) for sd in slaves_data]

        # Parse symbol mapping
        symbol_mapping = data.get("symbol_mapping", {}) or {}

        # Parse filling mode configuration
        filling_mode_data = data.get("filling_mode", {}) or {}
        if "default" not in filling_mode_data:
            filling_mode_data["default"] = "IOC"

        # Parse settings
        settings_data = data.get("settings", {})
        settings = SettingsConfig(**cls._known(SettingsConfig, settings_data))

        # The risk guard used to be one global number. Hand it to every slave
        # that has not been given its own, so a file written before the move
        # keeps guarding exactly as it did, then drop it: the global value is
        # never read again and never written back (one-way, like news_windows).
        if settings.max_risk_pct_per_trade > 0:
            legacy_guard = settings.max_risk_pct_per_trade
            migrated = []
            for slave, sd in zip(slaves, slaves_data):
                if "max_risk_pct_per_trade" not in sd:
                    slave.max_risk_pct_per_trade = legacy_guard
                    migrated.append(slave.name)
            if migrated:
                logging.getLogger("metact").info(
                    "Migrated global settings.max_risk_pct_per_trade=%.2f%% onto "
                    "slave(s) %s — the guard is per-slave now",
                    legacy_guard, ", ".join(migrated),
                )
            settings.max_risk_pct_per_trade = 0.0

        # Price calculator (optional section — absent in older configs)
        price_calc_data = data.get("price_calc", {}) or {}
        price_calc = PriceCalcConfig(**cls._known(PriceCalcConfig, price_calc_data))

        # News calendar (optional section — absent in older configs)
        news_data = data.get("news", {}) or {}
        news = NewsConfig(**cls._known(NewsConfig, news_data))

        tracing_data = data.get("tracing", {}) or {}
        tracing = TracingConfig(**cls._known(TracingConfig, tracing_data))

        retention_data = data.get("retention", {}) or {}
        retention = RetentionConfig(**cls._known(RetentionConfig, retention_data))

        terminals_data = data.get("terminals", {}) or {}
        terminals = TerminalLifecycleConfig(
            **cls._known(TerminalLifecycleConfig, terminals_data)
        )

        return cls(
            master=master,
            slave_terminal=slave_terminal,
            slaves=slaves,
            symbol_mapping=symbol_mapping,
            filling_mode=filling_mode_data,
            settings=settings,
            price_calc=price_calc,
            news=news,
            tracing=tracing,
            retention=retention,
            terminals=terminals,
            config_path=config_path,
        )

    def get_enabled_slaves(self) -> list[SlaveConfig]:
        """Get list of enabled slave accounts."""
        return [s for s in self.slaves if s.enabled]

    def map_symbol(self, master_symbol: str) -> str:
        """Map master symbol to slave symbol."""
        return self.symbol_mapping.get(master_symbol, master_symbol)


# MT5 filling mode constants (duplicated here to avoid importing MetaTrader5
# at module level — MT5 may not be installed in all environments, e.g. tests)
_FILLING_IOC = 1
_FILLING_FOK = 0
_FILLING_RETURN = 2

_FILLING_MODE_MAP: dict[str, int] = {
    "IOC": _FILLING_IOC,
    "FOK": _FILLING_FOK,
    "RETURN": _FILLING_RETURN,
}


def get_filling_mode(symbol: str, filling_mode_config: dict[str, str]) -> int:
    """Look up the MT5 filling mode constant for a symbol.

    Args:
        symbol: Trading symbol name (e.g. "EURUSD").
        filling_mode_config: Dict with ``"default"`` key and optional
            per-symbol overrides (e.g. ``{"default": "IOC", "XAUUSD": "FOK"}``).

    Returns:
        Integer filling mode constant usable in MT5 order requests.
    """
    mode_str = filling_mode_config.get(
        symbol, filling_mode_config.get("default", "IOC")
    ).upper()
    return _FILLING_MODE_MAP.get(mode_str, _FILLING_IOC)
