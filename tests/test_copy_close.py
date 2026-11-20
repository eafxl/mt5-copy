"""Tests for closing copied positions on slave accounts."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.master import PositionChange
from src.slave_state import SlaveStateMachine, SlaveStatus


class TestCopyClose:
    """Closing positions on slave accounts."""

    def _setup_tracked(self, copier, master_ticket, slaves, symbol="EURUSD"):
        """Helper: add a tracked master position with slave entries."""
        copier.tracker.add_position(
            master_ticket=master_ticket, symbol=symbol,
            order_type=0, master_volume=0.1,
        )
        for slave_id, slave_ticket in slaves.items():
            copier.tracker.add_slave_position(
                master_ticket=master_ticket,
                slave_id=slave_id,
                slave_ticket=slave_ticket,
                volume=0.1,
            )

    @pytest.mark.asyncio
    async def test_close_all_slaves(
        self, copier_factory, make_config, event_collector
    ):
        """Close master -> close all slaves, tracker removes position."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 200, {"200000@TestServer": 900001, "200001@TestServer": 900002})

        changes = PositionChange(
            new_positions=[], closed_tickets=[200], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(200)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 2

    @pytest.mark.asyncio
    async def test_close_one_slave_fails(
        self, copier_factory, make_config, event_collector
    ):
        """Close master -> 2 slaves, one fails -> error event emitted."""
        config = make_config(num_slaves=2)

        def fail_slave2(task: SlaveTask) -> SlaveResult:
            if task.slave_name == "Slave2":
                return SlaveResult(
                    slave_name="", success=False, action="close",
                    ticket=task.params.get("ticket"),
                    error="Close rejected",
                )
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"profit": 5.0},
            )

        copier = copier_factory(config, result_factory=fail_slave2)
        self._setup_tracked(copier, 201, {"200000@TestServer": 900001, "200001@TestServer": 900002})

        changes = PositionChange(
            new_positions=[], closed_tickets=[201], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # Position removed from tracker regardless of slave failure
        assert not copier.tracker.is_tracked(201)

        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert len(errors) == 1
        assert errors[0].slave_name == "Slave2"

    @pytest.mark.asyncio
    async def test_close_untracked_position_noop(
        self, copier_factory, make_config
    ):
        """Close master -> position not tracked -> no-op."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        changes = PositionChange(
            new_positions=[], closed_tickets=[999], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_emit_trade_closed_event_with_profit(
        self, copier_factory, make_config, event_collector
    ):
        """Verify emit_trade_closed event with profit value."""
        config = make_config(num_slaves=1)

        def profit_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"profit": 42.50},
            )

        copier = copier_factory(config, result_factory=profit_factory)
        self._setup_tracked(copier, 202, {"200000@TestServer": 900001})

        changes = PositionChange(
            new_positions=[], closed_tickets=[202], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert closed[0].profit == 42.50

    @pytest.mark.asyncio
    async def test_profit_from_result_details(
        self, copier_factory, make_config, event_collector
    ):
        """Verify profit comes from result.details['profit']."""
        config = make_config(num_slaves=1)

        def specific_profit_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"profit": -15.75},
            )

        copier = copier_factory(config, result_factory=specific_profit_factory)
        self._setup_tracked(copier, 203, {"200000@TestServer": 900001})

        changes = PositionChange(
            new_positions=[], closed_tickets=[203], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert closed[0].profit == -15.75

    @pytest.mark.asyncio
    async def test_position_already_closed_on_slave(
        self, copier_factory, make_config, event_collector
    ):
        """Position already closed on slave (success=True, already_closed=True)."""
        config = make_config(num_slaves=1)

        def already_closed_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"already_closed": True, "profit": 12.50},
            )

        copier = copier_factory(config, result_factory=already_closed_factory)
        self._setup_tracked(copier, 204, {"200000@TestServer": 900001})

        changes = PositionChange(
            new_positions=[], closed_tickets=[204], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(204)
        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert closed[0].profit == 12.50

    @pytest.mark.asyncio
    async def test_reconnect_to_master_after_close(
        self, copier_factory, make_config, mock_master
    ):
        """Verify reconnect to master called after close."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 205, {"200000@TestServer": 900001})

        changes = PositionChange(
            new_positions=[], closed_tickets=[205], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # reconnect calls get_account_info internally
        mock_master.mt5.get_account_info.assert_called()

    @pytest.mark.asyncio
    async def test_close_no_slave_positions_removes_immediately(
        self, copier_factory, make_config
    ):
        """Close with no slave positions -> remove from tracker immediately."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        # Track master but add NO slaves
        copier.tracker.add_position(
            master_ticket=206, symbol="EURUSD", order_type=0, master_volume=0.1,
        )

        changes = PositionChange(
            new_positions=[], closed_tickets=[206], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(206)
        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_multiple_positions_closing_same_batch(
        self, copier_factory, make_config, event_collector
    ):
        """Multiple positions closing in same process_changes batch."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        self._setup_tracked(copier, 207, {"200000@TestServer": 900011})
        self._setup_tracked(copier, 208, {"200000@TestServer": 900012})

        changes = PositionChange(
            new_positions=[], closed_tickets=[207, 208], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(207)
        assert not copier.tracker.is_tracked(208)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 2

    @pytest.mark.asyncio
    async def test_correct_slave_ticket_passed(
        self, copier_factory, make_config
    ):
        """Verify correct slave ticket passed to close task."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 209, {"200000@TestServer": 900021, "200001@TestServer": 900022})

        changes = PositionChange(
            new_positions=[], closed_tickets=[209], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        tasks = copier.executor.submitted_tasks
        assert len(tasks) == 2

        slave1_task = [t for t in tasks if t.slave_name == "Slave1"][0]
        slave2_task = [t for t in tasks if t.slave_name == "Slave2"][0]
        assert slave1_task.params["ticket"] == 900021
        assert slave2_task.params["ticket"] == 900022

    @pytest.mark.asyncio
    async def test_close_event_has_correct_symbol_and_volume(
        self, copier_factory, make_config, event_collector
    ):
        """Verify close event carries symbol and volume from tracker."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        copier.tracker.add_position(
            master_ticket=210, symbol="GBPUSD", order_type=1, master_volume=0.3,
        )
        copier.tracker.add_slave_position(
            master_ticket=210, slave_id="200000@TestServer",
            slave_ticket=900031, volume=0.3,
        )

        changes = PositionChange(
            new_positions=[], closed_tickets=[210], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert closed[0].symbol == "GBPUSD"
        assert closed[0].volume == 0.3

    @pytest.mark.asyncio
    async def test_close_slave_fails_still_emits_trade_closed(
        self, copier_factory, make_config, event_collector
    ):
        """When slave close fails, both ERROR and TRADE_CLOSED events are emitted.

        This ensures the UI always cleans up the trade row even when the
        close request failed (e.g. broker already closed the position via SL/TP).
        """
        config = make_config(num_slaves=1)

        def fail_close(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=False, action="close",
                ticket=task.params.get("ticket"),
                error="Position doesn't exist (retcode=10036)",
            )

        copier = copier_factory(config, result_factory=fail_close)
        self._setup_tracked(copier, 211, {"200000@TestServer": 900041})

        changes = PositionChange(
            new_positions=[], closed_tickets=[211], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        # Position removed from tracker regardless
        assert not copier.tracker.is_tracked(211)

        # Both ERROR and TRADE_CLOSED should be emitted
        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert len(errors) == 1
        assert errors[0].slave_name == "Slave1"

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert closed[0].slave_name == "Slave1"
        assert closed[0].profit == 0.0  # Defensive close uses profit=0

    @pytest.mark.asyncio
    async def test_close_already_closed_by_broker(
        self, copier_factory, make_config, event_collector
    ):
        """Worker returns success=True, already_closed=True -> TRADE_CLOSED emitted normally."""
        config = make_config(num_slaves=1)

        def already_closed_with_original_error(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={
                    "already_closed": True,
                    "original_error": "Position doesn't exist (retcode=10036)",
                    "profit": -8.25,
                },
            )

        copier = copier_factory(config, result_factory=already_closed_with_original_error)
        self._setup_tracked(copier, 212, {"200000@TestServer": 900042})

        changes = PositionChange(
            new_positions=[], closed_tickets=[212], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert not copier.tracker.is_tracked(212)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert closed[0].profit == -8.25

        # No error events since success=True
        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert len(errors) == 0


    @pytest.mark.asyncio
    async def test_paused_slave_is_still_closed(
        self, copier_factory, make_config, event_collector
    ):
        """Pause stops NEW copies — it must never block an EXIT.

        A paused slave holding a live position while the master has gone flat is
        unhedged risk the user never asked for, and the ticket is claimed on the
        way in, so a skipped close is never retried.
        """
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 213, {"200000@TestServer": 900051, "200001@TestServer": 900052})

        state = SlaveStateMachine()
        state.add_slave("200000@TestServer", SlaveStatus.ON)
        state.add_slave("200001@TestServer", SlaveStatus.ON)
        state.pause("200001@TestServer")

        changes = PositionChange(
            new_positions=[], closed_tickets=[213], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        tasks = copier.executor.submitted_tasks
        assert {t.slave_name for t in tasks} == {"Slave1", "Slave2"}
        assert not copier.tracker.is_tracked(213)

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert {e.slave_name for e in closed} == {"Slave1", "Slave2"}

    @pytest.mark.asyncio
    async def test_off_slave_is_reported_not_silently_dropped(
        self, copier_factory, make_config, event_collector
    ):
        """An Off slave can't be closed — but the UI must not keep showing it.

        claim_close has already released the ticket and the master ticket is
        gone, so nothing will revisit this position. It gets an ERROR (the
        position really is still open at the broker) AND a TRADE_CLOSED, so the
        row leaves Active Trades instead of lingering there forever.
        """
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 214, {"200000@TestServer": 900061, "200001@TestServer": 900062})

        state = SlaveStateMachine()
        state.add_slave("200000@TestServer", SlaveStatus.ON)
        state.add_slave("200001@TestServer", SlaveStatus.OFF)

        changes = PositionChange(
            new_positions=[], closed_tickets=[214], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        tasks = copier.executor.submitted_tasks
        assert [t.slave_name for t in tasks] == ["Slave1"]

        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert {e.slave_name for e in closed} == {"Slave1", "Slave2"}

        errors = [e for e in event_collector if e.event_type == EventType.ERROR]
        assert [e.slave_name for e in errors] == ["Slave2"]
        assert "not closed" in errors[0].error.lower()

    @pytest.mark.asyncio
    async def test_every_slave_off_still_clears_the_ui(
        self, copier_factory, make_config, event_collector
    ):
        """The all-skipped path used to return without emitting anything."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)
        self._setup_tracked(copier, 215, {"200000@TestServer": 900071, "200001@TestServer": 900072})

        state = SlaveStateMachine()
        state.add_slave("200000@TestServer", SlaveStatus.OFF)
        state.add_slave("200001@TestServer", SlaveStatus.OFF)

        changes = PositionChange(
            new_positions=[], closed_tickets=[215], modified_positions=[], source="Polling"
        )
        await copier.process_changes(changes)

        assert copier.executor.submitted_tasks == []
        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert {e.slave_name for e in closed} == {"Slave1", "Slave2"}


    @pytest.mark.asyncio
    async def test_zero_profit_with_sl_set_logs_error(
        self, copier_factory, make_config, event_collector, caplog
    ):
        """PnL=0 with non-zero SL should log an error (history lookup likely failed)."""
        import logging
        config = make_config(num_slaves=1)

        def zero_profit_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"profit": 0.0},
            )

        copier = copier_factory(config, result_factory=zero_profit_factory)
        # Track with SL set on the slave position
        copier.tracker.add_position(
            master_ticket=214, symbol="EURUSD", order_type=0, master_volume=0.1,
        )
        copier.tracker.add_slave_position(
            master_ticket=214, slave_id="200000@TestServer",
            slave_ticket=900061, volume=0.1, sl=1.09000,
        )

        changes = PositionChange(
            new_positions=[], closed_tickets=[214], modified_positions=[], source="Polling"
        )
        with caplog.at_level(logging.ERROR):
            await copier.process_changes(changes)

        assert any("PnL=0.00 but SL was set" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_zero_profit_without_sl_no_error(
        self, copier_factory, make_config, event_collector, caplog
    ):
        """PnL=0 with SL=0 should NOT log the warning (no SL means normal)."""
        import logging
        config = make_config(num_slaves=1)

        def zero_profit_factory(task: SlaveTask) -> SlaveResult:
            return SlaveResult(
                slave_name="", success=True, action="close",
                ticket=task.params.get("ticket"),
                details={"profit": 0.0},
            )

        copier = copier_factory(config, result_factory=zero_profit_factory)
        self._setup_tracked(copier, 215, {"200000@TestServer": 900062})

        changes = PositionChange(
            new_positions=[], closed_tickets=[215], modified_positions=[], source="Polling"
        )
        with caplog.at_level(logging.ERROR):
            await copier.process_changes(changes)

        assert not any("PnL=0.00 but SL was set" in msg for msg in caplog.messages)


class TestGetHistoryProfit:
    """Unit tests for _get_history_profit() helper."""

    def test_sums_deals_profit_swap_commission(self):
        """Deals with profit + swap + commission are summed correctly."""
        import sys
        from types import SimpleNamespace
        fake_mt5 = sys.modules["MetaTrader5"]

        deals = [
            SimpleNamespace(profit=50.0, swap=-1.5, commission=-2.0),
            SimpleNamespace(profit=0.0, swap=0.0, commission=-2.0),
        ]
        fake_mt5.history_deals_get.return_value = deals

        from src.parallel_worker import _get_history_profit
        result = _get_history_profit(12345)

        assert result == pytest.approx(44.5)
        fake_mt5.history_deals_get.assert_called_with(position=12345)

        # Cleanup
        fake_mt5.history_deals_get.return_value = None

    def test_returns_none_when_no_deals(self):
        """Returns None when history_deals_get returns empty or None."""
        import sys
        fake_mt5 = sys.modules["MetaTrader5"]
        fake_mt5.history_deals_get.return_value = None

        from src.parallel_worker import _get_history_profit
        assert _get_history_profit(99999) is None

        fake_mt5.history_deals_get.return_value = []
        assert _get_history_profit(99999) is None

    def test_returns_none_on_exception(self):
        """Returns None when history_deals_get raises an exception."""
        import sys
        fake_mt5 = sys.modules["MetaTrader5"]
        fake_mt5.history_deals_get.side_effect = RuntimeError("MT5 error")

        from src.parallel_worker import _get_history_profit
        assert _get_history_profit(88888) is None

        # Cleanup
        fake_mt5.history_deals_get.side_effect = None
        fake_mt5.history_deals_get.return_value = None
