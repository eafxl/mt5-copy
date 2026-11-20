"""Comprehensive tests for all calculation logic in pip_calculator."""
import pytest

from src.utils.pip_calculator import (
    calculate_price_distance,
    calculate_slave_volume,
    calculate_slave_sl_tp,
    check_entry_deviation,
    calculate_risk_based_volume,
    normalize_price,
)


# ---------------------------------------------------------------------------
# calculate_slave_volume
# ---------------------------------------------------------------------------
class TestCalculateSlaveVolume:
    """Tests for volume calculation with leverage, multiplier, clamping."""

    def test_same_leverage(self):
        """Same leverage, multiplier=1 → same volume."""
        result = calculate_slave_volume(
            master_volume=0.05,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
        )
        assert result == 0.05

    def test_higher_slave_leverage(self):
        """Slave leverage 5x higher → volume / 5."""
        result = calculate_slave_volume(
            master_volume=0.50,
            master_leverage=100,
            slave_leverage=500,
            multiplier=1.0,
            adjust_by_leverage=True,
        )
        assert result == 0.10

    def test_lower_slave_leverage(self):
        """Slave leverage 5x lower → volume * 5."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=20,
            multiplier=1.0,
            adjust_by_leverage=True,
        )
        assert result == 0.50

    def test_multiplier_doubles(self):
        """Multiplier=2.0 doubles the volume."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=100,
            multiplier=2.0,
            adjust_by_leverage=True,
        )
        assert result == 0.20

    def test_multiplier_half(self):
        """Multiplier=0.5 halves the volume."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=100,
            multiplier=0.5,
            adjust_by_leverage=True,
        )
        assert result == 0.05

    def test_no_leverage_adjust(self):
        """adjust_by_leverage=False ignores leverage ratio."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=500,
            multiplier=1.0,
            adjust_by_leverage=False,
        )
        # Without adjustment: 0.10 * 1.0 = 0.10
        assert result == 0.10

    def test_no_leverage_adjust_with_multiplier(self):
        """adjust_by_leverage=False with multiplier."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=500,
            multiplier=3.0,
            adjust_by_leverage=False,
        )
        assert result == 0.30

    def test_clamp_to_min(self):
        """Tiny calculated volume clamps to min_volume."""
        result = calculate_slave_volume(
            master_volume=0.01,
            master_leverage=100,
            slave_leverage=500,
            multiplier=1.0,
            adjust_by_leverage=True,
            min_volume=0.01,
        )
        # 0.01 * (100/500) = 0.002 → step rounds to 0.0 → clamp to 0.01
        assert result == 0.01

    def test_clamp_to_max(self):
        """Huge calculated volume clamps to max_volume."""
        result = calculate_slave_volume(
            master_volume=50.0,
            master_leverage=500,
            slave_leverage=10,
            multiplier=2.0,
            adjust_by_leverage=True,
            max_volume=100.0,
        )
        # 50 * (500/10) * 2 = 5000 → clamp to 100
        assert result == 100.0

    def test_volume_step_rounding_001(self):
        """Volume rounds to step=0.01."""
        result = calculate_slave_volume(
            master_volume=0.15,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
            volume_step=0.01,
        )
        assert result == 0.15

    def test_volume_step_rounding_010(self):
        """Volume rounds to step=0.1.

        Note: 0.15 / 0.1 = 1.4999... in IEEE 754, rounds to 1.
        """
        result = calculate_slave_volume(
            master_volume=0.15,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
            volume_step=0.1,
        )
        # 0.15 / 0.1 = 1.4999... → round to 1 → 1 * 0.1 = 0.1
        assert result == 0.10

    def test_volume_step_rounding_010_down(self):
        """Volume rounds down with step=0.1."""
        result = calculate_slave_volume(
            master_volume=0.14,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
            volume_step=0.1,
        )
        # 0.14 / 0.1 = 1.4 → round to 1 → 1 * 0.1 = 0.1
        assert result == 0.10

    def test_zero_slave_leverage_fallback(self):
        """slave_leverage=0 falls back to no adjustment."""
        result = calculate_slave_volume(
            master_volume=0.10,
            master_leverage=100,
            slave_leverage=0,
            multiplier=1.0,
            adjust_by_leverage=True,
        )
        # Falls back to master_volume * multiplier = 0.10
        assert result == 0.10

    def test_real_scenario_bybit_nas100(self):
        """Real scenario: master=0.02, lev=100, slave_lev=500, min_vol=0.1.

        This is the actual issue observed in production:
        0.02 * (100/500) * 1.0 = 0.004 → step(0.01)=0.0 → clamp to 0.1
        """
        result = calculate_slave_volume(
            master_volume=0.02,
            master_leverage=100,
            slave_leverage=500,
            multiplier=1.0,
            adjust_by_leverage=True,
            min_volume=0.1,
            volume_step=0.01,
        )
        assert result == 0.10

    def test_real_scenario_bybit_larger_volume(self):
        """Real scenario with larger master volume on Bybit.

        0.09 * (100/500) * 1.0 = 0.018 → step(0.01)=0.02 → clamp to 0.1
        """
        result = calculate_slave_volume(
            master_volume=0.09,
            master_leverage=100,
            slave_leverage=500,
            multiplier=1.0,
            adjust_by_leverage=True,
            min_volume=0.1,
            volume_step=0.01,
        )
        assert result == 0.10

    def test_rounding_precision(self):
        """Verify no floating-point drift in result."""
        result = calculate_slave_volume(
            master_volume=0.33,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
            volume_step=0.01,
        )
        assert result == 0.33
        assert isinstance(result, float)

    def test_combined_leverage_and_multiplier(self):
        """Leverage and multiplier applied together."""
        result = calculate_slave_volume(
            master_volume=1.0,
            master_leverage=200,
            slave_leverage=100,
            multiplier=0.5,
            adjust_by_leverage=True,
        )
        # 1.0 * (200/100) * 0.5 = 1.0
        assert result == 1.0

    def test_min_equals_max(self):
        """When min_volume equals max_volume, result is always that value."""
        result = calculate_slave_volume(
            master_volume=5.0,
            master_leverage=100,
            slave_leverage=100,
            multiplier=1.0,
            adjust_by_leverage=True,
            min_volume=0.5,
            max_volume=0.5,
        )
        assert result == 0.5


# ---------------------------------------------------------------------------
# calculate_risk_based_volume
# ---------------------------------------------------------------------------
class TestCalculateRiskBasedVolume:
    """Tests for risk-based volume calculation."""

    def test_basic_forex(self):
        """EURUSD: $100 risk, 50-pip SL.

        sl_distance = 0.00500
        sl_ticks = 0.00500 / 0.00001 = 500
        loss_per_lot = 500 * 10 = 5000
        volume = 100 / 5000 = 0.02
        """
        result = calculate_risk_based_volume(
            risk_usd=100.0,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
        )
        assert result == 0.02

    def test_index_nas100(self):
        """NAS100: $200 risk, SL 50 points.

        sl_distance = 50.0
        sl_ticks = 50.0 / 0.01 = 5000
        loss_per_lot = 5000 * 0.01 = 50
        volume = 200 / 50 = 4.0
        """
        result = calculate_risk_based_volume(
            risk_usd=200.0,
            entry_price=25400.00,
            sl_price=25350.00,
            trade_tick_value=0.01,
            trade_tick_size=0.01,
        )
        assert result == 4.0

    def test_gold(self):
        """XAUUSD: $50 risk, SL 5.0 away.

        sl_distance = 5.0
        sl_ticks = 5.0 / 0.01 = 500
        loss_per_lot = 500 * 1.0 = 500
        volume = 50 / 500 = 0.10
        """
        result = calculate_risk_based_volume(
            risk_usd=50.0,
            entry_price=2050.00,
            sl_price=2045.00,
            trade_tick_value=1.0,
            trade_tick_size=0.01,
        )
        assert result == 0.10

    def test_clamp_to_min(self):
        """Very small risk → clamp to min_volume."""
        result = calculate_risk_based_volume(
            risk_usd=0.50,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
            min_volume=0.01,
        )
        # 0.50 / 5000 = 0.0001 → clamp to 0.01
        assert result == 0.01

    def test_clamp_to_max(self):
        """Huge risk → clamp to max_volume."""
        result = calculate_risk_based_volume(
            risk_usd=1000000.0,
            entry_price=1.10000,
            sl_price=1.09999,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
            max_volume=100.0,
        )
        assert result == 100.0

    def test_volume_step_rounding(self):
        """Volume rounds to step=0.10."""
        # risk=75, sl_dist=0.005, ticks=500, loss_per_lot=5000, raw=0.015
        # round(0.015/0.10)*0.10 = round(0.15)*0.10 = 0.0 → clamp to 0.01
        result = calculate_risk_based_volume(
            risk_usd=75.0,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
            volume_step=0.10,
        )
        assert result == 0.01  # clamped to min

    def test_volume_step_rounding_up(self):
        """Volume rounds up to nearest step."""
        # risk=300, sl_dist=0.005, ticks=500, loss_per_lot=5000, raw=0.06
        # round(0.06/0.10)*0.10 = round(0.6)*0.10 = 1*0.10 = 0.10
        result = calculate_risk_based_volume(
            risk_usd=300.0,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
            volume_step=0.10,
        )
        assert result == 0.10

    def test_zero_sl_distance(self):
        """Entry == SL → returns 0.0."""
        result = calculate_risk_based_volume(
            risk_usd=100.0,
            entry_price=1.10000,
            sl_price=1.10000,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
        )
        assert result == 0.0

    def test_zero_tick_value(self):
        """Zero tick value → returns 0.0."""
        result = calculate_risk_based_volume(
            risk_usd=100.0,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=0.0,
            trade_tick_size=0.00001,
        )
        assert result == 0.0

    def test_zero_tick_size(self):
        """Zero tick size → returns 0.0."""
        result = calculate_risk_based_volume(
            risk_usd=100.0,
            entry_price=1.10000,
            sl_price=1.09500,
            trade_tick_value=10.0,
            trade_tick_size=0.0,
        )
        assert result == 0.0

    def test_sell_position(self):
        """SELL: SL above entry, same abs formula, same result."""
        result = calculate_risk_based_volume(
            risk_usd=100.0,
            entry_price=1.10000,
            sl_price=1.10500,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
        )
        assert result == 0.02

    def test_rounding_precision(self):
        """No floating-point drift."""
        # sl_dist=0.00330, ticks=330, loss_per_lot=3300, vol=33/3300=0.01
        result = calculate_risk_based_volume(
            risk_usd=33.0,
            entry_price=1.10000,
            sl_price=1.09670,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
        )
        assert result == 0.01
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# calculate_slave_sl_tp
# ---------------------------------------------------------------------------
class TestCalculateSlaveSlTp:
    """Tests for SL/TP distance-based calculation."""

    def test_buy_with_sl_tp(self):
        """BUY: preserve SL/TP distances from entry."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.09500,
            master_tp=1.11000,
            slave_entry=1.10100,
            is_buy=True,
            symbol_digits=5,
        )
        # SL distance = 1.10000 - 1.09500 = 0.00500
        # TP distance = 1.11000 - 1.10000 = 0.01000
        assert sl == pytest.approx(1.09600, abs=1e-5)  # 1.10100 - 0.00500
        assert tp == pytest.approx(1.11100, abs=1e-5)  # 1.10100 + 0.01000

    def test_sell_with_sl_tp(self):
        """SELL: preserve SL/TP distances from entry."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.10500,
            master_tp=1.09000,
            slave_entry=1.09900,
            is_buy=False,
            symbol_digits=5,
        )
        # SL distance = 1.10500 - 1.10000 = 0.00500
        # TP distance = 1.10000 - 1.09000 = 0.01000
        assert sl == pytest.approx(1.10400, abs=1e-5)  # 1.09900 + 0.00500
        assert tp == pytest.approx(1.08900, abs=1e-5)  # 1.09900 - 0.01000

    def test_buy_no_sl(self):
        """BUY with no SL → slave_sl = 0."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=0.0,
            master_tp=1.11000,
            slave_entry=1.10100,
            is_buy=True,
            symbol_digits=5,
        )
        assert sl == 0.0
        assert tp == pytest.approx(1.11100, abs=1e-5)

    def test_buy_no_tp(self):
        """BUY with no TP → slave_tp = 0."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.09500,
            master_tp=0.0,
            slave_entry=1.10100,
            is_buy=True,
            symbol_digits=5,
        )
        assert sl == pytest.approx(1.09600, abs=1e-5)
        assert tp == 0.0

    def test_no_sl_no_tp(self):
        """No SL and no TP → both 0."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=0.0,
            master_tp=0.0,
            slave_entry=1.10100,
            is_buy=True,
            symbol_digits=5,
        )
        assert sl == 0.0
        assert tp == 0.0

    def test_buy_different_entry_price(self):
        """BUY: slave entry far from master, distances still preserved."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=25400.00,
            master_sl=25350.00,
            master_tp=25500.00,
            slave_entry=25410.00,
            is_buy=True,
            symbol_digits=2,
        )
        # SL distance = 25400 - 25350 = 50
        # TP distance = 25500 - 25400 = 100
        assert sl == pytest.approx(25360.00, abs=0.01)  # 25410 - 50
        assert tp == pytest.approx(25510.00, abs=0.01)  # 25410 + 100

    def test_sell_different_entry_price(self):
        """SELL: slave entry far from master, distances still preserved."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=25400.00,
            master_sl=25450.00,
            master_tp=25300.00,
            slave_entry=25390.00,
            is_buy=False,
            symbol_digits=2,
        )
        # SL distance = 25450 - 25400 = 50
        # TP distance = 25400 - 25300 = 100
        assert sl == pytest.approx(25440.00, abs=0.01)  # 25390 + 50
        assert tp == pytest.approx(25290.00, abs=0.01)  # 25390 - 100

    def test_rounding_5_digits(self):
        """EURUSD-like: 5 decimal digits."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.123456,
            master_sl=1.120000,
            master_tp=1.130000,
            slave_entry=1.123500,
            is_buy=True,
            symbol_digits=5,
        )
        # SL distance = 1.123456 - 1.120000 = 0.003456
        # slave_sl = 1.123500 - 0.003456 = 1.120044
        assert sl == round(1.120044, 5)
        # TP distance = 1.130000 - 1.123456 = 0.006544
        # slave_tp = 1.123500 + 0.006544 = 1.130044
        assert tp == round(1.130044, 5)

    def test_rounding_2_digits(self):
        """XAUUSD-like: 2 decimal digits."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=2050.50,
            master_sl=2045.00,
            master_tp=2060.00,
            slave_entry=2051.00,
            is_buy=True,
            symbol_digits=2,
        )
        # SL distance = 2050.50 - 2045.00 = 5.50
        assert sl == pytest.approx(2045.50, abs=0.01)
        # TP distance = 2060.00 - 2050.50 = 9.50
        assert tp == pytest.approx(2060.50, abs=0.01)

    def test_rounding_0_digits(self):
        """Index-like: 0 decimal digits (integers)."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=25400.0,
            master_sl=25350.0,
            master_tp=25500.0,
            slave_entry=25405.0,
            is_buy=True,
            symbol_digits=0,
        )
        # SL distance = 50, TP distance = 100
        assert sl == 25355.0
        assert tp == 25505.0

    def test_large_gap_buy(self):
        """Slave entry very far from master - distances still correct."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.09000,
            master_tp=1.12000,
            slave_entry=1.15000,
            is_buy=True,
            symbol_digits=5,
        )
        # SL distance = 0.01000, TP distance = 0.02000
        assert sl == pytest.approx(1.14000, abs=1e-5)
        assert tp == pytest.approx(1.17000, abs=1e-5)

    def test_large_gap_sell(self):
        """SELL with large entry gap."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.11000,
            master_tp=1.08000,
            slave_entry=1.05000,
            is_buy=False,
            symbol_digits=5,
        )
        # SL distance = 0.01000, TP distance = 0.02000
        assert sl == pytest.approx(1.06000, abs=1e-5)
        assert tp == pytest.approx(1.03000, abs=1e-5)

    def test_real_trace_nas100_sell(self):
        """Real scenario from trace: NAS100 SELL ticket 499294011.

        Master: entry≈25876, sl=25914.78, tp=25787.01
        Slave gets different entry due to spread/timing.
        """
        sl, tp = calculate_slave_sl_tp(
            master_entry=25876.24,
            master_sl=25914.78,
            master_tp=25787.01,
            slave_entry=25877.76,  # ask price
            is_buy=False,
            symbol_digits=2,
        )
        # SL distance = 25914.78 - 25876.24 = 38.54
        # TP distance = 25876.24 - 25787.01 = 89.23
        expected_sl = round(25877.76 + 38.54, 2)
        expected_tp = round(25877.76 - 89.23, 2)
        assert sl == pytest.approx(expected_sl, abs=0.01)
        assert tp == pytest.approx(expected_tp, abs=0.01)

    def test_same_entry_prices(self):
        """Master and slave enter at same price."""
        sl, tp = calculate_slave_sl_tp(
            master_entry=1.10000,
            master_sl=1.09500,
            master_tp=1.11000,
            slave_entry=1.10000,
            is_buy=True,
            symbol_digits=5,
        )
        assert sl == pytest.approx(1.09500, abs=1e-5)
        assert tp == pytest.approx(1.11000, abs=1e-5)


# ---------------------------------------------------------------------------
# check_entry_deviation
# ---------------------------------------------------------------------------
class TestCheckEntryDeviation:
    """Tests for entry deviation check (anti-chase logic).

    Direction-aware (when is_buy provided):
    - BUY: only block if price ROSE (favorable move = chasing)
    - SELL: only block if price FELL (favorable move = chasing)

    Legacy mode (is_buy=None): any movement from entry counts.
    """

    # --- Legacy mode tests (is_buy=None) ---

    def test_legacy_within_range(self):
        """Legacy mode: Small movement from entry within range → should NOT skip."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=1.10000,
            master_sl=1.09500,
            master_current_price=1.10002,  # moved 0.00002 from entry
            max_entry_deviation_ratio=0.15,
            is_buy=None,
        )
        assert should_skip is False
        # sl_distance = 0.00500, max_deviation = 0.00500 * 0.15 = 0.00075
        # price_deviation = 0.00002
        assert deviation == pytest.approx(0.00002, abs=1e-6)
        assert max_allowed == pytest.approx(0.00075, abs=1e-6)

    def test_legacy_beyond_range(self):
        """Legacy mode: Price moved too far from entry → should skip."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25400.00,
            master_sl=25350.00,  # sl_distance = 50
            master_current_price=25420.00,  # 20 from entry
            max_entry_deviation_ratio=0.15,  # max = 50 * 0.15 = 7.5
            is_buy=None,
        )
        assert should_skip is True
        assert deviation == pytest.approx(20.0, abs=0.01)
        assert max_allowed == pytest.approx(7.5, abs=0.01)

    def test_no_sl(self):
        """master_sl=0 → always pass (can't calculate distance)."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25400.00,
            master_sl=0.0,
            master_current_price=26000.00,  # huge movement
            max_entry_deviation_ratio=0.15,
        )
        assert should_skip is False
        assert deviation == 0.0
        assert max_allowed == 0.0

    def test_disabled_ratio_zero(self):
        """ratio=0 → always pass (feature disabled)."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25400.00,
            master_sl=25350.00,
            master_current_price=26000.00,
            max_entry_deviation_ratio=0.0,
        )
        assert should_skip is False
        assert deviation == 0.0
        assert max_allowed == 0.0

    def test_disabled_ratio_negative(self):
        """Negative ratio → always pass (feature disabled)."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25400.00,
            master_sl=25350.00,
            master_current_price=26000.00,
            max_entry_deviation_ratio=-1.0,
        )
        assert should_skip is False

    def test_at_entry_exactly(self):
        """Price at entry exactly → deviation = 0 → pass."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25876.24,
            master_sl=25914.78,
            master_current_price=25876.24,  # exactly at entry
            max_entry_deviation_ratio=0.2,
        )
        assert should_skip is False
        assert deviation == pytest.approx(0.0, abs=0.01)

    # --- Direction-aware tests (is_buy provided) ---

    def test_buy_price_rose_skip(self):
        """BUY: Price rose (favorable) beyond limit → skip (anti-chase)."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25000.00,  # sl_distance = 100
            master_current_price=25125.00,  # rose 25 from entry
            max_entry_deviation_ratio=0.2,  # max = 100 * 0.2 = 20
            is_buy=True,
        )
        assert should_skip is True
        assert deviation == pytest.approx(25.0, abs=0.01)
        assert max_allowed == pytest.approx(20.0, abs=0.01)

    def test_buy_price_rose_within_limit(self):
        """BUY: Price rose (favorable) within limit → pass."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25000.00,  # sl_distance = 100
            master_current_price=25110.00,  # rose 10 from entry
            max_entry_deviation_ratio=0.2,  # max = 100 * 0.2 = 20
            is_buy=True,
        )
        assert should_skip is False
        assert deviation == pytest.approx(10.0, abs=0.01)

    def test_buy_price_fell_pass(self):
        """BUY: Price fell (adverse, better entry) → always pass regardless of distance."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25000.00,  # sl_distance = 100
            master_current_price=25000.00,  # fell 100 from entry (at SL!)
            max_entry_deviation_ratio=0.2,  # max = 20, but should pass anyway
            is_buy=True,
        )
        # Price fell = better entry for BUY, no deviation counted
        assert should_skip is False
        assert deviation == pytest.approx(0.0, abs=0.01)

    def test_sell_price_fell_skip(self):
        """SELL: Price fell (favorable) beyond limit → skip (anti-chase)."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25200.00,  # sl_distance = 100 (SL above for SELL)
            master_current_price=25075.00,  # fell 25 from entry
            max_entry_deviation_ratio=0.2,  # max = 100 * 0.2 = 20
            is_buy=False,
        )
        assert should_skip is True
        assert deviation == pytest.approx(25.0, abs=0.01)
        assert max_allowed == pytest.approx(20.0, abs=0.01)

    def test_sell_price_fell_within_limit(self):
        """SELL: Price fell (favorable) within limit → pass."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25200.00,  # sl_distance = 100
            master_current_price=25090.00,  # fell 10 from entry
            max_entry_deviation_ratio=0.2,  # max = 100 * 0.2 = 20
            is_buy=False,
        )
        assert should_skip is False
        assert deviation == pytest.approx(10.0, abs=0.01)

    def test_sell_price_rose_pass(self):
        """SELL: Price rose (adverse, better entry) → always pass regardless of distance."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25100.00,
            master_sl=25200.00,  # sl_distance = 100
            master_current_price=25200.00,  # rose 100 from entry (at SL!)
            max_entry_deviation_ratio=0.2,  # max = 20, but should pass anyway
            is_buy=False,
        )
        # Price rose = better entry for SELL, no deviation counted
        assert should_skip is False
        assert deviation == pytest.approx(0.0, abs=0.01)

    def test_buy_at_boundary(self):
        """BUY: Deviation exactly at max → should NOT skip."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25400.00,
            master_sl=25300.00,  # sl_distance = 100
            master_current_price=25410.00,  # rose exactly 10 (= max)
            max_entry_deviation_ratio=0.1,  # max = 100 * 0.1 = 10
            is_buy=True,
        )
        assert should_skip is False
        assert deviation == pytest.approx(10.0, abs=0.01)
        assert max_allowed == pytest.approx(10.0, abs=0.01)

    def test_real_scenario_sell_price_fell(self):
        """Real SELL scenario: price fell significantly → skip (chasing).

        Master entry: 25876.24, SL: 25914.78 (SELL, SL above entry)
        sl_distance = 38.54, ratio = 0.2, max_deviation = 7.708
        Master current 25775.48 is 100.76 below entry → favorable → skip.
        """
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25876.24,
            master_sl=25914.78,
            master_current_price=25775.48,  # 100.76 below entry
            max_entry_deviation_ratio=0.2,
            is_buy=False,
        )
        assert should_skip is True
        assert deviation == pytest.approx(100.76, abs=0.01)
        assert max_allowed == pytest.approx(7.708, abs=0.01)

    def test_real_scenario_sell_price_rose(self):
        """Real SELL scenario: price rose → better entry, always pass."""
        should_skip, deviation, max_allowed = check_entry_deviation(
            master_entry=25876.24,
            master_sl=25914.78,
            master_current_price=25900.00,  # rose 23.76 from entry
            max_entry_deviation_ratio=0.2,  # max = 7.708
            is_buy=False,
        )
        # Price rose = better entry for SELL, should pass
        assert should_skip is False
        assert deviation == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# calculate_price_distance
