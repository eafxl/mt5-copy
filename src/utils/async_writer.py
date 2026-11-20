"""Async file writer - non-blocking I/O for logging and tracing.

All writes go through a background thread queue, ensuring the main
copy path is NEVER blocked by disk I/O.

MEMORY IS BOUNDED IN BYTES, NOT IN LINES
    The queue used to be capped at 10,000 ITEMS with no notion of how big an
    item is. A trace record carrying a full order request runs to a kilobyte or
    more, so that cap allowed something like 10 MB per writer — and there are
    four writers alive in a normal run (session log, trace, debug dump, worker
    log), plus one more per worker process. If the disk stalls or the file
    handle is lost, all of it stays resident.

    Both stages are now measured in bytes: the queue holds at most
    ``max_bytes`` (1 MB by default), and the writer thread's own buffer drains
    at most ``FLUSH_BYTES`` before it must write. Peak resident memory per
    writer is therefore about ``max_bytes + FLUSH_BYTES``, whatever the shape
    of the data.

DROPS ARE ADMITTED, NOT HIDDEN
    Dropping is still the right behaviour — a trade must never wait on a log
    line — but a silently truncated log is worse than a slow one, because the
    gap looks like nothing happened. When the backlog clears, the writer emits
    one line saying how much was lost.
"""
import atexit
import queue
import threading
import time
from pathlib import Path
from typing import Callable

#: Default cap on queued-but-unwritten bytes, per writer.
DEFAULT_MAX_BYTES = 1_000_000

#: The writer thread stops draining and writes once it holds this much. Keeps
#: the second copy of the data (queue -> buffer) from growing to the size of
#: the queue itself.
FLUSH_BYTES = 256 * 1024


