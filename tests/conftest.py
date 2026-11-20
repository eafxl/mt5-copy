"""Shared fixtures and mock infrastructure for trade-copying tests."""
import sys
import types
from unittest.mock import MagicMock

# Install a fake MetaTrader5 module BEFORE any src imports.
# The real module is Windows-only; tests run on any platform.
if "MetaTrader5" not in sys.modules:
    _fake_mt5 = types.ModuleType("MetaTrader5")
    # Constants used at module level in src/parallel_worker.py and src/mt5_client.py
    _fake_mt5.ORDER_FILLING_IOC = 1
    _fake_mt5.ORDER_FILLING_FOK = 0
    _fake_mt5.ORDER_FILLING_RETURN = 2
    _fake_mt5.ORDER_TIME_GTC = 0
    _fake_mt5.TRADE_ACTION_DEAL = 1
    _fake_mt5.TRADE_ACTION_SLTP = 6
    _fake_mt5.TRADE_RETCODE_DONE = 10009
    _fake_mt5.TRADE_RETCODE_REQUOTE = 10004
    # Callable stubs (never invoked in tests, but referenced at import time)
    _fake_mt5.initialize = MagicMock(return_value=True)
    _fake_mt5.shutdown = MagicMock()
    _fake_mt5.login = MagicMock(return_value=True)
    _fake_mt5.account_info = MagicMock(return_value=None)
    _fake_mt5.positions_get = MagicMock(return_value=None)
    _fake_mt5.symbol_info = MagicMock(return_value=None)
    _fake_mt5.symbol_info_tick = MagicMock(return_value=None)
    _fake_mt5.symbol_select = MagicMock(return_value=True)
    _fake_mt5.order_send = MagicMock(return_value=None)
    _fake_mt5.last_error = MagicMock(return_value=(0, ""))
    _fake_mt5.symbols_get = MagicMock(return_value=None)
    _fake_mt5.history_deals_get = MagicMock(return_value=None)
    sys.modules["MetaTrader5"] = _fake_mt5

