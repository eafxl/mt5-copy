"""Tests for position discovery and initial sync."""
import pytest

from src.parallel_worker import SlaveResult, SlaveTask, TaskAction
from src.events import EventType
from src.mt5_client import Position


@pytest.fixture
def config1(make_config):
    return make_config(num_slaves=1)


@pytest.fixture
def config2(make_config):
    return make_config(num_slaves=2)


class TestDiscovery:
    """Position discovery — matching existing slave positions to master."""

    @pytest.mark.asyncio
    async def test_discovery_slave_has_matching_magic(
        self, copier_factory, config1, make_position, mock_master
    ):
        """1 master pos, 1 slave has matching magic -> tracked, not re-copied."""
        master_pos = make_position(ticket=400, symbol="EURUSD", volume=0.1)
        mock_master.mt5.get_positions.return_value = [master_pos]

        def fetch_factory(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": [
                        {"ticket": 800001, "symbol": "EURUSD", "type": 0,
                         "volume": 0.1, "magic": 400, "sl": 0, "tp": 0},
                    ]},
                )
            # OPEN tasks (for sync) — should not be called
            return SlaveResult(
                slave_name="", success=True, action=task.action.value,
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config1, result_factory=fetch_factory)
        copier.config.settings.sync_existing_on_start = True

        await copier.sync_initial()

        # Position should be tracked via discovery
        assert copier.tracker.is_tracked(400)
        tracked = copier.tracker.get_position(400)
        assert "200000@TestServer" in tracked.slaves
        assert tracked.slaves["200000@TestServer"].ticket == 800001

    @pytest.mark.asyncio
    async def test_discovery_partial_sync(
        self, copier_factory, config2, make_position, mock_master
    ):
        """1 master pos, 2 slaves, only 1 has match -> partially synced."""
        master_pos = make_position(ticket=401, symbol="EURUSD", volume=0.1)
        mock_master.mt5.get_positions.return_value = [master_pos]

        def partial_factory(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                if task.slave_name == "Slave1":
                    return SlaveResult(
                        slave_name="", success=True, action="fetch_positions",
                        details={"positions": [
                            {"ticket": 800001, "symbol": "EURUSD", "type": 0,
                             "volume": 0.1, "magic": 401, "sl": 0, "tp": 0},
                        ]},
                    )
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": []},
                )
            # OPEN for unsynced slave
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config2, result_factory=partial_factory, embedded=True)
        await copier.sync_initial()

        tracked = copier.tracker.get_position(401)
        assert "200000@TestServer" in tracked.slaves
        # Slave2 should have been synced via open task
        assert "200001@TestServer" in tracked.slaves

    @pytest.mark.asyncio
    async def test_discovery_no_slave_positions(
        self, copier_factory, config1, make_position, mock_master
    ):
        """No slave positions -> all offered for sync."""
        master_pos = make_position(ticket=402)
        mock_master.mt5.get_positions.return_value = [master_pos]

        copier = copier_factory(config1, embedded=True)
        await copier.sync_initial()

        # Position should be synced (opened on slave)
        assert copier.tracker.is_tracked(402)
        open_tasks = [t for t in copier.executor.submitted_tasks
                      if t.action == TaskAction.OPEN]
        assert len(open_tasks) == 1

    @pytest.mark.asyncio
    async def test_discovery_slave_fetch_fails(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Slave fetch fails -> position treated as unsynced."""
        master_pos = make_position(ticket=403)
        mock_master.mt5.get_positions.return_value = [master_pos]

        def fail_fetch_factory(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=False, action="fetch_positions",
                    error="Connection timeout",
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config1, result_factory=fail_fetch_factory, embedded=True)
        await copier.sync_initial()

        # Should have tried to sync the position
        open_tasks = [t for t in copier.executor.submitted_tasks
                      if t.action == TaskAction.OPEN]
        assert len(open_tasks) == 1

    @pytest.mark.asyncio
    async def test_discovery_multiple_master_positions(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Multiple master positions, various match states."""
        pos1 = make_position(ticket=404, symbol="EURUSD")
        pos2 = make_position(ticket=405, symbol="GBPUSD")
        mock_master.mt5.get_positions.return_value = [pos1, pos2]

        def mixed_discovery(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": [
                        {"ticket": 800001, "symbol": "EURUSD", "type": 0,
                         "volume": 0.1, "magic": 404, "sl": 0, "tp": 0},
                    ]},
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config1, result_factory=mixed_discovery, embedded=True)
        await copier.sync_initial()

        # pos1 (404) discovered, pos2 (405) synced via open
        assert copier.tracker.is_tracked(404)
        assert copier.tracker.is_tracked(405)


