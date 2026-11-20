"""Tests for PositionTracker in-memory state management."""
import pytest

from src.position_tracker import PositionTracker, TrackedPosition, SlavePosition


class TestPositionTracker:
    """In-memory position tracker."""

    @pytest.fixture
    def tracker(self):
        return PositionTracker()

    def test_add_position_generates_trade_id(self, tracker):
        """add_position -> generates trade_id, stored by str(ticket)."""
        tracked = tracker.add_position(
            master_ticket=100, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        assert tracked.trade_id != ""
        assert len(tracked.trade_id) == 8
        assert tracker.is_tracked(100)

    def test_add_position_custom_trade_id(self, tracker):
        """add_position with custom trade_id preserves it."""
        tracked = tracker.add_position(
            master_ticket=101, symbol="EURUSD",
            order_type=0, master_volume=0.1,
            trade_id="CUSTOM01",
        )
        assert tracked.trade_id == "CUSTOM01"

    def test_add_slave_position(self, tracker):
        """add_slave_position -> slave added to tracked position."""
        tracker.add_position(
            master_ticket=102, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=102, slave_id="Slave1",
            slave_ticket=900001, volume=0.1, sl=1.09, tp=1.12,
        )

        tracked = tracker.get_position(102)
        assert "Slave1" in tracked.slaves
        assert tracked.slaves["Slave1"].ticket == 900001
        assert tracked.slaves["Slave1"].volume == 0.1
        assert tracked.slaves["Slave1"].sl == 1.09
        assert tracked.slaves["Slave1"].tp == 1.12

    def test_add_slave_position_untracked_master(self, tracker):
        """add_slave_position on non-tracked master -> warning, no crash."""
        # Should not raise
        tracker.add_slave_position(
            master_ticket=999, slave_id="Slave1",
            slave_ticket=900001, volume=0.1,
        )
        assert not tracker.is_tracked(999)

    def test_get_position_exists(self, tracker):
        """get_position -> returns TrackedPosition."""
        tracker.add_position(
            master_ticket=103, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        result = tracker.get_position(103)
        assert result is not None
        assert result.master_ticket == 103
        assert result.symbol == "EURUSD"

    def test_get_position_not_exists(self, tracker):
        """get_position -> returns None when not tracked."""
        assert tracker.get_position(999) is None

    def test_is_tracked(self, tracker):
        """is_tracked returns correct booleans."""
        tracker.add_position(
            master_ticket=104, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        assert tracker.is_tracked(104) is True
        assert tracker.is_tracked(999) is False

    def test_has_slave_position(self, tracker):
        """has_slave_position returns correct booleans."""
        tracker.add_position(
            master_ticket=105, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=105, slave_id="Slave1",
            slave_ticket=900001, volume=0.1,
        )
        assert tracker.has_slave_position(105, "Slave1") is True
        assert tracker.has_slave_position(105, "Slave2") is False
        assert tracker.has_slave_position(999, "Slave1") is False

    def test_remove_position(self, tracker):
        """remove_position -> gone from all lookups."""
        tracker.add_position(
            master_ticket=106, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.remove_position(106)
        assert not tracker.is_tracked(106)
        assert tracker.get_position(106) is None

    def test_remove_nonexistent_no_crash(self, tracker):
        """remove_position on non-existent ticket -> no crash."""
        tracker.remove_position(999)  # Should not raise

    def test_get_all_positions(self, tracker):
        """get_all_positions -> returns {int: TrackedPosition} dict."""
        tracker.add_position(
            master_ticket=107, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_position(
            master_ticket=108, symbol="GBPUSD",
            order_type=1, master_volume=0.2,
        )
        all_pos = tracker.get_all_positions()
        assert len(all_pos) == 2
        assert 107 in all_pos
        assert 108 in all_pos
        assert isinstance(all_pos[107], TrackedPosition)

    def test_get_slave_ticket(self, tracker):
        """get_slave_ticket -> returns ticket or None."""
        tracker.add_position(
            master_ticket=109, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=109, slave_id="Slave1",
            slave_ticket=900001, volume=0.1,
        )
        assert tracker.get_slave_ticket(109, "Slave1") == 900001
        assert tracker.get_slave_ticket(109, "Slave2") is None
        assert tracker.get_slave_ticket(999, "Slave1") is None

    def test_get_trade_id(self, tracker):
        """get_trade_id -> returns trade_id or None."""
        tracked = tracker.add_position(
            master_ticket=110, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        assert tracker.get_trade_id(110) == tracked.trade_id
        assert tracker.get_trade_id(999) is None

    def test_get_master_tickets(self, tracker):
        """get_master_tickets -> set of all tracked tickets."""
        tracker.add_position(
            master_ticket=111, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_position(
            master_ticket=112, symbol="GBPUSD",
            order_type=1, master_volume=0.2,
        )
        assert tracker.get_master_tickets() == {111, 112}

    def test_get_master_tickets_empty(self, tracker):
        """get_master_tickets on empty tracker -> empty set."""
        assert tracker.get_master_tickets() == set()

    def test_update_master_sl_tp(self, tracker):
        """update_master_sl_tp -> values updated."""
        tracker.add_position(
            master_ticket=113, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.update_master_sl_tp(113, sl=1.09, tp=1.12)
        tracked = tracker.get_position(113)
        assert tracked.master_sl == 1.09
        assert tracked.master_tp == 1.12

    def test_update_master_sl_tp_nonexistent(self, tracker):
        """update_master_sl_tp on non-existent -> no crash."""
        tracker.update_master_sl_tp(999, sl=1.09, tp=1.12)

    def test_update_slave_sl_tp(self, tracker):
        """update_slave_sl_tp -> slave values updated."""
        tracker.add_position(
            master_ticket=114, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=114, slave_id="Slave1",
            slave_ticket=900001, volume=0.1,
        )
        tracker.update_slave_sl_tp(114, "Slave1", sl=1.08, tp=1.13)

        tracked = tracker.get_position(114)
        assert tracked.slaves["Slave1"].sl == 1.08
        assert tracked.slaves["Slave1"].tp == 1.13

    def test_update_slave_sl_tp_nonexistent_slave(self, tracker):
        """update_slave_sl_tp on non-existent slave -> no crash."""
        tracker.add_position(
            master_ticket=115, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.update_slave_sl_tp(115, "NoSlave", sl=1.08, tp=1.13)

    def test_cleanup_missing_slaves(self, tracker):
        """cleanup_missing_slaves -> removes stale slave entries."""
        tracker.add_position(
            master_ticket=116, symbol="EURUSD",
            order_type=0, master_volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=116, slave_id="Slave1",
            slave_ticket=900001, volume=0.1,
        )
        tracker.add_slave_position(
            master_ticket=116, slave_id="OldSlave",
            slave_ticket=900002, volume=0.1,
        )

        tracker.cleanup_missing_slaves({"Slave1"})

        tracked = tracker.get_position(116)
        assert "Slave1" in tracked.slaves
        assert "OldSlave" not in tracked.slaves


class TestTrackedPositionSerialization:
    """TrackedPosition to_dict / from_dict round-trip."""

    def test_to_dict_from_dict_roundtrip(self):
        """TrackedPosition to_dict / from_dict round-trip."""
        original = TrackedPosition(
            master_ticket=200,
            symbol="EURUSD",
            order_type=0,
            master_volume=0.1,
            master_sl=1.09,
            master_tp=1.12,
            trade_id="ABCD1234",
            slaves={
                "Slave1": SlavePosition(ticket=900001, volume=0.1, sl=1.085, tp=1.125),
                "Slave2": SlavePosition(ticket=900002, volume=0.2, sl=0.0, tp=0.0),
            },
        )

        data = original.to_dict()
        restored = TrackedPosition.from_dict(data)

        assert restored.master_ticket == original.master_ticket
        assert restored.symbol == original.symbol
        assert restored.order_type == original.order_type
        assert restored.master_volume == original.master_volume
        assert restored.master_sl == original.master_sl
        assert restored.master_tp == original.master_tp
        assert restored.trade_id == original.trade_id
        assert len(restored.slaves) == 2
        assert restored.slaves["Slave1"].ticket == 900001
        assert restored.slaves["Slave1"].sl == 1.085
        assert restored.slaves["Slave2"].volume == 0.2

    def test_from_dict_legacy_no_trade_id(self):
        """from_dict with missing trade_id -> auto-generates one."""
        data = {
            "master_ticket": 201,
            "symbol": "EURUSD",
            "order_type": 0,
            "master_volume": 0.1,
            "slaves": {},
        }
        restored = TrackedPosition.from_dict(data)
        assert restored.trade_id != ""
        assert len(restored.trade_id) == 8

    def test_from_dict_legacy_no_sl_tp(self):
        """from_dict with missing sl/tp -> defaults to 0.0."""
        data = {
            "master_ticket": 202,
            "symbol": "EURUSD",
            "order_type": 0,
            "master_volume": 0.1,
            "trade_id": "TEST0001",
            "slaves": {
                "S1": {"ticket": 900001, "volume": 0.1},
            },
        }
        restored = TrackedPosition.from_dict(data)
        assert restored.master_sl == 0.0
        assert restored.master_tp == 0.0
        assert restored.slaves["S1"].sl == 0.0
