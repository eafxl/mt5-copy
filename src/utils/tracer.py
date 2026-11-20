"""Structured tracing for execution profiling.

Outputs JSON Lines format (.jsonl) for easy analysis.
All writes are ASYNC - never blocks the copy path.

Usage:
    from .utils.tracer import get_tracer

    tracer = get_tracer()

    with tracer.trace("copy_trade", trade_id="ABC123") as span:
        span.set_tag("symbol", "EURUSD")

        with tracer.span("fetch_price") as price_span:
            price = get_price()
            price_span.set_tag("bid", bid)

Analysis:
    grep "ABC123" traces/20240101_traces.jsonl | jq .
    jq 'select(.duration_ms > 100)' traces/*.jsonl

FOLLOWING ONE TRADE, END TO END
    Every record about one master position carries the same ``trace_id``: the
    position's ``trade_id``, minted once by the PositionTracker when the ticket
    is claimed and reused for the open, every SL/TP change, every partial, and
    the close. So one id covers the trade's whole life, not one operation.

        jq 'select(.trace_id=="A1B2C3D4")' traces/*.jsonl

    returns, in order:

      ``ea_signal``          the EA line that started it, with ``signal_id``
                             (``TCP#2/17``) — or nothing, when a poll cycle
                             found it, in which case the copy span's
                             ``signal_id`` reads ``POLL#412``
      ``copy_new_position``  the copier's decision: symbol, side, volume, the
                             slaves it built tasks for
      ``trade_action``       ONE PER SLAVE PER ACTION, including the slaves
                             that did nothing: ``skipped`` (paused, news,
                             duplicate), ``blocked`` (risk guard, entry
                             deviation), ``filled``, ``failed``
      ``worker_open``        the worker process's own span — connect time,
                             action time, ticket (linked by trace context;
                             see ``propagate``)
      ``broker_order_send``  one per ATTEMPT, with the retcode the broker
                             returned and how long it took
      ``update_sl_tp`` / ``reduce_position`` / ``close_position`` and their own
      ``trade_action`` records, later in the trade's life.

    The log file carries the same id in its own column (see
    ``utils.logging.TraceContextFilter``), so ``grep A1B2C3D4`` across
    ``logs/`` and ``traces/`` returns one trade's complete story — main
    process and worker processes alike.
"""
import asyncio
import functools
import json
import time
import uuid
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from .async_writer import AsyncFileWriter


@dataclass
class Span:
    """A traced operation span."""
    tracer: "Tracer"
    operation: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time: float
    tags: dict[str, Any] = field(default_factory=dict)
    _ended: bool = False
    #: Filtered out by subscription. Still parents its children correctly, so
    #: muting one operation cannot orphan the spans beneath it — it just does
    #: not write itself.
    silent: bool = False

    def set_tag(self, key: str, value: Any):
        """Add a tag to this span."""
        self.tags[key] = value

    def set_tags(self, **kwargs):
        """Add multiple tags."""
        self.tags.update(kwargs)

    def end(self, status: str = "ok", error: str | None = None):
        """End the span and write to trace log (async, non-blocking)."""
        if self._ended:
            return
        self._ended = True
        if self.silent:
            return

        end_time = time.perf_counter()
        duration_ms = (end_time - self.start_time) * 1000

        record = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        }

        if error:
            record["error"] = error

        if self.tags:
            record["tags"] = self.tags

        self.tracer._write(record)

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.end(status="error", error=str(exc_val))
        else:
            self.end(status="ok")
        return False


