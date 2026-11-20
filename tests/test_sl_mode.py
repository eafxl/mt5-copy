"""SL/TP calculation tests.

SL/TP is copied as a DISTANCE from the master and applied to the slave's
current entry price (single behavior — the per-slave "Calculate(time/price)"
mode was removed because the historical-tick lookup was unreliable on some
brokers, e.g. Bybit, and produced wrong-side / invalid stops). Distance-based
placement is always on the correct side of the slave's entry, so it can't
generate an Invalid-stops (10016) reject.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.pip_calculator import calculate_slave_sl_tp

DIGITS = 2


def test_buy_distance_applied_to_slave_entry():
    # Master: open 90.00, SL 89.50 (dist 0.50), TP 91.00 (dist 1.00).
    # Slave fills at 90.20 -> SL 89.70, TP 91.20 (same distances, correct side).
    sl, tp = calculate_slave_sl_tp(
        master_entry=90.00, master_sl=89.50, master_tp=91.00,
        slave_entry=90.20, is_buy=True, symbol_digits=DIGITS,
    )
    assert sl == 89.70
    assert tp == 91.20


def test_sell_distance_applied_to_slave_entry():
    # SELL: SL above, TP below. Master open 90.00, SL 90.50, TP 89.00.
    # Slave fills 90.20 -> SL 90.70, TP 89.20.
    sl, tp = calculate_slave_sl_tp(
        master_entry=90.00, master_sl=90.50, master_tp=89.00,
        slave_entry=90.20, is_buy=False, symbol_digits=DIGITS,
    )
    assert sl == 90.70
    assert tp == 89.20


def test_sl_always_correct_side_for_buy():
    # Even with drift, BUY SL stays BELOW entry and TP ABOVE — never wrong-side.
    sl, tp = calculate_slave_sl_tp(
        master_entry=27776.0, master_sl=27760.0, master_tp=27800.0,
        slave_entry=27797.85, is_buy=True, symbol_digits=DIGITS,
    )
    # BUY: SL below entry, TP above — never wrong-side even with drift.
    assert sl < 27797.85
    assert tp > 27797.85


def test_zero_sl_tp_stays_zero():
    sl, tp = calculate_slave_sl_tp(
        master_entry=90.0, master_sl=0.0, master_tp=0.0,
        slave_entry=90.2, is_buy=True, symbol_digits=DIGITS,
    )
    assert sl == 0.0 and tp == 0.0