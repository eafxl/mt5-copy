"""Tests for the balance-tiered risk ladder (dynamic_pct risk mode)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.risk_ladder import (  # noqa: E402
    DEFAULT_TIERS,
    default_tiers_as_dicts,
    normalize_tiers,
    resolve_risk_pct,
)


INITIAL = 100_000.0


def _risk(balance: float, tiers=None, fallback: float = 1.0) -> float:
    pct, _ = resolve_risk_pct(INITIAL, balance, tiers or DEFAULT_TIERS, fallback)
    return pct


# ---------------------------------------------------------------- the ladder

@pytest.mark.parametrize("balance,expected", [
    (90_000.0, 0.20),    # deep drawdown
    (97_400.0, 0.20),    # just past -2.6%
    (97_500.0, 0.20),    # exactly -2.5% -> still the lower tier
    (97_600.0, 0.25),    # inside the band
    (100_000.0, 0.25),   # flat
    (102_500.0, 0.25),   # exactly +2.5% -> still the middle tier
    (102_600.0, 0.30),   # in profit
    (150_000.0, 0.30),   # far in profit
])
def test_default_ladder_bands(balance, expected):
    assert _risk(balance) == expected


def test_scaled_up_initial_balance_moves_the_whole_ladder():
    """After a prop scale-up the operator raises initial_balance; a balance
    that used to sit in the top tier now sits in the middle one."""
    assert resolve_risk_pct(100_000.0, 110_000.0, DEFAULT_TIERS, 1.0)[0] == 0.30
    assert resolve_risk_pct(110_000.0, 110_000.0, DEFAULT_TIERS, 1.0)[0] == 0.25


def test_equity_base_is_just_the_value_passed_in():
    # The caller picks balance vs equity; the ladder only sees a number.
    assert _risk(96_000.0) == 0.20


def test_custom_ladder():
    tiers = [{"up_to_pct": -10, "risk_pct": 0.1}, {"up_to_pct": None, "risk_pct": 2.0}]
    assert _risk(85_000.0, tiers) == 0.1
    assert _risk(99_000.0, tiers) == 2.0


# ------------------------------------------------------------- unusable ladder

def test_no_initial_balance_falls_back():
    pct, reason = resolve_risk_pct(0.0, 100_000.0, DEFAULT_TIERS, 0.77)
    assert pct == 0.77
    assert "initial balance" in reason


def test_no_account_value_falls_back():
    pct, reason = resolve_risk_pct(INITIAL, 0.0, DEFAULT_TIERS, 0.77)
    assert pct == 0.77
    assert "account value" in reason


def test_no_tiers_falls_back():
    pct, reason = resolve_risk_pct(INITIAL, INITIAL, [], 0.77)
    assert pct == 0.77
    assert "tiers" in reason


def test_zero_fallback_signals_block():
    """The copy path passes fallback=0 so an unusable ladder blocks the trade
    instead of quietly sizing at some other risk."""
    assert resolve_risk_pct(0.0, 100_000.0, DEFAULT_TIERS, 0.0)[0] == 0.0


def test_reason_is_reported_for_a_normal_hit():
    _, reason = resolve_risk_pct(INITIAL, 95_000.0, DEFAULT_TIERS, 1.0)
    assert "-5.00%" in reason and "0.20%" in reason


# --------------------------------------------------------------- normalization

def test_tiers_sort_low_to_high_with_open_tier_last():
    tiers = normalize_tiers([
        {"up_to_pct": None, "risk_pct": 0.3},
        {"up_to_pct": 2.5, "risk_pct": 0.25},
        {"up_to_pct": -2.5, "risk_pct": 0.2},
    ])
    assert tiers == [(-2.5, 0.2), (2.5, 0.25), (None, 0.3)]


def test_missing_open_tier_is_appended():
    """Without a top bucket a balance above every threshold would size nothing."""
    tiers = normalize_tiers([{"up_to_pct": 2.5, "risk_pct": 0.25}])
    assert tiers == [(2.5, 0.25), (None, 0.25)]
    assert _risk(200_000.0, tiers) == 0.25


def test_half_filled_and_zero_rows_are_dropped():
    assert normalize_tiers([{"up_to_pct": 1.0, "risk_pct": None}]) == []
    assert normalize_tiers([{"up_to_pct": 1.0, "risk_pct": 0}]) == []
    assert normalize_tiers([{"up_to_pct": "abc", "risk_pct": 0.5}]) == []


def test_pairs_and_strings_accepted():
    assert normalize_tiers([(-2.5, "0.2"), ["", 0.3]]) == [(-2.5, 0.2), (None, 0.3)]


def test_defaults_round_trip():
    assert normalize_tiers(default_tiers_as_dicts()) == DEFAULT_TIERS


# --------------------------------------------------------------- config plumbing

def test_slave_config_defaults_and_normalizes():
    from src.config import RISK_MODE_DYNAMIC_PCT, SlaveConfig

    s = SlaveConfig(name="S", login=1, password="p", server="X")
    assert s.risk_tiers == default_tiers_as_dicts()
    assert s.initial_balance == 0.0
    assert s.dynamic_base == "balance"

    s2 = SlaveConfig(
        name="S", login=1, password="p", server="X",
        risk_mode=RISK_MODE_DYNAMIC_PCT,
        initial_balance=50_000.0,
        dynamic_base="equity",
        risk_tiers=[{"up_to_pct": None, "risk_pct": 1.0}, {"up_to_pct": -5, "risk_pct": 0.5}],
    )
    assert s2.risk_mode == "dynamic_pct"
    assert s2.risk_tiers == [{"up_to_pct": -5.0, "risk_pct": 0.5},
                             {"up_to_pct": None, "risk_pct": 1.0}]


def test_negative_initial_balance_rejected():
    from src.config import SlaveConfig

    with pytest.raises(ValueError):
        SlaveConfig(name="S", login=1, password="p", server="X", initial_balance=-1.0)


def test_live_settings_carries_the_ladder():
    from src.config import RISK_MODE_DYNAMIC_PCT, SlaveConfig
    from src.live_settings import LiveSettings, get_live_settings

    LiveSettings.reset()
    settings = get_live_settings()
    slave = SlaveConfig(
        name="S", login=42, password="p", server="Broker",
        risk_mode=RISK_MODE_DYNAMIC_PCT, initial_balance=25_000.0,
        dynamic_base="equity",
    )
    settings.update_from_dict({"slaves": [{
        "name": slave.name, "login": slave.login, "server": slave.server,
        "risk_mode": slave.risk_mode, "initial_balance": slave.initial_balance,
        "dynamic_base": slave.dynamic_base, "risk_tiers": slave.risk_tiers,
    }]})

    live = settings.get_slave(slave.slave_id)
    assert live is not None
    assert live.initial_balance == 25_000.0
    assert live.dynamic_base == "equity"
    assert normalize_tiers(live.risk_tiers) == DEFAULT_TIERS
    LiveSettings.reset()