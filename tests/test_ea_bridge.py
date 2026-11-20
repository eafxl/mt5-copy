"""Tests for the EA push bridge parser — ensures id + timestamp + levels are
properly assigned and connected from the raw EA JSON into PositionChange."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ea_bridge import parse_line, change_from_event, position_from_event


def test_open_event_maps_all_fields():
    ev = {
        "event": "open", "ticket": 581957168, "symbol": "NAS100", "type": 1,
        "volume": 0.1, "price": 27776.53, "sl": 27788.8, "tp": 27700.1,
        "time_msc": 1785325784123, "magic": 0, "comment": "manual",
    }
    p = position_from_event(ev)
    assert p.ticket == 581957168            # master id connected
    assert p.symbol == "NAS100"
    assert p.type == 1
    assert p.volume == 0.1
    assert p.price_open == 27776.53
    assert p.price_current == 27776.53      # current == open at push time
    assert p.sl == 27788.8 and p.tp == 27700.1
    assert p.time == 1785325784             # ms -> s (master server epoch)
    assert p.magic == 0


def test_open_change():
    ch = parse_line('{"event":"open","ticket":10,"symbol":"XAUUSD","type":0,'
                    '"volume":0.5,"price":4026.0,"sl":3990.0,"tp":4075.0,'
                    '"time_msc":1785000000000,"magic":0,"comment":""}')
    assert len(ch.new_positions) == 1
    assert ch.closed_tickets == [] and ch.modified_positions == []
    assert ch.new_positions[0].ticket == 10
    assert ch.new_positions[0].time == 1785000000


def test_modify_change_carries_new_levels():
    ch = parse_line('{"event":"modify","ticket":10,"symbol":"XAUUSD","type":0,'
                    '"volume":0.5,"price":4026.0,"sl":4000.0,"tp":4080.0,'
                    '"time_msc":1785000000000,"magic":0,"comment":""}')
    assert ch.new_positions == [] and ch.closed_tickets == []
    assert len(ch.modified_positions) == 1
    m = ch.modified_positions[0]
    assert m.ticket == 10 and m.sl == 4000.0 and m.tp == 4080.0


def test_reduce_change_carries_new_volume():
    # Partial close (scale-out): EA pushes the position's NEW smaller volume.
    ch = parse_line('{"event":"reduce","ticket":10,"symbol":"XAUUSD","type":0,'
                    '"volume":0.3,"price":4026.0,"sl":3990.0,"tp":4075.0,'
                    '"time_msc":1785000000000,"magic":0,"comment":""}')
    assert ch.new_positions == [] and ch.closed_tickets == []
    assert ch.modified_positions == []
    assert len(ch.reduced_positions) == 1
    assert ch.reduced_positions[0].ticket == 10
    assert ch.reduced_positions[0].volume == 0.3
    assert ch.source == "TCP"


def test_tracker_volume_updates_after_scaleout():
    from src.position_tracker import PositionTracker
    t = PositionTracker()
    t.claim_position(master_ticket=5, symbol="X", order_type=0, master_volume=1.0)
    t.add_slave_position(master_ticket=5, slave_id="s1", slave_ticket=99, volume=2.0)
    t.update_master_volume(5, 0.4)          # master scaled 1.0 -> 0.4
    t.update_slave_volume(5, "s1", 0.8)     # slave kept 40% (2.0 -> 0.8)
    pos = t.get_position(5)
    assert pos.master_volume == 0.4
    assert pos.slaves["s1"].volume == 0.8


def test_close_change_only_ticket():
    ch = parse_line('{"event":"close","ticket":10}')
    assert ch.closed_tickets == [10]
    assert ch.new_positions == [] and ch.modified_positions == []


def test_heartbeat_is_noop():
    assert parse_line('{"event":"heartbeat","ts_msc":1785000000000}') is None


def test_blank_line_is_noop():
    assert parse_line("   ") is None


def test_unknown_event_is_noop():
    assert change_from_event({"event": "whatever"}) is None


def test_claim_position_dedups_across_sources():
    # The single guard that prevents EA-push + polling from copying the same
    # trade twice: first claim wins (is_new True), second is rejected.
    from src.position_tracker import PositionTracker
    t = PositionTracker()
    p1, new1 = t.claim_position(master_ticket=42, symbol="NAS100",
                                order_type=1, master_volume=0.1)
    p2, new2 = t.claim_position(master_ticket=42, symbol="NAS100",
                                order_type=1, master_volume=0.1)
    assert new1 is True
    assert new2 is False                 # second source is blocked
    assert p1.trade_id == p2.trade_id    # not overwritten; same trade
    # A different ticket claims fresh.
    _, new3 = t.claim_position(master_ticket=43, symbol="NAS100",
                               order_type=1, master_volume=0.1)
    assert new3 is True


def test_missing_optional_fields_default_zero():
    # No sl/tp/magic -> zeros, no crash. id + time still required/mapped.
    p = position_from_event({"event": "open", "ticket": 7, "symbol": "EURUSD",
                             "type": 0, "volume": 1.0, "price": 1.1,
                             "time_msc": 0})
    assert p.ticket == 7 and p.sl == 0.0 and p.tp == 0.0 and p.magic == 0
    assert p.time == 0