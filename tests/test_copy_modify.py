"""Tests for SL/TP modifications on slave accounts."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.master import PositionChange
from src.slave_state import SlaveStateMachine, SlaveStatus


class TestCopyModify:
    """SL/TP modifications on slave accounts."""

    def _setup_tracked(self, copier, master_ticket, slaves, symbol="EURUSD",
                       master_sl=0.0, master_tp=0.0):
        """Helper: add a tracked master position with slave entries."""
        copier.tracker.add_position(
            master_ticket=master_ticket, symbol=symbol,
            order_type=0, master_volume=0.1,
            master_sl=master_sl, master_tp=master_tp,
        )
        for slave_id, slave_ticket in slaves.items():
            copier.tracker.add_slave_position(
                master_ticket=master_ticket,
                slave_id=slave_id,
                slave_ticket=slave_ticket,
                volume=0.1,
            )

    @pytest.mark.asyncio
    async def test_modify_sl_tp_all_slaves_updated(
        self, copier_factory, make_config, make_position
    ):
        """Modify SL/TP -> all slaves updated, tracker updated."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 300, {"200000@TestServer": 900001, "200001@TestServer": 900002})

        pos = make_position(ticket=300, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        # Master SL/TP updated in tracker
        tracked = copier.tracker.get_position(300)
        assert tracked.master_sl == 1.09000
        assert tracked.master_tp == 1.12000

        # Tasks submitted for both slaves
        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2

        # Slave SL/TP updated from result details
        for sid in ["200000@TestServer", "200001@TestServer"]:
            slave = tracked.slaves[sid]
            assert slave.sl == 1.09000
            assert slave.tp == 1.12000

    @pytest.mark.asyncio
    async def test_modify_one_slave_fails(
        self, copier_factory, make_config, make_position
    ):
        """Modify -> one slave fails -> error logged, other succeeds."""
        config = make_config(num_slaves=2)

        def fail_slave2(task: SlaveTask) -> SlaveResult:
            if task.slave_name == "Slave2":
                return SlaveResult(
                    slave_name="", success=False, action="modify",
                    ticket=task.params.get("ticket"),
                    error="Modify rejected",
                )
            return SlaveResult(
                slave_name="", success=True, action="modify",
                ticket=task.params.get("ticket"),
                details={"sl": 1.09000, "tp": 1.12000},
            )

        copier = copier_factory(config, result_factory=fail_slave2)
        self._setup_tracked(copier, 301, {"200000@TestServer": 900001, "200001@TestServer": 900002})

        pos = make_position(ticket=301, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        tracked = copier.tracker.get_position(301)
        # Slave1 updated, Slave2 not
        assert tracked.slaves["200000@TestServer"].sl == 1.09000
        assert tracked.slaves["200001@TestServer"].sl == 0.0


    @pytest.mark.asyncio
    async def test_modify_untracked_noop(
        self, copier_factory, make_config, make_position
    ):
        """Modify -> position not tracked -> no-op."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        pos = make_position(ticket=999, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_modify_params_passed_correctly(
        self, copier_factory, make_config, make_position
    ):
        """Verify SL/TP params passed correctly (master_entry, is_buy)."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 302, {"200000@TestServer": 900001})

        pos = make_position(
            ticket=302, price_open=1.10000, sl=1.09000, tp=1.12000, type=0,
        )
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        task = copier.executor.submitted_tasks[0]
        assert task.action == TaskAction.MODIFY
        assert task.params["ticket"] == 900001
        assert task.params["master_entry"] == 1.10000
        assert task.params["master_sl"] == 1.09000
        assert task.params["master_tp"] == 1.12000
        assert task.params["is_buy"] is True

    @pytest.mark.asyncio
    async def test_modify_no_change(
        self, copier_factory, make_config, make_position
    ):
        """No change (same SL/TP) -> result has no_change: True."""
        config = make_config(num_slaves=1)

        def no_change_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="modify",
                ticket=task.params.get("ticket"),
                details={"no_change": True, "sl": 1.09000, "tp": 1.12000},
            )

        copier = copier_factory(config, result_factory=no_change_factory)
        self._setup_tracked(copier, 303, {"200000@TestServer": 900001},
                           master_sl=1.09000, master_tp=1.12000)

        pos = make_position(ticket=303, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        # Still tracked, no crash
        assert copier.tracker.is_tracked(303)

    @pytest.mark.asyncio
    async def test_tracker_update_slave_sl_tp_called(
        self, copier_factory, make_config, make_position
    ):
        """Verify tracker.update_slave_sl_tp called with result values."""
        config = make_config(num_slaves=1)

        def custom_sl_tp(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="modify",
                ticket=task.params.get("ticket"),
                details={"sl": 1.08500, "tp": 1.13000},
            )

        copier = copier_factory(config, result_factory=custom_sl_tp)
        self._setup_tracked(copier, 304, {"200000@TestServer": 900001})

        pos = make_position(ticket=304, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        tracked = copier.tracker.get_position(304)
        assert tracked.slaves["200000@TestServer"].sl == 1.08500
        assert tracked.slaves["200000@TestServer"].tp == 1.13000

    @pytest.mark.asyncio
    async def test_multiple_modifications_same_batch(
        self, copier_factory, make_config, make_position
    ):
        """Multiple modifications in same batch."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 305, {"200000@TestServer": 900001})
        self._setup_tracked(copier, 306, {"200000@TestServer": 900002})

        pos1 = make_position(ticket=305, sl=1.09, tp=1.12)
        pos2 = make_position(ticket=306, sl=1.08, tp=1.13)
        changes = PositionChange(
            new_positions=[], closed_tickets=[],
            modified_positions=[pos1, pos2], source="Polling"
        )
        await copier.process_changes(changes)

        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_reconnect_to_master_after_modify(
        self, copier_factory, make_config, make_position, mock_master
    ):
        """Verify reconnect to master called after modify."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 307, {"200000@TestServer": 900001})

        pos = make_position(ticket=307, sl=1.09, tp=1.12)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        mock_master.mt5.get_account_info.assert_called()

    @pytest.mark.asyncio
    async def test_modify_skipped_for_paused_slave(
        self, copier_factory, make_config, make_position
    ):
        """Paused slave should NOT receive modify tasks."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 308, {"200000@TestServer": 900001, "200001@TestServer": 900002})

        # Pause Slave2
        state = SlaveStateMachine()
        state.add_slave("200000@TestServer", SlaveStatus.ON)
        state.add_slave("200001@TestServer", SlaveStatus.ON)
        state.pause("200001@TestServer")

        pos = make_position(ticket=308, sl=1.09000, tp=1.12000)
        changes = PositionChange(
            new_positions=[], closed_tickets=[], modified_positions=[pos], source="Polling"
        )
        await copier.process_changes(changes)

        # Only Slave1 should have received a modify task
        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 1
        assert tasks[0].slave_name == "Slave1"

        # Slave1 updated, Slave2 untouched
        tracked = copier.tracker.get_position(308)
        assert tracked.slaves["200000@TestServer"].sl == 1.09000
        assert tracked.slaves["200001@TestServer"].sl == 0.0
