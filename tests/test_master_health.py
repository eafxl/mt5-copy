"""The master terminal: measured, not assumed.

WHAT WAS WRONG
    Three separate things told the user about the master, and none of them had
    asked it:

    1. The Trading tab's Master pill was lit from "is the service running".
       Kill the terminal and it stayed green.
    2. The Terminals tab read the master from a PowerShell process listing
       that answers "nothing is running" whenever the query itself fails, and
       cannot read the path of a process owned by another user — which a
       hand-launched master may well be.
    3. "Relaunch master" called a helper ending in ``mt5.shutdown()`` IN THE
       GUI PROCESS. MetaTrader5 is a per-process singleton, so relaunching the
       master tore down the service's own connection to it. The terminal came
       back; the link did not; Algo Trading read "?" until a trade happened to
       trigger a reconnect.

    The fix is one source of truth: the service asks the terminal, on its own
    thread, and publishes the answer.
"""
import pytest

from src.master import MasterMonitor


@pytest.fixture
def gui(monkeypatch):
    """src.gui.terminals_tab against the REAL Qt (conftest stubs it for the
    rest of the suite, and a class whose base is a mock cannot be built)."""
    import importlib
    import sys

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in [m for m in list(sys.modules) if m.startswith("PyQt6")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    pytest.importorskip("PyQt6.QtWidgets")
    module = importlib.import_module("src.gui.terminals_tab")
    yield module
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


class _NullLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _Client:
    """Just the two calls MasterMonitor.health/ensure_connected make."""

    def __init__(self, state=None, connect_ok=True):
        self.state = state if state is not None else {}
        self.connect_ok = connect_ok
        self.initialized = 0

    def terminal_state(self):
        return self.state

    def initialize(self, path="", timeout_ms=60000):
        self.initialized += 1
        if self.connect_ok:
            self.state = {"connected": True, "algo_trading": True}
        return self.connect_ok

    def get_account_info(self):
        if not self.state.get("connected"):
            return None
        return type("Info", (), {"login": 123, "leverage": 100})()


def monitor(client):
    m = MasterMonitor.__new__(MasterMonitor)
    m.mt5 = client
    m.terminal_path = "C:\\MT5\\terminal64.exe"
    m.logger = _NullLogger()
    m._last_positions = {}
    m._account_info = None
    m._poll_seq = 0
    return m


# ==================== asking the terminal ====================

def test_a_live_terminal_reports_connected_and_its_algo_switch():
    m = monitor(_Client({"connected": True, "algo_trading": True}))

    assert m.health() == (True, True)


def test_algo_off_is_reported_as_off_not_as_unknown():
    """The one switch that silently stops every order."""
    m = monitor(_Client({"connected": True, "algo_trading": False}))

    assert m.health() == (True, False)


def test_an_unreachable_terminal_is_down_with_algo_unknown():
    """Not False: 'we could not ask' and 'you switched it off' are different
    facts, and only one of them is the operator's to fix."""
    m = monitor(_Client({}))

    assert m.health() == (False, None)


def test_a_terminal_that_is_up_but_disconnected_is_not_connected():
    m = monitor(_Client({"connected": False, "algo_trading": True}))

    connected, _ = m.health()

    assert connected is False


# ==================== reviving it ====================

def test_a_dead_master_is_reconnected_on_the_services_own_cycle():
    """The bug this replaces: nothing reconnected until the next trade, which
    may be hours away — so a relaunched terminal stayed 'down' in the UI."""
    client = _Client({})
    m = monitor(client)

    assert m.ensure_connected() is True
    assert client.initialized == 1


def test_a_live_master_is_left_alone():
    """Reconnecting a healthy terminal costs a full re-initialize."""
    client = _Client({"connected": True, "algo_trading": True})
    m = monitor(client)

    assert m.ensure_connected() is True
    assert client.initialized == 0


def test_a_terminal_that_will_not_come_back_reports_failure():
    client = _Client({}, connect_ok=False)

    assert monitor(client).ensure_connected() is False


# ==================== the process listing must be able to say "don't know" ====================

def test_a_failed_process_query_is_unknown_not_empty(monkeypatch):
    """Answering 'nothing is running' on a failed query marked every terminal
    dead and started reconnecting them all."""
    import subprocess
    import sys

    import src.parallel_worker as pw

    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*a, **k):
        raise OSError("powershell missing")

    monkeypatch.setattr(subprocess, "run", boom)

    assert pw._get_running_terminal_paths() is None


def test_a_non_windows_host_is_unknown_not_empty():
    """Nothing here can enumerate terminals; that is not evidence of death."""
    import sys

    import src.parallel_worker as pw

    if sys.platform == "win32":            # pragma: no cover - dev machines
        pytest.skip("this asserts the non-Windows answer")
    assert pw._get_running_terminal_paths() is None


def test_an_unknown_result_does_not_disconnect_anything(gui):
    """The Terminals tab must keep its previous answer rather than act on a
    failed query."""
    tt = gui

    tab = tt.TerminalsTab.__new__(tt.TerminalsTab)
    tab._running_paths = {"c:\\mt5\\terminal64.exe"}
    tab._copier = object()
    rebuilt = []
    tab._rebuild_table = lambda: rebuilt.append(True)

    tab._on_health_result(None)

    assert tab._running_paths == {"c:\\mt5\\terminal64.exe"}
    assert rebuilt, "the table still refreshes, it just learns nothing new"


# ==================== the relaunch must not kill the connection ====================

def test_the_relaunch_runs_in_a_child_process(gui):
    """`_warm_master_terminal` ends in mt5.shutdown(); running it in this
    process is what tore down the service's master connection."""
    import inspect

    source = inspect.getsource(gui._MasterRelaunchThread.run)

    assert "ProcessPoolExecutor" in source
    assert "pool.submit(_warm_master_terminal" in source


def test_the_helper_still_ends_by_shutting_its_own_connection_down():
    """If this ever stops being true the subprocess is no longer required —
    and this test is the reminder to revisit the thread above."""
    import inspect

    from src.main import _warm_master_terminal

    assert "mt5.shutdown()" in inspect.getsource(_warm_master_terminal)
