"""Position state management with in-memory storage."""
import uuid
from dataclasses import dataclass, field
from typing import Any

from .utils.logging import get_logger


def generate_trade_id() -> str:
    """Generate a short unique trade ID (8 chars)."""
    return uuid.uuid4().hex[:8].upper()


@dataclass
class SlavePosition:
    """Slave position info."""
    ticket: int
    volume: float
    sl: float = 0.0
    tp: float = 0.0


@dataclass
class TrackedPosition:
    """Tracked position with master and slave info."""
    master_ticket: int
    symbol: str
    order_type: int
    master_volume: float
    master_sl: float
    master_tp: float
    trade_id: str = ""  # Unique ID for this trade across all accounts
    slaves: dict[str, SlavePosition] = field(default_factory=dict)

    def __post_init__(self):
        """Generate trade_id if not provided."""
        if not self.trade_id:
            self.trade_id = generate_trade_id()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "master_ticket": self.master_ticket,
            "symbol": self.symbol,
            "order_type": self.order_type,
            "master_volume": self.master_volume,
            "master_sl": self.master_sl,
            "master_tp": self.master_tp,
            "trade_id": self.trade_id,
            "slaves": {
                name: {
                    "ticket": sp.ticket,
                    "volume": sp.volume,
                    "sl": sp.sl,
                    "tp": sp.tp,
                }
                for name, sp in self.slaves.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedPosition":
        """Create from dictionary."""
        slaves = {}
        for name, sp_data in data.get("slaves", {}).items():
            slaves[name] = SlavePosition(
                ticket=sp_data["ticket"],
                volume=sp_data["volume"],
                sl=sp_data.get("sl", 0.0),
                tp=sp_data.get("tp", 0.0),
            )

        # Handle legacy data without trade_id
        trade_id = data.get("trade_id", "")
        if not trade_id:
            trade_id = generate_trade_id()

        return cls(
            master_ticket=data["master_ticket"],
            symbol=data["symbol"],
            order_type=data["order_type"],
            master_volume=data["master_volume"],
            master_sl=data.get("master_sl", 0.0),
            master_tp=data.get("master_tp", 0.0),
            trade_id=trade_id,
            slaves=slaves,
        )


class PositionTracker:
    """Tracks position mappings between master and slaves.

    Uses in-memory dict — starts empty on each app launch.
    Positions are discovered from the MT5 API on startup.
    """

    def __init__(self):
        self._positions: dict[str, TrackedPosition] = {}
        self.logger = get_logger()
        self.logger.info("Position tracker initialized (in-memory)")

    def add_position(
        self,
        master_ticket: int,
        symbol: str,
        order_type: int,
        master_volume: float,
        master_sl: float = 0.0,
        master_tp: float = 0.0,
        trade_id: str = "",
    ) -> TrackedPosition:
        """Add a new tracked position. Generates trade_id if not provided."""
        position = TrackedPosition(
            master_ticket=master_ticket,
            symbol=symbol,
            order_type=order_type,
            master_volume=master_volume,
            master_sl=master_sl,
            master_tp=master_tp,
            trade_id=trade_id,  # Will auto-generate if empty
        )
        self._positions[str(master_ticket)] = position
        self.logger.debug(f"Added position {master_ticket} with trade_id={position.trade_id}")
        return position

    def claim_position(
        self,
        master_ticket: int,
        symbol: str,
        order_type: int,
        master_volume: float,
        master_sl: float = 0.0,
        master_tp: float = 0.0,
        trade_id: str = "",
    ) -> tuple["TrackedPosition", bool]:
        """Atomically claim a master ticket for copying.

        Returns (position, is_new). ``is_new`` is False if the ticket was
        ALREADY tracked — the caller must then NOT copy again. This is the
        single dedup point that lets the EA-push and polling paths run
        concurrently without ever copying the same trade twice: the check and
        the insert happen together with no await in between, so exactly one
        caller wins the claim.
        """
        key = str(master_ticket)
        existing = self._positions.get(key)
        if existing is not None:
            return existing, False
        position = TrackedPosition(
            master_ticket=master_ticket,
            symbol=symbol,
            order_type=order_type,
            master_volume=master_volume,
            master_sl=master_sl,
            master_tp=master_tp,
            trade_id=trade_id,
        )
        self._positions[key] = position
        self.logger.debug(
            f"Claimed position {master_ticket} with trade_id={position.trade_id}"
        )
        return position, True

    def add_slave_position(
        self,
        master_ticket: int,
        slave_id: str,
        slave_ticket: int,
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
    ):
        """Add slave position to tracked position."""
        position = self._positions.get(str(master_ticket))
        if position is None:
            self.logger.warning(f"Master position {master_ticket} not tracked")
            return

        position.slaves[slave_id] = SlavePosition(
            ticket=slave_ticket,
            volume=volume,
            sl=sl,
            tp=tp,
        )

    def update_master_volume(self, master_ticket: int, volume: float):
        """Record the master position's new volume after a scale-out."""
        position = self._positions.get(str(master_ticket))
        if position is not None:
            position.master_volume = volume

    def update_slave_volume(self, master_ticket: int, slave_id: str, volume: float):
        """Record a slave's new volume after a partial close."""
        position = self._positions.get(str(master_ticket))
        if position is not None and slave_id in position.slaves:
            position.slaves[slave_id].volume = volume

    def update_master_sl_tp(self, master_ticket: int, sl: float, tp: float):
        """Update master position SL/TP."""
        position = self._positions.get(str(master_ticket))
        if position:
            position.master_sl = sl
            position.master_tp = tp

    def update_slave_sl_tp(
        self, master_ticket: int, slave_id: str, sl: float, tp: float
    ):
        """Update slave position SL/TP."""
        position = self._positions.get(str(master_ticket))
        if position and slave_id in position.slaves:
            position.slaves[slave_id].sl = sl
            position.slaves[slave_id].tp = tp

    def claim_close(self, master_ticket: int) -> "TrackedPosition | None":
        """Atomically take ownership of closing a master ticket.

        Pops and returns the tracked position, or None if another caller
        already took it. Mirrors ``claim_position`` on the open path: the EA
        push and polling loops both detect the same close, and without this
        both passed the old ``get_position() is None`` guard (removal happened
        only at the END of the close handler, after awaits). That produced two
        close dispatches, two TRADE_CLOSED events with disagreeing profits,
        and a double-counted stop in the circuit breaker.
        """
        return self._positions.pop(str(master_ticket), None)

    def remove_position(self, master_ticket: int):
        """Remove tracked position."""
        key = str(master_ticket)
        if key in self._positions:
            del self._positions[key]

    def get_position(self, master_ticket: int) -> TrackedPosition | None:
        """Get tracked position by master ticket."""
        return self._positions.get(str(master_ticket))

    def get_all_positions(self) -> dict[int, TrackedPosition]:
        """Get all tracked positions."""
        return {int(key): pos for key, pos in self._positions.items()}

    def get_slave_ticket(self, master_ticket: int, slave_id: str) -> int | None:
        """Get slave ticket for a master position."""
        position = self._positions.get(str(master_ticket))
        if position and slave_id in position.slaves:
            return position.slaves[slave_id].ticket
        return None

    def has_slave_position(self, master_ticket: int, slave_id: str) -> bool:
        """Check if slave has position for master ticket."""
        position = self._positions.get(str(master_ticket))
        return position is not None and slave_id in position.slaves

    def is_tracked(self, master_ticket: int) -> bool:
        """Check if master position is tracked."""
        return str(master_ticket) in self._positions

    def get_trade_id(self, master_ticket: int) -> str | None:
        """Get trade_id for a master position."""
        position = self._positions.get(str(master_ticket))
        return position.trade_id if position else None

    def get_master_tickets(self) -> set[int]:
        """Get all tracked master tickets."""
        return {int(key) for key in self._positions.keys()}

    def cleanup_missing_slaves(self, active_slave_ids: set[str]):
        """Remove slave entries for slaves no longer in config."""
        for key, position in self._positions.items():
            to_remove = [sid for sid in position.slaves if sid not in active_slave_ids]
            for sid in to_remove:
                del position.slaves[sid]
