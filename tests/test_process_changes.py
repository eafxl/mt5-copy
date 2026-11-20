"""Tests for mixed change processing."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.master import PositionChange


class TestProcessChanges:
    """Mixed change batch processing."""

    def _setup_tracked(self, copier, master_ticket, slaves, symbol="EURUSD"):
        copier.tracker.add_position(
            master_ticket=master_ticket, symbol=symbol,
            order_type=0, master_volume=0.1,
        )
        for slave_name, slave_ticket in slaves.items():
            copier.tracker.add_slave_position(
                master_ticket=master_ticket,
                slave_name=slave_name,
                slave_ticket=slave_ticket,
                volume=0.1,
            )

    @pytest.mark.asyncio
    async def test_mixed_new_closed_modified(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """New + closed + modified all in same PositionChange -> all handled."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        # Pre-track positions for close and modify
        self._setup_tracked(copier, 500, {"Slave1": 900001})
        self._setup_tracked(copier, 501, {"Slave1": 900002})

        new_pos = make_position(ticket=502, symbol="GBPUSD")
        mod_pos = make_position(ticket=501, sl=1.09, tp=1.12)

        changes = PositionChange(
            new_positions=[new_pos],
            closed_tickets=[500],
            modified_positions=[mod_pos], source="Polling"
        )
        await copier.process_changes(changes)

        # New position tracked
        assert copier.tracker.is_tracked(502)
        # Closed position removed
        assert not copier.tracker.is_tracked(500)
        # Modified position still tracked
        assert copier.tracker.is_tracked(501)

    @pytest.mark.asyncio
    async def test_open_then_close_next_cycle(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """New position + same ticket closed in next cycle."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        # Cycle 1: open
        pos = make_position(ticket=503)
        changes1 = PositionChange(
            new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes1)
        assert copier.tracker.is_tracked(503)

        # Cycle 2: close
        changes2 = PositionChange(
            new_positions=[], closed_tickets=[503], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes2)
        assert not copier.tracker.is_tracked(503)

    @pytest.mark.asyncio
    async def test_rapid_open_modify_close(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """Rapid successive changes (open, modify, close)."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        pos = make_position(ticket=504)
        # Open
        await copier.process_changes(PositionChange(
            new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling"
        ))
        # Modify
        mod_pos = make_position(ticket=504, sl=1.09, tp=1.12)
        await copier.process_changes(PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[mod_pos], source="Polling"
        ))
        # Close
        await copier.process_changes(PositionChange(
            new_positions=[], closed_tickets=[504], modified_positions=[], source="Polling"
        ))
        assert not copier.tracker.is_tracked(504)

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(opened) == 1
        assert len(closed) == 1

    @pytest.mark.asyncio
    async def test_empty_changes_noop(
        self, copier_factory, make_config
    ):
        """Empty PositionChange -> no-op."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_error_in_one_handler_doesnt_block_others(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """Error in one handler doesn't block others (gather with return_exceptions)."""
        config = make_config(num_slaves=1)
        call_count = {"open": 0, "close": 0}

        def tracking_factory(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.OPEN:
                call_count["open"] += 1
                return SlaveResult(
                    slave_name="", success=True, action="open",
                    ticket=900099, volume=0.1,
                    details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
                )
            elif task.action == TaskAction.CLOSE:
                call_count["close"] += 1
                return SlaveResult(
                    slave_name="", success=False, action="close",
                    ticket=task.params.get("ticket"),
                    error="Close failed hard",
                )
            return SlaveResult(slave_name="", success=True, action=task.action.value)

        copier = copier_factory(config, result_factory=tracking_factory)
        self._setup_tracked(copier, 505, {"Slave1": 900001})

        new_pos = make_position(ticket=506)
        changes = PositionChange(
            new_positions=[new_pos], closed_tickets=[505], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # Both handlers ran despite close failure
        assert call_count["open"] == 1
        assert call_count["close"] == 1

    @pytest.mark.asyncio
    async def test_multiple_new_different_symbols(
        self, copier_factory, make_config, make_position
    ):
        """Multiple new positions across different symbols."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        positions = [
            make_position(ticket=510, symbol="EURUSD"),
            make_position(ticket=511, symbol="GBPUSD"),
            make_position(ticket=512, symbol="USDJPY"),
        ]
        changes = PositionChange(
            new_positions=positions, closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        for t in [510, 511, 512]:
            assert copier.tracker.is_tracked(t)

        tasks = copier.executor.submitted_tasks
        symbols = {t.params["symbol"] for t in tasks}
        assert symbols == {"EURUSD", "GBPUSD", "USDJPY"}

    @pytest.mark.asyncio
    async def test_process_changes_calls_get_leverage(
        self, copier_factory, make_config, make_position, mock_master
    ):
        """Verify process_changes calls master.get_leverage."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        pos = make_position(ticket=513)
        changes = PositionChange(
            new_positions=[pos], closed_tickets=[], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # get_leverage should have been called (checked inside _run_blocking)
        # Since we patched _run_blocking to call directly, verify the
        # leverage is used (master has leverage 100)
        task = copier.executor.submitted_tasks[0]
        assert task.params["master_leverage"] == 100

    @pytest.mark.asyncio
    async def test_concurrent_changes_different_positions(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """Multiple changes processed in single gather call."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)

        # Pre-track a position for closing
        self._setup_tracked(copier, 520, {"Slave1": 900001, "Slave2": 900002})

        # Two new positions + one close
        changes = PositionChange(
            new_positions=[
                make_position(ticket=521),
                make_position(ticket=522),
            ],
            closed_tickets=[520],
            modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(520)
        assert copier.tracker.is_tracked(521)
        assert copier.tracker.is_tracked(522)

    @pytest.mark.asyncio
    async def test_close_and_open_same_symbol(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """Close one position and open another for the same symbol."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 530, {"Slave1": 900001}, symbol="EURUSD")

        changes = PositionChange(
            new_positions=[make_position(ticket=531, symbol="EURUSD")],
            closed_tickets=[530],
            modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(530)
        assert copier.tracker.is_tracked(531)