class Tracer:
    """Async tracer - all writes go to background thread."""

    _instance: "Tracer | None" = None
    _lock = threading.Lock()

    #: Operations muted unless the config says otherwise.
    #:
    #: These are heartbeats: one line per poll, saying the same thing as the
    #: last one. Measured on a single 11-hour run, the first two were 100%
    #: of an 11.8 MB trace file — and the interesting record, the price
    #: comparison itself, was buried in 83% duplicates of its own.
    #: A trace nobody can open is not observability.
    #:
    #: The worker-side three were added after a 46-minute session wrote 12,287
    #: records of which 12,015 (97.8%) were these: a cache-hit notice and two
    #: read-only polls, at 2/s, saying "still fine". They are the reason a day
    #: of tracing reached 28 MB. What they were being kept for — is the poll
    #: alive, and how fast — is answered better by the 20s poll summary in the
    #: log, which has counts and percentiles instead of one line per event.
    #:
    #: Nothing here touches a trade: opens, closes, modifies, the broker calls
    #: and the per-slave decisions are all still recorded, always.
    DEFAULT_EXCLUDE = (
        "mt5_get_price",
        "price_calc_cycle",
        "worker_connect_cached",
        "worker_fetch_positions",
        "worker_terminal_state",
    )

    #: Tags ignored when deciding whether a deduped event repeats itself.
    #: These move every cycle by construction — they are clocks, not findings —
    #: so including them would defeat the dedupe entirely.
    VOLATILE_TAGS = frozenset({
        # Clocks.
        "lookback_s", "tv_age_s", "master_age_s", "age_gap_s",
        "tv_published_age_s", "master_quote_age_s", "quote_age_s",
        "quote_time", "sampled_at", "srv_offset",
        # The live quote. Carried in the record for context, but it ticks
        # constantly and is explicitly NOT part of the measurement, so a change
        # in it is not news — and treating it as news defeated the dedupe
        # almost entirely (68% saved instead of 94%).
        "master_bid", "master_ask", "spread_now",
    })

    def __init__(
        self,
        trace_dir: Path | None = None,
        enabled: bool = True,
        include: "tuple[str, ...] | list[str]" = (),
        exclude: "tuple[str, ...] | list[str] | None" = None,
    ):
        if trace_dir is None:
            from ..paths import traces_dir
            trace_dir = traces_dir()

        self.trace_dir = trace_dir
        self.enabled = enabled
        self.set_filter(include, exclude)
        # Last written payload per (operation, key), for `event(dedupe_key=...)`.
        self._last_seen: dict[tuple[str, str], Any] = {}
        self._local = threading.local()  # Thread-local span stack AND trace ID

        # Async writer - NEVER blocks the copy path
        if enabled:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self._writer = AsyncFileWriter(
                get_file_path=self._get_trace_path,
                max_queue_size=10000,
                flush_interval=0.5,
            )
        else:
            self._writer = None

    def set_filter(self, include=(), exclude=None):
        """Subscribe to operations. ``include`` wins when both are given.

        ``include`` empty means "everything except ``exclude``"; a non-empty
        ``include`` means "only these", which is the useful mode when chasing
        one thing. ``exclude=None`` keeps DEFAULT_EXCLUDE; pass ``()`` to mute
        nothing.
        """
        self.include = tuple(include or ())
        self.exclude = tuple(
            self.DEFAULT_EXCLUDE if exclude is None else exclude
        )

    def _should_trace(self, operation: str) -> bool:
        if not self.enabled:
            return False
        if self.include:
            return operation in self.include
        return operation not in self.exclude

    def _get_trace_path(self) -> Path:
        """This RUN's trace file.

        Per session, not per day. A day file accumulated every run that
        happened to share a date — 28 MB by lunchtime on one machine — which
        made the GUI's tail expensive and made "what did this run do" a search
        problem. Worker processes inherit the stamp through the environment,
        so they append to the same file as their parent.
        """
        from .logging import session_stamp

        return self.trace_dir / f"{session_stamp()}_traces.jsonl"

    def _write(self, record: dict):
        """Write a record (async, non-blocking)."""
        if self._writer:
            self._writer.write(json.dumps(record, default=str))

    def _get_span_stack(self) -> list[str]:
        """Get thread-local span stack."""
        if not hasattr(self._local, "span_stack"):
            self._local.span_stack = []
        return self._local.span_stack

    def _get_trace_id(self) -> str | None:
        """Get thread-local current trace ID."""
        return getattr(self._local, "trace_id", None)

    def _set_trace_id(self, value: str | None):
        """Set thread-local current trace ID."""
        self._local.trace_id = value

    @contextmanager
    def trace(self, operation: str, trade_id: str | None = None) -> Generator[Span, None, None]:
        """Start a new trace (top-level span)."""
        if trade_id:
            self._set_trace_id(trade_id)

        trace_id = self._get_trace_id() or uuid.uuid4().hex[:8].upper()
        self._set_trace_id(trace_id)
        self._get_span_stack().clear()

        with self.span(operation) as span:
            yield span

        self._set_trace_id(None)
        self._get_span_stack().clear()

    @contextmanager
    def span(self, operation: str) -> Generator[Span, None, None]:
        """Create a child span within the current trace."""
        if not self.enabled:
            yield Span(
                tracer=self,
                operation=operation,
                trace_id="",
                span_id="",
                parent_span_id=None,
                start_time=0,
            )
            return

        trace_id = self._get_trace_id() or uuid.uuid4().hex[:8].upper()
        span_id = uuid.uuid4().hex[:8]
        span_stack = self._get_span_stack()
        parent_span_id = span_stack[-1] if span_stack else None

        span_stack.append(span_id)

        span = Span(
            tracer=self,
            operation=operation,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            start_time=time.perf_counter(),
            silent=not self._should_trace(operation),
        )

        try:
            yield span
        finally:
            if not span._ended:
                span.end()
            if span_stack and span_stack[-1] == span_id:
                span_stack.pop()

    def get_trace_context(self) -> dict[str, str | None]:
        """Get current trace context for cross-process propagation.

        Returns dict with trace_id and parent_span_id, analogous to
        W3C Trace Context 'traceparent' header.
        """
        span_stack = self._get_span_stack()
        return {
            "trace_id": self._get_trace_id(),
            "parent_span_id": span_stack[-1] if span_stack else None,
        }

    @contextmanager
    def propagate(self, trace_context: dict) -> Generator[None, None, None]:
        """Continue a trace from propagated context (cross-process boundary).

        Sets trace_id and parent_span_id so that child spans created inside
        this block are linked to the originating trace.

        Usage (worker process):
            with tracer.propagate(task.trace_context):
                with tracer.span("worker_connect") as s:
                    ...  # s.parent_span_id == trace_context["parent_span_id"]
        """
        trace_id = trace_context.get("trace_id")
        parent_span_id = trace_context.get("parent_span_id")

        old_trace_id = self._get_trace_id()
        old_stack = list(self._get_span_stack())

        self._set_trace_id(trace_id)
        span_stack = self._get_span_stack()
        span_stack.clear()
        if parent_span_id:
            span_stack.append(parent_span_id)

        try:
            yield
        finally:
            self._set_trace_id(old_trace_id)
            span_stack.clear()
            span_stack.extend(old_stack)

    def event(self, operation: str, dedupe_key: str | None = None, **tags):
        """Log an instant event (zero-duration span).

        ``dedupe_key`` suppresses a repeat: the record is written only when
        the tags differ from the last one written under the same key. The
        real-price cycle re-measures every 5s but its instant is quantized to
        the minute, so eleven of every twelve records restated the same
        measurement with a larger age counter.
        """
        if not self._should_trace(operation):
            return

        if dedupe_key is not None:
            slot = (operation, dedupe_key)
            fingerprint = tuple(sorted(
                (k, v) for k, v in tags.items() if k not in self.VOLATILE_TAGS
            ))
            if self._last_seen.get(slot) == fingerprint:
                return
            self._last_seen[slot] = fingerprint

        trace_id = self._get_trace_id() or "NO_TRACE"
        span_stack = self._get_span_stack()
        parent_span_id = span_stack[-1] if span_stack else None

        record = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "trace_id": trace_id,
            "span_id": uuid.uuid4().hex[:8],
            "parent_span_id": parent_span_id,
            "operation": operation,
            "duration_ms": 0,
            "status": "event",
        }

        if tags:
            record["tags"] = tags

        self._write(record)

    def get_stats(self) -> dict[str, Any]:
        """Get tracer statistics."""
        if not self._writer:
            return {"enabled": False}
        return {
            "enabled": True,
            "total_writes": self._writer.total_writes,
            "dropped": self._writer.dropped_count,
            "pending": self._writer.pending_count,
            "dropped_bytes": self._writer.dropped_bytes,
            "queued_bytes": self._writer.queued_bytes,
            "peak_queued_bytes": self._writer.peak_queued_bytes,
        }

    def close(self):
        """Close the tracer."""
        if self._writer:
            self._writer.close()


# Global tracer instance
_tracer: Tracer | None = None


def setup_tracer(
    trace_dir: Path | None = None,
    enabled: bool = True,
    include=(),
    exclude=None,
) -> Tracer:
    """Initialize the global tracer. See Tracer.set_filter for the filter."""
    global _tracer
    _tracer = Tracer(
        trace_dir=trace_dir, enabled=enabled, include=include, exclude=exclude
    )
    return _tracer


def get_tracer() -> Tracer:
    """Get the global tracer instance."""
    global _tracer
    if _tracer is None:
        _tracer = Tracer()
    return _tracer


def traced(operation: str, **default_tags):
    """Decorator to wrap a function in a trace span.

    Works with both sync and async functions.

    Usage:
        @traced("fetch_price", source="mt5")
        def fetch_price(symbol):
            ...

        @traced("connect_slave")
        async def connect(login):
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            tracer = get_tracer()
            with tracer.span(operation) as s:
                s.set_tags(**default_tags)
                return fn(*args, **kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            tracer = get_tracer()
            with tracer.span(operation) as s:
                s.set_tags(**default_tags)
                return await fn(*args, **kwargs)

        return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
    return decorator
