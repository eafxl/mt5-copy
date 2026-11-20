"""Slave account operations."""
from dataclasses import dataclass, field

from .config import SlaveConfig
from .mt5_client import MT5Client, Position, AccountInfo, OrderType, SymbolInfo
from .utils.logging import get_logger
from .utils.tracer import get_tracer
from .config import (
    RISK_MODE_MULTIPLIER, RISK_MODE_FIXED_VOLUME, RISK_MODE_FIXED_USD,
    RISK_MODE_PCT_DEPOSIT, RISK_MODE_DYNAMIC_PCT,
)
from .utils.pip_calculator import (
    calculate_slave_volume,
    calculate_slave_sl_tp,
    check_entry_deviation,
    calculate_risk_based_volume,
)
from .utils.risk_ladder import resolve_risk_pct


@dataclass
class SlaveContext:
    """Context for a slave account."""
    config: SlaveConfig
    account_info: AccountInfo
    leverage: int
    # Cached symbol info: master_symbol -> (slave_symbol, SymbolInfo)
    symbol_cache: dict[str, tuple[str, SymbolInfo]] = field(default_factory=dict)


class SlaveManager:
    """Manages operations on slave accounts."""

    def __init__(
        self,
        mt5_client: MT5Client,
        terminal_path: str = "",
        max_slippage: int = 0,
        max_entry_deviation_ratio: float = 0.2,
    ):
        self.mt5 = mt5_client
        self.terminal_path = terminal_path
        self.max_slippage = max_slippage
        self.max_entry_deviation_ratio = max_entry_deviation_ratio
        self.logger = get_logger()
        self._current_slave: SlaveContext | None = None

    def connect(self, slave_config: SlaveConfig) -> bool:
        """Connect to a slave account using slave terminal.

        Optimized: skips re-login if already connected to the same account.
        """
        tracer = get_tracer()

        # Check if already connected to this slave
        if self._current_slave is not None:
            if self._current_slave.config.login == slave_config.login:
                # Already connected to this account, verify still valid
                account_info = self.mt5.get_account_info()
                if account_info and account_info.login == slave_config.login:
                    tracer.event("slave_connect_cached", slave=slave_config.name)
                    return True
                # Connection lost, need to reconnect
                self.logger.debug(f"Connection to {slave_config.name} lost, reconnecting...")

        with tracer.span("slave_connect") as span:
            span.set_tag("slave", slave_config.name)

            # Initialize slave terminal
            if not self.mt5.initialize(self.terminal_path):
                self.logger.error(f"Failed to initialize slave terminal")
                span.set_tag("result", "init_failed")
                return False

            # Login to slave account
            if not self.mt5.login(
                slave_config.login,
                slave_config.password,
                slave_config.server,
            ):
                self.logger.error(f"Failed to login to slave '{slave_config.name}'")
                span.set_tag("result", "login_failed")
                return False

            account_info = self.mt5.get_account_info()
            if account_info is None:
                self.logger.error(f"Failed to get account info for '{slave_config.name}'")
                span.set_tag("result", "account_info_failed")
                return False

            self._current_slave = SlaveContext(
                config=slave_config,
                account_info=account_info,
                leverage=account_info.leverage,
                symbol_cache={},  # Fresh cache for this slave
            )

            span.set_tags(result="ok", leverage=account_info.leverage)

            self.logger.info(
                f"Connected to slave '{slave_config.name}' "
                f"(login: {account_info.login}, leverage: 1:{account_info.leverage})"
            )

            return True

    def _get_symbol_info(self, master_symbol: str) -> tuple[str, SymbolInfo | None]:
        """
        Get slave symbol and its info for a master symbol.
        Uses per-slave symbol mapping and caches result.

        Returns (slave_symbol, symbol_info) or (slave_symbol, None) if not available.
        """
        if self._current_slave is None:
            return master_symbol, None

        tracer = get_tracer()
        cache = self._current_slave.symbol_cache

        # Check cache first
        if master_symbol in cache:
            tracer.event("symbol_cache_hit", master_symbol=master_symbol)
            return cache[master_symbol]

        with tracer.span("symbol_mapping") as span:
            span.set_tags(master_symbol=master_symbol, slave=self._current_slave.config.name)

            # Map symbol using slave-specific mapping (defaults to master symbol)
            slave_symbol = self._current_slave.config.map_symbol(master_symbol)
            span.set_tag("slave_symbol", slave_symbol)

            # Get symbol info from MT5
            symbol_info = self.mt5.get_symbol_info(slave_symbol)

            # Cache the result (even if None, to avoid repeated lookups)
            if symbol_info is not None:
                cache[master_symbol] = (slave_symbol, symbol_info)
                span.set_tags(result="cached", cached=True)
                self.logger.debug(
                    f"[{self._current_slave.config.name}] Cached symbol mapping: "
                    f"{master_symbol} -> {slave_symbol}"
                )
            else:
                span.set_tag("result", "not_found")

            return slave_symbol, symbol_info

    def get_current_slave(self) -> SlaveContext | None:
        """Get current slave context."""
        return self._current_slave

    def get_positions(self) -> list[Position]:
        """Get all positions on current slave."""
        return self.mt5.get_positions()

    def get_position_by_ticket(self, ticket: int) -> Position | None:
        """Get position by ticket on current slave."""
        return self.mt5.get_position_by_ticket(ticket)

    def open_position(
        self,
        master_position: Position,
        master_leverage: int,
        trade_id: str = "",
    ) -> tuple[int | None, float]:
        """
        Open a copied position on current slave.

        Args:
            master_position: The master position to copy
            master_leverage: Master account leverage
            trade_id: Unique trade ID for cross-account tracking

        Returns (ticket, volume) or (None, 0) on failure.
        """
        if self._current_slave is None:
            return None, 0

        slave = self._current_slave
        config = slave.config

        # Map symbol and get info (uses per-slave mapping + cache)
        slave_symbol, symbol_info = self._get_symbol_info(master_position.symbol)

        if symbol_info is None:
            self.logger.error(
                f"Symbol '{slave_symbol}' not available on '{config.name}'"
            )
            return None, 0

        # Get current price for slave
        bid, ask = self.mt5.get_current_price(slave_symbol)
        if bid == 0:
            self.logger.error(f"Cannot get price for '{slave_symbol}'")
            return None, 0

        # Check entry deviation - skip if price moved favorably (anti-chase)
        # For BUY: block if price rose; for SELL: block if price fell
        should_skip, price_deviation, max_deviation = check_entry_deviation(
            master_entry=master_position.price_open,
            master_sl=master_position.sl,
            master_current_price=master_position.price_current,
            max_entry_deviation_ratio=self.max_entry_deviation_ratio,
            is_buy=master_position.is_buy,
        )
        if should_skip:
            direction = "rose" if master_position.is_buy else "fell"
            self.logger.warning(
                f"[{config.name}] Skipping {master_position.symbol}: "
                f"price {direction} {price_deviation:.5f} > max {max_deviation:.5f} "
                f"(master entry: {master_position.price_open}, "
                f"master current: {master_position.price_current})"
            )
            return None, 0

        tracer = get_tracer()

        # Entry price for slave (current market price) - reuse bid/ask from above
        slave_entry = ask if master_position.is_buy else bid

        # Calculate SL/TP based on pip distance
        with tracer.span("calculate_sl_tp") as sltp_span:
            sl, tp = calculate_slave_sl_tp(
                master_entry=master_position.price_open,
                master_sl=master_position.sl,
                master_tp=master_position.tp,
                slave_entry=slave_entry,
                is_buy=master_position.is_buy,
                symbol_digits=symbol_info.digits,
            )
            sltp_span.set_tags(
                master_entry=master_position.price_open,
                slave_entry=slave_entry,
                master_sl=master_position.sl,
                master_tp=master_position.tp,
                slave_sl=sl,
                slave_tp=tp,
            )

        # Calculate volume based on risk mode
        risk_mode = getattr(config, 'risk_mode', RISK_MODE_MULTIPLIER)
        with tracer.span("calculate_volume") as vol_span:
            if risk_mode == RISK_MODE_DYNAMIC_PCT:
                # Risk % from the tier ladder keyed on drift from the account's
                # configured initial balance. An unusable ladder or a missing SL
                # leaves volume at 0 — the caller reports it rather than sizing
                # by some other rule the operator never picked.
                dynamic_base = getattr(config, 'dynamic_base', 'balance')
                base_value = (
                    slave.account_info.equity if dynamic_base == "equity"
                    else slave.account_info.balance
                )
                ladder_pct, ladder_reason = resolve_risk_pct(
                    getattr(config, 'initial_balance', 0.0),
                    base_value,
                    getattr(config, 'risk_tiers', []),
                    0.0,
                )
                computed_risk_usd = base_value * ladder_pct / 100.0
                volume = 0.0
                if ladder_pct > 0 and sl > 0:
                    volume = calculate_risk_based_volume(
                        risk_usd=computed_risk_usd,
                        entry_price=slave_entry,
                        sl_price=sl,
                        trade_tick_value=symbol_info.trade_tick_value,
                        trade_tick_size=symbol_info.trade_tick_size,
                        min_volume=symbol_info.volume_min,
                        max_volume=symbol_info.volume_max,
                        volume_step=symbol_info.volume_step,
                    )
                vol_span.set_tags(
                    risk_mode="dynamic_pct",
                    risk_ladder=ladder_reason,
                    risk_pct=ladder_pct,
                    dynamic_base=dynamic_base,
                    base_value=base_value,
                    initial_balance=getattr(config, 'initial_balance', 0.0),
                    computed_risk_usd=computed_risk_usd,
                    slave_volume=volume,
                )
            elif risk_mode == RISK_MODE_PCT_DEPOSIT and getattr(config, 'risk_pct', 0) > 0 and sl > 0:
                # % of Deposit mode - compute risk_usd from account balance/equity
                risk_pct_base = getattr(config, 'risk_pct_base', 'balance')
                base_value = (
                    slave.account_info.equity if risk_pct_base == "equity"
                    else slave.account_info.balance
                )
                computed_risk_usd = base_value * config.risk_pct / 100.0
                volume = calculate_risk_based_volume(
                    risk_usd=computed_risk_usd,
                    entry_price=slave_entry,
                    sl_price=sl,
                    trade_tick_value=symbol_info.trade_tick_value,
                    trade_tick_size=symbol_info.trade_tick_size,
                    min_volume=symbol_info.volume_min,
                    max_volume=symbol_info.volume_max,
                    volume_step=symbol_info.volume_step,
                )
                vol_span.set_tags(
                    risk_mode="pct_deposit",
                    risk_pct=config.risk_pct,
                    risk_pct_base=risk_pct_base,
                    base_value=base_value,
                    computed_risk_usd=computed_risk_usd,
                    slave_volume=volume,
                )
            elif risk_mode == RISK_MODE_FIXED_VOLUME and config.fixed_volume > 0:
                # Fixed volume mode - use configured lot size directly
                volume = config.fixed_volume
                # Clamp to broker limits
                volume = max(symbol_info.volume_min, min(symbol_info.volume_max, volume))
                # Round to volume step
                volume = round(volume / symbol_info.volume_step) * symbol_info.volume_step
                volume = round(volume, 2)
                vol_span.set_tags(
                    risk_mode="fixed_volume",
                    fixed_volume=config.fixed_volume,
                    slave_volume=volume,
                )
            elif risk_mode == RISK_MODE_FIXED_USD and config.fixed_risk_usd > 0 and sl > 0:
                # Fixed USD risk mode - calculate volume so SL loss = fixed USD
                volume = calculate_risk_based_volume(
                    risk_usd=config.fixed_risk_usd,
                    entry_price=slave_entry,
                    sl_price=sl,
                    trade_tick_value=symbol_info.trade_tick_value,
                    trade_tick_size=symbol_info.trade_tick_size,
                    min_volume=symbol_info.volume_min,
                    max_volume=symbol_info.volume_max,
                    volume_step=symbol_info.volume_step,
                )
                vol_span.set_tags(
                    risk_mode="fixed_usd",
                    fixed_risk_usd=config.fixed_risk_usd,
                    slave_entry=slave_entry,
                    slave_sl=sl,
                    slave_volume=volume,
                )
            else:
                # Multiplier mode (default) - use leverage-adjusted multiplier
                volume = calculate_slave_volume(
                    master_volume=master_position.volume,
                    master_leverage=master_leverage,
                    slave_leverage=slave.leverage,
                    multiplier=config.multiplier,
                    adjust_by_leverage=config.adjust_by_leverage,
                    min_volume=symbol_info.volume_min,
                    max_volume=symbol_info.volume_max,
                    volume_step=symbol_info.volume_step,
                )
                vol_span.set_tags(
                    risk_mode="multiplier",
                    master_volume=master_position.volume,
                    slave_volume=volume,
                    master_leverage=master_leverage,
                    slave_leverage=slave.leverage,
                    multiplier=config.multiplier,
                )

        if volume <= 0:
            # dynamic_pct with no initial balance / no tiers / no SL lands here.
            # Sending 0 lots would just earn a broker reject with no explanation.
            self.logger.error(
                f"[{config.name}] Sized 0 lots for {slave_symbol} "
                f"(risk_mode={risk_mode}) — not opening"
            )
            return None, 0

        # Build comment with trade_id for cross-account tracking
        # Format: "CP:{trade_id}" (short to fit MT5's ~31 char limit)
        comment = f"CP:{trade_id}" if trade_id else f"Copy:{master_position.ticket}"

        # Open position
        ticket = self.mt5.open_position(
            symbol=slave_symbol,
            order_type=master_position.type,
            volume=volume,
            sl=sl,
            tp=tp,
            deviation=self.max_slippage,
            magic=master_position.ticket,  # Store master ticket as magic
            comment=comment,
        )

        if ticket:
            self.logger.info(
                f"[{config.name}] Opened {slave_symbol} "
                f"{'BUY' if master_position.is_buy else 'SELL'} "
                f"{volume} lots (master: {master_position.volume}, trade_id: {trade_id})"
            )
            return ticket, volume

        return None, 0

    def close_position(self, ticket: int) -> bool:
        """Close position by ticket."""
        if self._current_slave is None:
            return False

        config = self._current_slave.config
        result = self.mt5.close_position(ticket, self.max_slippage)

        if result:
            self.logger.info(f"[{config.name}] Closed position {ticket}")

        return result

    def modify_position(
        self,
        ticket: int,
        master_position: Position,
        master_entry: float,
    ) -> bool:
        """Modify position SL/TP based on master changes."""
        if self._current_slave is None:
            return False

        config = self._current_slave.config

        # Get slave position
        slave_position = self.get_position_by_ticket(ticket)
        if slave_position is None:
            self.logger.warning(f"[{config.name}] Position {ticket} not found")
            return False

        # Get symbol info (uses per-slave mapping + cache)
        slave_symbol, symbol_info = self._get_symbol_info(master_position.symbol)
        if symbol_info is None:
            return False

        # Calculate new SL/TP
        sl, tp = calculate_slave_sl_tp(
            master_entry=master_entry,
            master_sl=master_position.sl,
            master_tp=master_position.tp,
            slave_entry=slave_position.price_open,
            is_buy=master_position.is_buy,
            symbol_digits=symbol_info.digits,
        )

        # Skip if no change
        if sl == slave_position.sl and tp == slave_position.tp:
            return True

        result = self.mt5.modify_position(ticket, sl, tp)

        if result:
            self.logger.info(
                f"[{config.name}] Modified position {ticket}: SL={sl}, TP={tp}"
            )

        return result

    def sync_position(
        self,
        master_position: Position,
        master_leverage: int,
        trade_id: str = "",
    ) -> tuple[int | None, float]:
        """
        Sync a master position (for initial sync).
        Same as open_position but with different logging.
        """
        if self._current_slave is None:
            return None, 0

        self.logger.info(
            f"[{self._current_slave.config.name}] Syncing position "
            f"{master_position.symbol} from master (trade_id: {trade_id})"
        )

        return self.open_position(master_position, master_leverage, trade_id)
