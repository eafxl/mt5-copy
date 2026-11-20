"""One trade, one id, from the wire to the broker's answer.

WHY THIS FILE EXISTS
    Tracing is the kind of feature that looks present and is not: spans get
    written, ids get generated, and none of it JOINS. The failure is silent —
    you only find out at the moment you are trying to explain a bad fill, which
    is the worst possible time.

    So these tests pin the joins rather than the plumbing:

      * a log line emitted inside a span carries that span's trace id, so
        ``grep <trade_id>`` reaches the prose as well as the spans;
      * a worker process, given only a serialized context, lands its spans
        under the SAME trace id as the copier that dispatched the task;
      * an EA line arrives with an identity (``TCP#2/17``) that is carried on
        the PositionChange, so the copy can be walked back to the wire;
      * every call to the broker is recorded per attempt, with its retcode.
"""
import json
import logging
import socket
import threading
import time

import pytest

import src.utils.tracer as tracer_mod
from src.ea_bridge import EABridge, describe_event, parse_line
from src.utils.logging import NO_TRACE, TraceContextFilter
from src.utils.tracer import Tracer


@pytest.fixture
def tracer(tmp_path, monkeypatch):
    """A tracer writing to a temp dir, installed as the global one."""
    t = Tracer(trace_dir=tmp_path / "traces", enabled=True)
    monkeypatch.setattr(tracer_mod, "_tracer", t)
    yield t
    t.close()


