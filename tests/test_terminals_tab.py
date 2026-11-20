"""Terminals tab: what the table says about each terminal, and why.

No Qt here — these cover the decisions the table renders, which is where the
bugs were. The widget layering is exercised by driving the real widget
offscreen; what is pinned below is the logic that decides what to show.
"""
import pytest

from src.gui.terminals_tab import _ALGO_DISPLAY, MASTER_ROW_ID, _terminal_label


# ==================== "could not ask" is not "switched off" ====================
#
# Algo Trading is the one switch that turns a working copier into one that
# silently places no orders. Defaulting an unreadable terminal to False would
# cry wolf; defaulting it to True would hide the real thing.

def test_the_three_algo_states_are_distinct():
    on_label, on_colour = _ALGO_DISPLAY[True]
    off_label, off_colour = _ALGO_DISPLAY[False]
    unknown_label, unknown_colour = _ALGO_DISPLAY[None]

    assert {on_label, off_label, unknown_label} == {"On", "OFF", "?"}
    # Three different colours, so the state is legible without reading.
    assert len({on_colour, off_colour, unknown_colour}) == 3


def test_off_is_the_loud_one():
    """It is the actionable failure — the operator can fix it in one click."""
    assert _ALGO_DISPLAY[False][1] == "#C62828"      # red
    assert _ALGO_DISPLAY[None][1] == "#9E9E9E"       # grey: nothing to act on


# ==================== the master's row ====================

def test_the_master_id_cannot_collide_with_a_real_terminal():
    """Slave ids are login@server. The master is identified by being master."""
    from src.config import make_slave_id

    assert MASTER_ROW_ID != make_slave_id(0, "")
    assert "@" not in MASTER_ROW_ID


@pytest.mark.parametrize("path,expected", [
    (r"C:\Program Files\MetaTrader 5\terminal64.exe", "MetaTrader 5"),
    (r"C:\Program Files\MT5Copy\MetaTrader 5 Bybit\terminal64.exe",
     "MetaTrader 5 Bybit"),
    ("/opt/mt5/MetaTrader 5/terminal64.exe", "MetaTrader 5"),
    ("terminal64.exe", "Master"),
    ("", "Master"),
])
def test_the_label_is_the_folder_the_terminal_lives_in(path, expected):
    """What tells two installs apart — they are all called terminal64.exe.

    Split on both separators: these are Windows paths, but the GUI is also
    developed and screenshotted on POSIX, where pathlib treats a backslash as
    an ordinary character and hands back an empty name.
    """
    assert _terminal_label(path, "Master") == expected


# ==================== the worker side ====================

def test_warmup_reports_algo_trading(monkeypatch):
    """Captured while already connected, so the column is right immediately."""
    import src.parallel_worker as pw

    monkeypatch.setattr(
        pw.mt5, "account_info",
        lambda: type("A", (), {"login": 1, "server": "S", "leverage": 100,
                               "balance": 1000.0})(),
        raising=False,
    )
    monkeypatch.setattr(pw.mt5, "symbols_get", lambda: [], raising=False)
    monkeypatch.setattr(
        pw.mt5, "terminal_info",
        lambda: type("T", (), {"trade_allowed": True, "connected": True,
                               "build": 4620})(),
        raising=False,
    )

    result = pw._execute_warmup(None, slave_name="S")

    assert result.success
    assert result.details["algo_trading"] is True
    assert result.details["build"] == 4620


def test_an_unreadable_terminal_reports_nothing_rather_than_false(monkeypatch):
    """The distinction the whole column rests on."""
    import src.parallel_worker as pw

    monkeypatch.setattr(pw.mt5, "terminal_info", lambda: None, raising=False)
    assert pw._read_terminal_state() == {}

    def boom():
        raise RuntimeError("IPC send failed")

    monkeypatch.setattr(pw.mt5, "terminal_info", boom, raising=False)
    assert pw._read_terminal_state() == {}


def test_the_state_probe_fails_loudly_rather_than_claiming_algo_is_off(monkeypatch):
    import src.parallel_worker as pw

    monkeypatch.setattr(pw.mt5, "terminal_info", lambda: None, raising=False)

    result = pw._execute_terminal_state(None, slave_name="S")

    assert result.success is False
    assert result.details == {}          # absent -> the table shows "?"


def test_the_master_client_reads_its_own_algo_flag(monkeypatch):
    import src.mt5_client as mc

    monkeypatch.setattr(
        mc.mt5, "terminal_info",
        lambda: type("T", (), {"trade_allowed": False, "connected": True,
                               "build": 4620, "path": "C:/mt5"})(),
        raising=False,
    )
    client = mc.MT5Client.__new__(mc.MT5Client)

    assert client.terminal_state()["algo_trading"] is False


def test_the_master_client_returns_nothing_when_it_cannot_ask(monkeypatch):
    import src.mt5_client as mc

    monkeypatch.setattr(mc.mt5, "terminal_info", lambda: None, raising=False)
    client = mc.MT5Client.__new__(mc.MT5Client)

    assert client.terminal_state() == {}
