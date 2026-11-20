"""One run, one set of files — and a Debug tab that cannot choke on them.

WHY THIS FILE EXISTS
    Traces and dumps used to roll by calendar day. A machine left running
    accumulated every run of that day into one file, and the Debug tab tailed
    it starting from offset zero: opening the app in the afternoon meant
    reading 28 MB, splitting it into ~250,000 strings, and laying them out as
    a rich-text document on the GUI thread. The copy engine was healthy the
    whole time; the window said Not Responding.

    So: files are named per session, and every step between a file and the
    screen is bounded — the first read skips history, each tick reads a capped
    number of bytes, and the panes discard their oldest lines.
"""
import os

import pytest

from src.utils import logging as log_mod
from src.utils.logging import SESSION_ENV_VAR, session_stamp


@pytest.fixture
def qapp(gui):
    """A QApplication, needed before any widget is constructed."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def fresh_stamp(monkeypatch):
    """Each test resolves the session stamp from scratch."""
    monkeypatch.setattr(log_mod, "_SESSION_STAMP", None)
    monkeypatch.delenv(SESSION_ENV_VAR, raising=False)
    yield
    monkeypatch.setattr(log_mod, "_SESSION_STAMP", None)


# ==================== the stamp ====================

def test_the_stamp_is_stable_within_a_process():
    assert session_stamp() == session_stamp()


def test_the_stamp_is_exported_for_child_processes():
    """A worker computing its own would name files after the moment the pool
    happened to spawn it — neither this run, nor the same answer twice."""
    stamp = session_stamp()

    assert os.environ[SESSION_ENV_VAR] == stamp


def test_a_child_process_inherits_rather_than_inventing(monkeypatch):
    monkeypatch.setenv(SESSION_ENV_VAR, "20240101_120000")

    assert session_stamp() == "20240101_120000"


def test_the_stamp_still_sorts_by_time():
    """Dated names group by day and sort chronologically; that was never the
    problem with per-day files, so it must survive."""
    stamp = session_stamp()

    assert len(stamp) == len("YYYYMMDD_HHMMSS")
    assert stamp[8] == "_"
    assert stamp.replace("_", "").isdigit()


# ==================== every output file uses it ====================

def test_the_trace_file_is_named_for_the_session(tmp_path):
    from src.utils.tracer import Tracer

    tracer = Tracer(trace_dir=tmp_path, enabled=True)
    try:
        assert tracer._get_trace_path().name == f"{session_stamp()}_traces.jsonl"
    finally:
        tracer.close()


def test_the_dump_file_is_named_for_the_session(tmp_path):
    from src.utils.debug import DebugDumper

    dumper = DebugDumper(dump_dir=tmp_path)
    try:
        assert dumper._get_dump_path().name == f"{session_stamp()}_debug.jsonl"
    finally:
        dumper._writer.close()


def test_the_session_log_is_named_for_the_session(tmp_path):
    logger = log_mod.setup_logging("INFO", log_dir=tmp_path)
    logger.info("something worth writing")
    for h in logger.handlers[:]:            # closing flushes the async writer
        h.close()
        logger.removeHandler(h)

    assert (tmp_path / f"copier_{session_stamp()}.log").exists()


def test_two_runs_do_not_share_a_file(monkeypatch, tmp_path):
    """The whole point: 'what did THIS run do' must not start with a search."""
    from src.utils.tracer import Tracer

    monkeypatch.setenv(SESSION_ENV_VAR, "20240101_090000")
    monkeypatch.setattr(log_mod, "_SESSION_STAMP", None)
    first = Tracer(trace_dir=tmp_path, enabled=True)
    first_path = first._get_trace_path()
    first.close()

    monkeypatch.setenv(SESSION_ENV_VAR, "20240101_170000")
    monkeypatch.setattr(log_mod, "_SESSION_STAMP", None)
    second = Tracer(trace_dir=tmp_path, enabled=True)
    second_path = second._get_trace_path()
    second.close()

    assert first_path != second_path


def test_retention_protects_this_session_across_all_four_files(monkeypatch, tmp_path):
    from src import retention

    monkeypatch.setenv("METACT_HOME", str(tmp_path))
    from src import paths
    paths.reset_cache()

    names = {p.name for p in retention.active_files()}
    stamp = session_stamp()

    assert names == {
        f"{stamp}_traces.jsonl",
        f"{stamp}_debug.jsonl",
        f"copier_{stamp}.log",
        f"worker_{stamp}.log",
    }
    paths.reset_cache()


def test_retention_still_recognises_the_session_named_files():
    """A pattern that stopped matching would keep every file forever."""
    import fnmatch

    from src.retention import SWEPT

    stamp = "20240101_120000"
    assert any(fnmatch.fnmatch(f"{stamp}_traces.jsonl", p) for p in SWEPT["traces"])
    assert any(fnmatch.fnmatch(f"{stamp}_debug.jsonl", p) for p in SWEPT["dumps"])
    assert any(fnmatch.fnmatch(f"copier_{stamp}.log", p) for p in SWEPT["logs"])
    assert any(fnmatch.fnmatch(f"worker_{stamp}.log", p) for p in SWEPT["logs"])


# ==================== the tail is bounded ====================
#
# conftest replaces PyQt6 with MagicMock for the rest of the suite, and a class
# whose base is a mock cannot be instantiated. These tests need the real
# widgets, so they swap the real PyQt6 back in for their own duration —
# monkeypatch restores sys.modules afterwards, leaving the stub in place for
# everyone else.


@pytest.fixture
def gui(monkeypatch):
    """src.gui.main_window imported against the REAL Qt, offscreen."""
    import sys

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in [m for m in list(sys.modules) if m.startswith("PyQt6")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    pytest.importorskip("PyQt6.QtWidgets")
    import importlib

    module = importlib.import_module("src.gui.main_window")
    yield module
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.fixture
def collector(tmp_path, gui):
    """A stats collector rooted at a temp data dir, no QApplication needed."""
    col = gui._StatsCollector.__new__(gui._StatsCollector)
    col._running = False
    col._start_time = 0.0
    col._base_dir = tmp_path
    col._trace_pos = None
    col._dump_pos = None
    col._tailing = {}
    (tmp_path / "traces").mkdir()
    return col


def _trace_file(base, lines: int, tag="old"):
    path = base / "traces" / f"{session_stamp()}_traces.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(f'{{"n":{i},"tag":"{tag}"}}\n' for i in range(lines))
    return path


def test_history_already_in_the_file_is_not_replayed(collector, tmp_path):
    """The pane shows what happens FROM NOW. The file holds the history."""
    _trace_file(tmp_path, 80_000)

    lines, path = collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    assert lines == []
    assert path.endswith("_traces.jsonl")


def test_lines_written_after_the_first_look_do_arrive(collector, tmp_path):
    _trace_file(tmp_path, 10)
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")
    _trace_file(tmp_path, 5, tag="new")

    lines, _ = collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    assert len(lines) == 5
    assert all("new" in line for line in lines)


def test_a_burst_is_truncated_to_the_line_cap(collector, tmp_path):
    _trace_file(tmp_path, 1)
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")
    _trace_file(tmp_path, 5000, tag="burst")

    lines, _ = collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    assert len(lines) <= collector.MAX_TAIL_LINES + 1     # + the notice


def test_the_truncation_says_so_rather_than_hiding_it(collector, tmp_path):
    _trace_file(tmp_path, 1)
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")
    _trace_file(tmp_path, 5000, tag="burst")

    lines, _ = collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    assert "not shown" in lines[0]


def test_a_replaced_file_is_read_from_its_start(collector, tmp_path):
    """A new session's file is shorter than the offset held for the old one."""
    path = _trace_file(tmp_path, 100)
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")
    path.write_text('{"n":0,"tag":"fresh"}\n', encoding="utf-8")

    lines, _ = collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    assert len(lines) == 1


