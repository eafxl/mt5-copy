"""Pip and price calculation utilities."""
import logging
from dataclasses import dataclass
from typing import NamedTuple

_calc_logger = logging.getLogger("metact.calc")


class SymbolInfo(NamedTuple):
    """Symbol information for price calculations."""
    point: float
    digits: int


def calculate_price_distance(price1: float, price2: float) -> float:
    """Calculate absolute price distance."""
    return abs(price1 - price2)


def check_entry_deviation(
    master_entry: float,
    master_sl: float,
    master_current_price: float,
    max_entry_deviation_ratio: float,
    is_buy: bool,
) -> tuple[bool, float, float]:
    """
    Check if the master price has moved too far from entry to copy.

    Deviation is measured as a fraction of the SL distance on the MASTER:
        deviation = (favorable_move) / sl_distance

    Direction-aware blocking (anti-chase):
    - For BUY: block if price has RISEN (current > entry) - don't chase up
    - For SELL: block if price has FALLEN (current < entry) - don't chase down

    Only favorable moves count as deviation. Adverse moves (price going
    against the trade) are allowed - the trade is getting a better entry.

    Returns (should_skip, price_deviation, max_allowed_deviation).
    should_skip=True means the trade should NOT be opened.
    """
    if master_sl == 0 or max_entry_deviation_ratio <= 0:
        _calc_logger.debug(
            "ENTRY_DEVIATION: skipped check (master_sl=%.5f ratio=%.4f)",
            master_sl, max_entry_deviation_ratio,
        )
        return False, 0.0, 0.0

    sl_distance = abs(master_entry - master_sl)
    max_deviation = sl_distance * max_entry_deviation_ratio

    # Only count favorable moves (price chasing the trade direction)
    if is_buy:
        # BUY: only count if price rose (current > entry)
        price_deviation = max(0.0, master_current_price - master_entry)
    else:
        # SELL: only count if price fell (current < entry)
        price_deviation = max(0.0, master_entry - master_current_price)

    should_skip = price_deviation > max_deviation

    _calc_logger.debug(
        "ENTRY_DEVIATION: side=%s master_entry=%.5f master_sl=%.5f "
        "master_current=%.5f sl_distance=%.5f max_deviation=%.5f "
        "price_deviation=%.5f ratio=%.4f result=%s",
        "BUY" if is_buy else "SELL",
        master_entry, master_sl, master_current_price,
        sl_distance, max_deviation, price_deviation, max_entry_deviation_ratio,
        "SKIP" if should_skip else "PASS",
    )

    return should_skip, price_deviation, max_deviation


def calculate_slave_sl_tp(
    master_entry: float,
    master_sl: float,
    master_tp: float,
    slave_entry: float,
    is_buy: bool,
    symbol_digits: int,
) -> tuple[float, float]:
    """
    Calculate slave SL/TP based on pip distance from master.

    For a BUY:
    - SL is below entry, TP is above entry
    - SL distance = entry - sl (positive)
    - TP distance = tp - entry (positive)

    For a SELL:
    - SL is above entry, TP is below entry
    - SL distance = sl - entry (positive)
    - TP distance = entry - tp (positive)

    Apply these distances to slave entry price.
    """
    slave_sl = 0.0
    slave_tp = 0.0
    sl_distance = 0.0
    tp_distance = 0.0

    if is_buy:
        # BUY position
        if master_sl > 0:
            sl_distance = master_entry - master_sl
            slave_sl = slave_entry - sl_distance
        if master_tp > 0:
            tp_distance = master_tp - master_entry
            slave_tp = slave_entry + tp_distance
    else:
        # SELL position
        if master_sl > 0:
            sl_distance = master_sl - master_entry
            slave_sl = slave_entry + sl_distance
        if master_tp > 0:
            tp_distance = master_entry - master_tp
            slave_tp = slave_entry - tp_distance

    raw_sl = slave_sl
    raw_tp = slave_tp

    # Round to symbol digits
    if slave_sl > 0:
        slave_sl = round(slave_sl, symbol_digits)
    if slave_tp > 0:
        slave_tp = round(slave_tp, symbol_digits)

    _calc_logger.debug(
        "CALC_SLTP: is_buy=%s master_entry=%.5f master_sl=%.5f master_tp=%.5f "
        "slave_entry=%.5f sl_dist=%.5f tp_dist=%.5f raw_sl=%.5f raw_tp=%.5f "
        "final_sl=%.5f final_tp=%.5f digits=%d",
        is_buy, master_entry, master_sl, master_tp,
        slave_entry, sl_distance, tp_distance, raw_sl, raw_tp,
        slave_sl, slave_tp, symbol_digits,
    )

    return slave_sl, slave_tp


