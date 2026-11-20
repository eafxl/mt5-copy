"""Tests for MasterMonitor change detection logic."""
import pytest
from unittest.mock import MagicMock

from src.master import MasterMonitor, PositionChange
from src.mt5_client import Position, AccountInfo


@pytest.fixture
def make_pos():
    """Shorthand factory for Position objects."""
    def _make(ticket=100, symbol="EURUSD", type=0, volume=0.1,
              price_open=1.1, price_current=1.1, sl=0.0, tp=0.0,
              profit=0.0, magic=0, comment="", time=0):
        return Position(
            ticket=ticket, symbol=symbol, type=type, volume=volume,
            price_open=price_open, price_current=price_current,
            sl=sl, tp=tp, profit=profit, magic=magic,
            comment=comment, time=time,
        )
    return _make


@pytest.fixture
def mock_mt5():
    mt5 = MagicMock()
    mt5.get_positions.return_value = []
    mt5.get_account_info.return_value = AccountInfo(
        login=10000, server="TestServer", leverage=100,
        balance=10000.0, equity=10000.0, margin=0.0,
        margin_free=10000.0, currency="USD",
    )
    mt5.initialize.return_value = True
    return mt5


@pytest.fixture
def monitor(mock_mt5):
    return MasterMonitor(mock_mt5, terminal_path="/terminal/master")


class TestChangeDetection:
    """MasterMonitor change detection."""

    def test_no_changes_empty(self, monitor, mock_mt5):
        """No changes -> empty PositionChange."""
        mock_mt5.get_positions.return_value = []
        changes = monitor.detect_changes()

        assert changes.new_positions == []
        assert changes.closed_tickets == []
        assert changes.modified_positions == []

    def test_new_position_appears(self, monitor, mock_mt5, make_pos):
        """New position appears -> in new_positions."""
        # First snapshot: empty
        mock_mt5.get_positions.return_value = []
        monitor.detect_changes()

        # Second snapshot: one position
        pos = make_pos(ticket=100)
        mock_mt5.get_positions.return_value = [pos]
        changes = monitor.detect_changes()

        assert len(changes.new_positions) == 1
        assert changes.new_positions[0].ticket == 100

    def test_position_disappears(self, monitor, mock_mt5, make_pos):
        """Position disappears -> in closed_tickets."""
        pos = make_pos(ticket=101)
        mock_mt5.get_positions.return_value = [pos]
        monitor.detect_changes()  # snapshot

        mock_mt5.get_positions.return_value = []
        changes = monitor.detect_changes()

        assert 101 in changes.closed_tickets

    def test_sl_changed(self, monitor, mock_mt5, make_pos):
        """SL changed -> in modified_positions."""
        pos = make_pos(ticket=102, sl=0.0)
        mock_mt5.get_positions.return_value = [pos]
        monitor.detect_changes()

        pos_modified = make_pos(ticket=102, sl=1.09)
        mock_mt5.get_positions.return_value = [pos_modified]
        changes = monitor.detect_changes()

        assert len(changes.modified_positions) == 1
        assert changes.modified_positions[0].ticket == 102
        assert changes.modified_positions[0].sl == 1.09

    def test_tp_changed(self, monitor, mock_mt5, make_pos):
        """TP changed -> in modified_positions."""
        pos = make_pos(ticket=103, tp=0.0)
        mock_mt5.get_positions.return_value = [pos]
        monitor.detect_changes()

        pos_modified = make_pos(ticket=103, tp=1.12)
        mock_mt5.get_positions.return_value = [pos_modified]
        changes = monitor.detect_changes()

        assert len(changes.modified_positions) == 1
        assert changes.modified_positions[0].tp == 1.12

    def test_volume_price_not_modified(self, monitor, mock_mt5, make_pos):
        """Volume/price changes -> NOT in modified (only SL/TP tracked)."""
        pos = make_pos(ticket=104, volume=0.1, price_current=1.1)
        mock_mt5.get_positions.return_value = [pos]
        monitor.detect_changes()

        # Change volume and price but NOT sl/tp
        pos_changed = make_pos(ticket=104, volume=0.2, price_current=1.2)
        mock_mt5.get_positions.return_value = [pos_changed]
        changes = monitor.detect_changes()

        assert len(changes.modified_positions) == 0

    def test_multiple_simultaneous_changes(self, monitor, mock_mt5, make_pos):
        """Multiple simultaneous changes (new + close + modify)."""
        pos_a = make_pos(ticket=105)
        pos_b = make_pos(ticket=106, sl=0.0)
        mock_mt5.get_positions.return_value = [pos_a, pos_b]
        monitor.detect_changes()

        # pos_a disappears, pos_b gets SL changed, pos_c is new
        pos_b_modified = make_pos(ticket=106, sl=1.09)
        pos_c = make_pos(ticket=107)
        mock_mt5.get_positions.return_value = [pos_b_modified, pos_c]
        changes = monitor.detect_changes()

        assert 105 in changes.closed_tickets
        assert any(p.ticket == 107 for p in changes.new_positions)
        assert any(p.ticket == 106 for p in changes.modified_positions)

    def test_snapshot_positions(self, monitor, mock_mt5, make_pos):
        """snapshot_positions -> updates internal state."""
        pos = make_pos(ticket=108)
        mock_mt5.get_positions.return_value = [pos]

        monitor.snapshot_positions()

        assert 108 in monitor.last_positions
        assert monitor.last_positions[108].ticket == 108

    def test_get_leverage_with_account_info(self, monitor, mock_mt5):
        """get_leverage -> returns from account_info."""
        monitor._account_info = AccountInfo(
            login=10000, server="TestServer", leverage=200,
            balance=10000.0, equity=10000.0, margin=0.0,
            margin_free=10000.0, currency="USD",
        )
        assert monitor.get_leverage() == 200

    def test_get_leverage_no_account_info(self, monitor):
        """get_leverage -> no account_info -> default 100."""
        monitor._account_info = None
        assert monitor.get_leverage() == 100

    def test_connect_success(self, monitor, mock_mt5):
        """connect -> success path."""
        result = monitor.connect()
        assert result is True
        mock_mt5.initialize.assert_called_once()
        mock_mt5.get_account_info.assert_called()

    def test_connect_init_fails(self, monitor, mock_mt5):
        """connect -> init fails."""
        mock_mt5.initialize.return_value = False
        result = monitor.connect()
        assert result is False

    def test_reconnect_still_connected(self, monitor, mock_mt5):
        """reconnect -> already connected -> no-op (returns True)."""
        result = monitor.reconnect()
        assert result is True
        # initialize should NOT be called since we're still connected
        mock_mt5.initialize.assert_not_called()

    def test_reconnect_lost_connection(self, monitor, mock_mt5):
        """reconnect -> connection lost -> reconnects."""
        # First call to get_account_info returns None (disconnected)
        mock_mt5.get_account_info.side_effect = [None, AccountInfo(
            login=10000, server="TestServer", leverage=100,
            balance=10000.0, equity=10000.0, margin=0.0,
            margin_free=10000.0, currency="USD",
        )]
        result = monitor.reconnect()
        assert result is True
        mock_mt5.initialize.assert_called_once()