def test_a_missing_file_is_not_an_error(collector):
    assert collector._read_new_lines(
        "dumps", "", "_debug.jsonl", "_dump_pos"
    ) == ([], "")


def test_the_panes_discard_their_oldest_lines(gui, qapp):
    """Without a cap the document grows all session, and laying it out is what
    stopped the window repainting."""
    view = gui.DashboardTab._make_output("")
    for i in range(gui.DashboardTab.OUTPUT_MAX_LINES * 2):
        view.appendPlainText(f"line {i}")

    assert view.blockCount() <= gui.DashboardTab.OUTPUT_MAX_LINES


def test_the_panes_are_plain_text(gui, qapp):
    """A trace line containing '<' must not be read as markup, and plain text
    lays out far faster than a rich-text document."""
    from PyQt6.QtWidgets import QPlainTextEdit

    assert isinstance(gui.DashboardTab._make_output(""), QPlainTextEdit)


# ==================== the volume that caused it ====================

def test_the_poll_heartbeats_are_muted_by_default():
    """12,015 of one session's 12,287 trace records were these three."""
    from src.utils.tracer import Tracer

    for op in ("worker_connect_cached", "worker_fetch_positions",
               "worker_terminal_state"):
        assert op in Tracer.DEFAULT_EXCLUDE


def test_nothing_about_a_trade_is_muted():
    from src.utils.tracer import Tracer

    for op in ("worker_open", "worker_close", "worker_modify", "trade_action",
               "broker_order_send", "copy_new_position", "close_position",
               "update_sl_tp", "ea_signal"):
        assert op not in Tracer.DEFAULT_EXCLUDE


def test_switching_to_this_sessions_file_starts_it_afresh(collector, tmp_path):
    """Before the service starts there is no file for this run, so an older
    one is tailed; the moment this run's appears the offset must not carry
    over and point into the middle of a different file."""
    old = tmp_path / "traces" / "20240101_090000_traces.jsonl"
    old.write_text("".join(f'{{"n":{i}}}\n' for i in range(500)), encoding="utf-8")
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")

    _trace_file(tmp_path, 3, tag="this-session")     # named for THIS session
    collector._read_new_lines("traces", "", "_traces.jsonl", "_trace_pos")
    _trace_file(tmp_path, 2, tag="after")

    lines, path = collector._read_new_lines(
        "traces", "", "_traces.jsonl", "_trace_pos"
    )

    assert session_stamp() in path
    assert len(lines) == 2
