"""Master account monitoring."""
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import MasterConfig
from .mt5_client import MT5Client, Position, AccountInfo
from .utils.logging import get_logger


@dataclass
class PositionChange:
    """Represents a change in positions."""
    new_positions: list[Position]
    closed_tickets: list[int]
    modified_positions: list[Position]  # SL/TP changed
    source: str  # "TCP" (EA push) or "Polling" — how it was detected (always set)
    reduced_positions: list[Position] = field(default_factory=list)  # volume scaled down (carries NEW volume)
    recv_ms: float = 0.0  # host epoch ms when this change was ingested (latency waterfall)
    #: Where this change came from, precisely: ``TCP#2/17`` is the 17th line of
    #: the 2nd EA connection, ``POLL#412`` the 412th poll cycle of this run.
    #: Carried into the copy trace so a trade can be walked back to the signal
    #: that caused it — the difference between "the EA sent it twice" and "we
    #: polled it twice", which are indistinguishable once the copy is under way.
    signal_id: str = ""


class MasterMonitor:
    """Monitors master account for position changes."""

    def __init__(self, mt5_client: MT5Client, terminal_path: str = ""):
        self.mt5 = mt5_client
        self.terminal_path = terminal_path
        self.logger = get_logger()
        self._last_positions: dict[int, Position] = {}
        self._account_info: AccountInfo | None = None
        self._poll_seq = 0

    def connect(self) -> bool:
        """Connect to master terminal (reads whatever account is logged in)."""
        if not self.mt5.initialize(self.terminal_path):
            self.logger.error("Failed to initialize MT5 master terminal")
            return False

        self._account_info = self.mt5.get_account_info()
        if self._account_info is None:
            self.logger.error("Failed to get master account info")
            return False

        self.logger.info(
            f"Connected to master account {self._account_info.login} "
            f"(leverage: 1:{self._account_info.leverage})"
        )
        return True

    def reconnect(self) -> bool:
        """Reconnect only if the connection is actually lost."""
        try:
            info = self.mt5.get_account_info()
            if info is not None:
                return True  # Still connected
        except Exception:
            pass
        self.logger.info("Master connection lost, reconnecting...")
        return self.connect()

    def health(self) -> tuple[bool, bool | None]:
        """``(connected, algo_trading)`` — the master terminal as it IS.

        WHY THIS EXISTS
            Nothing measured the master. The Trading tab's Master pill was lit
            from "is the service running", and the Terminals tab inferred the
            master from a PowerShell process listing. Kill the terminal and
            both kept saying it was fine, because neither had asked it.

            Asking is cheap: ``terminal_info`` is a local struct read on a
            connection the service already holds.

        ``algo_trading`` is None when the terminal could not be asked at all —
        distinct from False, which is a switch the operator can go and flip.
        """
        state = self.mt5.terminal_state()
        if not state:
            return False, None
        return bool(state.get("connected", False)), state.get("algo_trading")

    def ensure_connected(self) -> bool:
        """Reconnect if the terminal has gone. Returns whether it is up now.

        Called on the service's own cycle, so a terminal that was killed and
        relaunched is picked up without waiting for the next trade — which was
        the old behaviour, and meant a relaunched master looked dead until
        something happened to copy.
        """
        connected, _ = self.health()
        if connected:
            return True
        self.logger.warning("Master terminal is not connected — reconnecting")
        return self.connect()

    def get_account_info(self) -> AccountInfo | None:
        """Get master account info."""
        return self._account_info

    def get_leverage(self) -> int:
        """Get master account leverage."""
        if self._account_info:
            return self._account_info.leverage
        return 100  # Default fallback

    def get_all_positions(self) -> list[Position]:
        """Get all current master positions."""
        return self.mt5.get_positions()

    @property
    def last_positions(self) -> dict[int, Position]:
        """Last position snapshot from detect_changes() (no extra MT5 call)."""
        return self._last_positions

    def snapshot_positions(self):
        """Take snapshot of current positions for change detection."""
        positions = self.get_all_positions()
        self._last_positions = {p.ticket: p for p in positions}
        self.logger.debug(f"Snapshot: {len(self._last_positions)} positions")

    def detect_changes(self) -> PositionChange:
        """Detect position changes since last snapshot."""
        t0 = time.perf_counter()
        self._poll_seq += 1
        current = self.mt5.get_positions()
        fetch_ms = (time.perf_counter() - t0) * 1000
        if fetch_ms > 50:
            self.logger.warning("Slow position fetch: %.0fms", fetch_ms)

        current_map = {p.ticket: p for p in current}

        current_tickets = set(current_map.keys())
        last_tickets = set(self._last_positions.keys())

        # New positions
        new_tickets = current_tickets - last_tickets
        new_positions = [current_map[t] for t in new_tickets]

        # Closed positions
        closed_tickets = list(last_tickets - current_tickets)

        # Modified (SL/TP changed) + reduced (volume scaled down = partial close)
        modified_positions = []
        reduced_positions = []
        for ticket in current_tickets & last_tickets:
            curr = current_map[ticket]
            prev = self._last_positions[ticket]
            if curr.sl != prev.sl or curr.tp != prev.tp:
                modified_positions.append(curr)
            if curr.volume < prev.volume - 1e-9:
                reduced_positions.append(curr)  # carries the NEW (smaller) volume

        # Update snapshot
        self._last_positions = current_map

        return PositionChange(
            new_positions=new_positions,
            closed_tickets=closed_tickets,
            modified_positions=modified_positions,
            source="Polling",
            reduced_positions=reduced_positions,
            signal_id=f"POLL#{self._poll_seq}",
        )

    def get_position(self, ticket: int) -> Position | None:
        """Get specific position by ticket."""
        return self.mt5.get_position_by_ticket(ticket)