def records(tracer) -> list[dict]:
    """Everything written so far, decoded."""
    tracer._writer.close()          # flush the async writer
    out = []
    for path in sorted(tracer.trace_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ==================== logs join the trace ====================

def _record(msg="hello"):
    return logging.LogRecord("metact.test", logging.INFO, __file__, 1, msg, (), None)


def test_a_log_line_inside_a_span_carries_its_trace_id(tracer):
    filt = TraceContextFilter()
    rec = _record()

    with tracer.trace("copy_new_position", trade_id="A1B2C3D4"):
        filt.filter(rec)

    assert rec.trace == "A1B2C3D4"


def test_a_log_line_outside_any_trace_gets_a_placeholder(tracer):
    rec = _record()

    TraceContextFilter().filter(rec)

    assert rec.trace == NO_TRACE


def test_the_placeholder_is_fixed_width_so_the_column_holds(tracer):
    """A wandering column is a log nobody can scan down."""
    inside = _record()
    outside = _record()
    with tracer.trace("copy_new_position", trade_id="A1B2C3D4"):
        TraceContextFilter().filter(inside)
    TraceContextFilter().filter(outside)

    assert len(inside.trace) == len(outside.trace)


def test_logging_never_creates_a_tracer(monkeypatch, tmp_path):
    """A log line must not be able to create files as a side effect."""
    monkeypatch.setattr(tracer_mod, "_tracer", None)
    rec = _record()

    TraceContextFilter().filter(rec)

    assert rec.trace == NO_TRACE
    assert tracer_mod._tracer is None


def test_a_broken_tracer_does_not_break_logging(monkeypatch):
    class Exploding:
        def _get_trace_id(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(tracer_mod, "_tracer", Exploding())
    rec = _record()

    assert TraceContextFilter().filter(rec) is True
    assert rec.trace == NO_TRACE


# ==================== the process boundary ====================

def test_a_worker_lands_under_the_dispatching_trace(tracer):
    """The join that makes the worker's spans findable at all.

    The worker is a separate process with its own Tracer; all it receives is
    the dict from get_trace_context().
    """
    with tracer.trace("copy_new_position", trade_id="A1B2C3D4"):
        context = tracer.get_trace_context()

    worker = Tracer(trace_dir=tracer.trace_dir, enabled=True)
    with worker.propagate(context):
        with worker.span("worker_open"):
            pass
    worker.close()

    worker_spans = [r for r in records(tracer) if r["operation"] == "worker_open"]
    assert worker_spans, "the worker wrote nothing"
    assert worker_spans[0]["trace_id"] == "A1B2C3D4"


def test_the_worker_span_hangs_off_the_dispatching_span(tracer):
    with tracer.trace("copy_new_position", trade_id="A1B2C3D4"):
        context = tracer.get_trace_context()
        parent = context["parent_span_id"]

    worker = Tracer(trace_dir=tracer.trace_dir, enabled=True)
    with worker.propagate(context):
        with worker.span("worker_open"):
            pass
    worker.close()

    span = [r for r in records(tracer) if r["operation"] == "worker_open"][0]
    assert span["parent_span_id"] == parent


def test_context_is_restored_after_propagating(tracer):
    """A worker process reuses its Tracer across tasks — leakage would file
    the next trade's spans under the previous trade's id."""
    with tracer.trace("copy_new_position", trade_id="FIRST"):
        pass

    tracer._set_trace_id("OUTER")
    with tracer.propagate({"trace_id": "INNER", "parent_span_id": "abc"}):
        pass

    assert tracer._get_trace_id() == "OUTER"


# ==================== one trade keeps one id for its whole life ====================

def test_open_modify_and_close_share_the_trade_id(tracer):
    """The point of keying on trade_id rather than per-operation ids."""
    for op in ("copy_new_position", "update_sl_tp", "close_position"):
        with tracer.trace(op, trade_id="A1B2C3D4"):
            pass

    ids = {r["trace_id"] for r in records(tracer)}
    assert ids == {"A1B2C3D4"}


def test_two_trades_do_not_share_an_id(tracer):
    with tracer.trace("copy_new_position", trade_id="AAA"):
        pass
    with tracer.trace("copy_new_position", trade_id="BBB"):
        pass

    assert {r["trace_id"] for r in records(tracer)} == {"AAA", "BBB"}


def test_the_action_record_survives_the_default_filter():
    """These are the records that explain a skipped slave — muting them by
    default would make the trace lie by omission."""
    for op in ("trade_action", "ea_signal", "broker_order_send"):
        assert op not in Tracer.DEFAULT_EXCLUDE


# ==================== the broker call ====================

class _Reply:
    def __init__(self, retcode, comment="", order=0, price=0.0, deal=0):
        self.retcode = retcode
        self.comment = comment
        self.order = order
        self.price = price
        self.deal = deal


def _send(monkeypatch, tracer, reply, request=None):
    import MetaTrader5 as mt5
    import src.parallel_worker as pw

    monkeypatch.setattr(pw, "_get_worker_tracer", lambda: tracer)
    if isinstance(reply, Exception):
        monkeypatch.setattr(mt5, "order_send", lambda r: (_ for _ in ()).throw(reply))
    else:
        monkeypatch.setattr(mt5, "order_send", lambda r: reply)
    req = request or {"symbol": "EURUSD", "volume": 0.1, "price": 1.1}
    return pw._send_order(req, "open", attempt=0)


def test_a_fill_is_recorded_with_its_retcode(tracer, monkeypatch):
    import MetaTrader5 as mt5

    _send(monkeypatch, tracer, _Reply(mt5.TRADE_RETCODE_DONE, order=777, price=1.2))

    sent = [r for r in records(tracer) if r["operation"] == "broker_order_send"][0]
    assert sent["tags"]["outcome"] == "done"
    assert sent["tags"]["ticket"] == 777
    assert sent["tags"]["retcode"] == mt5.TRADE_RETCODE_DONE


def test_a_rejection_is_recorded_with_the_brokers_own_words(tracer, monkeypatch):
    _send(monkeypatch, tracer, _Reply(10016, comment="Invalid stops"))

    sent = [r for r in records(tracer) if r["operation"] == "broker_order_send"][0]
    assert sent["tags"]["outcome"] == "rejected"
    assert sent["tags"]["comment"] == "Invalid stops"


def test_each_retry_is_its_own_record(tracer, monkeypatch):
    import MetaTrader5 as mt5
    import src.parallel_worker as pw

    monkeypatch.setattr(pw, "_get_worker_tracer", lambda: tracer)
    monkeypatch.setattr(mt5, "order_send", lambda r: _Reply(10004))  # requote
    for attempt in range(3):
        pw._send_order({"symbol": "EURUSD"}, "open", attempt=attempt)

    sent = [r for r in records(tracer) if r["operation"] == "broker_order_send"]
    assert [r["tags"]["attempt"] for r in sent] == [1, 2, 3]


def test_an_exception_is_recorded_and_re_raised(tracer, monkeypatch):
    """Observing the call must not change the caller's retry behaviour."""
    with pytest.raises(RuntimeError):
        _send(monkeypatch, tracer, RuntimeError("terminal gone"))

    sent = [r for r in records(tracer) if r["operation"] == "broker_order_send"][0]
    assert sent["tags"]["outcome"] == "exception"


def test_a_none_reply_is_recorded_as_such(tracer, monkeypatch):
    assert _send(monkeypatch, tracer, None) is None

    sent = [r for r in records(tracer) if r["operation"] == "broker_order_send"][0]
    assert sent["tags"]["outcome"] == "none"


# ==================== the wire ====================

OPEN_LINE = json.dumps({
    "event": "open", "ticket": 51203, "symbol": "EURUSD", "type": 0,
    "volume": 0.1, "price": 1.1, "sl": 1.09, "tp": 1.12, "time_msc": 1700000000000,
})


class _FakeLoop:
    """Just enough asyncio loop to receive a thread-safe handoff."""

    def __init__(self):
        self.queued = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def put(self, change):
        self.queued.append(change)


@pytest.fixture
def bridge(tracer):
    loop = _FakeLoop()
    b = EABridge(loop, port=0)
    b.queue = type("Q", (), {"put_nowait": lambda self, c: loop.put(c)})()
    b.started_at = time.time()
    return b


def _link(bridge, number=1):
    from src.ea_bridge import _Connection

    link = _Connection(number, ("127.0.0.1", 51844))
    bridge._conn = link
    return link


def test_an_event_is_stamped_with_the_line_that_carried_it(bridge, tracer):
    link = _link(bridge, number=2)
    link.lines = 17

    bridge._dispatch(OPEN_LINE, link)

    change = bridge.loop.queued[0]
    assert change.signal_id == "TCP#2/17"


def test_the_signal_id_is_traced_so_the_copy_can_be_joined_to_it(bridge, tracer):
    link = _link(bridge, number=2)
    link.lines = 17

    bridge._dispatch(OPEN_LINE, link)

    signal = [r for r in records(tracer) if r["operation"] == "ea_signal"][0]
    assert signal["tags"]["signal_id"] == "TCP#2/17"
    assert signal["tags"]["ticket"] == 51203


def test_an_event_is_stamped_with_its_arrival_time(bridge):
    bridge._dispatch(OPEN_LINE, _link(bridge))

    assert bridge.loop.queued[0].recv_ms > 0


def test_a_heartbeat_is_counted_and_not_queued(bridge):
    link = _link(bridge)

    bridge._dispatch(json.dumps({"event": "heartbeat"}), link)

    assert link.heartbeats == 1
    assert bridge.loop.queued == []
    assert bridge.events_received == 0


def test_unreadable_json_is_counted_as_a_parse_error(bridge, tracer):
    link = _link(bridge)

    bridge._dispatch("{not json", link)

    assert link.parse_errors == 1
    assert bridge.parse_errors == 1
    assert [r for r in records(tracer) if r["operation"] == "ea_parse_error"]


def test_readable_json_with_an_unusable_payload_is_a_different_error(bridge, tracer):
    """Framing broke vs the EA and this build disagree — not the same bug."""
    link = _link(bridge)

    bridge._dispatch(json.dumps({"event": "open"}), link)   # no ticket

    assert [r for r in records(tracer) if r["operation"] == "ea_bad_payload"]
    assert not [r for r in records(tracer) if r["operation"] == "ea_parse_error"]


def test_a_bad_line_does_not_stop_the_next_good_one(bridge):
    link = _link(bridge)

    bridge._dispatch("{not json", link)
    bridge._dispatch(OPEN_LINE, link)

    assert len(bridge.loop.queued) == 1


def test_stats_report_the_silence_that_matters(bridge):
    """The EA heartbeats every second; a quiet connected link is a dead one."""
    link = _link(bridge)
    bridge.connected = True
    bridge.last_line_at = time.time() - 42.0

    stats = bridge.stats()

    assert stats["connected"] is True
    assert stats["silent_for_s"] >= 42.0


def test_stats_before_anything_connects_do_not_invent_numbers(bridge):
    stats = bridge.stats()

    assert stats["silent_for_s"] is None
    assert stats["since_last_event_s"] is None
    assert stats["connections"] == 0


def test_counters_survive_a_reconnect(bridge):
    """A link that flaps is the thing worth seeing; per-connection counters
    reset and hide it."""
    bridge._dispatch(OPEN_LINE, _link(bridge, number=1))
    bridge._dispatch(OPEN_LINE, _link(bridge, number=2))

    assert bridge.events_received == 2
    assert bridge._conn.events == 1


def test_the_log_summary_names_the_trade(bridge):
    assert "open ticket=51203" in describe_event(json.loads(OPEN_LINE))
    assert "EURUSD" in describe_event(json.loads(OPEN_LINE))


def test_parse_line_still_answers_for_each_event_kind():
    assert parse_line(OPEN_LINE).new_positions[0].ticket == 51203
    assert parse_line(json.dumps({"event": "close", "ticket": 7})).closed_tickets == [7]
    assert parse_line("") is None


# ==================== the polling path is identified too ====================

def test_a_polled_change_carries_a_poll_sequence():
    """Otherwise 'the EA sent it twice' and 'we polled it twice' look alike."""
    from src.master import MasterMonitor

    class _Client:
        def get_positions(self):
            return []

    monitor = MasterMonitor.__new__(MasterMonitor)
    monitor.mt5 = _Client()
    monitor.logger = logging.getLogger("metact.test")
    monitor._last_positions = {}
    monitor._poll_seq = 0

    assert monitor.detect_changes().signal_id == "POLL#1"
    assert monitor.detect_changes().signal_id == "POLL#2"