def calculate_slave_volume(
    master_volume: float,
    master_leverage: int,
    slave_leverage: int,
    multiplier: float,
    adjust_by_leverage: bool,
    min_volume: float = 0.01,
    max_volume: float = 100.0,
    volume_step: float = 0.01,
) -> float:
    """
    Calculate slave volume based on leverage and multiplier.

    If adjust_by_leverage=True:
        volume = master_volume * (master_leverage / slave_leverage) * multiplier

    If adjust_by_leverage=False:
        volume = master_volume * multiplier

    Result is clamped to min/max and rounded to volume step.
    """
    if adjust_by_leverage and slave_leverage > 0:
        leverage_factor = master_leverage / slave_leverage
        raw_volume = master_volume * leverage_factor * multiplier
    else:
        leverage_factor = 1.0
        raw_volume = master_volume * multiplier

    # Round to volume step
    after_step = round(raw_volume / volume_step) * volume_step

    # Clamp to min/max
    after_clamp = max(min_volume, min(max_volume, after_step))

    final = round(after_clamp, 2)

    _calc_logger.debug(
        "CALC_VOLUME: master_vol=%.4f master_lev=%d slave_lev=%d mult=%.4f "
        "adjust=%s leverage_factor=%.4f raw_vol=%.6f after_step=%.4f "
        "after_clamp=%.4f final=%.4f min=%.4f max=%.4f step=%.4f",
        master_volume, master_leverage, slave_leverage, multiplier,
        adjust_by_leverage, leverage_factor, raw_volume, after_step,
        after_clamp, final, min_volume, max_volume, volume_step,
    )

    return final


def calculate_risk_based_volume(
    risk_usd: float,
    entry_price: float,
    sl_price: float,
    trade_tick_value: float,
    trade_tick_size: float,
    min_volume: float = 0.01,
    max_volume: float = 100.0,
    volume_step: float = 0.01,
    loss_per_lot: float = 0.0,
) -> float:
    """Calculate volume so that if SL is hit, loss equals risk_usd.

    Formula:
        loss_per_lot = money lost on 1.0 lot when price moves entry -> sl
        volume       = risk_usd / loss_per_lot

    ``loss_per_lot`` should be passed in by the caller, computed the
    broker-accurate way (``mt5.order_calc_profit`` for 1 lot). This avoids
    trusting ``trade_tick_value``, which some brokers report wrongly for
    metals/CFDs (e.g. XAUUSD coming back like an index at 0.01 instead of ~$1,
    producing ~100x oversized lots).

    If ``loss_per_lot`` is not supplied (<= 0), it falls back to the legacy
    ``sl_ticks * trade_tick_value`` estimate.

    Args:
        risk_usd: Target loss amount in account deposit currency.
        entry_price: Expected entry price (slave's current ask/bid).
        sl_price: Stop-loss price level on the slave.
        trade_tick_value: Monetary value of 1 tick for 1 lot (legacy fallback).
        trade_tick_size: Price change per tick (legacy fallback).
        min_volume: Broker minimum lot size.
        max_volume: Broker maximum lot size.
        volume_step: Broker lot step size.
        loss_per_lot: Broker-accurate loss on 1.0 lot if SL is hit. Preferred.

    Returns:
        Calculated volume, rounded to volume_step and clamped to [min, max].
        Returns 0.0 if inputs are invalid (no computable loss).
    """
    sl_distance = abs(entry_price - sl_price)

    # Prefer the broker-accurate loss-per-lot supplied by the caller.
    if loss_per_lot <= 0:
        # Legacy fallback: derive from tick value/size.
        if sl_distance <= 0 or trade_tick_size <= 0 or trade_tick_value <= 0:
            _calc_logger.warning(
                "RISK_VOLUME: invalid inputs sl_dist=%.6f tick_size=%.6f tick_val=%.6f",
                sl_distance, trade_tick_size, trade_tick_value,
            )
            return 0.0
        loss_per_lot = (sl_distance / trade_tick_size) * trade_tick_value

    if loss_per_lot <= 0:
        _calc_logger.warning("RISK_VOLUME: non-positive loss_per_lot; returning 0")
        return 0.0

    raw_volume = risk_usd / loss_per_lot

    # Round to volume step
    after_step = round(raw_volume / volume_step) * volume_step

    # Clamp to min/max
    after_clamp = max(min_volume, min(max_volume, after_step))

    final = round(after_clamp, 2)

    _calc_logger.debug(
        "RISK_VOLUME: risk=%.2f entry=%.5f sl=%.5f sl_dist=%.5f "
        "tick_val=%.6f tick_size=%.6f loss_per_lot=%.4f "
        "raw_vol=%.6f after_step=%.4f final=%.4f min=%.4f max=%.4f step=%.4f",
        risk_usd, entry_price, sl_price, sl_distance,
        trade_tick_value, trade_tick_size, loss_per_lot,
        raw_volume, after_step, final,
        min_volume, max_volume, volume_step,
    )

    return final


def projected_sl_loss(
    volume: float,
    entry_price: float,
    sl_price: float,
    trade_tick_value: float,
    trade_tick_size: float,
) -> float:
    """Money lost if the stop-loss is hit, for a given volume.

    loss = (abs(entry - sl) / tick_size) * tick_value * volume

    Returns 0.0 if inputs are invalid (no SL, zero tick data, zero volume).
    Callers that must BLOCK on a missing SL should test ``sl_price <= 0``
    themselves — this helper only reports the computable loss.
    """
    if (
        sl_price <= 0
        or trade_tick_size <= 0
        or trade_tick_value <= 0
        or volume <= 0
    ):
        return 0.0
    sl_ticks = abs(entry_price - sl_price) / trade_tick_size
    return sl_ticks * trade_tick_value * volume


def normalize_price(price: float, digits: int) -> float:
    """Normalize price to symbol digits."""
    return round(price, digits)
