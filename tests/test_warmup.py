"""Tests for warmup and profit updates."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.live_settings import get_live_settings


class TestWarmup:
    """Warmup slave terminals."""

    @pytest.mark.asyncio
    async def test_warmup_all_success(
        self, copier_factory, make_config
    ):
        """Warmup all slaves -> success logged with leverage."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)

        await copier.warmup_slaves()

        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2
        assert all(t.action == TaskAction.WARMUP for t in tasks)
        slave_names = {t.slave_name for t in tasks}
        assert slave_names == {"Slave1", "Slave2"}

    @pytest.mark.asyncio
    async def test_warmup_one_fails(
        self, copier_factory, make_config
    ):
        """Warmup -> one slave fails -> warning logged, others succeed."""
        config = make_config(num_slaves=2)

        def partial_warmup(task: SlaveTask) -> SlaveResult:
            if task.slave_name == "Slave2":
                return SlaveResult(
                    slave_name="", success=False, action="warmup",
                    error="Connection timeout",
                )
            return SlaveResult(
                slave_name="", success=True, action="warmup",
                details={"leverage": 200, "balance": 5000.0, "symbols": ["EURUSD"]},
            )

        copier = copier_factory(config, result_factory=partial_warmup)
        await copier.warmup_slaves()

        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_warmup_no_enabled_slaves(
        self, copier_factory, make_config
    ):
        """Warmup -> no enabled slaves -> 'No enabled slaves' path."""
        config = make_config(num_slaves=1, slave_overrides=[{"enabled": False}])
        copier = copier_factory(config)

        await copier.warmup_slaves()

        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_warmup_symbols_cached(
        self, copier_factory, make_config
    ):
        """Warmup -> symbols cached in LiveSettings."""
        config = make_config(num_slaves=1)

        def symbols_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="warmup",
                details={
                    "leverage": 100,
                    "balance": 10000.0,
                    "symbols": ["EURUSD", "GBPUSD", "XAUUSD"],
                },
            )

        copier = copier_factory(config, result_factory=symbols_factory)
        await copier.warmup_slaves()

        settings = get_live_settings()
        cached = settings.get_slave_symbols("200000@TestServer")
        assert cached == ["EURUSD", "GBPUSD", "XAUUSD"]

    @pytest.mark.asyncio
    async def test_warmup_reconnect_after(
        self, copier_factory, make_config, mock_master
    ):
        """Warmup -> reconnect to master after warmup."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        await copier.warmup_slaves()

        mock_master.mt5.get_account_info.assert_called()


class TestEmitProfitUpdates:
    """Profit update emissions."""

    def test_emit_profit_updates_two_positions(
        self, copier_factory, make_config, make_position, mock_master, event_collector
    ):
        """emit_profit_updates() with 2 tracked positions -> 2 PROFIT_UPDATE events."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        # Set up master positions in last_positions
        pos1 = make_position(ticket=600, profit=10.0, volume=0.1)
        pos2 = make_position(ticket=601, profit=-5.0, volume=0.2)
        mock_master._last_positions = {600: pos1, 601: pos2}

        # Track positions with slaves
        copier.tracker.add_position(
            master_ticket=600, symbol="EURUSD", order_type=0, master_volume=0.1,
        )
        copier.tracker.add_slave_position(
            master_ticket=600, slave_id="200000@TestServer",
            slave_ticket=800001, volume=0.1,
        )
        copier.tracker.add_position(
            master_ticket=601, symbol="GBPUSD", order_type=0, master_volume=0.2,
        )
        copier.tracker.add_slave_position(
            master_ticket=601, slave_id="200000@TestServer",
            slave_ticket=800002, volume=0.2,
        )

        copier.emit_profit_updates()

        profit_events = [e for e in event_collector if e.event_type == EventType.PROFIT_UPDATE]
        assert len(profit_events) == 2

    def test_emit_profit_estimated_ratio(
        self, copier_factory, make_config, make_position, mock_master, event_collector
    ):
        """emit_profit_updates() -> estimated profit = master_profit * ratio."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        # Master has 0.2 lots, slave has 0.1 lots -> ratio = 0.5
        pos = make_position(ticket=602, profit=20.0, volume=0.2)
        mock_master._last_positions = {602: pos}

        copier.tracker.add_position(
            master_ticket=602, symbol="EURUSD", order_type=0, master_volume=0.2,
        )
        copier.tracker.add_slave_position(
            master_ticket=602, slave_id="200000@TestServer",
            slave_ticket=800003, volume=0.1,
        )

        copier.emit_profit_updates()

        profit_events = [e for e in event_collector if e.event_type == EventType.PROFIT_UPDATE]
        assert len(profit_events) == 1
        assert profit_events[0].profit == 10.0  # 20.0 * 0.1/0.2

    def test_emit_profit_master_gone(
        self, copier_factory, make_config, mock_master, event_collector
    ):
        """emit_profit_updates() -> master position gone -> skip."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        # Master positions empty (already closed)
        mock_master._last_positions = {}

        copier.tracker.add_position(
            master_ticket=603, symbol="EURUSD", order_type=0, master_volume=0.1,
        )
        copier.tracker.add_slave_position(
            master_ticket=603, slave_id="200000@TestServer",
            slave_ticket=800004, volume=0.1,
        )

        copier.emit_profit_updates()

        profit_events = [e for e in event_collector if e.event_type == EventType.PROFIT_UPDATE]
        assert len(profit_events) == 0
