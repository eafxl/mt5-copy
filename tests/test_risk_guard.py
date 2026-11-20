"""Tests for the global per-trade risk guard.

Covers the pure loss-projection helper plus the guard decision (block when the
loss-if-SL-hit exceeds max_pct % of balance, and when no SL is set).

The guard itself lives inline in parallel_worker._execute_open_with_retry and
needs a live MT5 connection, so here we test the pure math + decision logic
that the worker relies on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.pip_calculator import projected_sl_loss, calculate_risk_based_volume


# XAUUSD-like: 1 lot = 100 oz, tick_size 0.01 => $1 per 0.01 move per lot.
TICK_SIZE = 0.01
TICK_VALUE = 1.0


def _loss(volume, entry, sl):
    return projected_sl_loss(volume, entry, sl, TICK_VALUE, TICK_SIZE)


def test_projected_loss_basic():
    # entry 3977, sl 3990 => 13.0 distance = 1300 ticks * $1 * 1 lot = $1300.
    assert _loss(1.0, 3977.0, 3990.0) == 1300.0


def test_projected_loss_scales_with_volume():
    assert _loss(0.5, 3977.0, 3990.0) == 650.0
    assert _loss(22.58, 3977.0, 3990.0) == 1300.0 * 22.58


def test_projected_loss_zero_when_no_sl():
    # No SL -> not computable -> 0.0 (worker treats this as a BLOCK separately).
    assert _loss(1.0, 3977.0, 0.0) == 0.0


def test_projected_loss_zero_on_bad_tick_data():
    assert projected_sl_loss(1.0, 3977.0, 3990.0, 0.0, TICK_SIZE) == 0.0
    assert projected_sl_loss(1.0, 3977.0, 3990.0, TICK_VALUE, 0.0) == 0.0
    assert projected_sl_loss(0.0, 3977.0, 3990.0, TICK_VALUE, TICK_SIZE) == 0.0


def _guard_blocks(volume, entry, sl, balance, max_pct):
    """Reproduce the worker's guard decision for a trade WITH an SL."""
    max_risk_usd = balance * max_pct / 100.0
    loss = _loss(volume, entry, sl)
    return loss > max_risk_usd


def test_guard_blocks_oversized_trade():
    # The bug: 22.58 lots gold on a $100k account => $29k loss vs $1k cap.
    assert _guard_blocks(22.58, 3977.0, 3990.0, balance=100_000, max_pct=1.0)


def test_guard_allows_within_cap():
    # 0.5 lots => $650 loss, under 1% of $100k ($1000).
    assert not _guard_blocks(0.5, 3977.0, 3990.0, balance=100_000, max_pct=1.0)


def test_guard_cap_scales_with_balance():
    # Same trade: blocked on small account, allowed on large one.
    # 0.7 lots => $910 loss. 1% of 100k = $1000 (allow); 1% of 50k = $500 (block).
    assert not _guard_blocks(0.7, 3977.0, 3990.0, balance=100_000, max_pct=1.0)
    assert _guard_blocks(0.7, 3977.0, 3990.0, balance=50_000, max_pct=1.0)


def test_guard_boundary_equal_is_allowed():
    # loss exactly == cap is allowed (strict >). 1 lot => $1300; cap 1.3% of 100k.
    assert not _guard_blocks(1.0, 3977.0, 3990.0, balance=100_000, max_pct=1.3)


# --- Safety-switch protection: a config/dict reload must never silently turn
#     debug (simulate) mode OFF, exactly like `paused`. Regression test for the
#     bug where debug_skip_execution got reset to False by a reload and live
#     orders went through. ---

def test_debug_flag_survives_bulk_dict_reload():
    from src.live_settings import LiveSettings, get_live_settings

    LiveSettings.reset()
    ls = get_live_settings()
    ls.debug_skip_execution = True  # user enabled simulate mode

    # A config reload arrives with debug OFF (stale/absent in file).
    ls.update_from_dict({"settings": {"debug_skip_execution": False,
                                      "max_slippage_points": 5}})

    # Slippage updated, but debug must NOT have been clobbered.
    assert ls.max_slippage == 5
    assert ls.debug_skip_execution is True, "reload silently disabled debug mode"
    LiveSettings.reset()


def test_debug_flag_toggle_direct():
    from src.live_settings import LiveSettings, get_live_settings

    LiveSettings.reset()
    ls = get_live_settings()
    assert ls.debug_skip_execution is False  # safe default
    ls.debug_skip_execution = True
    assert ls.debug_skip_execution is True
    LiveSettings.reset()


# --- Gold oversizing fix: risk sizing must use a broker-accurate loss_per_lot,
#     not the bogus trade_tick_value some brokers report for XAUUSD. ---

def test_risk_volume_uses_loss_per_lot_override():
    # risk $100, loss_per_lot $4224 (gold, 42.24-pt SL, 100oz contract) -> ~0.02 lots.
    vol = calculate_risk_based_volume(
        risk_usd=100.0, entry_price=4026.04, sl_price=3983.80,
        trade_tick_value=0.01, trade_tick_size=0.01,  # bogus gold tick value
        min_volume=0.01, max_volume=100.0, volume_step=0.01,
        loss_per_lot=4224.0,  # broker-accurate — must win over tick math
    )
    assert vol == 0.02


def test_bogus_tick_value_would_oversize_without_override():
    # Same gold trade WITHOUT the override (legacy tick_value=0.01) -> ~100x too big.
    # This is the bug the override fixes; assert the legacy path is indeed wrong.
    bad = calculate_risk_based_volume(
        risk_usd=100.0, entry_price=4026.04, sl_price=3983.80,
        trade_tick_value=0.01, trade_tick_size=0.01,
        min_volume=0.01, max_volume=100.0, volume_step=0.01,
    )
    assert bad > 2.0  # ~2.37 lots — the oversized value we saw live


def test_override_ignored_when_zero_falls_back_to_tick_math():
    # loss_per_lot<=0 -> legacy tick path. NAS100-like: correct there.
    vol = calculate_risk_based_volume(
        risk_usd=100.0, entry_price=29403.0, sl_price=29179.0,
        trade_tick_value=0.01, trade_tick_size=0.01,  # correct for an index
        min_volume=0.01, max_volume=100.0, volume_step=0.01,
        loss_per_lot=0.0,
    )
    # loss_per_lot = (224/0.01)*0.01 = $224 -> 100/224 = 0.446 -> 0.45
    assert vol == 0.45