class TestSyncInitial:
    """Initial sync behavior."""

    @pytest.mark.asyncio
    async def test_sync_disabled(
        self, copier_factory, make_config, mock_master, make_position
    ):
        """sync_existing_on_start=False -> skip entirely."""
        config = make_config(num_slaves=1, sync_existing_on_start=False)
        mock_master.mt5.get_positions.return_value = [make_position(ticket=410)]

        copier = copier_factory(config)
        await copier.sync_initial()

        assert len(copier.executor.submitted_tasks) == 0
        assert not copier.tracker.is_tracked(410)

    @pytest.mark.asyncio
    async def test_sync_no_master_positions(
        self, copier_factory, make_config, mock_master
    ):
        """No master positions -> early return."""
        config = make_config(num_slaves=1)
        mock_master.mt5.get_positions.return_value = []

        copier = copier_factory(config)
        await copier.sync_initial()

        assert len(copier.executor.submitted_tasks) == 0

    @pytest.mark.asyncio
    async def test_sync_all_already_synced(
        self, copier_factory, config1, make_position, mock_master
    ):
        """All positions already synced -> 'All positions already synced' path."""
        master_pos = make_position(ticket=411)
        mock_master.mt5.get_positions.return_value = [master_pos]

        def fully_synced(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": [
                        {"ticket": 800001, "symbol": "EURUSD", "type": 0,
                         "volume": 0.1, "magic": 411, "sl": 0, "tp": 0},
                    ]},
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config1, result_factory=fully_synced)
        await copier.sync_initial()

        # No OPEN tasks should have been submitted
        open_tasks = [t for t in copier.executor.submitted_tasks
                      if t.action == TaskAction.OPEN]
        assert len(open_tasks) == 0

    @pytest.mark.asyncio
    async def test_sync_embedded_auto_approved(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Unsynced positions -> auto-approved in embedded mode."""
        master_pos = make_position(ticket=412)
        mock_master.mt5.get_positions.return_value = [master_pos]

        copier = copier_factory(config1, embedded=True)
        await copier.sync_initial()

        open_tasks = [t for t in copier.executor.submitted_tasks
                      if t.action == TaskAction.OPEN]
        assert len(open_tasks) == 1

    @pytest.mark.asyncio
    async def test_sync_tracker_has_all_positions(
        self, copier_factory, config1, make_position, mock_master
    ):
        """After sync, tracker has all positions."""
        pos1 = make_position(ticket=413, symbol="EURUSD")
        pos2 = make_position(ticket=414, symbol="GBPUSD")
        mock_master.mt5.get_positions.return_value = [pos1, pos2]

        copier = copier_factory(config1, embedded=True)
        await copier.sync_initial()

        assert copier.tracker.is_tracked(413)
        assert copier.tracker.is_tracked(414)

    @pytest.mark.asyncio
    async def test_sync_reconnect_called(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Reconnect to master called after discovery + after sync."""
        master_pos = make_position(ticket=415)
        mock_master.mt5.get_positions.return_value = [master_pos]

        copier = copier_factory(config1, embedded=True)
        await copier.sync_initial()

        # reconnect is called via get_account_info check
        mock_master.mt5.get_account_info.assert_called()


class TestEmitExistingPositions:
    """Emitting TRADE_OPENED for existing tracked positions."""

    def test_emit_existing_positions(
        self, copier_factory, make_config, event_collector
    ):
        """Emits TRADE_OPENED for each tracked slave position."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        copier.tracker.add_position(
            master_ticket=420, symbol="EURUSD", order_type=0, master_volume=0.1,
        )
        copier.tracker.add_slave_position(
            master_ticket=420, slave_id="200000@TestServer",
            slave_ticket=800001, volume=0.1,
        )

        copier.emit_existing_positions()

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 1
        assert opened[0].master_ticket == 420
        assert opened[0].slave_ticket == 800001

    def test_emit_existing_empty_tracker(
        self, copier_factory, make_config, event_collector
    ):
        """Empty tracker -> no events."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)

        copier.emit_existing_positions()

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 0

    def test_emit_existing_multiple_masters_and_slaves(
        self, copier_factory, make_config, event_collector
    ):
        """2 masters x 2 slaves -> 4 events."""
        config = make_config(num_slaves=2)
        copier = copier_factory(config)

        for ticket in [421, 422]:
            copier.tracker.add_position(
                master_ticket=ticket, symbol="EURUSD",
                order_type=0, master_volume=0.1,
            )
            copier.tracker.add_slave_position(
                master_ticket=ticket, slave_id="200000@TestServer",
                slave_ticket=800000 + ticket, volume=0.1,
            )
            copier.tracker.add_slave_position(
                master_ticket=ticket, slave_id="200001@TestServer",
                slave_ticket=800100 + ticket, volume=0.1,
            )

        copier.emit_existing_positions()

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 4

    def test_emit_existing_master_no_slaves(
        self, copier_factory, make_config, event_collector
    ):
        """Master position with 0 slaves tracked -> no event for that master."""
        config = make_config(num_slaves=1)
        copier = copier_factory(config)
        copier.tracker.add_position(
            master_ticket=423, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        # No slave positions added

        copier.emit_existing_positions()

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert len(opened) == 0


class TestFetchPositionsTasks:
    """Verify FETCH_POSITIONS task construction."""

    @pytest.mark.asyncio
    async def test_fetch_positions_task_built_correctly(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Verify FETCH_POSITIONS tasks built with terminal_path, login, server."""
        master_pos = make_position(ticket=430)
        mock_master.mt5.get_positions.return_value = [master_pos]

        copier = copier_factory(config1, embedded=True)
        await copier.sync_initial()

        fetch_tasks = [t for t in copier.executor.submitted_tasks
                       if t.action == TaskAction.FETCH_POSITIONS]
        assert len(fetch_tasks) == 1
        task = fetch_tasks[0]
        assert task.slave_name == "Slave1"
        assert task.terminal_path == "/terminal/slave1"
        assert task.login == 200000
        assert task.server == "TestServer"

    @pytest.mark.asyncio
    async def test_discovery_with_symbol_mapping(
        self, copier_factory, make_config, make_position, mock_master
    ):
        """Discovery with symbol mapping (slave has different symbol name)."""
        config = make_config(
            num_slaves=1,
            slave_overrides=[{"symbol_mapping": {"EURUSD": "EURUSDm"}}],
        )
        master_pos = make_position(ticket=431, symbol="EURUSD")
        mock_master.mt5.get_positions.return_value = [master_pos]

        def fetch_with_mapped(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": [
                        {"ticket": 800001, "symbol": "EURUSDm", "type": 0,
                         "volume": 0.1, "magic": 431, "sl": 0, "tp": 0},
                    ]},
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config, result_factory=fetch_with_mapped)
        await copier.sync_initial()

        # Matched by magic number, symbol doesn't matter for matching
        assert copier.tracker.is_tracked(431)
        tracked = copier.tracker.get_position(431)
        assert "200000@TestServer" in tracked.slaves

    @pytest.mark.asyncio
    async def test_discovery_unmatched_slave_positions_ignored(
        self, copier_factory, config1, make_position, mock_master
    ):
        """Partial discovery: slave has positions that don't match any master."""
        master_pos = make_position(ticket=432)
        mock_master.mt5.get_positions.return_value = [master_pos]

        def extra_positions(task: SlaveTask) -> SlaveResult:
            if task.action == TaskAction.FETCH_POSITIONS:
                return SlaveResult(
                    slave_name="", success=True, action="fetch_positions",
                    details={"positions": [
                        {"ticket": 800001, "symbol": "EURUSD", "type": 0,
                         "volume": 0.1, "magic": 432, "sl": 0, "tp": 0},
                        # This position doesn't match any master
                        {"ticket": 800002, "symbol": "XAUUSD", "type": 0,
                         "volume": 0.5, "magic": 999999, "sl": 0, "tp": 0},
                    ]},
                )
            return SlaveResult(
                slave_name="", success=True, action="open",
                ticket=900099, volume=0.1,
                details={"slave_sl": 0, "slave_tp": 0, "open_price": 1.1, "open_time": 0.0},
            )

        copier = copier_factory(config1, result_factory=extra_positions)
        await copier.sync_initial()

        assert copier.tracker.is_tracked(432)
        # Only the matched position, not the unmatched one
        assert not copier.tracker.is_tracked(999999)
