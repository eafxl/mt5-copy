"""Logging setup for MetaCT.

All file writes are ASYNC - never blocks the copy path.
Console output remains synchronous for immediate feedback.
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from .async_writer import AsyncFileWriter


#: Root logger name. Every module reaches it through ``get_logger()`` or by
#: naming a child of it (``metact.worker``), so the whole tree can be levelled
#: and handled in one place. It is a plain string rather than the app name so a
#: rebrand cannot silently split the tree in two — a child of the wrong root
#: keeps logging, just to nowhere the file handler is attached.
LOGGER_NAME = "metact"


#: Written into every log record as ``%(trace)s`` when no trace is in scope.
#: A fixed-width placeholder, so the column does not wander between lines.
NO_TRACE = "--------"


class TraceContextFilter(logging.Filter):
    """Stamp each record with the trace it happened inside.

    WHY
        The trace file says a copy took 812ms; the log says the broker rejected
        an order. Before this, joining the two meant matching timestamps by eye
        across two files with different clocks — and the trade_id only appeared
        in log lines where somebody had remembered to interpolate it.

        Every span already carries a trace_id, and the tracer keeps the current
        one in thread-local state, so any log line emitted inside a span can be
        stamped with it for free. ``grep <trade_id>`` then returns the trace
        records AND the log lines for one trade, across the copy path, the
        worker processes and the GUI.

    Attached to HANDLERS, not to the logger: a filter on a logger is not
    consulted for records that a child logger propagated up to it, and
    ``metact.worker`` / ``metact.tcp`` are exactly the ones worth correlating.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace = _current_trace_id()
        return True


def _current_trace_id() -> str:
    """The in-scope trace id, or the placeholder. Never raises, never builds
    a tracer: logging must not be able to create files as a side effect."""
    try:
        from . import tracer as _tracer_mod

        tracer = _tracer_mod._tracer
        if tracer is None:
            return NO_TRACE
        return tracer._get_trace_id() or NO_TRACE
    except Exception:
        return NO_TRACE


class AsyncFileHandler(logging.Handler):
    """Non-blocking file handler using background thread.

    Writes are queued and flushed asynchronously.
    NEVER blocks the caller (drops if queue full).
    """

    def __init__(
        self,
        file_path: Path | None = None,
        get_file_path: Callable[[], Path] | None = None,
        max_queue_size: int = 10000,
    ):
        super().__init__()
        self._writer = AsyncFileWriter(
            file_path=file_path,
            get_file_path=get_file_path,
            max_queue_size=max_queue_size,
            flush_interval=0.2,  # Flush every 200ms
        )

    def emit(self, record: logging.LogRecord):
        """Emit a log record (non-blocking)."""
        try:
            msg = self.format(record)
            self._writer.write(msg)
        except Exception:
            pass  # Never crash on logging errors

    @property
    def dropped_count(self) -> int:
        """Number of log messages dropped due to full queue."""
        return self._writer.dropped_count

    @property
    def pending_count(self) -> int:
        """Number of log messages pending in queue."""
        return self._writer.pending_count

    def close(self):
        """Close the handler and flush remaining data."""
        self._writer.close()
        super().close()


# Captured at first use so every handler created in this process writes to the
# same file name. Subsequent setup calls reuse it, so child loggers added by
# the GUI / service don't fragment into multiple files.
_SESSION_STAMP: str | None = None

#: Exported into the environment so WORKER PROCESSES inherit the parent's
#: stamp. A worker computing its own would name its files after the moment the
#: pool happened to spawn it, which is neither the session the user started nor
#: the same answer twice.
SESSION_ENV_VAR = "METACT_SESSION"


def session_stamp() -> str:
    """The YYYYMMDD_HHMMSS suffix identifying this RUN.

    Every file the app writes about itself — log, trace, debug dump, worker
    log — is named with it, so one run's output is one set of files.

    WHY NOT PER DAY
        Traces and dumps used to roll by calendar day, which meant a machine
        left running accumulated every run into one file. Two consequences,
        both bad: the file the GUI tails was 28 MB by lunchtime (see
        DashboardTab's tail, which used to read all of it), and answering
        "what did THIS run do" started with finding where it began.

        Dated names still sort chronologically and still group by day, so
        nothing is lost by making them per-run.
    """
    global _SESSION_STAMP
    if _SESSION_STAMP is None:
        inherited = os.environ.get(SESSION_ENV_VAR)
        if inherited:
            _SESSION_STAMP = inherited
        else:
            _SESSION_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.environ[SESSION_ENV_VAR] = _SESSION_STAMP
    return _SESSION_STAMP


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """Set up logging with async file and console handlers.

    File writes are async (never block copy path).
    Console writes are sync (immediate feedback).

    Each *process launch* gets its own log file
    `copier_<YYYYMMDD_HHMMSS>.log` so sessions never overwrite or interleave.
    The session stamp is computed once and cached for the process lifetime.
    """
    if log_dir is None:
        from ..paths import logs_dir
        log_dir = logs_dir()

    # If a plain file exists where the log directory should be, rename it
    if log_dir.exists() and not log_dir.is_dir():
        log_dir.rename(log_dir.with_suffix(".old"))
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create logger
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    # Console handler (sync - for immediate feedback)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)

    # Async file handler (NEVER blocks copy path).
    # One file per session so a hung / blocked app can be debugged without
    # mixing log lines from earlier runs.
    stamp = session_stamp()
    log_path = log_dir / f"copier_{stamp}.log"

    file_handler = AsyncFileHandler(file_path=log_path)
    file_handler.setLevel(logging.DEBUG)
    # The trace column goes in the file only. The console is read live by a
    # person watching one thing happen, where an eight-character id per line is
    # noise; the file is read afterwards, next to the trace, where it is the
    # whole point.
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(trace)s | %(name)s | %(message)s"
    )
    file_handler.setFormatter(file_format)
    file_handler.addFilter(TraceContextFilter())

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    logger.info("=" * 60)
    logger.info("Session log file: %s", log_path)
    logger.info("=" * 60)

    return logger


def get_logger() -> logging.Logger:
    """Get the MetaCT root logger."""
    return logging.getLogger(LOGGER_NAME)


def get_logging_stats() -> dict:
    """Get logging statistics (dropped messages, pending, etc.)."""
    logger = logging.getLogger(LOGGER_NAME)
    stats = {}

    for handler in logger.handlers:
        if isinstance(handler, AsyncFileHandler):
            stats["file_handler"] = {
                "dropped": handler.dropped_count,
                "pending": handler.pending_count,
                # The bytes are what say whether the 1 MB cap is anywhere near
                # biting; the counts alone cannot, since record sizes vary by
                # two orders of magnitude between a log line and a trace record.
                "dropped_bytes": handler._writer.dropped_bytes,
                "queued_bytes": handler._writer.queued_bytes,
                "peak_queued_bytes": handler._writer.peak_queued_bytes,
            }

    return stats