# ---------------------------------------------------------------------------
class TestCalculatePriceDistance:
    """Tests for absolute price distance."""

    def test_positive_distance(self):
        assert calculate_price_distance(1.10000, 1.09500) == pytest.approx(0.005, abs=1e-6)

    def test_negative_direction(self):
        """Order of arguments doesn't matter (absolute)."""
        assert calculate_price_distance(1.09500, 1.10000) == pytest.approx(0.005, abs=1e-6)

    def test_zero_distance(self):
        assert calculate_price_distance(1.10000, 1.10000) == 0.0

    def test_large_distance(self):
        assert calculate_price_distance(25400.0, 25300.0) == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# normalize_price
# ---------------------------------------------------------------------------
class TestNormalizePrice:
    """Tests for price normalization."""

    def test_5_digits(self):
        assert normalize_price(1.123456789, 5) == 1.12346

    def test_2_digits(self):
        # 2050.555 in IEEE 754 is slightly below .555, rounds down (banker's rounding)
        assert normalize_price(2050.555, 2) == 2050.55

    def test_0_digits(self):
        assert normalize_price(25400.7, 0) == 25401.0

    def test_already_normalized(self):
        assert normalize_price(1.12345, 5) == 1.12345

    def test_3_digits(self):
        assert normalize_price(1234.5678, 3) == 1234.568


