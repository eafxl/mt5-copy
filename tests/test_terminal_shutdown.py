"""Terminal shutdown: ask MT5 to close, force-kill only what refuses.

MT5 writes chart templates (EA inputs), terminal.ini and the experts config
only on a clean exit. taskkill /F skips all of it, so every setting made that
session is lost. These tests pin the ask-then-force order and the fallback.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import parallel_worker as pw  # noqa: E402
from src import template_manager as tm  # noqa: E402


@pytest.fixture
def on_windows(monkeypatch):
    """Pretend we're on Windows — every helper is a no-op elsewhere.

    Patched on the real `sys` module because the shutdown paths do a local
    `import sys` at call time.
    """
    monkeypatch.setattr(sys, "platform", "win32")


class _Procs:
    """A controllable process table: PIDs die after N liveness polls."""

    def __init__(self):
        self.closed: list[int] = []
        self.killed: list[int] = []
        self.order: list[str] = []
        self.polls: dict[int, int] = {}
        self.dies_after: dict[int, int] = {}

    def request_close(self, pids):
        self.closed.extend(pids)
        self.order.append("close")
        return len(pids)

    def alive(self, pid):
        if pid in self.killed:
            return False
        self.polls[pid] = self.polls.get(pid, 0) + 1
        return self.polls[pid] <= self.dies_after.get(pid, 0)

    def kill(self, pids):
        self.killed.extend(pids)
        self.order.append("kill")
        return len(pids)


@pytest.fixture
def fake_procs(monkeypatch, on_windows):
    procs = _Procs()
    monkeypatch.setattr(tm, "request_close", procs.request_close)
    monkeypatch.setattr(tm, "pid_alive", procs.alive)
    monkeypatch.setattr(tm, "kill_pids", procs.kill)
    return procs


def test_clean_exit_is_never_force_killed(fake_procs):
    """The whole point: a terminal that closes itself keeps its settings."""
    fake_procs.dies_after = {101: 1, 102: 2}
    clean, forced = tm.stop_pids([101, 102], grace_s=5.0)

    assert fake_procs.closed == [101, 102]
    assert fake_procs.killed == []
    assert (clean, forced) == (2, 0)


def test_wedged_terminal_is_force_killed_after_the_grace_period(fake_procs):
    fake_procs.dies_after = {201: 999}          # never exits on its own
    clean, forced = tm.stop_pids([201], grace_s=0.6)

    assert fake_procs.closed == [201]
    assert fake_procs.killed == [201]
    assert (clean, forced) == (0, 1)


def test_close_is_requested_before_any_kill(fake_procs):
    """Order matters — a kill first would defeat the save."""
    fake_procs.dies_after = {301: 999}
    tm.stop_pids([301], grace_s=0.3)
    assert fake_procs.order == ["close", "kill"]


def test_mixed_batch_only_forces_the_stragglers(fake_procs):
    fake_procs.dies_after = {401: 1, 402: 999}
    clean, forced = tm.stop_pids([401, 402], grace_s=0.6)

    assert fake_procs.killed == [402]
    assert (clean, forced) == (1, 1)


def test_zero_grace_still_asks_first(fake_procs):
    """grace_s=0 is 'don't wait', not 'don't ask'."""
    fake_procs.dies_after = {501: 999}
    clean, forced = tm.stop_pids([501], grace_s=0.0)

    assert fake_procs.closed == [501]
    assert (clean, forced) == (0, 1)


def test_on_wait_is_pumped_while_waiting(fake_procs):
    """The GUI passes processEvents here so the close doesn't look hung."""
    ticks = []
    fake_procs.dies_after = {601: 3}
    tm.stop_pids([601], grace_s=5.0, on_wait=lambda: ticks.append(1))
    assert len(ticks) >= 3


def test_a_raising_on_wait_does_not_abort_the_shutdown(fake_procs):
    fake_procs.dies_after = {701: 1}

    def boom():
        raise RuntimeError("widget already deleted")

    clean, forced = tm.stop_pids([701], grace_s=5.0, on_wait=boom)
    assert (clean, forced) == (1, 0)


def test_empty_pid_list_does_nothing(fake_procs):
    assert tm.stop_pids([], grace_s=5.0) == (0, 0)
    assert fake_procs.closed == []
    assert fake_procs.killed == []


def test_wait_for_exit_returns_survivors(fake_procs):
    fake_procs.dies_after = {801: 1, 802: 999}
    assert tm.wait_for_exit([801, 802], 0.6) == [802]


def test_non_windows_is_a_no_op():
    assert sys.platform != "win32"          # guard: fixture not applied here
    assert tm.stop_pids([1, 2]) == (0, 0)
    assert tm.request_close([1, 2]) == 0
    assert tm.wait_for_exit([1, 2], 5.0) == [1, 2]
    assert tm.post_close_to_pid(1) == 0
    assert tm.pid_alive(1) is True


def test_default_grace_gives_the_terminal_real_time_to_save():
    assert tm.GRACEFUL_CLOSE_TIMEOUT_S >= 10.0


# --------------------------------------------------- executor-side targeting

def test_kill_mt5_terminals_stops_only_matching_paths(monkeypatch, on_windows):
    monkeypatch.setattr(tm, "list_terminal64_pids_with_paths", lambda: [
        (11, r"c:\mt5copy\slave1\terminal64.exe"),
        (12, r"c:\other\terminal64.exe"),
    ])

    seen = {}

    def fake_stop(pids, grace_s=None, on_wait=None):
        seen["pids"] = pids
        seen["grace"] = grace_s
        return len(pids), 0

    monkeypatch.setattr(tm, "stop_pids", fake_stop)
    pw._kill_mt5_terminals({r"C:\MT5Copy\Slave1\terminal64.exe"}, grace_s=7.0)

    assert seen["pids"] == [11]          # the unrelated terminal is left alone
    assert seen["grace"] == 7.0


def test_kill_mt5_terminals_defaults_to_the_graceful_timeout(monkeypatch, on_windows):
    monkeypatch.setattr(tm, "list_terminal64_pids_with_paths",
                        lambda: [(21, r"c:\a\terminal64.exe")])

    seen = {}

    def fake_stop(pids, grace_s=None, on_wait=None):
        seen["grace"] = grace_s
        return 1, 0

    monkeypatch.setattr(tm, "stop_pids", fake_stop)
    pw._kill_mt5_terminals({r"C:\A\terminal64.exe"})

    assert seen["grace"] == tm.GRACEFUL_CLOSE_TIMEOUT_S