# Also stub PyQt6 so sync_approval.py can be imported without a real Qt install.
for _mod_name in [
    "PyQt6", "PyQt6.QtWidgets", "PyQt6.QtCore", "PyQt6.QtGui",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.mt5_client import Position, AccountInfo
from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.config import (
    Config, SlaveConfig, MasterConfig, SlaveTerminalConfig, SettingsConfig,
)
from src.trade_copier import TradeCopier
from src.master import MasterMonitor, PositionChange
from src.position_tracker import PositionTracker
from src.live_settings import LiveSettings, get_live_settings
from src.events import UIEventQueue, TradeEvent, EventType
from src.slave_state import SlaveStateMachine


# ---------------------------------------------------------------------------
# Singleton reset (auto-use)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before and after every test for isolation."""
    LiveSettings.reset()
    UIEventQueue.reset()
    SlaveStateMachine.reset()
    yield
    LiveSettings.reset()
    UIEventQueue.reset()
    SlaveStateMachine.reset()


# ---------------------------------------------------------------------------
# Position / data factories
# ---------------------------------------------------------------------------

@pytest.fixture
def make_position():
    """Factory for Position objects with sensible defaults."""

    def _make(
        ticket=100000,
        symbol="EURUSD",
        type=0,  # BUY
        volume=0.1,
        price_open=1.10000,
        price_current=1.10050,
        sl=0.0,
        tp=0.0,
        profit=5.0,
        magic=0,
        comment="",
        time=1700000000,
    ) -> Position:
        return Position(
            ticket=ticket,
            symbol=symbol,
            type=type,
            volume=volume,
            price_open=price_open,
            price_current=price_current,
            sl=sl,
            tp=tp,
            profit=profit,
            magic=magic,
            comment=comment,
            time=time,
        )

    return _make


@pytest.fixture
def make_config():
    """Factory for Config with N slave configs, customizable."""

    def _make(
        num_slaves=1,
        slave_overrides=None,
        sync_existing_on_start=True,
        max_slippage_points=0,
        max_entry_deviation_ratio=0.2,
        poll_interval_ms=500,
        symbol_mapping=None,
        filling_mode=None,
    ) -> Config:
        slaves = []
        for i in range(num_slaves):
            defaults = dict(
                name=f"Slave{i + 1}",
                login=200000 + i,
                password="pass",
                server="TestServer",
                multiplier=1.0,
                adjust_by_leverage=True,
                enabled=True,
                symbol_mapping={},
                terminal_path=f"/terminal/slave{i + 1}",
            )
            if slave_overrides and i < len(slave_overrides):
                defaults.update(slave_overrides[i])
            slaves.append(SlaveConfig(**defaults))

        return Config(
            master=MasterConfig(
                terminal_path="/terminal/master",
                poll_interval_ms=poll_interval_ms,
            ),
            slave_terminal=SlaveTerminalConfig(terminal_path="/terminal/default"),
            slaves=slaves,
            symbol_mapping=symbol_mapping or {},
            filling_mode=filling_mode or {"default": "IOC"},
            settings=SettingsConfig(
                sync_existing_on_start=sync_existing_on_start,
                max_slippage_points=max_slippage_points,
                max_entry_deviation_ratio=max_entry_deviation_ratio,
            ),
        )

    return _make


@pytest.fixture
def make_slave_result():
    """Factory for SlaveResult (success/failure variants)."""

    def _make(
        slave_name="Slave1",
        slave_id="",
        success=True,
        action="open",
        ticket=900001,
        volume=0.1,
        error=None,
        duration_ms=50.0,
        retries_used=0,
        details=None,
    ) -> SlaveResult:
        return SlaveResult(
            slave_name=slave_name,
            success=success,
            action=action,
            slave_id=slave_id,
            ticket=ticket,
            volume=volume,
            error=error,
            duration_ms=duration_ms,
            retries_used=retries_used,
            details=details or {},
        )

    return _make


# ---------------------------------------------------------------------------
# MockExecutor
# ---------------------------------------------------------------------------

class MockExecutor:
    """Drop-in replacement for ParallelExecutor in tests.

    Accepts a result_factory callback: (SlaveTask) -> SlaveResult.
    Default factory returns success for every action type.
    """

    _ticket_counter = 900000

    def __init__(self, result_factory=None):
        self.result_factory = result_factory or self._default_factory
        self.submitted_tasks: list[SlaveTask] = []

    async def execute_tasks_async(self, tasks, on_result=None):
        results = []
        for task in tasks:
            self.submitted_tasks.append(task)
            result = self.result_factory(task)
            result.slave_name = task.slave_name
            result.slave_id = task.slave_id
            results.append(result)
            if on_result:
                on_result(task, result)
        return results

    def shutdown(self):
        pass

    @classmethod
    def _default_factory(cls, task: SlaveTask) -> SlaveResult:
        """Default: everything succeeds."""
        cls._ticket_counter += 1
        ticket = cls._ticket_counter

        if task.action == TaskAction.OPEN:
            return SlaveResult(
                slave_name="",
                success=True,
                action="open",
                ticket=ticket,
                volume=task.params.get("master_volume", 0.1),
                details={
                    "slave_sl": 0.0,
                    "slave_tp": 0.0,
                    "open_price": 1.10000,
                    "open_time": 1700000000.0,
                },
            )
        elif task.action == TaskAction.CLOSE:
            return SlaveResult(
                slave_name="",
                success=True,
                action="close",
                ticket=task.params.get("ticket"),
                details={"profit": 10.0, "close_price": 1.10050},
            )
        elif task.action == TaskAction.MODIFY:
            return SlaveResult(
                slave_name="",
                success=True,
                action="modify",
                ticket=task.params.get("ticket"),
                details={"sl": 1.09000, "tp": 1.12000},
            )
        elif task.action == TaskAction.WARMUP:
            return SlaveResult(
                slave_name="",
                success=True,
                action="warmup",
                details={
                    "leverage": 100,
                    "balance": 10000.0,
                    "symbols": ["EURUSD", "GBPUSD", "USDJPY"],
                },
            )
        elif task.action == TaskAction.FETCH_POSITIONS:
            return SlaveResult(
                slave_name="",
                success=True,
                action="fetch_positions",
                details={"positions": []},
            )
        else:
            return SlaveResult(
                slave_name="",
                success=False,
                action=str(task.action),
                error=f"Unknown action: {task.action}",
            )


@pytest.fixture
def mock_executor():
    """Provide a fresh MockExecutor instance."""
    MockExecutor._ticket_counter = 900000
    return MockExecutor()


# ---------------------------------------------------------------------------
# Event collector
# ---------------------------------------------------------------------------

@pytest.fixture
def event_collector():
    """Collect all TradeEvents emitted via UIEventQueue callbacks."""
    events: list[TradeEvent] = []
    q = UIEventQueue()
    q.register_callback(lambda e: events.append(e))
    return events


# ---------------------------------------------------------------------------
# Mock MT5Client + MasterMonitor
# ---------------------------------------------------------------------------

def _make_mock_mt5():
    """Create a MagicMock that acts like MT5Client."""
    mt5 = MagicMock()
    mt5.get_positions.return_value = []
    mt5.get_account_info.return_value = AccountInfo(
        login=10000, server="MasterServer", leverage=100,
        balance=10000.0, equity=10000.0, margin=0.0,
        margin_free=10000.0, currency="USD",
    )
    mt5.initialize.return_value = True
    mt5.get_current_price.return_value = (1.10000, 1.10010)
    mt5.get_position_by_ticket.return_value = None
    # No trade history by default: a MagicMock would iterate as an error, and
    # the daily-stop guard reads this after every close. An empty list is also
    # the honest default — a fresh test account has no losses today.
    mt5.closed_trades.return_value = []
    # The broker's own calendar date — what the daily limit counts by.
    from src.daily_stops import host_today as _host_today
    mt5.server_today.return_value = _host_today()
    return mt5


@pytest.fixture
def mock_mt5():
    return _make_mock_mt5()


@pytest.fixture
def mock_master(mock_mt5):
    """MasterMonitor with a mocked MT5Client."""
    master = MasterMonitor(mock_mt5, terminal_path="/terminal/master")
    # Pre-populate account info so get_leverage works
    master._account_info = mock_mt5.get_account_info()
    return master


# ---------------------------------------------------------------------------
# Copier factory
# ---------------------------------------------------------------------------

@pytest.fixture
def copier_factory(mock_mt5, mock_master, event_collector):
    """Create a TradeCopier with mock executor + mock master.

    Returns a callable: (config, **overrides) -> TradeCopier
    The copier's executor is replaced with a MockExecutor.
    """

    def _make(config, result_factory=None, embedded=True):
        tracker = PositionTracker()
        copier = TradeCopier(
            config=config,
            mt5_client=mock_mt5,
            master=mock_master,
            tracker=tracker,
            embedded=embedded,
        )
        MockExecutor._ticket_counter = 900000
        copier.executor = MockExecutor(result_factory=result_factory)
        # Patch _run_blocking to just call the function directly (no threading)
        async def _direct_run(func, *args):
            return func(*args)
        copier._run_blocking = _direct_run
        return copier

    return _make