# ---------------------------------------------------------------------------
# Trace context propagation
# ---------------------------------------------------------------------------
class TestTraceContext:
    """Tests for cross-process trace context propagation."""

    def test_get_trace_context_outside_trace(self):
        """No active trace → context has None values."""
        from src.utils.tracer import Tracer
        tracer = Tracer(enabled=False)
        ctx = tracer.get_trace_context()
        assert ctx["trace_id"] is None
        assert ctx["parent_span_id"] is None

    def test_get_trace_context_inside_trace(self):
        """Inside a trace → context has trace_id and span_id."""
        import tempfile
        from pathlib import Path
        from src.utils.tracer import Tracer

        with tempfile.TemporaryDirectory() as tmp:
            tracer = Tracer(trace_dir=Path(tmp), enabled=True)
            with tracer.trace("test_op", trade_id="TEST123") as span:
                ctx = tracer.get_trace_context()
                assert ctx["trace_id"] == "TEST123"
                assert ctx["parent_span_id"] == span.span_id
            tracer.close()

    def test_propagate_links_spans(self):
        """Propagated context links child spans to parent trace."""
        import tempfile
        from pathlib import Path
        from src.utils.tracer import Tracer

        with tempfile.TemporaryDirectory() as tmp:
            tracer = Tracer(trace_dir=Path(tmp), enabled=True)

            # Simulate main process creating a trace
            with tracer.trace("main_op", trade_id="TRACE_A") as main_span:
                ctx = tracer.get_trace_context()

            # Simulate worker process receiving context
            worker_tracer = Tracer(trace_dir=Path(tmp), enabled=True)
            with worker_tracer.propagate(ctx):
                with worker_tracer.span("worker_op") as worker_span:
                    assert worker_span.trace_id == "TRACE_A"
                    assert worker_span.parent_span_id == main_span.span_id

                    # Nested span chains correctly
                    with worker_tracer.span("worker_child") as child:
                        assert child.trace_id == "TRACE_A"
                        assert child.parent_span_id == worker_span.span_id

            tracer.close()
            worker_tracer.close()

    def test_propagate_restores_state(self):
        """After propagate exits, previous trace state is restored."""
        import tempfile
        from pathlib import Path
        from src.utils.tracer import Tracer

        with tempfile.TemporaryDirectory() as tmp:
            tracer = Tracer(trace_dir=Path(tmp), enabled=True)

            with tracer.trace("outer", trade_id="OUTER_ID") as outer_span:
                ctx_before = tracer.get_trace_context()

                # Propagate a different context temporarily
                with tracer.propagate({"trace_id": "OTHER", "parent_span_id": "xyz"}):
                    inner_ctx = tracer.get_trace_context()
                    assert inner_ctx["trace_id"] == "OTHER"

                # After propagate exits, original context is restored
                ctx_after = tracer.get_trace_context()
                assert ctx_after["trace_id"] == "OUTER_ID"
                assert ctx_after["parent_span_id"] == outer_span.span_id

            tracer.close()

    def test_propagate_empty_context(self):
        """Empty trace_context dict doesn't crash."""
        from src.utils.tracer import Tracer

        tracer = Tracer(enabled=False)
        with tracer.propagate({}):
            ctx = tracer.get_trace_context()
            assert ctx["trace_id"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
