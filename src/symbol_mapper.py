"""Symbol mapping and normalization."""
from .config import Config
from .mt5_client import MT5Client
from .utils.logging import get_logger


class SymbolMapper:
    """Handles symbol mapping between master and slave accounts."""

    def __init__(self, config: Config, mt5_client: MT5Client):
        self.config = config
        self.mt5 = mt5_client
        self.logger = get_logger()
        self._cache: dict[str, str] = {}

    def map_symbol(self, master_symbol: str) -> str:
        """
        Map master symbol to slave symbol.

        Priority:
        1. Explicit mapping in config
        2. Direct symbol name (if exists on slave)
        """
        # Check cache first
        if master_symbol in self._cache:
            return self._cache[master_symbol]

        # Check explicit mapping
        if master_symbol in self.config.symbol_mapping:
            mapped = self.config.symbol_mapping[master_symbol]
            self._cache[master_symbol] = mapped
            return mapped

        # Use direct symbol name
        self._cache[master_symbol] = master_symbol
        return master_symbol

    def validate_symbol(self, symbol: str) -> bool:
        """Check if symbol exists and is tradeable."""
        info = self.mt5.get_symbol_info(symbol)
        if info is None:
            return False

        # Check trade mode (0 = disabled)
        if info.trade_mode == 0:
            self.logger.warning(f"Symbol '{symbol}' trading is disabled")
            return False

        return True

    def clear_cache(self):
        """Clear symbol cache."""
        self._cache.clear()
