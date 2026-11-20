"""Tests for copying new positions to slave accounts."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.master import PositionChange


@pytest.fixture
def config1(make_config):
    return make_config(num_slaves=1)


@pytest.fixture
def config2(make_config):
    return make_config(num_slaves=2)


class TestCopyOpen:
    """Opening positions on slave accounts."""

    @pytest.mark.asyncio
    async def test_single_position_single_slave_success(
        self, copier_factory, config1, make_position, event_collector
    ):
        """Single master position -> 1 slave: success, tracker updated, event emitted."""
        copier = copier_factory(config1)
        pos = make_position(ticket=100)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        assert copier.tracker.is_tracked(100)
        tracked = copier.tracker.get_position(100)
        assert "200000@TestServer" in tracked.slaves
        assert tracked.slaves["200000@TestServer"].ticket > 900000

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 1
        assert opened[0].master_ticket == 100
        assert opened[0].slave_name == "Slave1"

    @pytest.mark.asyncio
    async def test_single_position_two_slaves_both_succeed(
        self, copier_factory, config2, make_position, event_collector
    ):
        """Single master position -> 2 slaves: both succeed."""
        copier = copier_factory(config2)
        pos = make_position(ticket=101)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        tracked = copier.tracker.get_position(101)
        assert "200000@TestServer" in tracked.slaves
        assert "200001@TestServer" in tracked.slaves

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 2

    @pytest.mark.asyncio
    async def test_single_position_two_slaves_one_fails(
        self, copier_factory, config2, make_position, event_collector
    ):
        """Single master position -> 2 slaves: one fails, one succeeds."""
        call_count = 0

        def mixed_factory(task: SlaveTask) -> SlaveResult:
            nonlocal call_count
            call_count += 1
            if task.slave_name == "Slave2":
                return SlaveResult(
                    slave_name="", success=False, action="open",
                    error="Connection failed",
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900001, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config2, result_factory=mixed_factory)
        pos = make_position(ticket=102)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        tracked = copier.tracker.get_position(102)
        assert "200000@TestServer" in tracked.slaves
        assert "200001@TestServer" not in tracked.slaves

        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert len(errors) == 1
        assert errors[0].slave_name == "Slave2"

    @pytest.mark.asyncio
    async def test_already_tracked_position_skipped(
        self, copier_factory, config1, make_position
    ):
        """Position already tracked -> skip (no duplicate)."""
        copier = copier_factory(config1)
        pos = make_position(ticket=103)

        # Pre-track the position
        copier.tracker.add_position(
            master_ticket=103, symbol="EURUSD", order_type=0,
            master_volume=0.1,
        )

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        # No tasks should have been submitted
        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_slave_disabled_not_copied(
        self, copier_factory, make_config, make_position
    ):
        """Slave disabled in LiveSettings -> not copied."""
        from src.live_settings import get_live_settings

        config = make_config(num_slaves=2)
        copier = copier_factory(config)

        settings = get_live_settings()
        settings.update_slave("200001@TestServer", enabled=False)

        pos = make_position(ticket=104)
        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        tracked = copier.tracker.get_position(104)
        assert "200000@TestServer" in tracked.slaves
        assert "200001@TestServer" not in tracked.slaves

        tasks = copier.executor.submitted_tasks
        assert all(t.slave_name == "Slave1" for t in tasks)

    @pytest.mark.asyncio
    async def test_entry_deviation_skipped(
        self, copier_factory, config1, make_position, event_collector
    ):
        """Entry deviation too high -> skipped (result has skipped_by_deviation)."""

        def deviation_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=False, action="open",
                error="Entry deviation 0.50000 > max 0.10000",
                details={"skipped_by_deviation": True},
            )

        copier = copier_factory(config1, result_factory=deviation_factory)
        pos = make_position(ticket=105)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        # Position is tracked (master side) but no slave position
        tracked = copier.tracker.get_position(105)
        assert tracked is not None
        assert len(tracked.slaves) == 0

    @pytest.mark.asyncio
    async def test_multiple_new_positions(
        self, copier_factory, config1, make_position, event_collector
    ):
        """Multiple new positions in one process_changes call."""
        copier = copier_factory(config1)
        pos1 = make_position(ticket=106, symbol="EURUSD")
        pos2 = make_position(ticket=107, symbol="GBPUSD")

        changes = PositionChange(
            new_positions=[pos1, pos2], closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert copier.tracker.is_tracked(106)
        assert copier.tracker.is_tracked(107)

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 2

    @pytest.mark.asyncio
    async def test_emit_trade_opened_event_fields(
        self, copier_factory, config1, make_position, event_collector
    ):
        """Verify emit_trade_opened event fields."""
        copier = copier_factory(config1)
        pos = make_position(ticket=108, symbol="EURUSD", type=0, volume=0.5)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 1
        evt = opened[0]
        assert evt.symbol == "EURUSD"
        assert evt.master_ticket == 108
        assert evt.order_type == 0
        assert evt.master_volume == 0.5
        assert evt.trade_id != ""

    @pytest.mark.asyncio
    async def test_emit_error_event_on_failure(
        self, copier_factory, config1, make_position, event_collector
    ):
        """Verify emit_error event on failure."""

        def fail_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=False, action="open",
                error="Order rejected",
            )

        copier = copier_factory(config1, result_factory=fail_factory)
        pos = make_position(ticket=109)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert len(errors) == 1
        assert "Order rejected" in errors[0].error
        assert errors[0].master_ticket == 109

    @pytest.mark.asyncio
    async def test_buy_and_sell_positions(
        self, copier_factory, config1, make_position, event_collector
    ):
        """BUY and SELL positions both copy correctly."""
        copier = copier_factory(config1)
        buy_pos = make_position(ticket=110, type=0)  # BUY
        sell_pos = make_position(ticket=111, type=1)  # SELL

        changes = PositionChange(
            new_positions=[buy_pos, sell_pos], closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2

        buy_task = [t for t in tasks if t.params["magic"] == 110][0]
        sell_task = [t for t in tasks if t.params["magic"] == 111][0]
        assert buy_task.params["order_type"] == 0
        assert buy_task.params["is_buy"] is True
        assert sell_task.params["order_type"] == 1
        assert sell_task.params["is_buy"] is False

    @pytest.mark.asyncio
    async def test_symbol_mapping_applied(
        self, copier_factory, make_config, make_position
    ):
        """Symbol mapping applied (master EURUSD -> slave EURUSDm)."""
        from src.live_settings import get_live_settings

        config = make_config(
            num_slaves=1,
            slave_overrides=[{"symbol_mapping": {"EURUSD": "EURUSDm"}}],
        )
        copier = copier_factory(config)

        pos = make_position(ticket=112, symbol="EURUSD")
        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        task = copier.executor.submitted_tasks[0]
        assert task.params["symbol"] == "EURUSDm"

    @pytest.mark.asyncio
    async def test_reconnect_to_master_called(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Verify reconnect to master called after copy."""
        copier = copier_factory(config1)
        pos = make_position(ticket=113)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        mock_master.mt5.get_account_info.assert_called()

    @pytest.mark.asyncio
    async def test_params_passed_to_executor(
        self, copier_factory, config1, make_position
    ):
        """Verify params passed to executor (magic, multiplier, deviation, etc.)."""
        copier = copier_factory(config1)
        pos = make_position(ticket=114, symbol="EURUSD", volume=0.2, sl=1.09, tp=1.12)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        task = copier.executor.submitted_tasks[0]
        assert task.action == TaskAction.OPEN
        assert task.params["magic"] == 114
        assert task.params["master_volume"] == 0.2
        assert task.params["master_sl"] == 1.09
        assert task.params["master_tp"] == 1.12
        assert task.params["deviation"] == 0  # from config max_slippage_points (0 = no limit)

    @pytest.mark.asyncio
    async def test_risk_mode_fixed_volume_passed(
        self, copier_factory, make_config, make_position
    ):
        """Risk mode: fixed_volume passed through."""
        config = make_config(
            num_slaves=1,
            slave_overrides=[{
                "fixed_volume": 0.5,
                "risk_mode": "fixed_volume",
            }],
        )
        copier = copier_factory(config)
        pos = make_position(ticket=115)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        task = copier.executor.submitted_tasks[0]
        assert task.params["fixed_volume"] == 0.5

    @pytest.mark.asyncio
    async def test_risk_mode_fixed_usd_passed(
        self, copier_factory, make_config, make_position
    ):
        """Risk mode: fixed_risk_usd passed through."""
        config = make_config(
            num_slaves=1,
            slave_overrides=[{
                "fixed_risk_usd": 50.0,
                "risk_mode": "fixed_usd",
            }],
        )
        copier = copier_factory(config)
        pos = make_position(ticket=116)

        changes = PositionChange(new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling")
        await copier.process_changes(changes)

        task = copier.executor.submitted_tasks[0]
        assert task.params["fixed_risk_usd"] == 50.0

    @pytest.mark.asyncio
    async def test_multiple_positions_opening_simultaneously(
        self, copier_factory, config2, make_position, event_collector
    ):
        """Multiple positions opening simultaneously across 2 slaves."""
        copier = copier_factory(config2)
        positions = [
            make_position(ticket=120 + i, symbol=f"SYM{i}")
            for i in range(3)
        ]

        changes = PositionChange(
            new_positions=positions, closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # 3 positions * 2 slaves = 6 tasks
        assert len(copier.executor.submitted_tasks) == 6
        for i in range(3):
            assert copier.tracker.is_tracked(120 + i)

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 6
