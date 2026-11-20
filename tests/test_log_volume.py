"""Two ways logging can hurt a trading app: it can bury you, or it can eat RAM.

WHY THIS FILE EXISTS
    Observed on a live run: two slaves polled once a second produced four INFO
    lines a second — "Executing 2 tasks", two "fetch_positions success", and
    "Completed: 2/2" — 345,600 lines a day saying nothing had changed. The
    twenty lines that mattered were somewhere inside that.

    The second failure is quieter. The write queue was capped at 10,000 ITEMS
    with no idea what an item weighs; a trace record carrying an order request
    is a kilobyte, so one writer could hold ~10 MB, and a normal run has four
    of them plus one per worker process.

    So: repetition is counted rather than printed, and both queue stages are
    measured in bytes.
"""
import logging

import pytest

from src.parallel_worker import (
    POLLING_ACTIONS,
    SUMMARY_INTERVAL_S,
    SlaveResult,
    _PollSummary,
)
from src.utils.async_writer import AsyncFileWriter


class _Captured(logging.Handler):
    """Collects formatted messages, so a test can read what a user would."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())      # already interpolated


@pytest.fixture
def logger():
    log = logging.getLogger("metact.test.volume")
    log.handlers.clear()
    log.setLevel(logging.INFO)
    handler = _Captured()
    log.addHandler(handler)
    log.propagate = False
    log._captured = handler          # for the tests to read
    yield log
    log.handlers.clear()


@pytest.fixture
def clock():
    return {"t": 1000.0}


@pytest.fixture
def summary(logger, clock):
    return _PollSummary(logger, interval_s=20.0, clock=lambda: clock["t"])


# ==================== repetition is counted, not printed ====================

def test_a_successful_poll_says_nothing_by_itself(summary, logger):
    summary.record("fetch_positions", "Bybit", True, 1.0)

    assert logger._captured.lines == []


def test_twenty_seconds_of_polling_is_one_line(summary, logger, clock):
    for _ in range(20):
        summary.record("fetch_positions", "Bybit", True, 1.0)
        summary.record("fetch_positions", "5ers_7k", True, 2.0)
    clock["t"] += 20.0

    summary.flush_if_due()

    assert len(logger._captured.lines) == 1


def test_the_summary_carries_the_count_and_the_timing(summary, logger, clock):
    """The count IS the information — 'it is alive, and this fast'."""
    for _ in range(40):
        summary.record("fetch_positions", "Bybit", True, 1.0)
    summary.record("fetch_positions", "Bybit", True, 9.0)
    clock["t"] += 20.0

    summary.flush_if_due()

    line = logger._captured.lines[0]
    assert "41 ok" in line
    assert "max 9ms" in line


def test_the_summary_names_the_slaves(summary, logger, clock):
    summary.record("fetch_positions", "Bybit", True, 1.0)
    summary.record("fetch_positions", "5ers_7k", True, 1.0)
    clock["t"] += 20.0

    summary.flush_if_due()

    assert "Bybit 1" in logger._captured.lines[0]
    assert "5ers_7k 1" in logger._captured.lines[0]


def test_nothing_is_logged_before_the_interval_elapses(summary, logger, clock):
    summary.record("fetch_positions", "Bybit", True, 1.0)
    clock["t"] += 19.9

    summary.flush_if_due()

    assert logger._captured.lines == []


def test_an_idle_interval_produces_no_line(summary, logger, clock):
    """Silence must not become its own kind of spam."""
    clock["t"] += 60.0

    summary.flush_if_due()

    assert logger._captured.lines == []


def test_counters_reset_after_a_flush(summary, logger, clock):
    summary.record("fetch_positions", "Bybit", True, 1.0)
    clock["t"] += 20.0
    summary.flush_if_due()

    assert summary.pending == 0


def test_shutdown_flushes_whatever_is_held(summary, logger, clock):
    """A stop right after a burst is when the numbers matter most."""
    summary.record("fetch_positions", "Bybit", True, 1.0)

    summary.flush_if_due(force=True)

    assert len(logger._captured.lines) == 1


# ==================== failures are never delayed ====================

def test_a_failure_is_still_counted_in_the_summary(summary, logger, clock):
    """The failure is logged at once elsewhere; here it needs a denominator."""
    for _ in range(49):
        summary.record("fetch_positions", "Bybit", True, 1.0)
    summary.record("fetch_positions", "Bybit", False, 0.0)
    clock["t"] += 20.0

    summary.flush_if_due()

    line = logger._captured.lines[0]
    assert "49 ok" in line
    assert "1 FAILED" in line


def test_a_failing_poll_is_logged_immediately(logger):
    """One in fifty failing must be visible now, not in twenty seconds."""
    from src.parallel_worker import ParallelExecutor

    ex = ParallelExecutor.__new__(ParallelExecutor)
    ex.logger = logger
    ex._summary = _PollSummary(logger, interval_s=20.0)

    ex._handle_result(SlaveResult(
        slave_name="Bybit", success=False, action="fetch_positions",
        error="terminal not responding",
    ))

    assert any("terminal not responding" in line for line in logger._captured.lines)


# ==================== money-moving actions are never summarized ====================

def test_only_polling_actions_are_summarized():
    """An open or a close is rare and consequential — it gets its own line."""
    assert "fetch_positions" in POLLING_ACTIONS
    assert "terminal_state" in POLLING_ACTIONS
    for action in ("open", "close", "modify", "reduce"):
        assert action not in POLLING_ACTIONS


def test_an_open_is_logged_individually(logger):
    from src.parallel_worker import ParallelExecutor

    ex = ParallelExecutor.__new__(ParallelExecutor)
    ex.logger = logger
    ex._summary = _PollSummary(logger, interval_s=20.0)

    ex._handle_result(SlaveResult(
        slave_name="Bybit", success=True, action="open", ticket=51203,
        duration_ms=812.0,
    ))

    assert any("51203" in line for line in logger._captured.lines)


def test_the_batch_banners_are_dropped_for_an_all_polling_batch(logger):
    """Otherwise the per-batch count returns three times over."""
    from src.parallel_worker import ParallelExecutor, SlaveTask, TaskAction

    ex = ParallelExecutor.__new__(ParallelExecutor)
    ex.logger = logger
    tasks = [SlaveTask(
        slave_id="1@x", slave_name="Bybit", terminal_path="", login=1,
        password="", server="x", action=TaskAction.FETCH_POSITIONS, params={},
    )]

    ex._log_batch(tasks, "Executing {n} tasks", n=1)

    assert logger._captured.lines == []


def test_the_batch_banners_survive_for_a_real_trade(logger):
    from src.parallel_worker import ParallelExecutor, SlaveTask, TaskAction

    ex = ParallelExecutor.__new__(ParallelExecutor)
    ex.logger = logger
    tasks = [SlaveTask(
        slave_id="1@x", slave_name="Bybit", terminal_path="", login=1,
        password="", server="x", action=TaskAction.OPEN, params={},
    )]

    ex._log_batch(tasks, "Executing {n} tasks", n=1)

    assert logger._captured.lines == ["Executing 1 tasks"]


def test_the_interval_is_twenty_seconds():
    assert SUMMARY_INTERVAL_S == 20.0


# ==================== memory is bounded in bytes ====================

@pytest.fixture
def writer(tmp_path):
    w = AsyncFileWriter(file_path=tmp_path / "out.log", max_bytes=1000,
                        flush_interval=60.0)
    w._running = False              # freeze the writer thread's effect
    yield w
    w._running = False


def test_the_queue_stops_accepting_at_its_byte_cap(writer):
    assert writer.write("x" * 900) is True
    assert writer.write("x" * 900) is False


def test_a_dropped_record_is_counted_in_bytes(writer):
    writer.write("x" * 900)
    writer.write("y" * 900)

    assert writer.dropped_count == 1
    assert writer.dropped_bytes == 901          # + the newline it appends


def test_small_records_still_fit_after_a_big_one_is_refused(writer):
    """Refusing an oversized record must not wedge the writer."""
    writer.write("x" * 900)
    writer.write("y" * 900)                     # dropped

    assert writer.write("z" * 50) is True


def test_the_high_water_mark_is_reported(writer):
    writer.write("x" * 400)
    writer.write("y" * 400)

    assert writer.peak_queued_bytes >= 800


def test_the_default_cap_is_one_megabyte():
    from src.utils.async_writer import DEFAULT_MAX_BYTES

    assert DEFAULT_MAX_BYTES == 1_000_000


def test_bytes_are_released_once_written(tmp_path):
    """Otherwise the cap is a one-way ratchet and the writer dies after 1 MB."""
    w = AsyncFileWriter(file_path=tmp_path / "out.log", flush_interval=0.01,
                        max_bytes=10_000)
    for _ in range(50):
        w.write("x" * 100)
    for _ in range(200):                 # let the writer thread catch up
        if w.queued_bytes == 0:
            break
        import time
        time.sleep(0.01)
    w.close()

    assert w.queued_bytes == 0
    assert w.dropped_count == 0
    assert (tmp_path / "out.log").stat().st_size > 5000


def test_a_drop_is_admitted_in_the_file(tmp_path):
    """A silent gap looks like nothing happened, which is worse than a slow log."""
    w = AsyncFileWriter(file_path=tmp_path / "out.log", flush_interval=0.01,
                        max_bytes=200)
    w._running = False                   # nothing drains: force the drops
    w.write("x" * 150)
    w.write("y" * 150)                   # dropped
    w._flush_buffer(["kept\n"])
    w.close()

    text = (tmp_path / "out.log").read_text(encoding="utf-8")
    assert "DROPPED 1 record(s)" in text
    assert "not contiguous" in text


def test_the_admission_is_written_once_not_every_flush(tmp_path):
    w = AsyncFileWriter(file_path=tmp_path / "out.log", flush_interval=0.01,
                        max_bytes=200)
    w._running = False
    w.write("x" * 150)
    w.write("y" * 150)                   # dropped
    w._flush_buffer(["a\n"])
    w._flush_buffer(["b\n"])
    w.close()

    text = (tmp_path / "out.log").read_text(encoding="utf-8")
    assert text.count("DROPPED") == 1