class AsyncFileWriter:
    """Non-blocking file writer using background thread.

    Writes are queued and flushed asynchronously. If the queue is full (by
    bytes or by count), messages are dropped — never blocks the caller.
    """

    def __init__(
        self,
        file_path: Path | None = None,
        max_queue_size: int = 10000,
        flush_interval: float = 0.1,
        get_file_path: Callable[[], Path] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        """
        Args:
            file_path: Static file path (or use get_file_path for dynamic)
            max_queue_size: Max queued writes before dropping (count guard)
            flush_interval: How often to flush buffer (seconds)
            get_file_path: Callable to get dynamic file path (e.g., daily rotation)
            max_bytes: Max queued-but-unwritten bytes before dropping. This is
                the guard that actually bounds memory; the count is a second
                line of defence for pathologically small records.
        """
        self._static_path = file_path
        self._get_file_path = get_file_path
        self._max_queue_size = max_queue_size
        self._flush_interval = flush_interval
        self._max_bytes = max_bytes

        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=max_queue_size)
        self._dropped = 0
        self._dropped_bytes = 0
        self._undeclared_drops = 0        # dropped since the last note written
        self._undeclared_bytes = 0
        self._queued_bytes = 0
        self._peak_queued_bytes = 0
        self._total_writes = 0
        self._lock = threading.Lock()

        self._running = False
        self._thread: threading.Thread | None = None
        self._file = None
        self._current_path: Path | None = None

        self._start()
        atexit.register(self.close)

    def _start(self):
        """Start the background writer thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="AsyncFileWriter")
        self._thread.start()

    def _get_current_path(self) -> Path | None:
        """Get the current file path."""
        if self._get_file_path:
            return self._get_file_path()
        return self._static_path

    def _take(self, item: str, buffer: list[str], held: int) -> int:
        """Move one item from the queue into the buffer, releasing its bytes."""
        size = len(item)
        buffer.append(item)
        with self._lock:
            self._queued_bytes -= size
        return held + size

    def _writer_loop(self):
        """Background thread that processes the write queue."""
        buffer: list[str] = []
        held = 0                      # bytes currently in `buffer`
        last_flush = time.time()

        while self._running:
            try:
                try:
                    item = self._queue.get(timeout=self._flush_interval)
                    if item is None:  # Shutdown signal
                        break
                    held = self._take(item, buffer, held)
                except queue.Empty:
                    pass

                # Drain what is waiting, but never more than one flush's worth:
                # the buffer is a SECOND copy of the queued data, so an
                # unbounded drain doubles the memory the queue cap was meant to
                # limit. Anything left stays queued (and accounted for) until
                # the next pass.
                while held < FLUSH_BYTES:
                    try:
                        item = self._queue.get_nowait()
                        if item is None:
                            break
                        held = self._take(item, buffer, held)
                    except queue.Empty:
                        break

                now = time.time()
                if buffer and (
                    now - last_flush >= self._flush_interval
                    or held >= FLUSH_BYTES
                    or len(buffer) > 100
                ):
                    self._flush_buffer(buffer)
                    buffer.clear()
                    held = 0
                    last_flush = now

            except Exception:
                pass  # Never crash the writer thread

        # Final flush
        if buffer:
            self._flush_buffer(buffer)

    def _drop_note(self) -> str | None:
        """One line admitting what was dropped since the last note, or None."""
        with self._lock:
            count, size = self._undeclared_drops, self._undeclared_bytes
            self._undeclared_drops = 0
            self._undeclared_bytes = 0
        if not count:
            return None
        return (
            f"[async-writer] DROPPED {count} record(s) / {size} bytes — the "
            f"write queue hit its {self._max_bytes}-byte cap. Output above and "
            f"below this line is not contiguous.\n"
        )

    def _flush_buffer(self, buffer: list[str]):
        """Flush buffer to file."""
        path = self._get_current_path()
        if not path:
            return

        try:
            # Rotate file if path changed
            if self._current_path != path:
                if self._file:
                    self._file.close()
                path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(path, "a", encoding="utf-8", buffering=8192)
                self._current_path = path

            if self._file:
                note = self._drop_note()
                if note:
                    self._file.write(note)
                self._file.writelines(buffer)
                self._file.flush()
        except Exception:
            pass  # Don't crash on write errors

    def write(self, data: str) -> bool:
        """
        Queue a write. NEVER BLOCKS.

        Returns True if queued, False if dropped (queue full by bytes or count).
        """
        if not data.endswith("\n"):
            data += "\n"

        size = len(data)
        with self._lock:
            if self._queued_bytes + size > self._max_bytes:
                self._dropped += 1
                self._dropped_bytes += size
                self._undeclared_drops += 1
                self._undeclared_bytes += size
                return False
            # Reserved BEFORE the put, so a burst of concurrent writers cannot
            # each see room that only one of them can actually have.
            self._queued_bytes += size
            self._peak_queued_bytes = max(self._peak_queued_bytes, self._queued_bytes)

        try:
            self._queue.put_nowait(data)
        except queue.Full:
            with self._lock:
                self._queued_bytes -= size
                self._dropped += 1
                self._dropped_bytes += size
                self._undeclared_drops += 1
                self._undeclared_bytes += size
            return False

        with self._lock:
            self._total_writes += 1
        return True

    def write_line(self, line: str) -> bool:
        """Queue a line write."""
        return self.write(line)

    @property
    def dropped_count(self) -> int:
        """Number of writes dropped due to a full queue."""
        with self._lock:
            return self._dropped

    @property
    def dropped_bytes(self) -> int:
        """Bytes never written because the queue was full."""
        with self._lock:
            return self._dropped_bytes

    @property
    def queued_bytes(self) -> int:
        """Bytes queued and not yet handed to the writer thread."""
        with self._lock:
            return self._queued_bytes

    @property
    def peak_queued_bytes(self) -> int:
        """High-water mark of queued bytes — how close the cap came to biting."""
        with self._lock:
            return self._peak_queued_bytes

    @property
    def total_writes(self) -> int:
        """Total writes accepted."""
        with self._lock:
            return self._total_writes

    @property
    def pending_count(self) -> int:
        """Number of writes pending in queue."""
        return self._queue.qsize()

    def close(self):
        """Shutdown the writer and flush remaining data."""
        if not self._running:
            return

        self._running = False
        try:
            self._queue.put_nowait(None)  # Shutdown signal
        except queue.Full:
            pass  # Writer thread will exit when _running becomes False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        if self._file:
            self._file.close()
            self._file = None


class AsyncLogHandler:
    """Non-blocking log handler using AsyncFileWriter."""

    def __init__(self, writer: AsyncFileWriter):
        self.writer = writer

    def emit(self, record: str):
        """Emit a log record (non-blocking)."""
        self.writer.write(record)
