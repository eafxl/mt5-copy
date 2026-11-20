"""Per-slave runtime state machine.

Thread-safe singleton tracking per-slave status: On / Off / Paused.
Used by the Terminals tab (GUI) and TradeCopier (service thread) for per-slave
copy control without restarting the service.

CLI mode compatibility: is_copy_active() returns True for untracked slaves.
"""
import logging
import threading
from enum import Enum
from typing import Callable

_logger = logging.getLogger("metact")


class SlaveStatus(Enum):
    ON = "on"        # Terminal running, copying enabled
    OFF = "off"      # Terminal not running, no copying
    PAUSED = "paused"  # Copying disabled, terminal may/may not be alive


class SlaveStateMachine:
    """Thread-safe singleton tracking per-slave runtime status."""

    _instance: "SlaveStateMachine | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "SlaveStateMachine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.RLock()
        self._slaves: dict[str, SlaveStatus] = {}
        self._callbacks: list[Callable[[str, SlaveStatus, SlaveStatus], None]] = []

    def add_slave(self, slave_id: str, status: SlaveStatus = SlaveStatus.ON):
        """Register a slave with initial status."""
        with self._lock:
            old = self._slaves.get(slave_id)
            self._slaves[slave_id] = status
        if old is not None and old != status:
            self._notify(slave_id, old, status)

    def remove_slave(self, slave_id: str):
        """Remove a slave from tracking."""
        with self._lock:
            self._slaves.pop(slave_id, None)

    def get_status(self, slave_id: str) -> SlaveStatus | None:
        """Get current status (None if not tracked)."""
        with self._lock:
            return self._slaves.get(slave_id)

    def get_all(self) -> dict[str, SlaveStatus]:
        """Get snapshot of all slave statuses."""
        with self._lock:
            return dict(self._slaves)

    # ---- Transitions ----

    def pause(self, slave_id: str) -> bool:
        """On -> Paused (manual)."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            if cur != SlaveStatus.ON:
                return False
            self._slaves[slave_id] = SlaveStatus.PAUSED
        _logger.info(f"[{slave_id}] Paused (manual)")
        self._notify(slave_id, SlaveStatus.ON, SlaveStatus.PAUSED)
        return True

    def resume(self, slave_id: str) -> bool:
        """Paused/Off -> On."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            if cur not in (SlaveStatus.PAUSED, SlaveStatus.OFF):
                return False
            self._slaves[slave_id] = SlaveStatus.ON
        if cur == SlaveStatus.PAUSED:
            _logger.info(f"[{slave_id}] Resumed (manual)")
        else:
            _logger.info(f"[{slave_id}] Connected (terminal ready)")
        self._notify(slave_id, cur, SlaveStatus.ON)
        return True

    def stop(self, slave_id: str) -> bool:
        """On/Paused -> Off (manual stop)."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            if cur not in (SlaveStatus.ON, SlaveStatus.PAUSED):
                return False
            self._slaves[slave_id] = SlaveStatus.OFF
        _logger.info(f"[{slave_id}] Stopped (manual)")
        self._notify(slave_id, cur, SlaveStatus.OFF)
        return True

    def disconnect(self, slave_id: str):
        """On -> Off (terminal died). No-op if not On."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            if cur != SlaveStatus.ON:
                return
            self._slaves[slave_id] = SlaveStatus.OFF
        _logger.warning(f"[{slave_id}] Disconnected (terminal process not found)")
        self._notify(slave_id, SlaveStatus.ON, SlaveStatus.OFF)

    def stop_all(self):
        """Set all slaves to Off."""
        with self._lock:
            changes = []
            for slave_id, cur in self._slaves.items():
                if cur != SlaveStatus.OFF:
                    changes.append((slave_id, cur))
                    self._slaves[slave_id] = SlaveStatus.OFF
        for slave_id, old in changes:
            self._notify(slave_id, old, SlaveStatus.OFF)

    # ---- Queries ----

    def can_delete(self, slave_id: str) -> bool:
        """True only if Off (or not tracked)."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            return cur is None or cur == SlaveStatus.OFF

    def is_copy_active(self, slave_id: str) -> bool:
        """True only if On (or not tracked — CLI mode compatibility)."""
        with self._lock:
            cur = self._slaves.get(slave_id)
            return cur is None or cur == SlaveStatus.ON

    # ---- Callbacks ----

    def register_callback(
        self, cb: Callable[[str, SlaveStatus, SlaveStatus], None]
    ):
        """Register callback: cb(slave_id, old_status, new_status)."""
        with self._lock:
            self._callbacks.append(cb)

    def unregister_callback(
        self, cb: Callable[[str, SlaveStatus, SlaveStatus], None]
    ):
        """Unregister a callback."""
        with self._lock:
            if cb in self._callbacks:
                self._callbacks.remove(cb)

    def _notify(self, slave_id: str, old: SlaveStatus, new: SlaveStatus):
        """Notify callbacks outside the lock."""
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(slave_id, old, new)
            except Exception:
                pass

    @classmethod
    def reset(cls):
        """Reset singleton (for testing)."""
        with cls._instance_lock:
            cls._instance = None


def get_slave_state() -> SlaveStateMachine:
    """Get the global SlaveStateMachine instance."""
    return SlaveStateMachine()
